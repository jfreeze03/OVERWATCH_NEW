"""Locks for the v4.8.2 perf pass (Codex-informed, telemetry-verified).

Pins: one idle scan (not two under different tiers), fact-first Control Room
(pulse + movers), tier-grouped batching on Overview/day-replay, the on-demand
jump box, the attribution-CTE prunes, and recent canary anchors.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# #6 — one idle scan
# ---------------------------------------------------------------------------

def test_idle_scan_runs_once_per_hour_not_twice():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    # v4.35.0 (r20 #1): BOTH sites use run_mart_first with the IDENTICAL
    # builder pair — mart and live reads each share one cache identity, so
    # the advisor's fetch serves the remediation block too.
    assert src.count("mart27_sql.eff_idle_analysis(days, company)") == 2
    assert src.count("insights_sql.idle_warehouse_analysis(days, company)") == 2
    assert src.count("idle_warehouse_analysis(") == 2


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

def test_overview_decoupled_and_day_replay_batched():
    # Codex #4: the filter-scoped board must NOT share a batch cache with the
    # fixed 45d MTD read (every filter change cold-started the fixed read).
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "run_batch" not in ov and "Deliberately NOT batched" in ov
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


# ---------------------------------------------------------------------------
# Codex round 2 (v4.8.3)
# ---------------------------------------------------------------------------

def test_health_values_fetched_once_and_passed_down():
    src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "health_vals = _health_values() if connected else {}" in src
    assert "_sidebar(pages, role, profile, connected, health_vals)" in src
    assert "_topbar_scope(health_vals)" in src
    assert "_persistent_status_bar(health_vals)" in src


def test_batch_supports_all_four_tiers():
    from app.core.query import _BATCH_FETCHERS, _FETCHERS, CACHE_TTLS
    # five since v4.31: "hourly" (r13 #3) — mart/fact sources load hourly,
    # a 300s TTL re-paid them 12x/hour (fleet evidence 2026-07-11).
    assert set(_BATCH_FETCHERS) == {"recent", "historical", "live", "metadata", "hourly"}
    assert set(_FETCHERS) == set(_BATCH_FETCHERS)
    assert CACHE_TTLS["hourly"] == 3600


def test_render_ms_spans_chrome():
    src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "_main_started = time.perf_counter()" in src
    assert "_render_started" not in src               # page-body-only clock removed


def test_telemetry_samples_the_healthy_baseline():
    from app.core.query import should_persist_telemetry
    # fast+ok normally skipped ...
    assert not should_persist_telemetry(50.0, ok=True, persisted=0)
    # ... but a low sample roll persists it; a high roll does not
    assert should_persist_telemetry(50.0, ok=True, persisted=0, sample_roll=0.01)
    assert not should_persist_telemetry(50.0, ok=True, persisted=0, sample_roll=0.5)
    # failure and cap semantics unchanged
    assert should_persist_telemetry(5.0, ok=False, persisted=0, sample_roll=0.99)
    assert not should_persist_telemetry(5.0, ok=False, persisted=60, sample_roll=0.01)


def test_cs_ratio_fact_builder_matches_live_contract():
    sql = mart_sql.fact_cloud_services_ratio(7, "ALFA")
    assert "FACT_WAREHOUSE_DAILY" in sql
    for col in ("COMPUTE_CREDITS", "CLOUD_SVC_CREDITS", "TOTAL_CREDITS",
                "CLOUD_SVC_PCT", "STATUS"):
        assert col in sql, col                         # same columns as the live builder
    assert "CREDITS_TOTAL - CREDITS_COMPUTE" in sql    # no migration needed (Codex #6)
    assert "'ELEVATED'" in sql and "'WATCH'" in sql    # same thresholds


def test_spend_tab_is_fact_first_for_movers_and_cs_ratio():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "fact_warehouse_window_vs_prior" in src and "fact_cloud_services_ratio" in src
    assert src.count("live fallback") >= 1             # honest degrade kept


def test_unit_costs_reads_go_out_as_one_batch():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert "run_batch(" in src
    assert 'tier="historical")' in src                 # same-tier group
    assert "else:" in src.split("_ub", 2)[2][:3000]    # serial fallback survives

