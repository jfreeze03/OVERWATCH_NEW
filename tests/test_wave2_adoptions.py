"""Locks for WAVE 2 (v4.12.0) — the V027 marts go fact-first in the panels.

Ten surfaces adopt: idle advisor + sizing + repeat queries (optimize),
compile-heavy + allocated attribution (spend), role share (chargeback),
task graphs + schema summary (operations), schema pulse + 48h timeline
(control room), AI by model (unit costs), posture trend (security). Every
adoption keeps its live builder as a labeled fallback via run_mart_first,
and each reader matches its live builder's output contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]

def _page(rel: str) -> str:
    return (_ROOT / "app" / "ui" / "pages" / rel).read_text(encoding="utf-8")

_OPT = _page("cost_parts/optimize.py")
_SPEND = _page("cost_parts/spend.py")
_CB = _page("cost_parts/ai_chargeback.py")
_OPS = _page("operations.py")
_CR = _page("control_room.py")
_UC = _page("cost_parts/unit_costs.py")
_SEC = _page("security.py")

# ---------------------------------------------------------------------------
# The helper: one fact-first pattern, everywhere
# ---------------------------------------------------------------------------

def test_run_mart_first_exists_and_is_the_adoption_vehicle():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "def run_mart_first(" in comp
    assert 'run(mart_sql, page=page, key=f"{key}_fact"' in comp   # distinct cache key
    total = sum(p.count("run_mart_first(") for p in (_OPT, _SPEND, _CB, _OPS))
    assert total >= 6, total                                      # the swap sites


# ---------------------------------------------------------------------------
# Reader contracts match the live builders they replace
# ---------------------------------------------------------------------------

def test_eff_readers_match_live_contracts():
    idle = mart27_sql.eff_idle_analysis(30, "ALFA")
    for col in ("METERED_HOURS", "IDLE_HOURS", "TOTAL_CREDITS", "IDLE_CREDITS"):
        assert col in idle, col
    assert "COMPANY = 'ALFA'" in idle
    prof = mart27_sql.eff_sizing_profile(30, "ALFA")
    for col in ("CREDITS_TOTAL", "IDLE_PCT", "QUERY_COUNT", "P95_ELAPSED_SEC",
                "QUEUED_SEC", "SPILL_REMOTE_GB"):
        assert col in prof, col


def test_family_readers_match_live_contracts():
    comp = mart27_sql.family_compile_heavy(30, "ALFA")
    for col in ("QUERY_PARAMETERIZED_HASH", "AVG_COMPILE_S", "AVG_TOTAL_S",
                "COMPILE_PCT", "TOTAL_COMPILE_HOURS"):
        assert col in comp, col
    rq = mart27_sql.family_repeat_fingerprints(30, "ALFA", 10,
                                               database="ALFA_EDW_PRD", schema_contains="rpt")
    for col in ("FINGERPRINT", "TOTAL_ELAPSED_HOURS", "AVG_ELAPSED_SEC",
                "TOTAL_TB_SCANNED", "AVG_CACHE_PCT", "QUERY_PREVIEW", "LAST_RUN"):
        assert col in rq, col
    assert "UPPER(f.DATABASE_NAME) = 'ALFA_EDW_PRD'" in rq        # filter parity (qualified)


def test_role_share_keeps_both_leak_guards():
    sql = mart27_sql.role_share(7, "ALFA")
    assert "COMPANY = 'ALFA'" in sql                              # fact company column
    # V044 (#18): ALFA role arm is positive evidence, not not-Trexis
    assert "LIKE '%ALFA%'" in sql and "SNOW_ACCOUNTADMINS" in sql
    # v4.34.1 attribution law: the share is computed over the whole
    # warehouse partition in a CTE; role visibility filters display rows
    # after (law locks live in test_r18_batch1).
    assert "RATIO_TO_REPORT(ELAPSED_SEC) OVER (PARTITION BY WAREHOUSE_NAME)" in sql
    assert "LIKE '%TRXS%'" in mart27_sql.role_share(7, "Trexis")


def test_alloc_reader_contract_and_bounds():
    sql = mart27_sql.alloc_attribution(30, "USER", "ALFA")
    for col in ("ELAPSED_SEC", "ELAPSED_SHARE", "ALLOC_CREDITS"):
        assert col in sql, col
    with pytest.raises(ValueError):
        mart27_sql.alloc_attribution(30, "PLANET")


def test_schema_summary_matches_query_window_contract():
    sql = mart27_sql.schema_window_summary(1, "ALFA", "ALFA_EDW_PRD", "stage")
    for col in ("QUERY_COUNT", "FAILED_COUNT", "P95_ELAPSED_SEC",
                "QUEUED_SEC", "SPILL_REMOTE_GB"):
        assert col in sql, col
    assert "SCHEMA_NAME" in sql and "UPPER(DATABASE_NAME) = 'ALFA_EDW_PRD'" in sql


def test_ai_and_graph_and_timeline_contracts():
    ai = mart27_sql.ai_costs_by_model(30)
    for col in ("FUNCTION_NAME", "MODEL_NAME", "REQUESTS", "TOKENS",
                "CREDITS", "CREDITS_PER_1M_TOKENS"):
        assert col in ai, col
    tg = mart27_sql.task_graphs(30, "ALFA", "ALFA_EDW_PRD", "rpt")
    assert "UPPER(DATABASE_NAME) = 'ALFA_EDW_PRD'" in tg
    assert "SCHEMA_NAME" in tg                                     # contains filter wired
    tl = mart27_sql.incident_timeline(48, "ALFA")
    assert "EVENT_TS AS AT" in tl and "KIND AS EVENT_TYPE" in tl and "TITLE AS LABEL" in tl
    assert "(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')" in tl    # account-level rows kept


# ---------------------------------------------------------------------------
# Page wiring: mart-first with the live builder retained as fallback
# ---------------------------------------------------------------------------

def test_optimize_adopts_eff_and_family_marts():
    assert "mart27_sql.eff_idle_analysis" in _OPT
    assert "insights_sql.idle_warehouse_analysis" in _OPT          # live fallback kept
    assert "mart27_sql.eff_sizing_profile" in _OPT
    assert "insights_sql.warehouse_sizing_profile" in _OPT
    assert "mart27_sql.family_repeat_fingerprints" in _OPT
    assert "insights_sql.repeat_query_fingerprints" in _OPT


def test_spend_adopts_family_and_allocation_marts():
    assert "mart27_sql.family_compile_heavy" in _SPEND
    assert "cost_sql.compile_heavy_families" in _SPEND
    assert "mart27_sql.alloc_attribution(" not in _SPEND   # P0-1: owner-scoped mart retired from spend
    assert "cost_sql.allocated_attribution" in _SPEND
    assert "if schema_contains:" in _SPEND                         # no mart carries schema grain
    assert "company, database)" in _SPEND                           # P0-1: unfiltered + db-filtered both -> xdim (warehouse-scoped)
    assert "mart27_sql.alloc_xdim_attribution" in _SPEND
    # v4.33.1: ONE dollarization formula on every path — global share x the
    # window total the caption states. The mart credits x rate branch used a
    # different window and included idle (SYSTEM alone exceeded the caption).
    assert 'alloc["ELAPSED_SHARE"].map(safe_float) * window_usd' in _SPEND
    assert 'alloc["ALLOC_CREDITS"].map(safe_float) * rate' not in _SPEND


def test_chargeback_role_share_goes_mart_first():
    assert "mart27_sql.role_share" in _CB
    assert "chargeback_sql.role_share_within_warehouse" in _CB


def test_operations_adopts_graph_and_schema_marts():
    assert "mart27_sql.task_graphs" in _OPS
    assert "graph_sql.graph_daily_costs" in _OPS
    assert "mart27_sql.schema_window_summary" in _OPS
    assert "elif not wh_filter and not user_filter:" in _OPS       # schema fact has no wh/user dims


def test_control_room_adopts_schema_pulse_and_48h_timeline():
    assert "mart27_sql.schema_window_summary" in _CR
    assert "mart27_sql.incident_timeline" in _CR
    assert "mart_sql.incident_timeline" in _CR                     # 7d + fallback stays live
    assert 'tl_win.startswith("48h")' in _CR


def test_unit_costs_ai_goes_mart_first_before_kpis():
    i_mart = _UC.find("mart27_sql.ai_costs_by_model")
    i_kpi = _UC.find("kpis = []")
    assert 0 < i_mart < i_kpi                                      # KPI and panel share the source
    assert "cortex_source_costs" in _UC                            # code-views fallback survives


def test_security_gains_posture_trend():
    assert "def _posture_trend_panel" in _SEC
    assert "mart27_sql.security_posture(90)" in _SEC
    assert "EXPIRING_CRED_10D" in _SEC                             # default metric follows V028
    assert "_posture_trend_panel(_post90)" in _SEC.split("def render", 1)[1]  # shares the header's 90d read (r14 #18)


def test_new_readers_are_canaried():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("eff_idle_analysis", "eff_sizing_profile", "family_compile_heavy",
                 "family_repeat_fingerprints", "role_share", "alloc_attribution",
                 "schema_window_summary", "ai_costs_by_model"):
        assert f"mart27_sql.{name}" in canary, name
