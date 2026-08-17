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
    # v4.35.0 (r20 #1) + v4.254 wave-3: THREE sites (advisor, remediation, and the
    # idle-waste headline above the sub-tabs) use run_mart_first with the IDENTICAL
    # builder pair — mart and live reads each share one cache identity, so the whole
    # tab pays for ONE idle scan per hour no matter how many sites read it.
    assert src.count("mart27_sql.eff_idle_analysis(days, company)") == 3
    assert src.count("insights_sql.idle_warehouse_analysis(days, company)") == 3
    assert src.count("idle_warehouse_analysis(") == 3


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
    # N4 (v4.85): Overview batches its two independent LIVE first-paint reads
    # (open alerts + owner-action queue). The exec board keeps its own cache key
    # and stays OUT of the batch — assert neither the board nor the fixed daily
    # read is inside the run_batch spec block.
    assert "Deliberately NOT batched" in ov
    _batch_block = ov.split("run_batch(", 1)[1].split("], page", 1)[0]
    assert "_load_board" not in _batch_block and "board" not in _batch_block
    assert "fact_daily" not in _batch_block
    cr = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    # retro recent + historical groups, plus the T2.1 live-trio group (open
    # incidents / proposals / triage alerts submitted as one live round trip)
    assert cr.count("run_batch(") == 3
    assert 'run_batch(_live_specs, page=_PAGE, tier="live")' in cr
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
    assert "_ow_jump_loadall" in body                             # rec16: explicit loader BUTTON, not a fake option


# ---------------------------------------------------------------------------
# #9 — attribution prunes + canary anchors
# ---------------------------------------------------------------------------

def test_graph_attribution_is_pruned_before_grouping():
    # Still pruned to task-run queries before the GROUP BY (perf pass #9); the key
    # is now the rolled-up COALESCE(ROOT_QUERY_ID, QUERY_ID) so proc children are
    # attributed to the task root (audit #10) rather than dropped.
    sql = graph_sql.graph_daily_costs(30)
    att = sql.split("QUERY_ATTRIBUTION_HISTORY", 1)[1].split(
        "GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)", 1)[0]
    assert "COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (" in att and "TASK_HISTORY" in att


def test_procedure_attribution_is_pruned_before_grouping():
    sql = insights_sql.procedure_costs_usd(30)
    att = sql.split("QUERY_ATTRIBUTION_HISTORY", 1)[1].split("GROUP BY 1", 1)[0]
    assert "QUERY_TYPE = 'CALL'" in att                           # semi-join to CALL roots


def test_canary_release_anchor_is_recent_not_fixed():
    src = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "2026-01-01" not in src                                # the 153s half-year scan
    assert "_recent_release_iso" in src
    from datetime import timedelta

    from app.data.canary import _recent_release_iso
    from app.logic.formulas import account_today
    assert _recent_release_iso() == (account_today() - timedelta(days=3)).isoformat()


# ---------------------------------------------------------------------------
# Codex round 2 (v4.8.3)
# ---------------------------------------------------------------------------

def test_health_values_are_owned_by_the_single_global_pulse():
    src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "_sidebar(pages, role, profile, connected)" in src
    assert "_topbar_scope()" in src
    assert "_health_strip(" not in src
    body = src.split("def _persistent_status_bar", 1)[1].split("\ndef ", 1)[0]
    assert "vals = _health_values()" in body
    assert "_persistent_status_bar(pages)" in src


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
    # #28: a failure qualifies regardless of the NON-failure row cap (persisted) — it draws
    # from its own reserved fail budget, so chatty healthy/slow traffic can't suppress it.
    assert should_persist_telemetry(5.0, ok=False, persisted=0, sample_roll=0.99)
    assert should_persist_telemetry(5.0, ok=False, persisted=60, sample_roll=0.01)
    # ... but the fail budget itself is bounded, so a broken page still can't spam forever.
    assert not should_persist_telemetry(5.0, ok=False, persisted=0, failed_persisted=20)


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


def test_cost_spend_section_batches_recent_and_defers_storage_unmapped():
    """#15: the default 'Spend & Attribution' section submits its four independent
    recent mart reads as ONE run_batch and defers Storage + Unmapped behind a
    toggle, so first paint pays one parallel group instead of ~10 serial reads."""
    cost = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    spend = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    # one batch feeds the eager Spend + Attribution panels (T1.1: hourly tier —
    # the members are hourly-loaded facts, so 3600s TTL not 300s)
    assert "run_batch(_spend_attr_recent_jobs(" in cost and 'tier="hourly")' in cost
    for k in ("metering", "csr", "wh", "daily"):        # every member threaded, none dropped
        assert f'_pf.get("{k}")' in cost, k
    # the spec-builder lives in spend.py and references all four fact builders
    assert "def _spend_attr_recent_jobs(" in spend
    for b in ("fact_metering_by_service", "fact_cloud_services_ratio",
              "fact_warehouse_window_vs_prior", "fact_warehouse_daily"):
        assert b in spend, b
    # Storage + Unmapped are inside the toggle gate (off the eager first paint)
    assert 'st.toggle("Load storage & unmapped-entity detail", key="cost_spend_detail"' in cost
    deferred = cost.split("cost_spend_detail", 1)[1]
    assert "_storage_tab(" in deferred and "unmapped_entities" in deferred
    # unmapped is DEFERRED, not batched — it must not appear in the recent batch spec
    assert "unmapped_entities" not in spend.split("_spend_attr_recent_jobs", 1)[1].split("def ", 2)[0]
    # each batched panel keeps its own live/historical fallback (prefetch-else-run)
    assert spend.count("if metering_res is not None else run(") == 1
    assert spend.count("if wh_res is not None else run(") == 1
