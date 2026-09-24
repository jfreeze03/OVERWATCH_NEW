"""Locks for the v4.575.0 UX pass (Themes C/D/E): drill discoverability, cross-page consistency,
write feedback + copy.
"""

from __future__ import annotations

from pathlib import Path

from app.logic import metric_registry

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_nav_primitives_default_a_row_select_hint():
    comp = _read("app/ui/components.py")
    assert 'hint: str = "Select a row to open its detail.") -> None:' in comp
    assert 'hint: str = "Select a row to open it in Control Room ▸ Entity 360.") -> None:' in comp
    # entity_nav_table suppresses the inner delegation's hint to avoid a double affordance
    assert "size_note=size_note, sort_label=sort_label, hint=\"\")" in comp


def test_heaviest_queries_announces_click():
    ops = _read("app/ui/pages/operations.py")
    assert 'row_select_hint("Click a query to load it in the drill-through below.")' in ops


def test_jump_to_rule_carries_identity():
    assert 'request_navigation("Alerts", "Rules", context={"rule_id": name})' in _read("app/main.py")
    al = _read("app/ui/pages/alerts.py")
    assert 'st.session_state["rule_prec_sel_last"] = _nav_rule' in al
    assert 'st.session_state["rule_pick"] = _nav_rule' in al


def test_task_failure_drill_carries_database():
    cr = _read("app/ui/pages/control_room.py")
    seg = cr.split('elif _kind == "Task failure":', 1)[1].split("elif", 1)[0]
    assert '_flt["database"] = _db' in seg


def test_delta_columns_are_signed():
    cmp = _read("app/ui/pages/cost_parts/compare.py")
    assert 'format="$%+.0f"' in cmp and 'format="%+.1f%%"' in cmp and 'format="$%+.2f"' in cmp
    assert 'format="%+.1f%%"' in _read("app/ui/pages/cost_parts/spend.py")
    assert 'st.column_config.NumberColumn("Δ %", format="%+.2f%%")' in _read("app/ui/pages/admin.py")


def test_security_verdict_renders_above_the_section_bar():
    sec = _read("app/ui/pages/security.py")
    assert "security_posture_verdict," in sec  # imported
    assert "_sec_verdict = security_posture_verdict(f[\"company\"])" in sec
    # and the helper exists in security_center, no longer emitting a duplicate line in the queue view
    scen = _read("app/ui/security_center.py")
    assert "def security_posture_verdict(company: str) -> dict | None:" in scen
    assert scen.count("page_verdict_line(") == 0  # the verdict line now lives in security.render()


def test_write_toasts_name_the_outcome():
    opt = _read("app/ui/pages/cost_parts/optimize.py")
    assert 'f"Resized {srow[\'WAREHOUSE_NAME\']} to {target_size}; "' in opt
    assert 'f"Added savings item: {desc}."' in opt
    assert 'notify(ok, msg)' not in _read("app/ui/pages/admin.py").split("adm_setting", 1)[1][:120]
    assert 'f"Mapped {name} → {department}."' in _read("app/ui/pages/cost_parts/ai_chargeback.py")


def test_write_buttons_labeled_by_outcome():
    assert '"Save setting"' in _read("app/ui/pages/admin.py")
    opt = _read("app/ui/pages/cost_parts/optimize.py")
    assert '"Add savings item"' in opt and '"Verify savings item"' in opt
    assert '"Map to department"' in _read("app/ui/pages/cost_parts/ai_chargeback.py")
    al = _read("app/ui/pages/alerts.py")
    assert '"RESOLVE": "Resolve"}.get(action, action.title()) + " + audit"' in al
    # the generic label is no longer used by the button/confirm_gate (only referenced in a comment)
    assert 'st.button("Execute with audit row"' not in al
    assert 'confirm_gate(action, "Execute with audit row"' not in al


def test_jargon_columns_have_help():
    for col in ("OOS", "QOP", "SCAN_PCT", "CACHE_PCT"):
        assert metric_registry.COLUMN_HELP.get(col)
