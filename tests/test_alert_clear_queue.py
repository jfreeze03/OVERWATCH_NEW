"""Bulk "clear the open queue" locks (2026-08-30, v4.372.0).

Operator-only action on Alerts ▸ Open events that acks or resolves EVERY open/ack event in the current
company scope in one step (past the feed's row cap) — for resetting the queue after pre-production
validation. Scope-based (STATUS + company), audit-first, and defaults to an untagged resolve so a
start-fresh clear does not skew per-rule precision.
"""

from __future__ import annotations

from pathlib import Path

from app.ui.pages.alerts import RESOLUTION_KINDS, _clear_open_queue_stmts

_ALERTS_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "alerts.py").read_text(
    encoding="utf-8")


def test_resolve_all_is_scope_based_audit_first_and_untagged_by_default() -> None:
    stmts = _clear_open_queue_stmts("ALFA", "RESOLVE", "cleared")
    assert len(stmts) == 2
    audit, update = stmts
    # audit runs FIRST (it SELECTs the to-be-transitioned rows before the UPDATE changes them)
    assert audit.startswith("INSERT INTO") and "ALERT_AUDIT" in audit
    assert update.startswith("UPDATE") and "ALERT_EVENTS" in update
    # matched by STATUS + company, not enumerated EVENT_IDs -> clears past the feed cap
    assert "EVENT_ID IN (" not in update and "EVENT_ID IN (" not in audit
    assert "STATUS IN ('OPEN', 'ACK')" in update
    assert "(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')" in update  # same scope as the KPI counts
    assert "SET STATUS = 'RESOLVED'" in update
    # untagged by default -> excluded from precision
    assert "RESOLUTION_KIND" not in update


def test_resolve_with_a_kind_tags_the_rows() -> None:
    for kind in RESOLUTION_KINDS:
        update = _clear_open_queue_stmts("ALFA", "RESOLVE", "n", kind)[1]
        assert f"RESOLUTION_KIND = '{kind}'" in update


def test_ack_only_transitions_open_and_never_resolves() -> None:
    audit, update = _clear_open_queue_stmts("ALFA", "ACK", "seen")
    assert "SET STATUS = 'ACK'" in update
    assert "WHERE STATUS = 'OPEN'" in update       # ACK only touches OPEN, not already-ACK rows
    assert "RESOLVED" not in update
    # the audit is scoped to the same OPEN set
    assert "STATUS = 'OPEN'" in audit


def test_company_all_drops_the_scope_clause() -> None:
    audit, update = _clear_open_queue_stmts("ALL", "RESOLVE", "n")
    assert "COMPANY" not in update and "COMPANY" not in audit


def test_snoozed_events_are_never_touched() -> None:
    # the transition set is OPEN/ACK only, so SNOOZED events (deliberately deferred) are left alone
    for action in ("ACK", "RESOLVE"):
        for s in _clear_open_queue_stmts("ALFA", action, "n"):
            assert "'SNOOZED'" not in s


def test_clear_queue_receipt_is_honest_per_verb() -> None:
    # alert-hunt #3: the receipt/toast used _open_total (OPEN+ACK) for BOTH verbs, which
    # overstates ACK (ACK moves only the OPEN rows and keeps them counting as open) and
    # wrongly calls an ACK 'cleared'. Now RESOLVE keeps the accurate count; ACK reports
    # the action without a misleading number.
    assert "event(s) {_c_verb}" not in _ALERTS_SRC        # old verb-agnostic overstatement gone
    assert "event(s) resolved." in _ALERTS_SRC            # RESOLVE: true transitioned count
    assert "open count until resolved" in _ALERTS_SRC     # ACK: honest, no count
