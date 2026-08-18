"""v4.149.0 — triage-routing identity pass + write-friction policy + decision honesty.

One behavioral test (triage_queue carries the warehouse) plus source-grep guards
for the UI-layer changes, in the house style (the UI calls st.* and needs a
Streamlit runtime, so those are pinned by source assertion like
test_v4143_status_bar_compat).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- rec22 (behavioral): the spend-anomaly triage row carries its warehouse -------
def test_triage_queue_carries_warehouse_on_spend_rows():
    from app.logic.actions import triage_queue

    queue = triage_queue(
        alerts=None,
        task_failures=None,
        anomalies=[
            {"label": "WH_ALFA_ETL", "value": 900.0, "z": 4.2,
             "excess_usd": 500.0, "day": "2026-08-10"},
            {"label": "WH_TRXS_BATCH", "value": 5.0, "z": -3.9,
             "excess_usd": 300.0, "day": "2026-08-10"},
        ],
    )
    assert "WAREHOUSE" in queue.columns
    by_kind = {str(r["KIND"]): r for _, r in queue.iterrows()}
    assert by_kind["Spend anomaly"]["WAREHOUSE"] == "WH_ALFA_ETL"
    # A spend COLLAPSE (z<0) is a distinct kind but still carries its warehouse.
    assert by_kind["Spend collapse"]["WAREHOUSE"] == "WH_TRXS_BATCH"


def test_triage_queue_non_spend_rows_have_blank_warehouse():
    """Alert / task rows must still define WAREHOUSE (blank) so the column is never
    NaN — a NaN would defeat the `str(... or "").strip()` guard in the router."""
    from app.logic.actions import triage_queue

    alerts = pd.DataFrame([{
        "SEVERITY": "CRITICAL", "TITLE": "boom", "DETAIL": "d",
        "RAISED_AT": "2026-08-10 01:00:00", "EVENT_ID": "evt-1", "RULE_ID": "r-1",
    }])
    queue = triage_queue(alerts=alerts, task_failures=None, anomalies=None)
    assert "WAREHOUSE" in queue.columns
    assert queue.iloc[0]["WAREHOUSE"] == ""
    # rec20: the alert row keeps its EVENT_ID so the router can hand it to the drawer.
    assert queue.iloc[0]["EVENT_ID"] == "evt-1"


# --- rec20/21/22: the router carries identity / section / scope -------------------
def test_open_triage_routes_identity_section_and_scope():
    src = _src("app/ui/pages/control_room.py")
    body = src.split("def _open_triage", 1)[1].split("selectable_nav_table", 1)[0]
    assert 'context={' not in body  # built as _ctx, passed positionally — sanity below
    assert '_ctx["event_id"]' in body                 # rec20: alert -> drawer identity
    assert '("Operations", "Tasks")' in body          # rec21: task -> owning section
    # rec22 (adversarial fix): route to Queries — the section that ACTUALLY consumes
    # warehouse_contains. Warehouses ignores it (its contract renders it "ignored").
    assert '("Operations", "Queries")' in body
    assert '("Operations", "Warehouses")' not in body
    assert '_flt["warehouse_contains"]' in body        # rec22: scoped, not account-wide
    assert "request_navigation(_dest[0], _dest[1], _flt or None, _ctx or None)" in body


# --- rec14 / rec1: one-click ACK, typed RESOLVE, policy documented ----------------
def test_ack_one_click_resolve_typed():
    body = _src("app/ui/pages/alerts.py")
    seg = body.split('if is_operator:', 1)[1].split("execute_action", 1)[0]
    assert 'if action in ("ACK", "SNOOZE"):' in seg           # ACK + SNOOZE = one click (V086)
    assert 'st.button("Execute with audit row"' in seg       # ACK = one click
    assert "confirm_gate(action" in seg                        # RESOLVE keeps the gate


def test_write_friction_policy_is_documented():
    laws = _src("CLAUDE.md")
    assert "Write-friction policy" in laws
    assert "friction matches" in laws.lower() or "consequence" in laws.lower()


# --- rec48: notify persists on failure, toast-only on success ---------------------
def test_notify_failure_persists_success_is_toast_only():
    body = _src("app/ui/components.py")
    fn = body.split("def notify(", 1)[1].split("\ndef ", 1)[0]
    assert "if not ok:" in fn and "st.error(msg)" in fn
    # The old unconditional "(st.success if ok else st.error)(msg)" is gone.
    assert "st.success if ok else st.error" not in fn
    assert "st.toast(" in fn


# --- rec48 (adversarial fix): non-idempotent create sites rerun on success --------
def test_create_sites_rerun_on_success():
    """With the success banner gone (rec48), the create INSERTs (non-idempotent, no
    other rerun) must st.rerun() on ok so the table is the receipt and a second
    click cannot double-insert."""
    wb = _src("app/ui/workbench.py")
    after_action = wb.split('key="action_new_exec"', 1)[1][:600]
    assert "notify(ok, msg)" in after_action and "st.rerun()" in after_action
    after_exp = wb.split('key=f"exp_create_{action_id}"', 1)[1][:600]
    assert "notify(ok, msg)" in after_exp and "st.rerun()" in after_exp
    sec = _src("app/ui/security_center.py")
    after_sec = sec.split('key="sec_exception_create"', 1)[1][:600]
    assert "notify(ok, message)" in after_sec and "st.rerun()" in after_sec


# --- rec48: notify still guarantees a success cue if the toast is unavailable ------
def test_notify_success_falls_back_when_toast_unavailable():
    fn = _src("app/ui/components.py").split("def notify(", 1)[1].split("\ndef ", 1)[0]
    assert "toasted = True" in fn
    assert "elif not toasted:" in fn and "st.success(msg)" in fn


# --- rec28: the two Confidence columns are labelled distinctly ---------------------
def test_confidence_columns_labelled_by_epistemics():
    comp = _src("app/ui/components.py")
    assert "confidence_label: str = \"Confidence\"" in comp
    assert 'config[confidence_label] = st.column_config.ProgressColumn(' in comp
    assert 'confidence_label="Confidence (evidence)"' in _src("app/ui/decision_studio.py")
    assert 'confidence_label="Confidence (authored)"' in _src("app/ui/workbench.py")


# --- rec17/18: no silent row-0 in experiments; scenarios empty-state ---------------
def test_experiments_require_selection_and_scenarios_guard_empty():
    ds = _src("app/ui/decision_studio.py")
    exp = ds.split("def _experiments", 1)[1].split("\ndef ", 1)[0]
    assert "if selected is None:" in exp and "return" in exp
    assert "index = int(selected) if selected is not None else 0" not in exp  # old row-0
    scen = ds.split("def _scenarios", 1)[1].split("\ndef ", 1)[0]
    assert "if actions.empty:" in scen and 'empty_state("no_data_yet"' in scen


# --- rec32: products dollar columns declare non-additivity in their help -----------
def test_products_dollar_columns_have_nonadditive_help():
    prod = _src("app/ui/decision_studio.py").split("def _products", 1)[1].split("\ndef ", 1)[0]
    cfg = prod.split("column_config=", 1)[1]
    # both dollar columns carry the non-additive warning in their column help
    assert cfg.count("A SEPARATE lens") >= 2
    assert cfg.count("never sum them") >= 2


# --- rec26/49: honesty captions ----------------------------------------------------
def test_entity360_names_metricless_types_and_security_scopes_its_write():
    wb = _src("app/ui/workbench.py")
    assert "No metric snapshot is defined for" in wb
    sec = _src("app/ui/pages/security.py")
    assert "OVERWATCH's own work queue" in sec


# --- rec10/11: exception-first + pill badge ---------------------------------------
def test_control_room_leads_with_exceptions_and_badges_pill():
    cr = _src("app/ui/pages/control_room.py")
    assert 'counts={"Incidents & triage": _open_crit}' in cr
    # exception_summary appears in the Incidents & triage branch, not only Pulse.
    triage_branch = cr.split('elif section == "Incidents & triage":', 1)[1].split("elif section ==", 1)[0]
    assert "exception_summary(" in triage_branch
