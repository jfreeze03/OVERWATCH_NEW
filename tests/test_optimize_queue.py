"""v4.597 (Decision Studio Option C, slice S1): Operations > Optimize fix queue.

Locks the fix-queue builder (advisor columns spliced into an otherwise BYTE-IDENTICAL portfolio),
the pure diagnosis precedence (live > mart > heuristic > none), the idempotent Track write builder
(entity-keyed, never HIGH, confidence 0-1, observed cost never written as an estimate), the
Track-all eligibility rule, and the UI's write gating + cache-sharing wiring.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core.query import _statement_allowed
from app.data import canary, workbench_sql
from app.logic import fix_queue, query_opt
from app.logic.decision import prioritize_workloads
from app.logic.fix_queue import (
    TRACK_ALL_CAP,
    advisor_input,
    diagnose_workloads,
    track_all_eligible,
    track_fingerprints_sql,
    track_items,
    with_track_status,
)
from app.logic.formulas import safe_float

sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]
_OPT = "app/ui/pages/ops_parts/optimize_queue.py"


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Builder: workload_portfolio is byte-identical to its pre-v4.597 text; optimize_queue is that
# text plus EXACTLY the three advisor fragments (the round-13 normalize-and-compare lesson).
# --------------------------------------------------------------------------------------------

# workload_portfolio() at d175854 (v4.596.0), before the _portfolio_sql refactor — verbatim up to
# whitespace (the two empty company-clause lines carry six trailing spaces in the real bytes, which
# the sha256 pins below lock exactly).
_PORTFOLIO_DEFAULT_V4596 = """
WITH costs AS (
    SELECT p.QUERY_HASH,
           SUM(p.RUNS) AS RUNS,
           SUM(p.CREDITS_ATTRIBUTED) AS CREDITS,
           COUNT(DISTINCT p.DAY) AS ACTIVE_DAYS,
           COUNT(DISTINCT p.DATABASE_NAME) AS DATABASES,
           HLL_ESTIMATE(HLL_COMBINE(p.USERS_HLL)) AS USERS,
           MAX(p.DAY) AS LAST_SEEN
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY p
    WHERE p.DAY >= DATEADD('day', -30, CURRENT_DATE())

    GROUP BY p.QUERY_HASH
), scoped_families AS (
    -- DS #3 (V082): the family mart now carries COMPANY at row grain, so scope on
    -- the real company instead of the lossy ANY_VALUE(DATABASE_NAME) heuristic that
    -- dropped a family whose representative database wasn't in the company's set.
    SELECT DISTINCT p.QUERY_HASH, p.COMPANY
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY p
    WHERE p.DAY >= DATEADD('day', -30, CURRENT_DATE())

), families AS (
    SELECT f.QUERY_HASH,
           SUM(f.FAILS) AS FAILS,
           SUM(COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)) AS TOTAL_ELAPSED_SEC,
           ROUND(SUM(COALESCE(f.CACHE_PCT_AVG, 0) * f.RUNS)
                 / NULLIF(SUM(f.RUNS), 0) * 100, 1) AS AVG_CACHE_PCT,
           MAX(f.P95_S) AS P95_SEC,
           ANY_VALUE(f.SAMPLE_TEXT) AS QUERY_PREVIEW
    FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY f
    JOIN scoped_families s
      ON s.QUERY_HASH = f.QUERY_HASH AND s.COMPANY = f.COMPANY
    WHERE f.DAY >= DATEADD('day', -30, CURRENT_DATE())
    GROUP BY f.QUERY_HASH
)
-- FAILS stays RAW (NULL on a family-mart join miss), like AVG_CACHE_PCT/P95_SEC below:
-- decision.py tracks evidence PRESENCE from notna(FAILS), so coalescing to 0 made a blind
-- family look measured (has_behavior always true, EVIDENCE_COVERAGE floored at 0.33).
-- fillna(0) in decision.py keeps FAIL_PCT correct. (Codex #1, confirmed)
SELECT c.QUERY_HASH AS FINGERPRINT, c.RUNS, f.FAILS AS FAILS,
       ROUND(c.CREDITS, 4) AS CREDITS, c.ACTIVE_DAYS, c.DATABASES, c.USERS,
       ROUND(COALESCE(f.TOTAL_ELAPSED_SEC, 0) / 3600, 2) AS TOTAL_ELAPSED_HOURS,
       f.AVG_CACHE_PCT, f.P95_SEC, c.LAST_SEEN, f.QUERY_PREVIEW
FROM costs c
LEFT JOIN families f ON f.QUERY_HASH = c.QUERY_HASH
WHERE c.RUNS > 0 AND c.CREDITS > 0
  -- Owner finding 2026-08-17: the portfolio ranked OVERWATCH's OWN runtime
  -- ("execute streamlit ... OVERWATCH_APP()") as the #1 ACT-NOW — the monitoring
  -- tool telling you to optimize the monitoring tool. Exclude the app's own
  -- Streamlit-runtime family. COALESCE guards the LEFT-JOIN miss (Codex #1): a
  -- NULL preview must read as "not the app", i.e. stay IN the portfolio.
  AND UPPER(COALESCE(f.QUERY_PREVIEW, '')) NOT LIKE 'EXECUTE STREAMLIT%'
  AND UPPER(COALESCE(f.QUERY_PREVIEW, '')) NOT LIKE '%OVERWATCH_APP%'
ORDER BY c.CREDITS DESC
LIMIT 200
"""

# sha256 of workload_portfolio(...) at d175854: default, scoped, bounded and hostile variants.
_PORTFOLIO_SHA_V4596 = {
    (30, "ALL", 200, None):
        "e2a42316765264ce478bf63f2fd92a8380ae788b49c1b89edd802fb1504d2022",
    (7, "ALFA", 200, None):
        "e7c4293076f29fb55edef7b65f471b2104114ac8611814e055ab21dc8184f681",
    (30, "Trexis", 50, (date(2026, 8, 1), date(2026, 9, 1))):
        "1f996ecb0d6076b3d15dffe17cae9826d876cc59e29c85630f15d630cf6146e7",
    (9999, "x' OR 1=1", 99999, None):
        "ad0522f5d1d7c9a5c42b12abdc45d3a0d3b46af47eaf552c26649cd163fa427a",
}


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_workload_portfolio_is_byte_identical_to_the_pre_refactor_snapshot():
    assert _norm(workbench_sql.workload_portfolio()) == _norm(_PORTFOLIO_DEFAULT_V4596)
    assert workbench_sql.workload_portfolio().splitlines()[10:13] == [
        "    WHERE p.DAY >= DATEADD('day', -30, CURRENT_DATE())", "      ", "    GROUP BY p.QUERY_HASH"]
    for (days, company, limit, bounds), sha in _PORTFOLIO_SHA_V4596.items():
        sql = workbench_sql.workload_portfolio(days, company, limit, bounds=bounds)
        assert hashlib.sha256(sql.encode()).hexdigest() == sha, (days, company, limit, bounds)


@pytest.mark.parametrize("args", [
    (30, "ALL", 200, None), (7, "ALFA", 200, None), (1, "UNKNOWN", 10, None),
    (30, "Trexis", 50, (date(2026, 8, 1), date(2026, 9, 1))),
])
def test_optimize_queue_is_the_portfolio_plus_exactly_the_advisor_fragments(args):
    days, company, limit, bounds = args
    advisor = workbench_sql.optimize_queue(days, company, limit, bounds=bounds)
    for frag in (workbench_sql._ADVISOR_COSTS_COLS, workbench_sql._ADVISOR_FAMILY_COLS,
                 workbench_sql._ADVISOR_SELECT_COLS):
        assert advisor.count(frag) == 1
        advisor = advisor.replace(frag, "", 1)
    assert advisor == workbench_sql.workload_portfolio(days, company, limit, bounds=bounds)


def test_optimize_queue_carries_the_advisor_columns():
    cols = sqlglot.parse_one(workbench_sql.optimize_queue(), read="snowflake").named_selects
    for col in ("FINGERPRINT", "RUNS", "FAILS", "CREDITS", "AVG_CACHE_PCT", "P95_SEC", "QUERY_PREVIEW",
                "TOTAL_ELAPSED_SEC", "FAMILY_RUNS", "TOTAL_EXEC_SEC", "TOTAL_COMPILE_SEC", "GB_SCANNED",
                "WAREHOUSES", "COMPILE_RUN_PCT", "COMPILE_DOMINANT_RUN_PCT", "TOP_COMPANY",
                "TOP_DATABASE", "OW_SELF", "SCOPE_FAMILIES_TOTAL", "SCOPE_CREDITS_TOTAL"):
        assert col in cols, col


def test_advisor_aggregates_are_qualified_and_scope_totals_are_uncapped():
    frag = workbench_sql._ADVISOR_FAMILY_COLS + workbench_sql._ADVISOR_COSTS_COLS
    body = re.sub(r"--[^\n]*", "", frag)                       # comments are prose
    # every mart column inside the new aggregates is f./p.-qualified (the alias-shadow rule):
    # a bare RUNS / COMPILE_MS_AVG would resolve to a same-SELECT alias in Snowflake.
    for col in ("COMPILE_MS_AVG", "GB_SCANNED_AVG", "TOTAL_EXEC_SEC", "TOTAL_ELAPSED_SEC", "RUNS",
                "WAREHOUSES", "CREDITS_ATTRIBUTED", "DATABASE_NAME", "COMPANY"):
        bare = [m.start() for m in re.finditer(rf"(?<![.\w]){col}\b", body)
                if not body[max(0, m.start() - 3):m.start()].endswith("AS ")]
        assert not bare, f"bare {col} in the advisor aggregates"
    assert "SUM(COALESCE(f.COMPILE_MS_AVG, 0) * f.RUNS)" in frag
    # the headline totals are window aggregates over every family in scope (before the LIMIT)
    sel = workbench_sql._ADVISOR_SELECT_COLS
    assert "COUNT(*) OVER () AS SCOPE_FAMILIES_TOTAL" in sel
    assert "SUM(c.CREDITS) OVER ()" in sel


def test_optimize_queue_keeps_the_portfolio_pins_and_never_names_the_queue():
    sql = workbench_sql.optimize_queue(30, "Trexis")
    assert sql.count("p.COMPANY = 'Trexis'") == 2
    assert "JOIN scoped_families" in sql and "s.COMPANY = f.COMPANY" in sql
    assert "f.FAILS AS FAILS" in sql and "COALESCE(f.FAILS" not in sql
    # both Streamlit-runtime exclusions kept, and the own-traffic flag uses a DIFFERENT expression
    assert sql.count("UPPER(COALESCE(f.QUERY_PREVIEW, ''))") == 2
    assert "NOT LIKE 'EXECUTE STREAMLIT%'" in sql and "NOT LIKE '%OVERWATCH_APP%'" in sql
    # own traffic = SQL naming DBA_MAINT_DB.OVERWATCH OR a credit-dominant DBA_MAINT_DB context (review r1:
    # the app's own reads never name the schema in their text)
    assert "(COALESCE(CONTAINS(UPPER(f.QUERY_PREVIEW), 'DBA_MAINT_DB.OVERWATCH.'), FALSE)" in sql
    assert "OR UPPER(COALESCE(c.TOP_DATABASE, '')) = 'DBA_MAINT_DB') AS OW_SELF" in sql
    # a queue write bumps the ACTION_QUEUE domain salt: naming it would re-cold this mart read
    assert "ACTION_QUEUE" not in sql.upper()
    assert "ACCOUNT_USAGE" not in sql.upper()
    sqlglot.parse_one(sql, read="snowflake")


def test_tracked_actions_is_one_grouped_account_wide_read():
    sql = workbench_sql.tracked_actions()
    parsed = sqlglot.parse_one(sql, read="snowflake")
    assert parsed.named_selects == ["ENTITY_KEY_U", "LATEST_ACTION_ID", "OPEN_ACTION_ID", "ACTION_STATUS",
                                    "ACTION_OWNER", "OPEN_N", "DROPPED_N", "LAST_DECIDED"]
    # the newest OPEN item: MAX_BY over an open-only ordering value (NULLs skipped), never the newest overall
    assert ("MAX_BY(q.ACTION_ID, IFF(UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS'), q.CREATED_AT, NULL)) "
            "AS OPEN_ACTION_ID") in sql
    assert "DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE q" in sql
    assert "UPPER(q.SOURCE_ENTITY_TYPE) = 'QUERY_FINGERPRINT'" in sql
    assert "GROUP BY UPPER(q.SOURCE_ENTITY_KEY)" in sql
    # open rows always; closed rows only when decided inside the cooldown window (90d default)
    assert "DATEADD('day', -90, CURRENT_TIMESTAMP())" in sql
    assert "company" not in inspect.signature(workbench_sql.tracked_actions).parameters
    assert "-365," in workbench_sql.tracked_actions(lookback_days=99999)   # clamped


def test_both_new_builders_have_canaries():
    names = {name for name, _fn in canary.CANARIES}
    assert {"workbench.optimize_queue", "workbench.tracked_actions"} <= names
    for name, fn in canary.CANARIES:
        if name in ("workbench.optimize_queue", "workbench.tracked_actions"):
            sqlglot.parse_one(fn(), read="snowflake")


def test_pathology_label_is_the_live_boards_map():
    assert query_opt.pathology_label("cold_scan") == "Scan amplification"
    assert query_opt.pathology_label("sleep_polling") == "Sleep polling"
    assert query_opt.pathology_label("compile_bound") == "Compilation heavy"
    assert query_opt.pathology_label("nope") == "Other"
    assert query_opt.pathology_label("") == "Other"


# --------------------------------------------------------------------------------------------
# advisor_input + diagnosis precedence
# --------------------------------------------------------------------------------------------

def _row(fp: str, **kw) -> dict:
    base = {"FINGERPRINT": fp, "RUNS": 600, "FAILS": 0, "CREDITS": 100.0, "ACTIVE_DAYS": 30, "DATABASES": 1, "USERS": 2,
                "TOTAL_ELAPSED_HOURS": 1.0, "AVG_CACHE_PCT": 80.0, "P95_SEC": 12.0, "QUERY_PREVIEW": "SELECT 1",
                "TOTAL_ELAPSED_SEC": 6000.0, "FAMILY_RUNS": 600, "TOTAL_EXEC_SEC": 5900.0, "TOTAL_COMPILE_SEC": 60.0,
                "GB_SCANNED": 1.0, "WAREHOUSES": 1, "COMPILE_RUN_PCT": 0.0, "COMPILE_DOMINANT_RUN_PCT": 0.0,
                "TOP_COMPANY": "ALFA", "TOP_DATABASE": "DB1", "OW_SELF": False}
    base.update(kw)
    return base


def test_advisor_input_maps_per_run_and_nan_runs_fall_to_the_sentinels():
    d = advisor_input(_row("a", TOTAL_ELAPSED_SEC=4000.0, TOTAL_EXEC_SEC=1200.0, TOTAL_COMPILE_SEC=2600.0,
                           FAMILY_RUNS=1000, GB_SCANNED=3.5, AVG_CACHE_PCT=12.0,
                           QUERY_PREVIEW="SELECT x"))
    assert d["ELAPSED_SEC"] == 4.0 and d["EXECUTION_SEC"] == 1.2 and d["COMPILE_SEC"] == 2.6
    assert d["GB_SCANNED"] == 3.5 and d["CACHE_PCT"] == 12.0 and d["SAMPLE_TEXT"] == "SELECT x"
    for runs in (float("nan"), 0, None):
        d = advisor_input(_row("b", FAMILY_RUNS=runs, COMPILE_RUN_PCT=float("nan"),
                               COMPILE_DOMINANT_RUN_PCT=float("nan")))
        assert "ELAPSED_SEC" not in d and "EXECUTION_SEC" not in d and "COMPILE_SEC" not in d
        # advise reads absent/NaN through safe_float defaults: elapsed 0, execution -1, guards -1
        assert safe_float(d.get("ELAPSED_SEC")) == 0.0
        assert safe_float(d.get("EXECUTION_SEC"), -1.0) == -1.0
        assert safe_float(d.get("COMPILE_RUN_PCT"), -1.0) == -1.0
    # a NaN preview never reaches advise's text matcher as a float
    assert advisor_input(_row("c", QUERY_PREVIEW=float("nan")))["SAMPLE_TEXT"] == ""


def _diagnose(rows: list[dict], **kw) -> pd.DataFrame:
    return diagnose_workloads(prioritize_workloads(pd.DataFrame(rows), 3.68, 30), **kw)


def test_mart_grain_diagnoses_cold_scan_and_sleep_polling():
    out = _diagnose([
        _row("cold", GB_SCANNED=120.0, AVG_CACHE_PCT=10.0, TOTAL_ELAPSED_SEC=24000.0,
             TOTAL_EXEC_SEC=22800.0, TOTAL_COMPILE_SEC=240.0),
        _row("sleep", QUERY_PREVIEW="CALL SYSTEM$WAIT(10,'MINUTES')"),
    ]).set_index("FINGERPRINT")
    assert out.loc["cold", "DIAGNOSIS"] == "Scan amplification"
    assert out.loc["cold", "DIAG_SOURCE"] == "mart"
    assert out.loc["cold", "BREAKDOWN"][0][0] == "Large scan"
    assert out.loc["sleep", "DIAGNOSIS"] == "Sleep polling"
    assert out.loc["sleep", "DIAG_SOURCE"] == "mart"


def test_a_compile_storm_on_a_minority_of_days_is_not_compile_bound():
    storm = {"TOTAL_ELAPSED_SEC": 4000.0, "TOTAL_EXEC_SEC": 1200.0, "TOTAL_COMPILE_SEC": 2600.0,
                 "FAMILY_RUNS": 1000, "P95_SEC": 12.0, "AVG_CACHE_PCT": 80.0}
    minority = _diagnose([_row("s", COMPILE_RUN_PCT=0.2, **storm)]).iloc[0]
    assert minority["DIAGNOSIS"] != "Compilation heavy"
    typical = _diagnose([_row("s", COMPILE_RUN_PCT=0.8, **storm)]).iloc[0]
    assert typical["DIAGNOSIS"] == "Compilation heavy" and typical["DIAG_SOURCE"] == "mart"


def _precedence_frame(**kw) -> pd.DataFrame:
    return _diagnose([
        _row("LIVE1", GB_SCANNED=120.0),                           # mart would say cold scan
        _row("MART1", GB_SCANNED=120.0),
        _row("FAIL1", FAILS=60),                                    # 10% fail, no mart finding
        _row("BLIND", FAILS=None, AVG_CACHE_PCT=None, P95_SEC=None),
        _row("PLAIN", P95_SEC=30.0),                                # Profile and tune, clean mart
        _row("LIVECLEAN"),
    ], **kw).set_index("FINGERPRINT")


def test_diagnosis_precedence_live_then_mart_then_heuristic_then_none():
    live = pd.DataFrame([
        {"FINGERPRINT": "live1", "PATHOLOGY": "Spill (memory)", "CONFIDENCE": 74,
         "FIRST_ACTION": "size up"},
        {"FINGERPRINT": "LIVECLEAN", "PATHOLOGY": "None", "CONFIDENCE": 40, "FIRST_ACTION": "x"},
    ])
    bds = {"live1": [("Remote spill", 45, "Spilled 3 GB remotely: size up one step."),
                     ("Large scan", 20, "Read 120 GB.")]}
    out = _precedence_frame(live_scored=live, live_breakdowns=bds)
    # 1. live wins (matched case-insensitively), with its lead fix and confidence / 100
    assert out.loc["LIVE1", "DIAG_SOURCE"] == "live"
    assert out.loc["LIVE1", "DIAGNOSIS"] == "Spill (memory)"
    assert out.loc["LIVE1", "FIRST_FIX"] == "Spilled 3 GB remotely: size up one step."
    assert out.loc["LIVE1", "DIAG_CONFIDENCE"] == pytest.approx(0.74)
    # 2. mart advise when no live row
    assert out.loc["MART1", "DIAG_SOURCE"] == "mart"
    assert out.loc["MART1", "DIAGNOSIS"] == "Scan amplification"
    # 3. the portfolio heuristic
    assert out.loc["FAIL1", "DIAG_SOURCE"] == "heuristic"
    assert out.loc["FAIL1", "DIAGNOSIS"] == "Stabilize failures"
    # 4. nothing specific: the blind family is Validate evidence, the clean one Profile it
    assert out.loc["BLIND", "DIAG_SOURCE"] == "none"
    assert out.loc["BLIND", "DIAGNOSIS"] == "Validate evidence"
    assert out.loc["PLAIN", "DIAG_SOURCE"] == "none"
    assert out.loc["PLAIN", "DIAGNOSIS"] == "Profile it"
    # a live row that found nothing ("None") does not override; with the live profile run, the
    # first fix no longer points at the toggle
    assert out.loc["LIVECLEAN", "DIAG_SOURCE"] == "none"
    assert "live profile found no actionable" in out.loc["LIVECLEAN", "FIRST_FIX"]
    assert out["SPECIFIC"].to_dict() == {"LIVE1": True, "MART1": True, "FAIL1": True,
                                         "BLIND": False, "PLAIN": False, "LIVECLEAN": False}


def test_without_the_live_profile_the_none_fix_names_what_the_marts_cannot_see():
    out = _precedence_frame()
    fix = out.loc["PLAIN", "FIRST_FIX"]
    assert "spill, partition pruning, queueing and rows returned" in fix
    assert "turn on the live query profile" in fix
    assert out.loc["LIVE1", "DIAG_SOURCE"] == "mart"          # no live frame -> mart


def test_only_failure_diagnoses_are_priced_and_never_at_observed_cost():
    out = _precedence_frame()
    priced = out["PRICED_USD_MO"]
    assert priced.notna().tolist() == out["DIAGNOSIS"].eq("Stabilize failures").tolist()
    fail = out.loc["FAIL1"]
    assert fail["PRICED_USD_MO"] == pytest.approx(round(fail["IMPACT_USD_30D"] * fail["FAIL_PCT"] / 100, 2))
    assert fail["PRICED_USD_MO"] < fail["IMPACT_USD_30D"]


def test_diagnosis_does_not_rerank_the_portfolio():
    base = prioritize_workloads(pd.DataFrame([_row(f"F{i}", CREDITS=10.0 * (i + 1)) for i in range(6)]),
                                3.68, 30)
    out = diagnose_workloads(base)
    assert out["FINGERPRINT"].tolist() == base["FINGERPRINT"].tolist()
    assert out["LANE"].tolist() == base["LANE"].tolist()
    assert diagnose_workloads(None).empty
    assert set(fix_queue.diagnose_workloads(base.iloc[0:0]).columns) >= {"DIAGNOSIS", "SPECIFIC"}


def test_evidence_sentence_humanizes_durations_and_tags_own_traffic():
    text = fix_queue.evidence_sentence(prioritize_workloads(pd.DataFrame([_row(
        "e", P95_SEC=38.0, AVG_CACHE_PCT=12.0, GB_SCANNED=1.2, TOTAL_COMPILE_SEC=240.0,
        FAMILY_RUNS=600, WAREHOUSES=3, FAILS=12)]), 3.68, 30).iloc[0].to_dict())
    assert text == "P95 38s · 12% cache · 1.2 GB/run · compile 400ms · 3 warehouses · 2.0% fail"
    own = fix_queue.evidence_sentence(_row("o", OW_SELF=True, FAILS=None, P95_SEC=None,
                                           AVG_CACHE_PCT=None, GB_SCANNED=None,
                                           TOTAL_COMPILE_SEC=None, WAREHOUSES=None))
    assert own == "OVERWATCH's own traffic · No behaviour evidence in the family mart"


# --------------------------------------------------------------------------------------------
# Track: items + the idempotent INSERT
# --------------------------------------------------------------------------------------------

def _items_frame() -> pd.DataFrame:
    live = pd.DataFrame([{"FINGERPRINT": "LIVE1", "PATHOLOGY": "Spill (memory)", "CONFIDENCE": 74,
                          "FIRST_ACTION": "size up"}])
    return _precedence_frame(live_scored=live, live_breakdowns={}).reset_index()


def test_track_items_map_company_severity_confidence_and_price():
    df = _items_frame()
    df.loc[df["FINGERPRINT"] == "LIVE1", "LANE"] = "ACT NOW"
    df.loc[df["FINGERPRINT"] != "LIVE1", "LANE"] = "PLAN"
    items = {i["ENTITY_KEY"]: i for i in track_items(df, "ALL")}
    live = items["LIVE1"]
    assert live["COMPANY"] == "ALFA"                               # TOP_COMPANY under ALL
    assert live["SEVERITY"] == "MEDIUM"                            # ACT NOW, never HIGH
    assert live["CONFIDENCE"] == pytest.approx(0.74)               # live 74 / 100
    assert live["ESTIMATED_USD"] is None and live["PERIOD"] == ""  # unpriced diagnosis
    assert live["TITLE"] == "Spill (memory): LIVE1… (DB1)"
    assert "confidence is OVERWATCH's evidence score, not an authored belief" in live["DETAIL"]
    assert "Observed cost $" in live["DETAIL"] and "(measured)" in live["DETAIL"]
    fail = items["FAIL1"]
    assert fail["SEVERITY"] == "LOW"
    assert fail["PERIOD"] == "MONTHLY" and fail["ESTIMATED_USD"] == pytest.approx(
        df.set_index("FINGERPRINT").loc["FAIL1", "PRICED_USD_MO"])
    # the selected company wins over TOP_COMPANY; a blank TOP_COMPANY under ALL -> 'ALL'
    assert {i["COMPANY"] for i in track_items(df, "Trexis")} == {"Trexis"}
    blank = df.assign(TOP_COMPANY="")
    assert {i["COMPANY"] for i in track_items(blank, "ALL")} == {"ALL"}
    assert all(len(i["DETAIL"]) <= 1000 and len(i["TITLE"]) <= 300 for i in items.values())


def test_track_items_dedupe_entity_keys_and_keep_the_disclosure_under_the_cap():
    df = _items_frame()
    dup = pd.concat([df.head(1), df.head(1).assign(FINGERPRINT=df.iloc[0]["FINGERPRINT"].lower())])
    assert len(track_items(dup, "ALL")) == 1
    long = df.head(1).assign(FIRST_FIX="x" * 5000)
    item = track_items(long, "ALL")[0]
    assert len(item["DETAIL"]) <= 1000
    assert item["DETAIL"].endswith("confidence is OVERWATCH's evidence score, not an authored belief.")


def _track_sql(bulk: bool, df: pd.DataFrame | None = None, actor: str = "'JOE'") -> str:
    return track_fingerprints_sql(track_items(_items_frame() if df is None else df, "ALL"),
                                  actor_sql=actor, bulk=bulk)


def test_track_sql_is_one_allowed_parseable_entity_keyed_statement():
    for bulk in (True, False):
        sql = _track_sql(bulk)
        assert _statement_allowed(sql) == (True, "")
        parsed = sqlglot.parse(sql, read="snowflake")
        assert len(parsed) == 1 and parsed[0].key == "insert"
        assert sql.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE")
        # keyed on the ENTITY + open status — never the title or company
        assert "UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY)" in sql
        assert "UPPER(q.SOURCE_ENTITY_TYPE) = 'QUERY_FINGERPRINT'" in sql
        assert "UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS')" in sql
        assert "q.TITLE" not in sql and "q.COMPANY" not in sql
        assert "FROM (VALUES" in sql and ") AS v (COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, USD, PER)" in sql
        assert "v.CONF::FLOAT" in sql and "v.USD::NUMBER(18,2)" in sql and "NULLIF(v.PER, '')" in sql
        assert "'UNASSIGNED', 'OPEN', 'Operations > Optimize'" in sql
        assert "NULLIF(v.PER, ''), 'JOE'" in sql                       # UPDATED_BY = actor_sql
    assert track_fingerprints_sql([], actor_sql="CURRENT_USER()", bulk=True) == ""


def test_only_bulk_track_carries_the_dismissed_cooldown():
    bulk, single = _track_sql(True), _track_sql(False)
    assert "UPPER(q.STATUS) = 'DROPPED'" in bulk
    assert "DATEADD('day', -90, CURRENT_TIMESTAMP())" in bulk
    assert "DROPPED" not in single                                       # a human may re-track


def test_track_sql_never_writes_high_severity_or_observed_cost():
    df = _items_frame()
    df["LANE"] = "ACT NOW"
    items = track_items(df, "ALL")
    sql = track_fingerprints_sql(items, actor_sql="'JOE'", bulk=True)
    assert "'HIGH'" not in sql and "'CRITICAL'" not in sql
    assert "IMPACT_USD_30D" not in sql
    by_fp = df.set_index("FINGERPRINT")
    for item in items:
        row = by_fp.loc[item["ENTITY_KEY"]]
        # the only dollar figure ever written is the priced failed-run share, never observed cost
        assert item["ESTIMATED_USD"] is None or item["ESTIMATED_USD"] == pytest.approx(row["PRICED_USD_MO"])
        assert f", {round(float(row['IMPACT_USD_30D']), 2)!r}, 'MONTHLY')" not in sql
    forged = [{"ENTITY_KEY": "k", "SEVERITY": "CRITICAL", "TITLE": "t", "DETAIL": "d",
               "COMPANY": "ALFA", "CONFIDENCE": 62, "ESTIMATED_USD": None, "PERIOD": ""}]
    forged_sql = track_fingerprints_sql(forged, actor_sql="CURRENT_USER()", bulk=False)
    assert "'CRITICAL'" not in forged_sql and "'LOW'" in forged_sql       # coerced, never escalated
    assert ", 1.0, NULL, '')" in forged_sql                              # confidence clamped to [0, 1]


def _values_rows(sql: str) -> list[str]:
    block = sql.split("FROM (VALUES", 1)[1].split(") AS v (", 1)[0]
    return [ln.strip().rstrip(",") for ln in block.strip().splitlines() if ln.strip()]


def test_estimate_and_period_are_set_only_when_priced():
    df = _items_frame().set_index("FINGERPRINT")
    for fp, row in df.iterrows():
        rows = _values_rows(track_fingerprints_sql(track_items(df.loc[[fp]].reset_index(), "ALL"),
                                                   actor_sql="CURRENT_USER()", bulk=False))
        assert len(rows) == 1
        conf = safe_float(row["DIAG_CONFIDENCE"])
        assert 0.0 <= conf <= 1.0
        if row["DIAGNOSIS"] == "Stabilize failures":
            assert rows[0].endswith(f"{round(row['PRICED_USD_MO'], 2)!r}, 'MONTHLY')")
        else:
            assert rows[0].endswith("NULL, '')"), rows[0]


def test_hostile_text_stays_inside_literals():
    evil = "x'); DROP TABLE ACTION_QUEUE; --\\"
    df = _items_frame().head(1).assign(FINGERPRINT=evil, TOP_DATABASE=evil, FIRST_FIX=evil,
                                       TOP_COMPANY=evil)
    sql = _track_sql(False, df, actor="CURRENT_USER()")
    assert _statement_allowed(sql) == (True, "")
    parsed = sqlglot.parse(sql, read="snowflake")
    assert len(parsed) == 1 and parsed[0].key == "insert"
    residue = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)
    assert "DROP" not in residue.upper()


# --------------------------------------------------------------------------------------------
# Track all ACT NOW eligibility + track status
# --------------------------------------------------------------------------------------------

def _queue(n: int = 40) -> pd.DataFrame:
    rows = [{"FINGERPRINT": f"FP{i:02d}", "LANE": "ACT NOW", "SPECIFIC": True, "OW_SELF": False,
             "PRIORITY_SCORE": float(i)} for i in range(n)]
    return pd.DataFrame(rows)


def _tracked(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([{"ENTITY_KEY_U": k, "LATEST_ACTION_ID": f"id-{k}", "ACTION_STATUS": s,
                          "OPEN_ACTION_ID": f"open-{k}" if o else None,
                          "ACTION_OWNER": "UNASSIGNED", "OPEN_N": o, "DROPPED_N": d,
                          "LAST_DECIDED": None} for k, s, o, d in rows])


def test_track_all_takes_specific_untracked_act_now_by_priority_capped_at_25():
    q = _queue(40)
    q.loc[q["FINGERPRINT"] == "FP39", "OW_SELF"] = True              # own traffic: never bulk
    q.loc[q["FINGERPRINT"] == "FP38", "SPECIFIC"] = False            # nothing specific
    q.loc[q["FINGERPRINT"] == "FP37", "LANE"] = "PLAN"
    tracked = _tracked([("FP36", "OPEN", 1, 0),                      # already open
                        ("fp35", "DROPPED", 0, 1),                   # dismissed in 90d (case-insens.)
                        ("FP34", "DONE", 0, 0),                      # done: re-trackable
                        ("FP33", "DONE", 1, 0),                      # newer DONE hides an open one
                        ("FP32", "DONE", 0, 1)])                     # newer DONE, dismissed earlier
    picked = track_all_eligible(q, tracked)
    keys = picked["FINGERPRINT"].tolist()
    assert len(keys) == TRACK_ALL_CAP == 25
    assert keys[0] == "FP34" and keys == sorted(keys, reverse=True)   # priority order
    assert keys[-1] == "FP08"                                         # the cap cuts the lowest
    for excluded in ("FP39", "FP38", "FP37", "FP36", "FP35", "FP33", "FP32"):
        assert excluded not in keys
    small = track_all_eligible(_queue(3), None)
    assert small["FINGERPRINT"].tolist() == ["FP02", "FP01", "FP00"]
    assert track_all_eligible(q.iloc[0:0], tracked).empty
    assert track_all_eligible(None, tracked).empty


def test_track_status_reads_open_dismissed_done_and_untracked():
    q = _queue(5)
    out = with_track_status(q, _tracked([("FP00", "OPEN", 1, 0), ("FP01", "DROPPED", 0, 1),
                                         ("FP02", "DONE", 0, 0), ("FP03", "DONE", 1, 0)]))
    assert out["TRACK_STATUS"].tolist() == ["Tracked (open)", "Dismissed", "Done",
                                            "Tracked (open)", "Untracked"]
    # an open family links to its newest OPEN item (FP03: a newer DONE must not hide it); a closed family
    # keeps its latest (closed) id for the caption (review r1)
    assert out["TRACKED_ACTION_ID"].tolist() == ["open-FP00", "id-FP01", "id-FP02", "open-FP03", ""]
    assert with_track_status(q, None)["TRACK_STATUS"].eq("Untracked").all()


# --------------------------------------------------------------------------------------------
# UI source locks: gating, latches, cache sharing, honesty copy, wiring
# --------------------------------------------------------------------------------------------

def test_track_blocks_are_operator_gated_and_latched_before_rerun():
    src = _src(_OPT)
    gates = [m.start() for m in re.finditer(r"write_gate_open\(", src)]
    assert len(gates) == 2 and len(re.findall(r"stamp_write\(", src)) == 2
    for g in gates:
        head = src[max(0, g - 220):g]
        assert "is_operator and st.button(" in head, "a Track block is not operator-gated"
    for key in ('"opt_track_all"', 'f"opt_track:{fp[:16]}"'):
        assert src.count(f"write_gate_open({key})") == 1 and src.count(f"stamp_write({key}, ok)") == 1
        stamp = src.index(f"stamp_write({key}, ok)")
        rerun = src.index("st.rerun()", stamp)
        assert stamp < rerun and src.index(f"write_gate_open({key})") < stamp
    # a row click never writes: the Track button lives only in the master_detail detail pane
    detail = src.split("def _render_detail(", 1)[1]
    assert 'st.button("Track", key=f"opt_track_btn:{fp[:16]}")' in detail
    assert 'master_detail(\n        portfolio, key="ops_optimize", id_col="FINGERPRINT",' in src
    assert "on_select=" not in src.split("def _list(", 1)[1].split("def _detail(", 1)[0]
    # the SQL preview is shown before each write button
    assert src.count('with st.expander("Statement"):') == 2
    assert 'st.caption("Read-only — an operator can track these into Action Center.")' in src


def test_live_toggle_shares_the_queries_board_cache_entry():
    ops, opt = _src("app/ui/pages/operations.py"), _src(_OPT)
    call = ("ops_sql.query_opportunity_fingerprints( days, company, wh_filter, user_filter, database, "
            "schema_contains, bounds=bounds), page=_PAGE, key=f\"q_opp_{company}_{days}{_lm}\", "
            "tier=\"historical\",")
    assert call in _norm(ops) and call in _norm(opt)
    assert 'key="ops_opt_live"' in opt and 'st.session_state.get("ops_opt_live", False)' in opt
    assert 'source="QUERY_HISTORY (live optimization profile)"' in opt
    assert "ACCOUNT_USAGE" not in opt                                   # budget 0


def test_the_queue_reads_batch_once_and_divide_by_the_real_span():
    opt = _src(_OPT)
    assert "run_batch_mixed(specs, page=_PAGE)" in opt and ") or {}" not in opt
    assert "workbench_sql.optimize_queue(days, company, _QUEUE_CAP, bounds=bounds)" in opt
    assert "workbench_sql.tracked_actions()" in opt
    assert "workbench_sql.watchlist(_viewer)" in opt
    assert opt.count('"tier": "recent"') == 2 and '"tier": "historical"' in opt
    assert 'f"ops_opt_queue_{company}_{days}{_lm}"' in opt
    # W12: Current month / Current year pass a day OFFSET; divide by the bounds' day span
    assert "span = (bounds[1] - bounds[0]).days if bounds is not None else days" in opt
    assert "prioritize_workloads(result.df, rate, span)" in opt
    # headline cost is the uncapped SQL scope total, not a sum over the capped frame
    assert 'result.df["SCOPE_CREDITS_TOTAL"].iloc[0]' in opt


def test_the_queue_keeps_the_portfolio_honesty_copy():
    opt = _src(_OPT)
    for phrase in ("evidence-weighted heuristics", "not promised savings", "ACT NOW",
                   "confidence < 0.5", "are tagged and never bulk-tracked", "run mainly in the DBA_MAINT_DB",
                   "NOT statistical confidence", "query families by measured credits",
                   "len(portfolio) >= _QUEUE_CAP", 'read_model_caption("workload_portfolio")',
                   'mark_watched(portfolio, _wl, "QUERY_FINGERPRINT", "FINGERPRINT")',
                   '["_LR", "WATCHED", "PRIORITY_SCORE"]', '"label": "Watching"',
                   'validate = portfolio[portfolio["LANE"].eq("VALIDATE")]',
                   'decision_col="DIAGNOSIS"', 'confidence_label="Confidence (evidence)"',
                   'stash_section_count(_PAGE, "Optimize", len(act_now), dims=("company", "days"))'):
        assert phrase in opt, phrase
    assert opt.index("exception_summary(") < opt.index("kpi_row([")
    assert 'section_header("Fix queue", alarm_health(' in opt


def test_operations_wires_optimize_after_warehouses_with_a_contract():
    ops = _src("app/ui/pages/operations.py")
    assert ('["Queries", "Tasks", "Warehouses", "Optimize", "Change impact",\n'
            '         "Pipeline SLA", "Release compare", "Emergency"], key="ops_section"') in ops
    assert "from app.ui.pages.ops_parts.optimize_queue import render_optimize" in ops
    contract = ops.split('"Optimize": {', 1)[1].split("},", 1)[0]
    assert '"applies": ("company", "days")' in contract
    assert '"partial": ("warehouse_contains", "user_contains", "database", "schema_contains")' in contract
    dispatch = ops.split('section_filter_contract(f, **_contracts[section])', 1)[1]
    assert dispatch.index('elif section == "Optimize":') < dispatch.index("else:")
    assert "render_optimize(" in dispatch.split('elif section == "Optimize":', 1)[1].split("elif", 1)[0]
    from app.logic.navigate import PAGE_SECTION_LABELS
    labels = PAGE_SECTION_LABELS["Operations"]
    assert labels.index("Optimize") == labels.index("Warehouses") + 1


def test_fix_queue_constants_match_the_owner_decisions():
    assert fix_queue.TRACK_ALL_CAP == 25
    assert fix_queue.TRACK_COOLDOWN_DAYS == 90
    assert fix_queue.TRACK_SOURCE == "Operations > Optimize"
    assert fix_queue.TRACK_ENTITY_TYPE == "QUERY_FINGERPRINT"


def test_none_first_fix_only_claims_the_live_profile_for_families_it_saw():
    """review r1: a family outside the live profile's filters/scope must not read 'the live profile found no
    actionable inefficiency' -- only a family the live read returned (incl. a clean 'None' row) may."""
    from app.logic.fix_queue import diagnose_workloads
    base = {"LANE": "PLAN", "NEXT_MOVE": "Profile it", "CONFIDENCE": 0.2, "IMPACT_USD_30D": 10.0,
            "FAIL_PCT": 0.0, "PRIORITY_SCORE": 1.0}
    port = pd.DataFrame([{**base, "FINGERPRINT": "SEEN"}, {**base, "FINGERPRINT": "UNSEEN"}])
    live = pd.DataFrame([{"FINGERPRINT": "seen", "PATHOLOGY": "None", "CONFIDENCE": 50, "FIRST_ACTION": ""}])
    out = diagnose_workloads(port, live_scored=live, live_breakdowns={}).set_index("FINGERPRINT")
    assert "found no actionable inefficiency" in out.loc["SEEN", "FIRST_FIX"]
    assert "did not cover this family" in out.loc["UNSEEN", "FIRST_FIX"]
    assert "found no actionable" not in out.loc["UNSEEN", "FIRST_FIX"]
    off = diagnose_workloads(port).set_index("FINGERPRINT")          # live toggle off
    assert "turn on the live query profile" in off.loc["UNSEEN", "FIRST_FIX"]


def test_open_in_action_center_is_offered_only_for_an_open_item():
    src = (_ROOT / "app" / "ui" / "pages" / "ops_parts" / "optimize_queue.py").read_text(encoding="utf-8")
    assert "if (action_id and status == _OPEN_STATUS and _cr_ok" in src
    assert "Include completed work" in src
