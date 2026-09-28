"""v4.156.0 locks: metric trust, decision-first surfaces, and hot-path performance."""

from __future__ import annotations

from pathlib import Path

from app.ui.pages.alerts import _optional_number

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_alert_headlines_match_the_company_scoped_queue() -> None:
    brief = _read("app/ui/pages/brief.py")
    control = _read("app/ui/pages/control_room.py")
    overview = _read("app/ui/pages/overview.py")
    for source in (brief, control, overview):
        assert "open_alert_severity_counts(company)" in source
    assert "same scope as Fires" in brief
    assert "same scope as the Alerts queue" in overview
    assert "The shell strip is intentionally account-wide" in control


def test_no_evidence_is_not_rendered_as_a_healthy_zero() -> None:
    operations = _read("app/ui/pages/operations.py")
    control = _read("app/ui/pages/control_room.py")
    overview = _read("app/ui/pages/overview.py")
    studio = _read("app/ui/decision_studio.py")
    assert operations.count("No query denominator") >= 1
    assert "No run denominator" in operations
    assert "No query denominator" in control
    assert "health cannot be cleared" in control
    # An idle account (0 queries) with a SUCCESSFUL throughput read is evidence-present,
    # not Incomplete — the gate no longer requires queries>0 (audit fix).
    assert 'if _thr.usable() and queries > 0:' not in overview
    assert 'if _thr.usable():' in overview
    # Wave-2 #10: an objective with no applicable evidence reads "—"/n/a, never a misleading
    # healthy zero. v4.597 (Option C): the custom SLO board (worst-burn "n/a") was retired; its
    # read-only successors, the built-in objectives, read "—" (never "0/0") without evidence.
    objectives = operations.split("def _builtin_objectives_panel", 1)[1].split("\ndef ", 1)[0]
    assert '"value": "—"' in objectives and "0/0" not in objectives
    # DS flagship ROI section (v4.251), now Proof ▸ Proof: a failed ledger read shows a no-data state
    # (not a healthy $0 verified total), and realization reads "—" until something is verified (ROI
    # fixes 2026-09-24: 'n/a' once items are verified but auto-measured, i.e. carry no estimate).
    signals = studio.split("def _proof_signals(", 1)[1].split("\ndef ", 1)[0]
    assert "if not ledger.ok:\n        _PROOF_MEMO.update(rate=_k, sig=None)\n        return None" in signals
    proof_tab = studio.split("def _proof_tab(", 1)[1].split("\ndef ", 1)[0]
    assert 'if sig is None:\n        empty_state("needs_setup"' in proof_tab
    # Next-Fifty #31: realization keeps reverted rows while verified_count drops them, so "anything
    # verified" counts both (the "—" stays for an empty record; "nothing verified yet" never sits beside a
    # real ratio)
    assert '("n/a" if _ver_any else "—")' in proof_tab
    assert '_ver_any = int(totals["verified_count"]) + int(totals.get("reverted_count") or 0)' in proof_tab
    # v4.365 (ds-hunt): the no-candidate state still reads "No evidence" (not a healthy $0), and an
    # eligible-but-unpriced queue reads "Unpriced" rather than a misleading $0.00 — now in the
    # Proof ▸ Pipeline projection.
    projection = studio.split("def _pipeline_projection(", 1)[1].split("\ndef _pipeline_tab(", 1)[0]
    assert 'return "No evidence"' in projection
    assert 'return format_usd(value) if _priced else "Unpriced"' in projection
    assert _optional_number(None, "%") == "n/a"
    assert _optional_number(float("nan"), "%") == "n/a"


def test_modeled_credit_spend_is_not_called_invoice_truth() -> None:
    registry = _read("app/logic/metric_registry.py")
    spend = _read("app/ui/pages/cost_parts/spend.py")
    brief = _read("app/ui/pages/brief.py")
    main = _read("app/main.py")
    assert '"Configured-rate credit spend"' in registry
    assert '"Credit commitment runway (modeled)"' in registry
    assert "USAGE_IN_CURRENCY is billing truth" in registry
    assert "modeled credit-spend view, not the full invoice" in spend
    assert "Credit commitment exhausts" in brief
    assert '"k": "MTD credit spend"' in main


def test_security_navigation_precedes_selected_section_reads() -> None:
    security = _read("app/ui/pages/security.py")
    switch = security.index('section = lazy_sections(')
    decision_branch = security.index('if section == "Decision queue":')
    overview = security.index('render_security_overview(f["company"])')
    assert switch < decision_branch < overview
    assert '"Decision queue", "Access", "AI guardrails", "Changes", "Clients", "Egress",' in security
    assert '"Least privilege", "Trust Center"' in security


def test_operations_decision_tables_do_not_default_to_row_zero() -> None:
    operations = _read("app/ui/pages/operations.py")
    optimize = _read("app/ui/pages/cost_parts/optimize.py")
    wh_block = operations.split("def _wh_change_block", 1)[1].split("\ndef ", 1)[0]
    assert "else df.iloc[0]" not in wh_block
    assert "Select a warehouse change" in wh_block
    assert 'key="idle_advisor_sel"' in optimize
    assert "Selected warehouse evidence" in optimize
    assert "Selected recommendation evidence" in optimize


def test_operations_healthy_query_path_is_batched_and_hourly() -> None:
    operations = _read("app/ui/pages/operations.py")
    body = operations.split("def _queries_tab", 1)[1].split("\ndef ", 1)[0]
    assert "_mart_jobs" in body and "_mart_pf = run_batch" in body
    for key in ('"activity"', '"summary"', '"top"', '"fails"'):
        assert key in body
    assert 'page=_PAGE, tier="hourly"' in body
    assert 'mart_tier="hourly", live_tier="recent"' in body
    contention = operations.split("def _contention_tab", 1)[1].split("\ndef ", 1)[0]
    assert '_chart_metric = "AVG_QUEUE_SEC"' in contention


def test_alert_event_payload_is_loaded_only_in_open_events() -> None:
    alerts = _read("app/ui/pages/alerts.py")
    render = alerts.split("def render()", 1)[1]
    switch = render.index('section = lazy_sections(')
    branch = render.index('if section == "Open events":')
    feed = render.index("open_alert_events(500, company)")
    assert switch < branch < feed
    assert render.count("open_alert_events(500, company)") == 1


def test_table_rendering_uses_cell_budgets_and_clients_has_one_export() -> None:
    components = _read("app/ui/components.py")
    security = _read("app/ui/pages/security.py")
    assert "STYLER_MAX_CELLS = 6_000" in components
    assert "_cell_count <= STYLER_MAX_CELLS" in components
    assert "EAGER_CSV_MAX_CELLS = 1_500" in components
    clients = security.split("def _clients_tab", 1)[1].split("\ndef ", 1)[0]
    assert "Driver inventory (CSV)" not in clients
    assert 'slug="client-drivers"' in clients
