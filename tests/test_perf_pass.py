"""Locks for the v4.8.2 perf pass (Codex-informed, telemetry-verified).

Pins: one idle scan (not two under different tiers), fact-first Control Room
(pulse + movers), tier-grouped batching on Overview/day-replay, the on-demand
jump box, the attribution-CTE prunes, and recent canary anchors.
"""

from __future__ import annotations

from pathlib import Path

from app.data import graph_sql, insights_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# #6 — one idle scan
# ---------------------------------------------------------------------------

def test_idle_scan_runs_once_per_hour_not_twice():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    calls = [chunk.split(")", 1)[0] for chunk in src.split("idle_warehouse_analysis(")[1:]]
    blocks = src.split("idle_warehouse_analysis")
    # both call sites must use the SAME tier so the cache dedupes them
    tiers = [b.split('tier="', 1)[1].split('"', 1)[0] for b in blocks[1:] if 'tier="' in b]
    assert len(calls) == 2 and tiers == ["historical", "historical"], (calls, tiers)


# ---------------------------------------------------------------------------
# #5 — fact-backed movers
# ---------------------------------------------------------------------------

def test_fact_window_vs_prior_shape():
    sql = mart_sql.fact_warehouse_window_vs_prior(7, "ALFA")
    assert "FACT_WAREHOUSE_DAILY" in sql
    assert "CREDITS_CURRENT" in sql and "CREDITS_PRIOR" in sql   # same contract as live
    assert "COMPANY = 'ALFA'" in sql
    assert "-14," in sql                                          # 2x window for the prior period
    assert "COMPANY = '" not in mart_sql.fact_warehouse_window_vs_prior(7, "ALL")


def test_control_room_is_fact_first_with_live_fallback():
    src = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    assert "fact_warehouse_window_vs_prior" in src
    assert "cost_sql.warehouse_window_vs_prior" in src            # live fallback kept
    assert "fact_query_window_summary" in src                     # #4: pulse fact-first
    assert "peak hourly" in src                                   # honest p95 label on the mart path


# ---------------------------------------------------------------------------
# #7 — parallel first paints with serial fallback
# ---------------------------------------------------------------------------

def test_overview_and_day_replay_batch():
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "run_batch" in ov and '"mtd45"' in ov
    cr = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    assert cr.count("run_batch(") == 2                            # recent + historical groups
    assert "else:" in cr.split("_b_hist", 2)[2][:2000]            # serial fallback survives


# ---------------------------------------------------------------------------
# #3 — jump box pays zero queries on normal paints
# ---------------------------------------------------------------------------

def test_jump_box_loads_live_targets_on_demand():
    src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = src.split("def _global_jump", 1)[1].split("\ndef ", 1)[0]
    assert "_ow_jump_loaded" in body
    gated = body.split('if bool(st.session_state.get("_ow_jump_loaded"))', 1)[1]
    assert "show_warehouses_sql" in gated and "alert_rules" in gated  # fetches only inside the gate
    assert "load all warehouses" in body                          # explicit loader row offered


# ---------------------------------------------------------------------------
# #9 — attribution prunes + canary anchors
# ---------------------------------------------------------------------------

def test_graph_attribution_is_pruned_before_grouping():
    sql = graph_sql.graph_daily_costs(30)
    att = sql.split("QUERY_ATTRIBUTION_HISTORY", 1)[1].split("GROUP BY QUERY_ID", 1)[0]
    assert "QUERY_ID IN (" in att and "TASK_HISTORY" in att


def test_procedure_attribution_is_pruned_before_grouping():
    sql = insights_sql.procedure_costs_usd(30)
    att = sql.split("QUERY_ATTRIBUTION_HISTORY", 1)[1].split("GROUP BY 1", 1)[0]
    assert "QUERY_TYPE = 'CALL'" in att                           # semi-join to CALL roots


def test_canary_release_anchor_is_recent_not_fixed():
    src = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "2026-01-01" not in src                                # the 153s half-year scan
    assert "_recent_release_iso" in src
    from datetime import date, timedelta

    from app.data.canary import _recent_release_iso
    assert _recent_release_iso() == (date.today() - timedelta(days=3)).isoformat()
