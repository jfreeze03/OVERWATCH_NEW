"""Next-Fifty #30: maintenance spend on objects nobody reads — builders, verdicts, SQL, booking, wiring.

Pure + source locks (both CI legs). The mart shortlist is EXECUTED in tests/test_unread_maintenance_harness.py;
the rendered panel is driven in tests/test_prc_c2_shaped.py (skipped on the streamlit 1.52.2 floor, so every
wiring claim is also locked by source below)."""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import pytest

from app.config import LEDGER_AUTOBOOKED_LEVERS
from app.core.query import _statement_allowed
from app.data import canary, cost_sql, insights_sql
from app.logic import remediation, savings_rollup
from app.logic.actions import can_verify
from app.logic.unread_maintenance import (
    ACTION_VERDICTS,
    ARM_FINDING_TYPE,
    VERDICT_COLUMNS,
    book_estimated_sql,
    unread_maintenance_verdicts,
)
from tests._source import read

sqlglot = pytest.importorskip("sqlglot")

_PAYLOADS = ("ZZINJZZ'", "ZZINJZZ' OR '1'='1", "ZZINJZZ; SELECT 1 --", 'ZZINJZZ"dq', "ZZINJZZ\\bs")


def _strip_literals(sql: str) -> str:
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql[i] == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


# --- the mart shortlist -------------------------------------------------------------------------------

def test_builder_is_mart_only_and_confirms_nothing_itself():
    sql = cost_sql.maintenance_on_unread(90, "ALFA")
    assert "FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY" in sql and "ACCOUNT_USAGE" not in sql
    assert "o.READ_CREDITS = 0" in sql
    assert "GROUP BY UPPER(REPLACE(OBJECT_FQN, '\"', ''))" in sql      # the normalized key (FQN-split class)
    assert "COST_ARM IN ('QUERY_COMPUTE_READ', 'QUERY_COMPUTE')" in sql  # legacy role-less compute counts as a read
    assert "OBJECT_FQN <> 'UNATTRIBUTED'" in sql
    assert "DAY < CURRENT_DATE()" in sql                                # today excluded
    assert "o.OBJECT_FQN IS NOT NULL" in sql                            # a read-only object is never a candidate
    tree = sqlglot.parse_one(sql, read="snowflake")
    assert tree.named_selects == [
        "OBJECT_FQN", "OBJECT_DOMAIN", "COMPANY", "CLUSTERING_CREDITS", "SEARCH_OPT_CREDITS",
        "MV_REFRESH_CREDITS", "MAINT_CREDITS", "MAINT_CREDITS_30D", "WRITE_CREDITS", "FIRST_MAINT_DAY",
        "LAST_MAINT_DAY", "MAINT_DAYS", "COVERAGE_START_DAY", "LEDGER_LAST_DAY", "CANDIDATES_WIN",
        "MAINT_CREDITS_WIN", "MAINT_CREDITS_30D_WIN"]


def test_builder_window_is_clamped_and_totals_are_uncapped():
    sql = cost_sql.maintenance_on_unread(999999)
    assert "DATEADD('day', -90, CURRENT_DATE())" in sql and "999999" not in sql
    limit_at = sql.index("LIMIT ")
    for total in ("COUNT(*) OVER () AS CANDIDATES_WIN", "SUM(c.MAINT_CREDITS) OVER ()",
                  "SUM(c.MAINT_CREDITS_30D) OVER ()"):
        assert sql.index(total) < limit_at, total                       # computed before the LIMIT
    assert sql.rstrip().endswith("LIMIT 50")
    assert cost_sql.maintenance_on_unread(limit=1).rstrip().endswith("LIMIT 5")
    assert cost_sql.maintenance_on_unread(limit=10_000).rstrip().endswith("LIMIT 200")
    assert ">= 1.0\n" in sql and ">= 2.5\n" in cost_sql.maintenance_on_unread(min_credits=2.5)
    assert ">= 1.0\n" in cost_sql.maintenance_on_unread(min_credits="garbage")     # garbage -> the default
    assert ">= 0.0\n" in cost_sql.maintenance_on_unread(min_credits=-5)
    # PR C review C7: the coverage bounds are LEDGER-WIDE (a one-row CTE with the window predicate only), never
    # the company / Database-filtered rows (a quiet database is not a failing load); cross-joined, so no row
    # multiplication before the window totals or the LIMIT
    bounds = sql.split("b AS (", 1)[1].split("\n),\n", 1)[0]
    assert "SELECT MIN(DAY) AS COVERAGE_START_DAY, MAX(DAY) AS LEDGER_LAST_DAY" in bounds
    assert "WHERE DAY >= DATEADD('day', -90, CURRENT_DATE()) AND DAY < CURRENT_DATE()" in bounds
    for scoped in ("COMPANY", "SPLIT_PART", "COST_ARM", "UNATTRIBUTED"):
        assert scoped not in bounds, scoped
    scoped_sql = cost_sql.maintenance_on_unread(90, "ALFA", "QUIET")
    assert scoped_sql.split("b AS (", 1)[1].split("\n),\n", 1)[0] == bounds       # scope never reaches it
    assert "b.COVERAGE_START_DAY, b.LEDGER_LAST_DAY" in sql and "FROM c\nCROSS JOIN b\n" in sql
    assert "OVER () AS COVERAGE_START_DAY" not in sql and "OVER () AS LEDGER_LAST_DAY" not in sql
    # an MV that is also clustered reads MATERIALIZED_VIEW, not the clustering arm's 'TABLE'
    assert "IFF(MAX(IFF(COST_ARM = 'MV_REFRESH', 1, 0)) = 1, 'MATERIALIZED_VIEW'" in sql


def test_builder_scopes_company_and_database():
    rendered = {c: cost_sql.maintenance_on_unread(90, c) for c in ("ALL", "ALFA", "Trexis", "UNKNOWN")}
    assert len(set(rendered.values())) == 4
    assert "COMPANY = " not in rendered["ALL"] and "COMPANY = 'ALFA'" in rendered["ALFA"]
    # the label is post-aggregation, from the object's own database (the V030 shape law)
    assert "COMPANY_FOR_DATABASE(SPLIT_PART(c.OBJECT_FQN, '.', 1)) AS COMPANY" in rendered["ALL"]
    assert "UPPER(SPLIT_PART(OBJECT_FQN, '.', 1)) = 'X'" in cost_sql.maintenance_on_unread(database="x")
    for bad in _PAYLOADS:
        for sql in (cost_sql.maintenance_on_unread(90, bad), cost_sql.maintenance_on_unread(90, "ALL", bad)):
            residue = _strip_literals(sql)
            assert "ZZINJZZ" not in residue and "'" not in residue, bad


def test_mart_builder_and_proof_are_canaries_and_confirm_is_not():
    names = dict(canary.CANARIES)
    assert "cost.maintenance_on_unread" in names and "cost.unread_maintenance_proof" in names
    for name in ("cost.maintenance_on_unread", "cost.unread_maintenance_proof"):
        sqlglot.parse_one(names[name](), read="snowflake")
    assert not [n for n in names if "object_reads" in n]
    assert not [n for n, fn in canary.CANARIES if "ACCESS_HISTORY" in fn()]   # the storage_reclaim precedent


def test_proof_builder_is_central_pinned_and_runnable():
    from app.data.common import account_today_sql
    proof = cost_sql.unread_maintenance_proof('Db.S."MyTable"', date(2026, 9, 29), 12.345678)
    assert ";" not in proof and account_today_sql() in proof and "CURRENT_DATE" not in proof
    assert "UPPER(REPLACE(f.OBJECT_FQN, '\"', '')) = 'DB.S.MYTABLE'" in proof   # the shortlist's normalized key
    assert "12.3457 AS BASELINE_MONTHLY_CREDITS" in proof
    assert "f.COST_ARM IN ('CLUSTERING', 'SEARCH_OPT', 'MV_REFRESH')" in proof
    # PR C review C10: the booking day is left out (it carries pre-ALTER credits) and the after-window ends at
    # the ledger's newest loaded day, so an unloaded day is neither summed as 0 nor divided by
    assert "f.DAY > '2026-09-29'::DATE AND f.DAY <= l.LOADED_THROUGH" in proof and "f.DAY >= " not in proof
    assert (f"WITH l AS (SELECT MAX(DAY) AS LOADED_THROUGH FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY "
            f"WHERE DAY >= '2026-09-29'::DATE AND DAY < {account_today_sql()})") in proof
    assert "NULLIF(GREATEST(MAX(DATEDIFF('day', '2026-09-29'::DATE, l.LOADED_THROUGH)), 0), 0)" in proof
    assert sqlglot.parse_one(proof, read="snowflake").named_selects == [
        "BASELINE_MONTHLY_CREDITS", "LOADED_THROUGH", "DAYS_MEASURED", "CREDITS_SINCE_BOOKED",
        "MONTHLY_CREDITS_NOW"]
    with pytest.raises(ValueError):
        cost_sql.unread_maintenance_proof("  ", date(2026, 9, 29), 1.0)
    hostile = cost_sql.unread_maintenance_proof("ZZINJZZ' OR '1'='1", date(2026, 9, 29), 1.0)
    assert "ZZINJZZ" not in _strip_literals(hostile)


# --- the access-history confirm -----------------------------------------------------------------------

def test_confirm_builder_shape():
    sql = insights_sql.object_reads_confirm(["A.B.C", "D.E.F"], 999)
    assert "SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY" in sql
    assert "BASE_OBJECTS_ACCESSED" in sql and "OBJECTS_MODIFIED" in sql and "MAX(IS_WRITE) AS IS_WRITE" in sql
    assert "t.OBJ_ID = i.TABLE_ID" in sql and "t.OBJ_KEY = i.OBJECT_KEY" in sql     # both keys ...
    assert " OR t.OBJ" not in sql and "UNION ALL" in sql                             # ... never an OR join
    assert "t.TABLE_CATALOG || '.' || t.TABLE_SCHEMA || '.' || t.TABLE_NAME = s.OBJECT_FQN" in sql
    assert "t.DELETED IS NULL" in sql
    assert "GRANTED_TO = 'SHARE' AND GRANTED_ON = 'DATABASE' AND DELETED_ON IS NULL" in sql
    assert "objectDomain" not in sql                                                  # no domain filter
    assert "DATEADD('day', -90, CURRENT_TIMESTAMP())" in sql and "999999" not in sql
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == [
        "OBJECT_FQN", "MATCHED_BY_ID", "SHARED_DATABASE", "READ_QUERIES", "READ_USERS", "LAST_READ",
        "WRITE_QUERIES"]
    empty = insights_sql.object_reads_confirm()
    assert "SELECT NULL::VARCHAR AS OBJECT_FQN WHERE FALSE" in empty
    sqlglot.parse_one(empty, read="snowflake")
    many = insights_sql.object_reads_confirm([f"D.S.T{i}" for i in range(500)])
    assert many.count("('D.S.T") == insights_sql.OBJECT_READS_MAX_FQNS == 200
    for bad in _PAYLOADS:
        assert "ZZINJZZ" not in _strip_literals(insights_sql.object_reads_confirm([bad])), bad
    import inspect
    assert "company" not in inspect.signature(insights_sql.object_reads_confirm).parameters


# --- verdicts, SQL, booking ----------------------------------------------------------------------------

def _short(**rows) -> pd.DataFrame:
    base = {"OBJECT_DOMAIN": "TABLE", "COMPANY": "ALFA", "CLUSTERING_CREDITS": 0.0, "SEARCH_OPT_CREDITS": 0.0,
            "MV_REFRESH_CREDITS": 0.0, "MAINT_CREDITS_30D": 1.0, "WRITE_CREDITS": 0.0,
            "LAST_MAINT_DAY": pd.Timestamp("2026-09-27")}
    out = []
    for fqn, over in rows.items():
        r = {**base, "OBJECT_FQN": fqn.replace("__", "."), **over}
        r["MAINT_CREDITS"] = r["CLUSTERING_CREDITS"] + r["SEARCH_OPT_CREDITS"] + r["MV_REFRESH_CREDITS"]
        out.append(r)
    return pd.DataFrame(out)


def _reads(**rows) -> pd.DataFrame:
    return pd.DataFrame([{"OBJECT_FQN": fqn.replace("__", "."), "MATCHED_BY_ID": True, "SHARED_DATABASE": False,
                          "READ_QUERIES": 0, "READ_USERS": 0, "LAST_READ": None, "WRITE_QUERIES": 0, **over}
                         for fqn, over in rows.items()])


def test_verdicts_per_arm_and_sql():
    short = _short(DB__S__T1={"CLUSTERING_CREDITS": 30.0, "SEARCH_OPT_CREDITS": 4.0, "MAINT_CREDITS_30D": 10.0},
                   DB__S__T2={"SEARCH_OPT_CREDITS": 20.0, "MAINT_CREDITS_30D": 5.0},
                   DB__S__MV1={"MV_REFRESH_CREDITS": 12.0, "CLUSTERING_CREDITS": 3.0, "MAINT_CREDITS_30D": 2.0,
                               "OBJECT_DOMAIN": "MATERIALIZED_VIEW"})
    out = unread_maintenance_verdicts(short, _reads(DB__S__T1={}, DB__S__T2={}, DB__S__MV1={}), rate=3.0)
    assert list(out.columns[:len(VERDICT_COLUMNS)]) == list(VERDICT_COLUMNS)
    by = out.set_index("OBJECT_FQN")
    assert by.loc["DB.S.T1", "VERDICT"] == "Suspend clustering"
    assert by.loc["DB.S.T1", "REVIEW_SQL"] == ("ALTER TABLE DB.S.T1 SUSPEND RECLUSTER;\n"
                                               "ALTER TABLE DB.S.T1 DROP SEARCH OPTIMIZATION;")
    assert by.loc["DB.S.T1", "REVERSE_SQL"].startswith("ALTER TABLE DB.S.T1 RESUME RECLUSTER;")
    assert by.loc["DB.S.T2", "VERDICT"] == "Drop search optimization"
    assert by.loc["DB.S.MV1", "VERDICT"] == "Suspend MV refresh"
    assert by.loc["DB.S.MV1", "REVIEW_SQL"] == ("ALTER MATERIALIZED VIEW DB.S.MV1 SUSPEND RECLUSTER;\n"
                                                "ALTER MATERIALIZED VIEW DB.S.MV1 SUSPEND;")
    assert by.loc["DB.S.T1", "EST_MONTHLY_USD"] == 30.0 and by.loc["DB.S.T1", "MAINT_USD"] == 102.0
    assert dict(by["FINDING_TYPE"]) == {"DB.S.T1": "SUSPEND_RECLUSTER", "DB.S.T2": "DROP_SEARCH_OPTIMIZATION",
                                        "DB.S.MV1": "SUSPEND_MV_REFRESH"}
    t1_rev = by.loc["DB.S.T1", "REVERSE_SQL"].splitlines()
    assert t1_rev[0] == "ALTER TABLE DB.S.T1 RESUME RECLUSTER;" and t1_rev[1].startswith("-- before the DROP")
    for ftype in ARM_FINDING_TYPE.values():
        assert len(ftype) <= 40 and ftype not in LEDGER_AUTOBOOKED_LEVERS
    # the action rows sort by estimate, largest first
    assert list(out["OBJECT_FQN"]) == ["DB.S.T1", "DB.S.T2", "DB.S.MV1"]
    # ties go in ARMS order (clustering before search optimization)
    tie = unread_maintenance_verdicts(_short(A__B__C={"CLUSTERING_CREDITS": 5.0, "SEARCH_OPT_CREDITS": 5.0}),
                                      _reads(A__B__C={}), rate=1.0)
    assert tie["VERDICT"].iloc[0] == "Suspend clustering"


def test_verdict_precedence_and_honesty():
    short = _short(D__S__READ={"CLUSTERING_CREDITS": 9.0}, D__S__SHARED={"CLUSTERING_CREDITS": 9.0},
                   D__S__OLD={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 0.0},
                   D__S__ACT={"CLUSTERING_CREDITS": 1.0, "MAINT_CREDITS_30D": 0.5},
                   D__S__MISS={"CLUSTERING_CREDITS": 50.0})
    reads = _reads(D__S__READ={"READ_QUERIES": 3, "READ_USERS": 2, "SHARED_DATABASE": True},
                   D__S__SHARED={"SHARED_DATABASE": "TRUE"}, D__S__OLD={}, D__S__ACT={"WRITE_QUERIES": 4})
    out = unread_maintenance_verdicts(short, reads, rate=2.0).set_index("OBJECT_FQN")
    assert out.loc["D.S.READ", "VERDICT"] == "Keep"                       # a read outranks the share flag
    assert out.loc["D.S.SHARED", "VERDICT"] == "Check share consumers"     # the shaped 'TRUE' string counts
    assert out.loc["D.S.OLD", "VERDICT"] == "No recent spend"
    assert out.loc["D.S.ACT", "VERDICT"] == "Suspend clustering"
    assert out.loc["D.S.MISS", "VERDICT"] == "Unconfirmed"                 # a merge miss is not a measured 0
    assert pd.isna(out.loc["D.S.MISS", "READ_QUERIES"]) and out.loc["D.S.ACT", "WRITE_QUERIES"] == 4
    for fqn in ("D.S.READ", "D.S.SHARED", "D.S.OLD", "D.S.MISS"):
        assert out.loc[fqn, "REVIEW_SQL"] is None and out.loc[fqn, "FINDING_TYPE"] is None, fqn
    for reads_frame in (None, pd.DataFrame(), pd.DataFrame({"X": [1]})):   # the confirm read failed
        failed = unread_maintenance_verdicts(short, reads_frame, rate=2.0)
        assert set(failed["VERDICT"]) == {"Unconfirmed"}
        assert failed["READ_QUERIES"].isna().all() and failed["REVIEW_SQL"].isna().all()
    # quoted / mixed-case / dotted names keep the verdict but get no SQL (fail closed)
    odd = unread_maintenance_verdicts(_short(DB__S__MyTable={"CLUSTERING_CREDITS": 9.0}),
                                      _reads(DB__S__MyTable={}), rate=1.0)
    assert odd["VERDICT"].iloc[0] == "Suspend clustering" and odd["REVIEW_SQL"].iloc[0] is None
    ordered = list(unread_maintenance_verdicts(short, reads, rate=2.0)["VERDICT"])
    assert ordered == ["Suspend clustering", "Check share consumers", "No recent spend", "Unconfirmed", "Keep"]
    for empty in (None, pd.DataFrame(), pd.DataFrame({"X": [1]})):
        e = unread_maintenance_verdicts(empty, None, rate=1.0)
        assert e.empty and list(e.columns) == list(VERDICT_COLUMNS)


def test_a_dropped_or_renamed_object_is_gone_not_an_action():
    """PR C review C11: the confirm's TABLES bridge (live rows only) found no object under this name, so it was
    dropped or renamed: no action verdict, no SQL, no finding type, and out of the KPIs / savings roll-up, even
    with maintenance spend still in the ledger's last 30 days (or a read before the drop)."""
    from app.logic.unread_maintenance import VERDICT_GONE
    short = _short(DB__S__DROPPED={"CLUSTERING_CREDITS": 8.0, "MAINT_CREDITS_30D": 8.0},
                   DB__S__RENAMED={"SEARCH_OPT_CREDITS": 5.0, "MAINT_CREDITS_30D": 3.0},
                   DB__S__LIVE={"CLUSTERING_CREDITS": 4.0, "MAINT_CREDITS_30D": 2.0},
                   DB__S__NOCOL={"CLUSTERING_CREDITS": 4.0, "MAINT_CREDITS_30D": 2.0})
    reads = _reads(DB__S__DROPPED={"MATCHED_BY_ID": False}, DB__S__RENAMED={"MATCHED_BY_ID": 0, "READ_QUERIES": 2},
                   DB__S__LIVE={"MATCHED_BY_ID": "TRUE"}, DB__S__NOCOL={"MATCHED_BY_ID": None})
    out = unread_maintenance_verdicts(short, reads, rate=3.0).set_index("OBJECT_FQN")
    assert out.loc["DB.S.DROPPED", "VERDICT"] == VERDICT_GONE == "Object gone"
    assert out.loc["DB.S.RENAMED", "VERDICT"] == VERDICT_GONE                 # gone outranks a pre-drop read
    for fqn in ("DB.S.DROPPED", "DB.S.RENAMED"):
        assert out.loc[fqn, "REVIEW_SQL"] is None and out.loc[fqn, "FINDING_TYPE"] is None, fqn
    assert out.loc["DB.S.LIVE", "VERDICT"] == "Suspend clustering"
    assert out.loc["DB.S.NOCOL", "VERDICT"] == "Suspend clustering"          # an unknown match is not 'gone'
    ordered = unread_maintenance_verdicts(short, reads, rate=3.0)
    acts = ordered[ordered["VERDICT"].isin(ACTION_VERDICTS)]
    assert set(acts["OBJECT_FQN"]) == {"DB.S.LIVE", "DB.S.NOCOL"} and acts["EST_MONTHLY_USD"].sum() == 12.0
    assert list(ordered["VERDICT"])[-2:] == ["Object gone", "Object gone"]    # after the actions
    assert {o.target for o in savings_rollup.unread_maintenance_opportunities(ordered)} == {"DB.S.LIVE", "DB.S.NOCOL"}
    with pytest.raises(ValueError):
        book_estimated_sql(ordered.set_index("OBJECT_FQN").loc["DB.S.DROPPED"].to_dict()
                           | {"OBJECT_FQN": "DB.S.DROPPED"}, proof_sql="SELECT 1")
    # a failed confirm still reads Unconfirmed (it outranks gone: nothing was measured)
    assert set(unread_maintenance_verdicts(short, None, rate=3.0)["VERDICT"]) == {"Unconfirmed"}


def test_confirm_failure_is_worded_by_its_kind():
    """PR C review C8 / C18: only an object-not-visible / edition failure blames the edition."""
    from app.logic.unread_maintenance import confirm_failure_note
    absent = confirm_failure_note("absent", "Object 'ACCESS_HISTORY' does not exist or not authorized.")
    assert "Enterprise edition" in absent and "IMPORTED PRIVILEGES" in absent
    assert "Enterprise edition" in confirm_failure_note("other", "Unsupported feature 'ACCESS_HISTORY'.")
    timeout = confirm_failure_note("timeout", "Statement reached its statement or warehouse timeout of 180 s")
    assert "timed out" in timeout and "Database filter" in timeout and "Enterprise" not in timeout
    other = confirm_failure_note("other", "SQL compilation error:\n  invalid argument")
    assert other == "the access-history check failed: SQL compilation error: invalid argument"
    assert "Enterprise" not in other
    for kind in ("unknown_function", "missing_column", "", None):
        assert "Enterprise" not in confirm_failure_note(kind, "boom"), kind
    assert confirm_failure_note("other") == "the access-history check failed"
    assert len(confirm_failure_note("other", "x" * 5000)) < 400                 # a runaway message is capped


def test_remediation_generators_fail_closed():
    assert remediation.suspend_recluster_object("DB.S.T") == ("ALTER TABLE DB.S.T SUSPEND RECLUSTER;",
                                                               "ALTER TABLE DB.S.T RESUME RECLUSTER;")
    assert remediation.suspend_recluster_object("DB.S.MV", materialized_view=True)[0] == (
        "ALTER MATERIALIZED VIEW DB.S.MV SUSPEND RECLUSTER;")
    # PR C review C9: the reverse of a no-ON drop is guidance, never a bare ADD SEARCH OPTIMIZATION (that re-adds
    # table-wide EQUALITY, not the dropped per-column methods): capture DESCRIBE first, then re-add each method
    drop, rev = remediation.drop_search_optimization("DB.S.T")
    assert drop == "ALTER TABLE DB.S.T DROP SEARCH OPTIMIZATION;"
    assert all(line.startswith("-- ") for line in rev.splitlines()), rev          # comments only: nothing to run
    assert "DESCRIBE SEARCH OPTIMIZATION ON DB.S.T;" in rev.splitlines()[0]
    assert "ALTER TABLE DB.S.T ADD SEARCH OPTIMIZATION ON <METHOD>(<target>)" in rev and "full rebuild" in rev
    assert "table-wide EQUALITY only, not the dropped configuration" in rev
    assert "ALTER TABLE DB.S.T ADD SEARCH OPTIMIZATION;  -- full rebuild" not in rev
    assert remediation.suspend_mv_refresh("D_1.S$.M") == ("ALTER MATERIALIZED VIEW D_1.S$.M SUSPEND;",
                                                          "ALTER MATERIALIZED VIEW D_1.S$.M RESUME;")
    for bad in ("DB.S.MyTable", 'DB.S."x"', "A.B", "A.B.C.D", "", "DB.S.T;DROP", "DB..T", "1DB.S.T"):
        for gen in (remediation.suspend_recluster_object, remediation.drop_search_optimization,
                    remediation.suspend_mv_refresh):
            with pytest.raises(ValueError):
                gen(bad)
    for stmt in (*remediation.suspend_recluster_object("DB.S.T"), *remediation.suspend_mv_refresh("DB.S.M"),
                 *remediation.drop_search_optimization("DB.S.T"),
                 remediation.suspend_recluster_object("DB.S.M", materialized_view=True)[0]):
        assert _statement_allowed(stmt)[0] is False, stmt           # never executable in-app


def _action_row() -> dict:
    short = _short(DB__S__T1={"CLUSTERING_CREDITS": 30.0, "MAINT_CREDITS_30D": 10.0})
    return unread_maintenance_verdicts(short, _reads(DB__S__T1={}), rate=3.0).iloc[0].to_dict()


def test_booking_sql_is_estimated_idempotent_and_executable():
    proof = cost_sql.unread_maintenance_proof("DB.S.T1", date(2026, 9, 29), 10.0)
    sql = book_estimated_sql(_action_row(), proof_sql=proof)
    assert sql.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER (DESCRIPTION, STATE, ESTIMATED_USD, "
                          "PROOF_SQL, NOTES, FINDING_TYPE, TARGET_OBJECT)")
    assert "'ESTIMATED', 30.0," in sql and "'SUSPEND_RECLUSTER'" in sql and "'DB.S.T1'" in sql
    # PR C review C6: the dedupe keys on the OBJECT across every unread-maintenance type and any live state
    assert "WHERE NOT EXISTS (" in sql and "STATE = 'ESTIMATED'" not in sql
    guard = sql.split("WHERE NOT EXISTS (", 1)[1]
    assert ("    WHERE TARGET_OBJECT = 'DB.S.T1'\n"
            "      AND FINDING_TYPE IN ('SUSPEND_RECLUSTER', 'DROP_SEARCH_OPTIMIZATION', 'SUSPEND_MV_REFRESH')\n"
            "      AND STATE <> 'REJECTED'\n)") in guard
    assert "FINDING_TYPE = " not in guard                                  # never the dominant arm alone
    assert _statement_allowed(sql) == (True, "")                           # ';' only inside literals
    sqlglot.parse_one(sql, read="snowflake")
    ok, why = can_verify({"STATE": "ESTIMATED", "PROOF_SQL": proof, "VERIFIED_USD": 5})
    assert ok, why
    for phrase in ("monthly verifier proves or rejects", "verifier will test actuals", "verifier tests actuals",
                   "monthly verifier compares", "the monthly verifier later proves"):
        assert phrase not in sql
    row = _action_row()
    for bad in ({"VERDICT": "Keep"}, {"VERDICT": "Unconfirmed"}, {"REVIEW_SQL": None}, {"REVIEW_SQL": float("nan")},
                {"EST_MONTHLY_USD": 0.0}, {"EST_MONTHLY_USD": float("nan")}, {"FINDING_TYPE": None},
                {"OBJECT_FQN": ""}):
        with pytest.raises(ValueError):
            book_estimated_sql({**row, **bad}, proof_sql=proof)
    with pytest.raises(ValueError):
        book_estimated_sql(row, proof_sql="  ")
    hostile = book_estimated_sql({**row, "OBJECT_FQN": "ZZINJZZ' OR '1'='1"}, proof_sql=proof)
    assert "ZZINJZZ" not in _strip_literals(hostile) and _statement_allowed(hostile)[0]


def test_booking_dedupes_on_the_object_executed():
    """PR C review C6, EXECUTED in sqlite: the dominant arm flips as the pre-ALTER clustering days roll out of the
    window (SUSPEND_RECLUSTER on day 1, DROP_SEARCH_OPTIMIZATION by day 10), but the object is one saving: the
    second arm's INSERT books nothing, nor does any click once the row is VERIFIED; a REJECTED booking can be
    booked again, and another object is unaffected."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE SAVINGS_LEDGER (DESCRIPTION TEXT, STATE TEXT, ESTIMATED_USD REAL, PROOF_SQL TEXT, "
                "NOTES TEXT, FINDING_TYPE TEXT, TARGET_OBJECT TEXT)")
    proof = cost_sql.unread_maintenance_proof("DB.S.T", date(2026, 9, 1), 20.0)

    def row(clustering, search, c30, fqn="DB.S.T"):
        short = _short(**{fqn.replace(".", "__"): {"CLUSTERING_CREDITS": clustering, "SEARCH_OPT_CREDITS": search,
                                                   "MAINT_CREDITS_30D": c30}})
        return unread_maintenance_verdicts(short, _reads(**{fqn.replace(".", "__"): {}}), rate=3.0).iloc[0].to_dict()

    def book(r):
        con.execute(book_estimated_sql(r, proof_sql=proof).replace("DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER",
                                                                   "SAVINGS_LEDGER"))

    def n():
        return con.execute("SELECT COUNT(*) FROM SAVINGS_LEDGER WHERE TARGET_OBJECT = 'DB.S.T'").fetchone()[0]

    day1, day10 = row(40.0, 35.0, 20.0), row(30.0, 35.0, 12.0)
    assert (day1["FINDING_TYPE"], day10["FINDING_TYPE"]) == ("SUSPEND_RECLUSTER", "DROP_SEARCH_OPTIMIZATION")
    book(day1)
    book(day1)                                                        # the repeat click
    book(day10)                                                       # the flipped arm
    assert n() == 1
    con.execute("UPDATE SAVINGS_LEDGER SET STATE = 'VERIFIED'")
    book(day10)
    assert n() == 1                                                   # a verified booking is still the booking
    con.execute("UPDATE SAVINGS_LEDGER SET STATE = 'REJECTED'")
    book(day10)
    assert n() == 2                                                   # rejected: bookable again
    book(row(9.0, 0.0, 4.0, fqn="DB.S.OTHER"))
    assert con.execute("SELECT COUNT(*) FROM SAVINGS_LEDGER").fetchone()[0] == 3


def test_rollup_registers_the_lever():
    short = _short(D__S__A={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 4.0},
                   D__S__K={"CLUSTERING_CREDITS": 9.0})
    verdicts = unread_maintenance_verdicts(short, _reads(D__S__A={}, D__S__K={"READ_QUERIES": 1}), rate=2.0)
    opps = savings_rollup.unread_maintenance_opportunities(verdicts)
    assert [(o.source, o.target, o.monthly_usd) for o in opps] == [("UNREAD_MAINT", "D.S.A", 8.0)]
    assert savings_rollup.unread_maintenance_opportunities(None) == []
    assert savings_rollup.effort_tier("UNREAD_MAINT") == "LOW"
    same = savings_rollup.rollup_savings([opps[0], savings_rollup.SavingsOpportunity("CLUSTERING", "D.S.A", 5.0, 0.6)])
    assert same.total_monthly_usd == 8.0 and len(same.dropped) == 1               # the larger one survives
    other = savings_rollup.rollup_savings([opps[0], savings_rollup.SavingsOpportunity("CLUSTERING", "D.S.B", 5.0, 0.6)])
    assert other.total_monthly_usd == 13.0 and not other.dropped
    assert "unread_maintenance_opportunities(" not in read("app/ui/pages/cost_parts/optimize.py")  # #35 owns the headline


# --- the Storage & waste wiring (source) ------------------------------------------------------------------

def _joined(src: str) -> str:
    """Adjacent string literals joined (a caption split across source lines reads as one sentence)."""
    return re.sub(r'"\s*\n\s*f?"', "", src)


def _storage_branch() -> str:
    opt = read("app/ui/pages/cost_parts/optimize.py")
    return opt.split('elif opt_section == "Storage & waste":', 1)[1].split('st.markdown("**Storage growth movers**")', 1)[0]


def test_optimize_wiring_source():
    opt = read("app/ui/pages/cost_parts/optimize.py")
    branch = _storage_branch()
    toggle = branch.index('key="cost_unread_maint_toggle"')
    assert toggle < branch.index("cost_sql.maintenance_on_unread(90, company, database=_oc_db)")
    assert toggle < branch.index("insights_sql.object_reads_confirm(")
    assert branch.index("cost_sql.maintenance_on_unread(") < branch.index("insights_sql.object_reads_confirm(")
    assert branch.count("probe=True") >= 4                               # the object-ledger pair + both new reads
    # (PR C review C8 moved the confirm SQL into _conf_sql ahead of the latch, so the historical-tier lock now
    # targets the run() call itself instead of a 400-char window after the builder: at least as strict)
    assert "_conf_sql = insights_sql.object_reads_confirm(" in branch
    conf_run = branch.split("_conf = run(_conf_sql,", 1)[1][:200]
    assert 'tier="historical"' in conf_run and "probe=True" in conf_run
    assert branch.count("run(_conf_sql") == 1
    # sticky selection resolved by FQN, never by a raw index into a re-sortable frame
    assert "_unread_maint_sel_seen" in branch and "unread_maint_sel_last" in branch
    assert '_uv[_uv["OBJECT_FQN"].astype(str) == _um_pick]' in branch
    # the booking: SQL shown first, operator + latch as the LAST condition, stamped, one click
    book = branch.split("_bk_key = f\"unread_maint_book_{_fqn}\"", 1)[1]
    assert branch.index("st.code(_bk, language=\"sql\")") < branch.index('st.button("Book estimated saving"')
    assert ('(is_operator and st.button("Book estimated saving", key="unread_maint_book_btn")\n'
            '                                    and write_gate_open(_bk_key))') in book
    assert book.index("execute_statement(_bk, page=_PAGE)") < book.index("stamp_write(_bk_key, ok)")
    assert "Book only after the ALTER above has run in a worksheet" in branch
    assert "REMEDIATION_LOG" not in branch
    # house budgets: counts unchanged except the one new latched write
    assert opt.count("ACCOUNT_USAGE") == 5
    assert opt.count("methodology_note(") == 4
    assert len(re.findall(r"write_gate_open\(", opt)) == len(re.findall(r"stamp_write\(", opt)) == 7
    assert len(re.findall(r"st\.(?:info|success)\(", opt)) <= 4
    assert opt.count("INSERT INTO {core_object('REMEDIATION_LOG')}") == 3
    # the block sits above the storage-waste latch the source-slice locks index from
    assert opt.index('key="cost_unread_maint_toggle"') < opt.index('write_gate_open("waste")')
    # degraded honesty: a failed confirm says so and offers no SQL; a stale ledger warns
    assert "ledger-only shortlist, not suspend candidates; no SQL." in branch
    # PR C review C8 / C18: the reason is worded by the error kind (never a blanket edition claim), and the
    # failure is latched per SQL + cache scope so the 180 s scan does not re-run on every rerun; retry on request
    assert "needs Enterprise edition" not in branch
    assert "confirm_failure_note(_conf.error_kind, _conf.error)" in branch
    latch = branch.split('_conf_fail = st.session_state.get("_unread_confirm_failed")', 1)[1]
    assert latch.index('_conf_fail.get("sig") == _conf_sig') < latch.index("_conf = run(_conf_sql,")
    assert latch.index("_conf = run(_conf_sql,") < latch.index('st.session_state["_unread_confirm_failed"] = {')
    assert "_cache_scope(_conf_sql)" in branch                             # Refresh re-arms the latch
    assert 'key="unread_maint_confirm_retry"' in branch and "on_click=_clear_unread_confirm_latch" in branch
    clear = opt.split("def _clear_unread_confirm_latch(", 1)[1].split("\ndef ", 1)[0]
    assert 'st.session_state.pop("_unread_confirm_failed", None)' in clear
    # PR C review C11: a dropped / renamed object says so and stays out of the totals
    assert "_uv[\"VERDICT\"].eq(VERDICT_GONE).sum()" in branch and "not counted in the totals above" in branch
    assert "VERDICT_GONE: " in branch
    # PR C review C9: the bulk caption never implies a bare ADD restores the dropped configuration
    assert "capture them first with DESCRIBE SEARCH OPTIMIZATION" in _joined(branch)
    assert "st.warning(f\"The object ledger's newest day is" in branch
    assert "A fixed 90-day window: the window picker does not narrow it." in branch


def test_status_chips_cover_every_verdict():
    from app.logic.unread_maintenance import (
        VERDICT_GONE,
        VERDICT_KEEP,
        VERDICT_NO_RECENT,
        VERDICT_SHARED,
        VERDICT_UNCONFIRMED,
    )
    from app.ui.status_colors import _VERDICTS
    for v in (*ACTION_VERDICTS, VERDICT_KEEP, VERDICT_SHARED, VERDICT_NO_RECENT, VERDICT_UNCONFIRMED, VERDICT_GONE):
        assert v.upper() in _VERDICTS, v
