"""Locks for bug-hunt round 4 (wf_6d70dac6): served-window on the all-in/egress reconciliation,
Ask evidence rate/share formatting, and the sticky-selection / write-rerun / capped-total fixes.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.data import cost_sql, mart_sql, workbench_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_org_all_in_honors_the_full_window_not_a_90d_clamp():
    # HIGH: org_all_in_window_usd is a tiny ORGANIZATION_USAGE per-day view, not a live scan, so it
    # must span the full requested window (the All-in tile + egress $/TB reconcile against 365d reads).
    sql = cost_sql.org_all_in_window_usd(365)
    assert "-90," not in sql and "DATEADD('day', -90" not in sql   # NOT clamped to 90
    assert "-365," in sql or "DATEADD('day', -365" in sql
    assert "days = bounded_days(days, MAX_MART_WINDOW_DAYS)" in inspect.getsource(cost_sql.org_all_in_window_usd)


def test_cloud_svc_rate_uses_the_canonical_credits_name():
    # CS_PER_1K_RUNS ends with RUNS -> _auto_formats treats it as an int COUNT and renders "0",
    # discarding the rate. The canonical sibling name CS_CREDITS_PER_1K hits the CREDITS format.
    for fn in (mart_sql.cloud_svc_top_shapes, mart_sql.cloud_svc_by_user):
        src = inspect.getsource(fn)
        assert "CS_CREDITS_PER_1K" in src and "CS_PER_1K_RUNS" not in src, fn.__name__
    assert "CS_PER_1K_RUNS" not in _read("app/logic/ask/registry.py")


def test_ask_credit_share_is_a_percentage_matching_the_headline():
    # CREDIT_SHARE was a 0-1 fraction rendered "0.4" beside a "44%" headline; now a 0-100 _PCT.
    src = _read("app/logic/ask/registry.py")
    assert 'ev["CREDIT_SHARE_PCT"] = ev["ALLOC_CREDITS"] / total * 100' in src
    assert 'ev["CREDIT_SHARE_PCT"] = ev["AI_CREDITS"] / total * 100' in src
    assert '"CREDIT_SHARE"]' not in src


def test_experiments_total_kpi_is_uncapped():
    assert "TOTAL_COUNT" in workbench_sql.experiment_verified_totals()
    ds = _read("app/ui/decision_studio.py")
    assert '_total_ct = int(safe_float(_vrow.get("TOTAL_COUNT"))) or len(frame)' in ds
    assert '{"label": "Experiments", "value": f"{_total_ct:,}"' in ds


def test_admin_error_family_selection_is_change_guarded():
    src = _read("app/ui/pages/admin.py")
    assert '_fam_sel != st.session_state.get("_err_family_sel_seen")' in src


def test_alert_unsnooze_reruns_and_reports_moved_count():
    src = _read("app/ui/pages/alerts.py")
    # the un-snooze block now reruns (durable receipt) and reports the proc-moved count
    assert "_n_moved = _proc_event_count(msg_s)" in src
    # locate the un-snooze block and confirm it reruns
    blk = src.split("execute_action(call, _unsnooze_stmts(_uids)", 1)[1][:1000]
    assert "st.rerun()" in blk
