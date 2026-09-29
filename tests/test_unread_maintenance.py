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
    assert "MIN(MIN(DAY)) OVER () AS COVERAGE_START_DAY" in sql and "MAX(MAX(DAY)) OVER () AS LEDGER_LAST_DAY" in sql
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
    assert "UPPER(REPLACE(OBJECT_FQN, '\"', '')) = 'DB.S.MYTABLE'" in proof   # the shortlist's normalized key
    assert "DAY >= '2026-09-29'::DATE" in proof and "12.3457 AS BASELINE_MONTHLY_CREDITS" in proof
    assert "COST_ARM IN ('CLUSTERING', 'SEARCH_OPT', 'MV_REFRESH')" in proof
    assert sqlglot.parse_one(proof, read="snowflake").named_selects == [
        "BASELINE_MONTHLY_CREDITS", "CREDITS_SINCE_BOOKED", "DAYS_SINCE_BOOKED", "MONTHLY_CREDITS_NOW"]
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


def test_remediation_generators_fail_closed():
    assert remediation.suspend_recluster_object("DB.S.T") == ("ALTER TABLE DB.S.T SUSPEND RECLUSTER;",
                                                               "ALTER TABLE DB.S.T RESUME RECLUSTER;")
    assert remediation.suspend_recluster_object("DB.S.MV", materialized_view=True)[0] == (
        "ALTER MATERIALIZED VIEW DB.S.MV SUSPEND RECLUSTER;")
    assert remediation.drop_search_optimization("DB.S.T") == (
        "ALTER TABLE DB.S.T DROP SEARCH OPTIMIZATION;", "ALTER TABLE DB.S.T ADD SEARCH OPTIMIZATION;  -- full rebuild")
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
    assert "WHERE NOT EXISTS (" in sql and "AND STATE = 'ESTIMATED'" in sql
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
    assert 'tier="historical"' in branch.split("insights_sql.object_reads_confirm(", 1)[1][:400]
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
    assert "st.warning(f\"The object ledger's newest day is" in branch
    assert "A fixed 90-day window: the window picker does not narrow it." in branch


def test_status_chips_cover_every_verdict():
    from app.logic.unread_maintenance import (
        VERDICT_KEEP,
        VERDICT_NO_RECENT,
        VERDICT_SHARED,
        VERDICT_UNCONFIRMED,
    )
    from app.ui.status_colors import _VERDICTS
    for v in (*ACTION_VERDICTS, VERDICT_KEEP, VERDICT_SHARED, VERDICT_NO_RECENT, VERDICT_UNCONFIRMED):
        assert v.upper() in _VERDICTS, v
