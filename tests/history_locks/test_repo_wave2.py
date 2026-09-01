"""Repo-review wave 2 (owner 2026-08-17): native ANOMALY_INSIGHTS second opinion,
QUERY_INSIGHTS feed, resource-monitor coverage, TOKENS_GRANULAR token economics —
every read probe-gated with an honest degrade (all four sources are optional /
schema-uncertain). Plus the mart-recon 'NONE' pseudo-warehouse fix."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import cortex_sql, cost_sql, insights_sql, mart_sql, ops_sql
from app.logic.wave2 import fleet_cache_hit_pct, monitor_coverage, token_economics

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------- builders ----

def test_wave2_builders_shapes():
    qi = insights_sql.query_insights_feed(7)
    assert "QUERY_INSIGHTS" in qi and "GROUP BY 1" in qi and "LIMIT 50" in qi
    na = cost_sql.native_anomaly_insights()
    assert "SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS" in na and "LIMIT 200" in na
    rm = ops_sql.show_resource_monitors_sql()
    assert rm == "SHOW RESOURCE MONITORS LIMIT 200"
    tt = cortex_sql.cortex_code_token_types(30)
    # TOKENS_GRANULAR is nested BY MODEL, so a single flatten read NULL keys and zeroed
    # everything (owner 2026-08-19). A RECURSIVE flatten + numeric-leaf filter pulls the
    # (token_type -> count) leaves keyed by F.KEY regardless of nesting.
    assert "LATERAL FLATTEN(INPUT => C.TOKENS_GRANULAR, RECURSIVE => TRUE)" in tt
    assert "F.KEY::VARCHAR) AS TOKEN_TYPE" in tt and "CORTEX_CODE_CLI_USAGE_HISTORY" in tt
    assert "IS NOT NULL" in tt        # numeric-leaf filter drops the model-object rows
    assert "DATEADD('day', -30," in tt


def test_wave2_reads_are_probe_gated_with_honest_degrades():
    ops = _src("app/ui/pages/operations.py")
    assert "query_insights_feed(7)" in ops and ops.count("probe=True") >= 2
    assert "isn't available on this account/edition" in ops
    assert "show_resource_monitors_sql()" in ops and "No monitor — uncapped" in ops
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert "native_anomaly_insights()" in spend
    assert "native ANOMALY_INSIGHTS feed isn't available" in spend
    ai = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert "cortex_code_token_types(days, bounds=bounds)" in ai   # tracks the page Window filter (v4.275)
    assert "TOKENS_GRANULAR isn't available" in ai


# ------------------------------------------------------ monitor coverage ----

def test_monitor_coverage_finds_uncapped_warehouses():
    whs = pd.DataFrame({
        "name": ["WH_A", "WH_B", "WH_C"],
        "resource_monitor": ["MONTHLY_CAP", "null", ""],
    })
    mons = pd.DataFrame({
        "name": ["MONTHLY_CAP"], "credit_quota": [1000.0], "used_credits": [250.0],
        "remaining_credits": [750.0], "frequency": ["MONTHLY"], "level": ["WAREHOUSE"],
    })
    cov = monitor_coverage(whs, mons)
    assert cov["covered"] == 1 and cov["uncovered"] == 2
    assert cov["uncovered_names"] == ["WH_B", "WH_C"]
    assert float(cov["monitors_df"]["PCT_CONSUMED"].iloc[0]) == 25.0


def test_account_level_monitor_suppresses_uncapped_alarm():
    # Review #4: a LEVEL=ACCOUNT monitor caps every warehouse; unassigned
    # warehouses are then a detail, not an "uncapped" alarm.
    whs = pd.DataFrame({"name": ["WH_A"], "resource_monitor": ["null"]})
    mons = pd.DataFrame({"name": ["GLOBAL_CAP"], "credit_quota": [5000.0],
                         "used_credits": [100.0], "level": ["ACCOUNT"]})
    cov = monitor_coverage(whs, mons)
    assert cov["account_monitor"] is True and cov["uncovered"] == 1
    ui = _src("app/ui/pages/operations.py")
    assert "account cap applies" in ui           # the KPI relabels instead of alarming


def test_missing_column_probe_degrade_is_quiet():
    # Review #3: an optional COLUMN missing on an EXISTING view (TOKENS_GRANULAR /
    # QUERY_INSIGHTS drift) must degrade like an absent object under probe=True,
    # not error-log on every render.
    from app.core.query import _classify_error
    assert _classify_error("SQL compilation error: error line 1 at position 53 "
                           "invalid identifier 'TOKENS_GRANULAR'") == "missing_column"
    assert _classify_error("Object 'X' does not exist or not authorized") == "absent"
    src = _src("app/core/query.py")
    assert '"missing_column")' in src            # in the probe suppression set


def test_native_anomaly_panel_is_toggle_gated_not_expander():
    # Review #2 (cost.py's own rule): an expander body runs every rerun and a
    # failed probe is never cached — the second-opinion read must be a toggle.
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert 'st.toggle("Second opinion' in spend
    assert 'with st.expander("Second opinion' not in spend


def test_monitor_coverage_is_safe_on_empty_or_odd_shapes():
    assert monitor_coverage(None, None)["uncovered"] == 0
    assert monitor_coverage(pd.DataFrame(), pd.DataFrame())["monitors_df"].empty
    # missing linkage column -> no false "uncapped" claims.
    cov = monitor_coverage(pd.DataFrame({"name": ["X"]}), None)
    assert cov["covered"] == 0 and cov["uncovered"] == 0


# ------------------------------------------------------- token economics ----

def test_token_economics_cache_hit_math():
    rows = pd.DataFrame({
        "USER_NAME": ["A", "A", "A", "B", "B"],
        # cache_read_input is Snowflake's actual TOKENS_GRANULAR leaf key (not cache_read).
        "TOKEN_TYPE": ["input", "cache_read_input", "output", "input", "output"],
        "TOKENS": [100.0, 300.0, 50.0, 200.0, 80.0],
    })
    econ = token_economics(rows)
    a = econ[econ["USER_NAME"] == "A"].iloc[0]
    assert a["CACHE_HIT_PCT"] == 75.0          # 300 / (300 + 100)
    b = econ[econ["USER_NAME"] == "B"].iloc[0]
    assert b["CACHE_HIT_PCT"] == 0.0           # no cache reads
    # fleet is token-weighted: 300 / (300 + 300) = 50%.
    assert fleet_cache_hit_pct(econ) == 50.0
    # sorted by total tokens desc.
    assert econ.iloc[0]["USER_NAME"] == "A"


def test_token_economics_empty_and_missing_columns():
    assert token_economics(None).empty
    assert token_economics(pd.DataFrame({"X": [1]})).empty
    assert fleet_cache_hit_pct(pd.DataFrame()) == 0.0


# ------------------------------------------------------- mart-recon fix ----

def test_recon_excludes_pseudo_and_none_warehouses():
    # Owner screenshot 2026-08-17: +93% false drift — V041 stores warehouse-less
    # rows as the STRING 'NONE', which IS NOT NULL never excluded.
    sql = mart_sql.mart_vs_live_recon()
    assert "UPPER(WAREHOUSE_NAME) NOT IN ('NONE', 'CLOUD_SERVICES_ONLY')" in sql
