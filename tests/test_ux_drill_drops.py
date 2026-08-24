"""UX drill-drop sweep (owner 2026-08-20): 17 previously-static tables made
click-to-drill or scope-to-selection, from the proactive UX gap sweep. Source-shape
locks so a future edit can't silently revert a drill. (The page-render apptests cover
that they still render; these lock the specific wiring.)"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_fact_task_daily_carries_schema_name():
    # the TASK drill (Ops ▸ Tasks ▸ Health) composes DB.SCHEMA.TASK — the mart must
    # keep SCHEMA_NAME in its SELECT for that key to resolve to Entity 360.
    assert "SCHEMA_NAME" in mart_sql.fact_task_daily(7, "ALL")


def test_overview_movers_and_alert_button():
    ov = _src("app/ui/pages/overview.py")
    assert 'entity_nav_table(_mv' in ov and 'entity_type="WAREHOUSE"' in ov     # #1
    assert 'request_navigation("Alerts", "Open events")' in ov                  # #10


def test_operations_task_and_query_drills():
    ops = _src("app/ui/pages/operations.py")
    assert 'entity_type="TASK"' in ops and 'key_col="TASK_FQN"' in ops           # task drills #2/12/13/15
    assert 'entity_type="WAREHOUSE"' in ops                                       # cost-per-query outliers #14
    assert 'selectable_table(_tr[_tr_cols]' in ops and '_ops_tri_sel_last' in ops  # triage drill #3


def test_security_dormant_drill_and_scope_filter():
    sec = _src("app/ui/pages/security.py")
    assert 'entity_type="USER"' in sec                                            # dormant users #16
    # scope-to-selection filter is case-insensitive (quoted lowercase identifiers)
    assert 'sec_lp_scopes_sel' in sec
    assert '.str.upper().str.startswith(_prefix)' in sec


def test_decision_studio_scenarios_drill():
    ds = _src("app/ui/decision_studio.py")
    assert 'key="ds_scenarios"' in ds and '_open_entity(' in ds                   # #5


def test_brief_and_control_room_alert_drills():
    assert "brief_fires_sel" in _src("app/ui/pages/brief.py")                     # #9
    cr = _src("app/ui/pages/control_room.py")
    assert "cr_inc_mem_sel" in cr and 'context={"event_id"' in cr                 # #11


def test_alerts_and_admin_inline_drills():
    al = _src("app/ui/pages/alerts.py")
    assert "rule_prec_sel" in al and "alert_fatigue_sel" in al                    # #17/#18
    ad = _src("app/ui/pages/admin.py")
    assert "perf_slo_sel" in ad and "err_family_sel" in ad                        # #19/#20
