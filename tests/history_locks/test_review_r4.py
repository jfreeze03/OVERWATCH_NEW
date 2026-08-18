"""Locks for bug + design review round 4 (v4.101).

Confirmed by a find -> adversarial-verify -> synthesize workflow, then implemented:
bugs (credits cent-rounding, contract-runway idle days, neutral-delta contrast,
grouped-nav desync, telemetry transient-error cascade) and design (KPI orphan,
severity-color collision, dead float chips, boss-chart color stability, section
headings, change-impact table, registry partial-day contradiction).
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# BUG 1 — credits_to_usd: sub-cent precision + no round-then-sum zeroing
# ---------------------------------------------------------------------------
def test_bug1_credits_to_usd_full_precision_option():
    from app.logic.formulas import credits_to_usd
    # default still rounds to cents (display)
    assert credits_to_usd(1.0, 3.68) == 3.68
    # sub-cent value survives at full precision instead of rounding to $0.00
    assert credits_to_usd(0.001, 3.68, round_cents=False) > 0.0
    assert credits_to_usd(0.001, 3.68) == 0.0        # the old cent-rounded behavior


def test_bug1_pipeline_series_summed_at_full_precision():
    import pandas as pd

    from app.logic.graphs import enrich_graph_daily, pipeline_summary
    # a pipeline at ~0.003 credits/day over many days must NOT read $0.00 (each day
    # rounded to cents would). Give many small-credit days for one pipeline.
    n = 60
    df = pd.DataFrame({
        "DAY": pd.date_range("2026-05-01", periods=n, freq="D"),
        "PIPELINE": ["p"] * n, "DATABASE_NAME": ["D"] * n, "SCHEMA_NAME": ["S"] * n,
        "WH_CREDITS": [0.003] * n, "GRAPH_RUNS": [1] * n, "TASK_RUNS": [1] * n,
        "RUNS_WITH_FAILURES": [0] * n, "P95_WALL_SEC": [1.0] * n,
    })
    enriched = enrich_graph_daily(df, 3.68)
    rolled = pipeline_summary(enriched)
    assert float(rolled.iloc[0]["USD"]) > 0.0        # ~0.003*3.68*60 ≈ $0.66, not $0.00


# ---------------------------------------------------------------------------
# BUG 4 — neutral KPI delta routes through the WCAG-AA muted token
# ---------------------------------------------------------------------------
def test_bug4_neutral_delta_uses_muted_token():
    comp = _src("app/ui/components.py")
    block = comp.split("def _delta_html", 1)[1].split("\ndef ", 1)[0]
    assert 'col, arrow = "var(--ow-ink-mute)"' in block   # the token...
    assert '"#6b7a90"' not in block                       # ...not the retired hex literal


# ---------------------------------------------------------------------------
# BUG 5 — grouped-nav highlight seeds off the authoritative page, not ?page=
# ---------------------------------------------------------------------------
def test_bug5_nav_highlight_from_authoritative_page():
    m = _src("app/main.py")
    # a ?page= deep link overrides _ow_page ONLY when it changes (else the stale
    # query param would re-highlight the old group after a cross-group click)
    assert '_req != st.session_state.get("_ow_req_seen")' in m
    assert "idx = members.index(current) if current in members else None" in m


# ---------------------------------------------------------------------------
# BUG 6 — telemetry downgrade needs TWO consecutive failures (no transient cascade)
# ---------------------------------------------------------------------------
def test_bug6_telemetry_downgrade_needs_two_failures():
    q = _src("app/core/query.py")
    block = q.split("def _flush_group", 1)[1].split("\ndef ", 1)[0]
    assert "if n >= 2:" in block                       # two consecutive failures
    assert '_ow_qtel_fail:' in block


# ---------------------------------------------------------------------------
# DESIGN 1 — KPI rows rebalance so the 5th (flagship) card is never orphaned
# ---------------------------------------------------------------------------
def test_design1_kpi_rows_rebalance():
    comp = _src("app/ui/components.py")
    body = comp.split("def kpi_row", 1)[1].split("\ndef ", 1)[0]
    assert "divmod(len(items), rows)" in body and "st.columns(len(chunk))" in body


# ---------------------------------------------------------------------------
# DESIGN 2 — HIGH is a distinct orange from CRITICAL red in status tables
# ---------------------------------------------------------------------------
def test_design2_high_distinct_from_critical():
    from app.ui.status_colors import status_css
    crit = status_css("SEVERITY", "CRITICAL")
    high = status_css("SEVERITY", "HIGH")
    med = status_css("SEVERITY", "MEDIUM")
    assert crit and high and med
    assert crit != high            # no longer pixel-identical
    assert high != med             # and distinct from MEDIUM amber


# ---------------------------------------------------------------------------
# DESIGN 3 — trust chips ride a wrapping right-aligned group (no dead float)
# ---------------------------------------------------------------------------
def test_design3_chip_group_no_dead_float():
    theme = _src("app/theme.py")
    comp = _src("app/ui/components.py")
    assert ".ow-card__chips" in theme and "margin-left:auto" in theme
    assert "float:right" not in theme.split(".ow-src-badge {", 1)[1].split("}", 1)[0]
    assert 'class="ow-card__chips"' in comp


# ---------------------------------------------------------------------------
# DESIGN 4 — boss chart colors by stable entity identity
# ---------------------------------------------------------------------------
def test_design4_boss_chart_stable_color():
    ch = _src("app/ui/charts.py")
    block = ch.split("def monthly_stacked_usd", 1)[1].split("\ndef ", 1)[0]
    assert "_stable_color(category_col" in block       # entity-stable, not set-order
    assert 'alt.Order("_RANK:Q")' in block             # size-ordered stacking preserved


# ---------------------------------------------------------------------------
# DESIGN 5 — change-impact table drops the duplicate id column + labels/formats
# ---------------------------------------------------------------------------
def test_design5_change_impact_table_cleaned():
    ops = _src("app/ui/pages/operations.py")
    block = ops.split('key="chg_sel"', 1)[0].rsplit("show_cols =", 1)[1]
    assert '"CHANGED_BY"' not in block                 # the raw duplicate id is gone
    assert '"VERDICT_DETAIL"' not in block             # moved to the row drill
    assert '"D_CALLS"' in block                        # collapsed to deltas


# ---------------------------------------------------------------------------
# BUG 2 — allocated share is multiplied by a pool over its OWN window
# ---------------------------------------------------------------------------
def test_bug2_alloc_pool_matches_live_share_window():
    from app.config import MAX_LIVE_WINDOW_DAYS
    from app.data.common import resolve_effective_window
    # the live-share fallback (cost_sql.allocated_attribution) and the r4 matched pool
    # clamp to the SAME window, so a >90d allocation multiplies a 90d share by a 90d
    # pool instead of the full 182d pool (which zeroed older-half entities).
    share_eff, _ = resolve_effective_window(365, "START_TIME", max_days=MAX_LIVE_WINDOW_DAYS)
    pool_eff, _ = resolve_effective_window(365, max_days=MAX_LIVE_WINDOW_DAYS)
    assert share_eff == pool_eff and share_eff <= MAX_LIVE_WINDOW_DAYS
    sp = _src("app/ui/pages/cost_parts/spend.py")
    # a live-served dim uses the matched pool; the 90-day live-scan cap is UNCHANGED
    assert "_pool = _alloc_pool(res.source)" in sp
    assert "* _pool" in sp and "_pool - _top_usd" in sp   # codex#32: _other from the shown top-10
    assert 'resolve_effective_window(days, max_days=MAX_LIVE_WINDOW_DAYS)' in sp
    live = _src("app/data/cost_sql.py")   # the guardrail is preserved, not lifted
    assert 'resolve_effective_window(days, "START_TIME", max_days=90)' in live


# ---------------------------------------------------------------------------
# DESIGN 7 — registry validate() catches a window/partial-day contradiction
# ---------------------------------------------------------------------------
def test_design7_registry_partial_day_consistency():
    from app.logic import metric_registry as mr
    # the live registry is clean...
    assert mr.validate() == []
    # ...and a contradictory metric is caught (rolling-daily includes today)
    bad = mr.Metric("x", "X", mr.METERED, "g", "s", mr.UTC, "l", "v",
                    window="rolling-daily", partial_day="excluded", unit="USD",
                    required_sources=("T",), coverage="c")
    orig = mr.METRICS
    try:
        mr.METRICS = (*orig, bad)
        assert any("partial_day" in p for p in mr.validate())
    finally:
        mr.METRICS = orig
