"""Bulk 'resolve open incidents' (v4.375.0) — the incident-side companion to the
Alerts clear-queue.

Forward-only and scope-based (STATUS + company, never enumerated ids), so a
validation reset covers the whole open set past the 50-row feed cap. Because the
INCIDENTS write bumps the 'incidents' cache domain, Brief/Overview's open-incident
count — a driver of their 'Attention needed' verdict — refreshes on the next read.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from app.ui.pages.control_room import _clear_open_incidents_sql


def test_resolves_open_and_mitigated_forward_only():
    sql = _clear_open_incidents_sql("ALL", "UNKNOWN", "reset")
    assert "UPDATE" in sql and "INCIDENTS" in sql
    assert "STATUS = 'RESOLVED'" in sql
    # forward-only: only OPEN/MITIGATED move, so a RESOLVED/reopened row is never rewritten
    assert "WHERE STATUS IN ('OPEN', 'MITIGATED')" in sql
    assert "RESOLVED_AT = CURRENT_TIMESTAMP()" in sql
    assert "UPDATED_AT = CURRENT_TIMESTAMP()" in sql


def test_is_scope_based_not_id_enumerated():
    # matched by STATUS (+ company), never a list of INCIDENT_IDs — so it clears the
    # whole open set, not just the LIMIT-50 feed the operator can see
    sql = _clear_open_incidents_sql("ALL", "UNKNOWN", "reset")
    assert "INCIDENT_ID" not in sql


def test_company_all_drops_the_company_scope():
    sql = _clear_open_incidents_sql("ALL", "UNKNOWN", "reset")
    assert "COMPANY" not in sql.split("WHERE", 1)[1]


def test_specific_company_includes_account_level():
    sql = _clear_open_incidents_sql("ALFA", "UNKNOWN", "reset")
    assert "COMPANY = 'ALFA'" in sql
    assert "UPPER(COMPANY) = 'ALL'" in sql   # account-level incidents cleared with the company's


def test_kind_uppercased_and_note_carried():
    sql = _clear_open_incidents_sql("ALL", "deploy", "cleared for validation")
    assert "ROOT_CAUSE_KIND = 'DEPLOY'" in sql
    assert "cleared for validation" in sql


def test_note_is_sql_escaped():
    sql = _clear_open_incidents_sql("ALL", "UNKNOWN", "O'Brien said reset")
    assert "O''Brien said reset" in sql      # single quote doubled — never breaks out of the literal


def test_panel_renders_no_write_ui_for_non_operator_or_empty(monkeypatch):
    import app.ui.pages.control_room as cr

    def _boom(*_a, **_k):
        raise AssertionError("write UI rendered when it must not")

    monkeypatch.setattr(cr.st, "expander", _boom)
    cr._incident_reset_panel("ALL", 5, is_op=False)   # non-operator -> nothing
    cr._incident_reset_panel("ALL", 0, is_op=True)    # nothing open -> nothing
