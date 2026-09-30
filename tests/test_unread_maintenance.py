"""Next-Fifty #30: maintenance spend on objects nobody reads — builders, verdicts, SQL, booking, wiring.

Pure + source locks (both CI legs). The mart shortlist is EXECUTED in tests/test_unread_maintenance_harness.py;
the rendered panel is driven in tests/test_prc_c2_shaped.py (skipped on the streamlit 1.52.2 floor, so every
wiring claim is also locked by source below)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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
from tests._source import ROOT, read

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
    # #35: the headlines never build the lever themselves; its ONLY call site outside tests is the body of
    # savings_rollup.unread_handoff (so only a CONFIRMED Storage & waste scan can put dollars in Addressable $/mo)
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/decision_studio.py"):
        assert "unread_maintenance_opportunities(" not in read(rel), rel
    calls = [(str(py.relative_to(ROOT)), src.count("unread_maintenance_opportunities("))
             for py in sorted((ROOT / "app").rglob("*.py"))
             if "unread_maintenance_opportunities(" in (src := py.read_text(encoding="utf-8")
                                                        .replace("def unread_maintenance_opportunities(", ""))]
    assert calls == [(str(Path("app/logic/savings_rollup.py")), 1)], calls
    handoff_body = read("app/logic/savings_rollup.py").split("def unread_handoff(", 1)[1].split("\ndef ", 1)[0]
    assert handoff_body.count("unread_maintenance_opportunities(verdicts)") == 1


# --- #35: the UNREAD_MAINT lever's session handoff into Addressable $/mo (pure) ----------------------------

_AS_OF = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)      # R1-17: the UI stamps aware UTC (utc_now)
_WHERE = "Storage & waste"
_RATE = 2.0                                                         # _mix()'s pricing rate


def _mix(rate: float = 2.0) -> pd.DataFrame:
    """One object per verdict: an action (D.S.A), Keep, Check share consumers, No recent spend, Object gone and
    Unconfirmed (no confirm row)."""
    short = _short(D__S__A={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 4.0},
                   D__S__K={"CLUSTERING_CREDITS": 9.0}, D__S__SH={"CLUSTERING_CREDITS": 9.0},
                   D__S__OLD={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 0.0},
                   D__S__GONE={"CLUSTERING_CREDITS": 9.0}, D__S__MISS={"CLUSTERING_CREDITS": 9.0})
    reads = _reads(D__S__A={}, D__S__K={"READ_QUERIES": 1}, D__S__SH={"SHARED_DATABASE": True}, D__S__OLD={},
                   D__S__GONE={"MATCHED_BY_ID": False})
    return unread_maintenance_verdicts(short, reads, rate=rate)


def _hand(verdicts=None, status="confirmed", **kw) -> dict:
    kw.setdefault("company", "ALFA")
    kw.setdefault("database", "")
    kw.setdefault("scope", "S")
    kw.setdefault("as_of", _AS_OF)
    kw.setdefault("rate", _RATE)
    return savings_rollup.unread_handoff(verdicts, status=status, **kw)


def _lever(handoff, *, company="ALFA", scope="S", age_sec=60.0, where=_WHERE, rate=_RATE):
    return savings_rollup.unread_lever(handoff, company=company, scope=scope,
                                       now=_AS_OF + timedelta(seconds=age_sec), rate=rate, where=where)


def test_handoff_carries_only_confirmed_action_rows():
    verdicts = _mix()
    assert set(verdicts["VERDICT"]) == {"Suspend clustering", "Keep", "Check share consumers", "No recent spend",
                                        "Object gone", "Unconfirmed"}
    h = _hand(verdicts)
    assert h["status"] == "confirmed" and h["rows"] == [["D.S.A", 8.0, 0.6]] and h["booked_excluded"] == 0
    assert _hand(verdicts, status=savings_rollup.UNREAD_CONFIRM_FAILED)["rows"] == []
    for status in (savings_rollup.UNREAD_CLEAN, savings_rollup.UNREAD_SHORTLIST_FAILED, "bogus"):
        assert _hand(verdicts, status=status)["rows"] == [], status
    # a mart-only $ can never pass: without the confirm every row is Unconfirmed, even if labelled confirmed
    unconfirmed = unread_maintenance_verdicts(_short(D__S__A={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 4.0}),
                                              None, rate=2.0)
    h = _hand(unconfirmed)
    assert h["rows"] == [] and h["status"] == "confirmed"
    assert h["company"] == "ALFA" and h["database"] == "" and h["scope"] == "S"
    assert h["as_of"] == "2026-09-30T10:00:00+00:00" and h["rate"] == 2.0
    assert _hand(None, company="  alfa ", database=" db1 ")["company"] == "ALFA"
    assert _hand(None, company="", database=" db1 ")["company"] == "ALL"
    assert _hand(None, database=" db1 ")["database"] == "DB1"


def test_handoff_round_trips_to_the_registered_generator():
    short = _short(D__S__A={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 4.0},
                   D__S__B={"SEARCH_OPT_CREDITS": 9.0, "MAINT_CREDITS_30D": 1.25},
                   D__S__K={"CLUSTERING_CREDITS": 9.0})
    verdicts = unread_maintenance_verdicts(short, _reads(D__S__A={}, D__S__B={}, D__S__K={"READ_QUERIES": 2}),
                                           rate=3.0)
    lever = _lever(_hand(verdicts, rate=3.0), rate=3.0)
    assert lever.included and lever.reason == "" and lever.note == ""
    assert lever.opportunities == tuple(savings_rollup.unread_maintenance_opportunities(verdicts))
    assert [o.target for o in lever.opportunities] == ["D.S.A", "D.S.B"]


def test_handoff_is_session_safe():
    import json
    for status in ("confirmed", "clean", "confirm_failed", "shortlist_failed"):
        h = _hand(_mix(), status=status, checked=50, truncated=True, booked=frozenset({"X"}))
        assert json.loads(json.dumps(h)) == h, status                   # primitives only
        assert all(isinstance(v, (str, int, float, bool, list)) for v in h.values())
    assert _hand(_mix(), booked=None)["status"] == "ledger_failed"
    assert json.dumps(_hand(_mix(), booked=None))


def test_handoff_leaves_out_booked_objects_and_needs_the_ledger():
    h = _hand(_mix(), booked=frozenset({"D.S.A"}))
    assert h["rows"] == [] and h["booked_excluded"] == 1 and h["status"] == "confirmed"
    h = _hand(_mix(), booked=frozenset({"D.S.OTHER"}))
    assert h["rows"] == [["D.S.A", 8.0, 0.6]] and h["booked_excluded"] == 0
    # the ledger read failed: with rows to count nothing is counted (never a possible double count)
    failed = _hand(_mix(), booked=None)
    assert failed["status"] == "ledger_failed" and failed["rows"] == [] and failed["booked_excluded"] == 0
    # ... but with nothing to count there is nothing to leave out, so the scan still counts ($0)
    assert _hand(_mix(), status=savings_rollup.UNREAD_CONFIRM_FAILED, booked=None)["status"] == "confirm_failed"
    assert _hand(None, booked=None)["status"] == "confirmed"


def test_booked_objects_matches_the_booking_dedupe():
    import sqlite3

    from app.logic.unread_maintenance import booked_objects, object_key
    ledger = pd.DataFrame([
        {"TARGET_OBJECT": "DB.S.A", "FINDING_TYPE": "SUSPEND_RECLUSTER", "STATE": "ESTIMATED"},
        {"TARGET_OBJECT": "DB.S.V", "FINDING_TYPE": "DROP_SEARCH_OPTIMIZATION", "STATE": "VERIFIED"},
        {"TARGET_OBJECT": "DB.S.R", "FINDING_TYPE": "DROP_SEARCH_OPTIMIZATION", "STATE": "REJECTED"},
        {"TARGET_OBJECT": "DB.S.N", "FINDING_TYPE": "SUSPEND_MV_REFRESH", "STATE": None},
        {"TARGET_OBJECT": "DB.S.W", "FINDING_TYPE": "AUTO_SUSPEND", "STATE": "ESTIMATED"},
        {"TARGET_OBJECT": '"db"."s"."q"', "FINDING_TYPE": " suspend_mv_refresh ", "STATE": " verified "},
        {"TARGET_OBJECT": None, "FINDING_TYPE": "SUSPEND_RECLUSTER", "STATE": "ESTIMATED"},
    ])
    assert booked_objects(ledger) == frozenset({"DB.S.A", "DB.S.V", "DB.S.Q"})
    assert object_key('"db"."s"."q"') == "DB.S.Q" and object_key(None) == "" and object_key(float("nan")) == ""
    assert booked_objects(pd.DataFrame()) == frozenset()
    assert booked_objects(None) is None and booked_objects("x") is None       # type: ignore[arg-type]
    assert booked_objects(ledger.drop(columns="STATE")) is None
    assert booked_objects(pd.DataFrame({"TARGET_OBJECT": ["DB.S.A"], "FINDING_TYPE": [pd.NA],
                                        "STATE": ["ESTIMATED"]})) == frozenset()
    # the SAME predicate the Book button refuses on, EXECUTED: every plain-name object booked_objects calls booked
    # gets no second row, every other one does (REJECTED, a NULL state, another finding type)
    proof = cost_sql.unread_maintenance_proof("DB.S.A", date(2026, 9, 1), 10.0)
    assert "STATE <> 'REJECTED'" in book_estimated_sql(_action_row(), proof_sql=proof)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE SAVINGS_LEDGER (DESCRIPTION TEXT, STATE TEXT, ESTIMATED_USD REAL, PROOF_SQL TEXT, "
                "NOTES TEXT, FINDING_TYPE TEXT, TARGET_OBJECT TEXT)")
    plain = ledger[ledger["TARGET_OBJECT"].isin(["DB.S.A", "DB.S.V", "DB.S.R", "DB.S.N", "DB.S.W"])]
    con.executemany("INSERT INTO SAVINGS_LEDGER (STATE, FINDING_TYPE, TARGET_OBJECT) VALUES (?, ?, ?)",
                    plain[["STATE", "FINDING_TYPE", "TARGET_OBJECT"]].itertuples(index=False, name=None))
    booked = booked_objects(plain)
    for fqn in ("DB.S.A", "DB.S.V", "DB.S.R", "DB.S.N", "DB.S.W"):
        before = con.execute("SELECT COUNT(*) FROM SAVINGS_LEDGER WHERE TARGET_OBJECT = ?", (fqn,)).fetchone()[0]
        row = {**_action_row(), "OBJECT_FQN": fqn}
        con.execute(book_estimated_sql(row, proof_sql=proof).replace("DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER",
                                                                      "SAVINGS_LEDGER"))
        after = con.execute("SELECT COUNT(*) FROM SAVINGS_LEDGER WHERE TARGET_OBJECT = ?", (fqn,)).fetchone()[0]
        assert (after == before) == (fqn in booked), fqn


_R = {
    "not_run": "not checked this session: run the unread-maintenance scan in Storage & waste",
    "company": "last checked for ALFA, not TRXS: re-run the scan in Storage & waste",
    "database": "last checked for database DB1 only: clear the Database filter and re-run the scan in Storage & waste",
    "stale": ("the last check was shown over 1h ago, or cached data was refreshed since: re-run the scan in "
              "Storage & waste"),
    "shortlist": "the object-cost ledger could not be read in Storage & waste",
    "confirm": "the access-history check failed, so no object is confirmed unread",
    "ledger": "the Savings ledger could not be read, so objects already booked there could not be left out",
    "rate": ("the credit rate changed since the last check, which priced its objects at the old rate: re-run the "
             "scan in Storage & waste"),
}


@pytest.mark.parametrize(("handoff", "kwargs", "reason"), [
    (None, {}, "not_run"),
    ("not a mapping", {}, "not_run"),
    ({"status": "bogus"}, {}, "not_run"),
    ({"status": ["confirmed"]}, {}, "not_run"),
    ("confirmed", {"company": "trxs"}, "company"),
    ({"database": "db1"}, {}, "database"),
    ("confirmed", {"scope": "S2"}, "stale"),
    ("confirmed", {"age_sec": 3601}, "stale"),
    ({"as_of": "not a timestamp"}, {}, "stale"),
    ({"as_of": "2026-09-30T10:00:00"}, {}, "stale"),              # R1-17: a naive wall-clock stamp never fresh
    ("confirmed", {"age_sec": -61}, "stale"),                     # R1-17: stamped in the future past the skew
    ("confirmed", {"age_sec": -3600}, "stale"),                   # ... e.g. a DST fall-back repeat of an hour
    ("confirmed", {"rate": 3.0}, "rate"),                         # R1-16: the rows were priced at $2.00/credit
    ({"rate": None}, {}, "rate"),                                 # an unstamped handoff never counts dollars
    ({"rate": "2.0"}, {}, "rate"),
    ({"status": "shortlist_failed"}, {}, "shortlist"),
    ({"status": "confirm_failed"}, {}, "confirm"),
    ({"status": "ledger_failed"}, {}, "ledger"),
])
def test_unread_lever_absent_reasons(handoff, kwargs, reason):
    base = _hand(_mix())
    h = {**base, **handoff} if isinstance(handoff, dict) else (base if handoff == "confirmed" else handoff)
    lever = _lever(h, **kwargs)
    assert lever.included is False and lever.opportunities == () and lever.note == ""
    assert lever.reason == _R[reason]


def test_unread_lever_included_states_and_notes():
    clean = _lever(_hand(None, status=savings_rollup.UNREAD_CLEAN))
    assert clean.included and clean.opportunities == () and clean.reason == "" and clean.note == ""
    zero = _lever(_hand(_mix(), booked=frozenset({"D.S.A"}), checked=50, truncated=True))
    assert zero.included and zero.opportunities == ()                          # counted, at $0
    assert zero.note == ("only the top 50 shortlisted objects were checked, so this is a floor; 1 already booked "
                         "on the Savings ledger left out")
    one = _lever(_hand(_mix(), checked=1234, truncated=True))
    assert one.note == "only the top 1,234 shortlisted objects were checked, so this is a floor"
    assert [(o.source, o.target, o.monthly_usd, o.confidence) for o in one.opportunities] == [
        ("UNREAD_MAINT", "D.S.A", 8.0, 0.6)]
    # malformed rows are skipped, never raised on
    odd = _lever({**_hand(_mix()), "rows": [["X", 5.0], "bad", ["Y", -1, 0.6], ["Z", "7.5", "0.6"], ["", 3, 0.6],
                                            [None, 2, 0.6], None]})
    assert [(o.target, o.monthly_usd, o.confidence) for o in odd.opportunities] == [("Z", 7.5, 0.6)]
    assert _lever({**_hand(_mix()), "rows": "nope"}).opportunities == ()


def test_unread_lever_age_boundary_and_scope():
    h = _hand(_mix())
    assert _lever(h, age_sec=3600).included and not _lever(h, age_sec=3601).included
    assert _lever(h, age_sec=-5).included                                      # a clock wobble is not stale
    assert _lever(h, age_sec=-60).included and not _lever(h, age_sec=-61).included   # R1-17: the skew bound
    # R1-17: a naive 'now' (the pre-fix account_now wall clock) never reads fresh, even against a naive stamp
    naive = _hand(_mix(), as_of=_AS_OF.replace(tzinfo=None))
    assert not savings_rollup.unread_lever(naive, company="ALFA", scope="S", rate=_RATE, where=_WHERE,
                                           now=_AS_OF.replace(tzinfo=None) + timedelta(minutes=1)).included
    assert not savings_rollup.unread_lever(h, company="ALFA", scope="S", rate=_RATE, where=_WHERE,
                                           now=_AS_OF.replace(tzinfo=None) + timedelta(minutes=1)).included
    # R1-17: across the 2026-11-01 fall-back an aware stamp ages in real time. A stamp at 01:50 CDT is stale 61 real
    # minutes later (01:51 CST), where the naive wall clock read it as 40 minutes in the future until ~2h had passed.
    # Both sides here carry the SAME Chicago tzinfo, where Python subtracts wall clocks: the age must be taken in UTC
    from zoneinfo import ZoneInfo
    chicago = ZoneInfo("America/Chicago")
    stamp = datetime(2026, 11, 1, 1, 50, tzinfo=chicago)                        # fold=0: CDT (UTC-5)
    dst = _hand(_mix(), as_of=stamp)

    def after(minutes: int) -> datetime:
        return (stamp.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(chicago)

    assert after(61).strftime("%H:%M") == "01:51" and after(61).utcoffset() == timedelta(hours=-6)

    def at(minutes: int) -> bool:
        return savings_rollup.unread_lever(dst, company="ALFA", scope="S", now=after(minutes), rate=_RATE,
                                           where=_WHERE).included

    assert at(59) and at(60) and not at(61) and not at(119)
    # R1-16: a clean / all-booked scan is $0 at any rate, so a rate change leaves it counted
    assert _lever(_hand(None, status=savings_rollup.UNREAD_CLEAN), rate=9.99).included
    assert _lever(_hand(_mix(), booked=frozenset({"D.S.A"})), rate=9.99).included
    assert _lever(h, rate=2.0 + 1e-12).included                                # float noise is the same rate
    assert _lever(_hand(_mix(), company="alfa"), company="ALFA").included
    assert _lever(_hand(_mix(), company="ALFA"), company=" alfa ").included
    assert _lever(_hand(_mix(), company=""), company="ALL").included
    assert _lever(_hand(_mix(), company="ALL"), company="").included
    assert not _lever(_hand(_mix(), company="ALL"), company="ALFA").included
    assert _lever(h, where="Cost ▸ Optimization & Savings ▸ Storage & waste", scope="other").reason.endswith(
        "re-run the scan in Cost ▸ Optimization & Savings ▸ Storage & waste")


def test_handoff_max_age_is_the_historical_cache_ttl():
    from app.core.query import CACHE_TTLS
    from app.logic.formulas import humanize_duration
    assert savings_rollup.UNREAD_HANDOFF_MAX_AGE_SEC == CACHE_TTLS["historical"] == 3600
    assert savings_rollup.UNREAD_HANDOFF_MAX_SKEW_SEC == 60
    assert humanize_duration(savings_rollup.UNREAD_HANDOFF_MAX_AGE_SEC) == "1h"
    assert savings_rollup.UNREAD_HANDOFF_KEY == "_ow_unread_maint_handoff"
    assert not savings_rollup.UNREAD_HANDOFF_KEY.startswith(("flt_", "cost_", "opt_"))   # never a widget key


def test_lever_basis_and_short_wording():
    basis = savings_rollup.lever_basis(
        ["IDLE", "UNREAD_MAINT"], {"RESIZE": "turn on 'Load right-sizing profile (heavy scan)' above"},
        {"UNREAD_MAINT": "only the top 50 shortlisted objects were checked, so this is a floor"})
    assert basis == ("Levers counted: idle timer + unread maintenance (only the top 50 shortlisted objects were "
                     "checked, so this is a floor). Not counted: right-sizing (turn on 'Load right-sizing profile "
                     "(heavy scan)' above).")
    assert savings_rollup.lever_basis([], {}) == "Levers counted: none."
    assert savings_rollup.lever_basis(["IDLE"], {}, {"UNREAD_MAINT": ""}) == "Levers counted: idle timer."
    assert savings_rollup.lever_basis([], {"IDLE": "a", "UNREAD_MAINT": "b"}) == (
        "Levers counted: none. Not counted: idle timer (a); unread maintenance (b).")
    assert savings_rollup.lever_short(["IDLE"]) == "idle-timer only"
    assert savings_rollup.lever_short([]) == "no lever counted"
    assert savings_rollup.lever_short(["IDLE", "RESIZE"]) == "idle timer + right-sizing"
    assert savings_rollup.lever_short(["IDLE", "RESIZE", "UNREAD_MAINT"]) == (
        "idle timer + right-sizing + unread maintenance")


def test_handoff_note_wording():
    note = savings_rollup.unread_handoff_note
    for silent in (None, "x", _hand(None, status="clean"), _hand(_mix(), status="shortlist_failed"),
                   _hand(_mix(), booked=None), {"status": "bogus"}):
        assert note(silent) == "", silent
    assert note(_hand(_mix(), database="db1")) == (
        "Not added to Addressable $/mo: this scan is narrowed to database DB1. Clear the Database filter and re-run "
        "it to count confirmed objects in Idle & sizing and on Proof ▸ Pipeline.")
    assert note(_hand(_mix(), status="confirm_failed", database="db1")).startswith(
        "Not added to Addressable $/mo: this scan is narrowed to database DB1.")
    assert note(_hand(_mix(), status="confirm_failed")) == (
        "Not added to Addressable $/mo: no object is confirmed unread.")
    assert note(_hand(None)) == "No confirmed-unread object to add to Addressable $/mo."
    assert note(_hand(_mix(), booked=frozenset({"D.S.A"}))) == (
        "No confirmed-unread object to add to Addressable $/mo: the 1 confirmed are already booked on the Savings "
        "ledger.")
    assert note(_hand(_mix())) == (
        "1 confirmed-unread object(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline for this "
        "Company. They drop out when cached data is refreshed, the credit rate changes, or 1h after this panel was "
        "last shown. An object booked in another session keeps counting here until the scan is re-run at "
        "least 5m after that booking (the Savings-ledger read that leaves booked objects out is cached for "
        "up to 5m): at most 1h 5m after the booking.")
    short = _short(D__S__A={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 4.0},
                   D__S__B={"CLUSTERING_CREDITS": 9.0, "MAINT_CREDITS_30D": 2.0})
    two = unread_maintenance_verdicts(short, _reads(D__S__A={}, D__S__B={}), rate=1.0)
    assert note(_hand(two, checked=50, truncated=True, booked=frozenset({"D.S.B"}))) == (
        "1 confirmed-unread object(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline for this "
        "Company (a floor: only the top 50 shortlisted objects were checked). 1 already booked on the Savings "
        "ledger are left out. They drop out when cached data is refreshed, the credit rate changes, or 1h after "
        "this panel was last shown. An object booked in another session keeps counting here until the scan is "
        "re-run at least 5m after that booking (the Savings-ledger read that leaves booked objects out is cached "
        "for up to 5m): at most 1h 5m after the booking.")
    assert savings_rollup.S_LEDGER_UNAVAILABLE == (
        "The Savings ledger could not be read, so confirmed objects are not added to Addressable $/mo (objects "
        "already booked could not be left out).")


def test_savings_rollup_stays_pure():
    import ast
    for rel in ("app/logic/savings_rollup.py", "app/logic/unread_maintenance.py"):
        src = read(rel)
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module or "")
        assert not [m for m in mods if m == "streamlit" or m.startswith(("streamlit.", "app.data", "app.ui"))], rel
        assert "account_now(" not in src and "datetime.now(" not in src and "date.today(" not in src, rel


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
    assert opt.count("ACCOUNT_USAGE") == 6        # 5 -> 6 at v4.604: the #38 cluster-cap read's source label
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


def test_storage_and_waste_publishes_the_handoff_source():
    """#35 (the floor leg skips the shaped twin): Storage & waste is the ONLY writer of the handoff, only inside the
    scan toggle, once per outcome (clean / confirmed-or-confirm-failed / shortlist failed); a booking leaves the
    object out in the same run; the booked-objects read is gated and never clears the handoff."""
    opt = read("app/ui/pages/cost_parts/optimize.py")
    branch = _storage_branch()
    write = "st.session_state[UNREAD_HANDOFF_KEY] = unread_handoff("
    assert branch.count(write) == 3 and opt.count(write) == 3
    assert "status=UNREAD_CONFIRMED if _conf.ok else UNREAD_CONFIRM_FAILED" in branch
    assert "status=UNREAD_CLEAN" in branch and "status=UNREAD_SHORTLIST_FAILED" in branch
    # R1-16 / R1-17: every write stamps the aware UTC clock and the rate its rows were priced at (the same `rate`
    # the verdicts are priced with); the naive account wall clock never stamps it
    assert branch.count("as_of=utc_now(), rate=rate") == 3
    assert "as_of=account_now()" not in opt and "unread_maintenance_verdicts(_um.df, _conf.df if _conf.ok else " \
        "None, rate=rate)" in branch
    toggle = branch.index('key="cost_unread_maint_toggle"')
    writes = [m.start() for m in re.finditer(re.escape(write), branch)]
    assert all(toggle < w for w in writes)
    clean_at = branch.index("status=UNREAD_CLEAN")
    assert branch.index("if _um.ok and _um.empty:") < clean_at < branch.index('elif guard(_um, ""):')
    confirmed_at = branch.index("status=UNREAD_CONFIRMED if _conf.ok else UNREAD_CONFIRM_FAILED")
    assert branch.index("stamp_write(_bk_key, ok)") < confirmed_at            # after any booking this run
    assert branch.index("A fixed 90-day window") < confirmed_at < branch.index("result_caption(_um)\n"
                                                                                "                result_caption(_conf)")
    tail = branch.split("result_caption(_conf)\n", 1)[1]
    assert tail.lstrip().startswith("else:") and "status=UNREAD_SHORTLIST_FAILED" in tail
    # a booking leaves the headline in the same run (notify() does not rerun)
    book = branch.split("stamp_write(_bk_key, ok)  # C48\n", 1)[1]
    assert book.lstrip().startswith("if ok and _booked is not None:\n")
    assert "_booked = _booked | {object_key(_fqn)}" in book.split("notify(", 1)[0]
    # the booked-objects read: only with a confirmed action row and no Database filter, Proof's SQL + tier
    gate = 'if _conf.ok and not _ua.empty and not str(_oc_db or "").strip():'
    assert branch.count(gate) == 1
    led = branch.split(gate, 1)[1].split("_cap = f", 1)[0]
    assert 'run(mart_sql.savings_ledger(limit=None), page=_PAGE, key="booked_unread_ledger",' in led
    assert 'tier="recent"' in led and "probe=True" not in led
    assert "unread_maint_" not in "booked_unread_ledger"                  # toggle_cost_hint keys on that prefix
    assert "booked_objects(_led.df) if _led.ok and not _led.truncated else None" in led
    assert branch.index('_booked: frozenset[str] | None = frozenset()') < branch.index(gate)
    assert "unread_handoff_note(st.session_state[UNREAD_HANDOFF_KEY])" in branch
    assert 'empty_state("unavailable", md_dollars(S_LEDGER_UNAVAILABLE), detail=_led_err)' in branch
    # never cleared (the toggle resets on every revisit), never built here, and no new ACCOUNT_USAGE read
    assert "pop(UNREAD_HANDOFF_KEY" not in opt and "del st.session_state[UNREAD_HANDOFF_KEY]" not in opt
    assert opt.count("ACCOUNT_USAGE") == 6   # v4.604 #38: + the toggled cluster-use read (test_perf_budgets optimize.py 5 -> 6)
    ds = read("app/ui/decision_studio.py")
    assert "UNREAD_HANDOFF_KEY] =" not in ds and "unread_handoff(" not in ds


def test_idle_and_sizing_headline_reads_only_the_handoff_source():
    opt = read("app/ui/pages/cost_parts/optimize.py")
    idle = opt.split('if opt_section == "Idle & sizing":', 1)[1].split('elif opt_section == "Queries & patterns":', 1)[0]
    call = ("_unread = unread_lever(st.session_state.get(UNREAD_HANDOFF_KEY), company=company, scope=cache_scope(),\n"
            '                               now=utc_now(), rate=rate, where="Storage & waste")')
    assert idle.count(call) == 1
    assert idle.index(call) < idle.index("_savings_opps.extend(_unread.opportunities)") < idle.index(
        "_roll = rollup_savings(_savings_opps)")
    for read_call in ("maintenance_on_unread(", "object_reads_confirm(", "savings_ledger(", "unread_handoff("):
        assert read_call not in idle, read_call                                    # zero reads here
    # #35 storage leg: the storage-waste lever joins the same caption
    assert ("lever_basis(_counted, _absent, {\"UNREAD_MAINT\": _unread.note, \"STORAGE_WASTE\": _storage.note})"
            in idle)
    assert "st.caption(md_dollars(_basis))" in idle
    assert 'st.caption(md_dollars("No open opportunities from the levers counted. " + _basis))' in idle
    joined = _joined(opt)
    assert "plug into the same rollup next" not in joined
    # #35 storage leg: storage waste is in the total now, except the bytes no lever claims
    assert ("Also not in this total: the failed-query **Wasted spend** board (Operations), the **Serverless ROI** "
            "panel above, the automatic-clustering panel in Storage & waste, and the storage-waste bytes no lever "
            "claims (fail-safe, clone-retained, and tables someone reads).") in joined
    assert "or stopping maintenance on an unread object" in joined
    assert ("an object you stopped without booking keeps counting until those 30 days roll off.") in joined


def test_the_handoff_clock_is_aware_utc_on_both_sides():
    """R1-17: the writers (Storage & waste) and both readers (Idle & sizing, Proof ▸ Pipeline) pass
    formulas.utc_now(), an aware UTC clock, so the 1h life is real time across a DST change; the logic still
    reads no clock itself."""
    from app.logic.formulas import utc_now
    now = utc_now()
    assert now.utcoffset() == timedelta(0) and abs((datetime.now(UTC) - now).total_seconds()) < 5
    opt, ds = read("app/ui/pages/cost_parts/optimize.py"), read("app/ui/decision_studio.py")
    assert opt.count("unread_lever(") == 1 and "now=utc_now(), rate=rate," in opt
    assert ds.count("unread_lever(") == 1 and "now=utc_now(), rate=rate," in ds
    assert opt.count("unread_handoff(") == 3 == opt.count("as_of=utc_now(), rate=rate")


def test_only_the_book_button_changes_the_unread_booked_set_in_app():
    """R1-16: the handoff keeps the booked set read when the scan ran and is not invalidated by other Savings-ledger
    writes. That is safe only while no in-app write can change that set except the Book button, which updates it in
    the same run (test_a_booked_object_leaves_the_addressable_headline). Lock the premise: the unread finding types
    are spelled only in the verdict module and ledger_measure's read-only basis map, the booking SQL has one call
    site, and the app's one ledger REJECT writer (Savings ▸ 'Reject superseded duplicates') matches only the
    autobooked warehouse levers (a REJECT could only un-book anyway: an under-count). Another session's booking is
    disclosed instead."""
    types = ("SUSPEND_RECLUSTER", "DROP_SEARCH_OPTIMIZATION", "SUSPEND_MV_REFRESH")
    spelled = sorted({str(py.relative_to(ROOT).as_posix()) for py in (ROOT / "app").rglob("*.py")
                      if any(t in py.read_text(encoding="utf-8") for t in types)})
    assert spelled == ["app/logic/ledger_measure.py", "app/logic/unread_maintenance.py"], spelled
    srcs = {str(py.relative_to(ROOT).as_posix()): py.read_text(encoding="utf-8")
            for py in (ROOT / "app").rglob("*.py")}
    booking = [(rel, src.count("book_estimated_sql(")) for rel, src in srcs.items()
               if "book_estimated_sql(" in src.replace("def book_estimated_sql(", "")]
    assert booking == [("app/ui/pages/cost_parts/optimize.py", 1)], booking
    assert [rel for rel, src in srcs.items() if "STATE = 'REJECTED'," in src] == ["app/data/mart_sql.py"]
    from app.config import LEDGER_AUTOBOOKED_LEVERS
    from app.data import mart_sql
    assert "FROM ({_ledger_twin_select()}) t" in srcs["app/data/mart_sql.py"].split(
        "def supersede_ledger_twins_sql(", 1)[1].split("\ndef ", 1)[0]
    twin = mart_sql._ledger_twin_select()
    assert "AND UPPER(TRIM(m.FINDING_TYPE)) IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')" in twin
    assert not set(LEDGER_AUTOBOOKED_LEVERS) & set(ARM_FINDING_TYPE.values())
    assert savings_rollup.N_ELSEWHERE in savings_rollup.unread_handoff_note(_hand(_mix()))
    # review r2 R2-6 / R2-9: both headlines' help carry the one shared rule (the ledger read's cache time included)
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/decision_studio.py"):
        src = _joined(read(rel))
        assert src.count(" + H_BOOKED + ") == 1, rel
        assert "when that scan ran" not in src and "since then keeps counting until the scan is re-run" not in src


def test_another_sessions_booking_is_disclosed_with_the_ledger_cache_time():
    """Review r2 R2-6 / R2-9: the booked set comes from a 'recent'-tier (5-minute) cached ledger read, and no salt
    sees another session's booking. So a scan re-run within 5 minutes of that booking still counts the object and
    re-stamps the handoff for another hour: it can count up to 1h 5m after the booking, and a re-run does not
    always drop it. The disclosure said 'until the scan is re-run (at most 1h)'."""
    from app.core.query import CACHE_TTLS
    assert savings_rollup.BOOKED_LEDGER_CACHE_SEC == CACHE_TTLS["recent"] == 300
    led = _storage_branch().split('key="booked_unread_ledger"', 1)
    assert "run(mart_sql.savings_ledger(limit=None), page=_PAGE," in led[0][-80:] and 'tier="recent",' in led[1][:60]
    # the scenario: the ledger read is cached at t0 - 5s, X is booked elsewhere at t0, the scan is re-run at
    # t0 + 290s (a cache hit until t0 + 295s: X still counts) and stamped then -- X still counts at t0 + 1h 4m 40s
    t0 = _AS_OF
    restamp = _hand(_mix(), as_of=t0 + timedelta(seconds=290))
    counted = savings_rollup.unread_lever(restamp, company="ALFA", scope="S", rate=_RATE, where=_WHERE,
                                          now=t0 + timedelta(seconds=3880))
    assert counted.included and [o.target for o in counted.opportunities] == ["D.S.A"]
    age_at_check = 3880                                                      # seconds after the booking
    assert age_at_check > savings_rollup.UNREAD_HANDOFF_MAX_AGE_SEC          # past the old 'at most 1h'
    assert age_at_check <= savings_rollup.UNREAD_HANDOFF_MAX_AGE_SEC + savings_rollup.BOOKED_LEDGER_CACHE_SEC
    assert savings_rollup.N_ELSEWHERE == (
        "An object booked in another session keeps counting here until the scan is re-run at least 5m after that "
        "booking (the Savings-ledger read that leaves booked objects out is cached for up to 5m): at most 1h 5m "
        "after the booking.")
    assert savings_rollup.H_BOOKED == (
        "less any already booked on the Savings ledger as of the scan's ledger read, which is cached for up to 5m "
        "(an object booked in another session keeps counting until the scan is re-run at least 5m after that "
        "booking: at most 1h 5m)")
    for text in (savings_rollup.N_ELSEWHERE, savings_rollup.H_BOOKED):
        assert "at most 1h)" not in text and "at most 1h." not in text


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
