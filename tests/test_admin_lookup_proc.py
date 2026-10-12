"""v4.610.2: the admin-access lookup CALLs V175's SP_ADMIN_ROLE_MEMBERS() once V175 is applied.

Owner decision 2026-10-06: SNOW_SYSADMINS will own the app. SHOW GRANTS OF ROLE answers as the APP owner, so a
SNOW_SYSADMINS-owned app could see fewer DSA grantees than SNOW_ACCOUNTADMINS does, and every DSA-only admin would
silently drop to read-only. The CALL answers as the PROCEDURE's owner (the role that applied V175) instead.

What this file proves:
  * the switch -- CALL when schema_gate.has_migration(175) is True, SHOW when it is False or the gate raises;
  * the statement runs as a read (submit_collect), never through the write executor, and is recorded for messages;
  * fail closed -- a failed CALL is lookup_failed and never makes an admin; a CALL answer with no USER row is
    unverified; the error log, the roster verdict and the Admin hint name the CALL;
  * the CALL's rows (granted_to / grantee_name, as V175 returns them) feed role_grant_members unchanged.
The SQL side (V175 itself) is locked in tests/migrations/test_v175_admin_role_members_proc.py.
"""

from __future__ import annotations

import inspect

import pytest
import streamlit as st

import app.config as cfg
import app.core.errors as errors_mod
import app.core.identity as ident
import app.core.query as q
import app.core.session as sess
import app.ui.schema_gate as schema_gate
from app.data import access_sql
from app.logic import access_review

_CALL = f"CALL {cfg.OVERWATCH_DB}.{cfg.CORE_SCHEMA}.SP_ADMIN_ROLE_MEMBERS()"
_SHOW = f'SHOW GRANTS OF ROLE "{cfg.ADMIN_ACCESS_ROLE}"'
_DSA_USER = "DSA_PERSON1"


class _Session:
    def sql(self, text: str) -> str:
        return text


@pytest.fixture
def lookup(monkeypatch):
    """The real _admin_role_rows over a scripted Snowflake: ``lk.applied`` drives has_migration(175),
    ``lk.rows`` / ``lk.raise_`` the answer; ``lk.ran`` records each statement collected."""
    st.session_state.clear()

    class Lk:
        def __init__(self) -> None:
            self.applied = True
            self.gate_raises = False
            self.rows: list = [{"granted_to": "USER", "grantee_name": _DSA_USER},
                               {"granted_to": "ROLE", "grantee_name": "SOME_NESTED_ROLE"}]
            self.raise_: Exception | None = None
            self.ran: list = []
            self.errors: list = []

    lk = Lk()

    def _has_migration(v, page):
        if lk.gate_raises:
            raise RuntimeError("gate unreadable")
        assert (v, page) == (access_sql.ADMIN_MEMBERS_MIGRATION, "session")
        return lk.applied

    def _collect(session, statement, params, **_kw):
        lk.ran.append(statement)
        if lk.raise_ is not None:
            raise lk.raise_
        return list(lk.rows)

    monkeypatch.setattr(schema_gate, "has_migration", _has_migration)
    monkeypatch.setattr(sess, "get_session", lambda: _Session())
    monkeypatch.setattr(sess, "submit_collect", _collect)
    monkeypatch.setattr(sess, "statement_params", lambda *a, **k: None)
    monkeypatch.setattr(ident, "viewer_name", lambda: _DSA_USER)
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    monkeypatch.setattr(errors_mod, "record_error",
                        lambda page, exc, context="": lk.errors.append((page, exc, context)) or "ref")
    yield lk
    st.session_state.clear()


def test_the_call_builder_names_the_v175_procedure():
    assert access_sql.ADMIN_MEMBERS_PROC == "SP_ADMIN_ROLE_MEMBERS"
    assert access_sql.call_admin_role_members_sql() == _CALL
    # no argument: the role is hard-coded in V175, so the app cannot ask it about any other role
    assert access_sql.call_admin_role_members_sql().endswith("()")


def test_the_fake_gate_matches_the_real_signature():
    # the fixture calls has_migration(v, page) positionally; a keyword-only change must fail here, not hide
    inspect.signature(schema_gate.has_migration).bind(access_sql.ADMIN_MEMBERS_MIGRATION, "session")


def test_show_before_v175_call_once_applied(lookup):
    lookup.applied = False
    assert sess._admin_lookup_sql() == _SHOW
    lookup.applied = True
    assert sess._admin_lookup_sql() == _CALL


def test_the_call_is_sticky_once_this_session_saw_v175(lookup):
    # review r1 #3: after the owner switch a fallback SHOW would answer as SNOW_SYSADMINS and could list fewer
    # users with no error, so an unreadable SCHEMA_VERSION later in the session must not undo the CALL
    assert sess._admin_lookup_sql() == _CALL
    lookup.applied = False
    assert sess._admin_lookup_sql() == _CALL
    lookup.gate_raises = True
    assert sess._admin_lookup_sql() == _CALL
    assert lookup.errors == []          # the sticky answer never consulted the gate


def test_a_raising_gate_keeps_the_pre_v175_show_and_is_logged_once(lookup):
    lookup.gate_raises = True
    assert sess._admin_lookup_sql() == _SHOW
    assert sess._admin_lookup_sql() == _SHOW
    assert len(lookup.errors) == 1
    page, _exc, context = lookup.errors[0]
    assert page == "Access" and "V175 schema check failed" in context and _SHOW in context


def test_the_lookup_collects_the_chosen_statement_and_records_it(lookup):
    lookup.applied = False
    sess._admin_role_rows()
    assert lookup.ran == [_SHOW]
    assert st.session_state[sess._LOOKUP_SQL_KEY] == _SHOW
    lookup.applied = True
    rows = sess._admin_role_rows()
    assert lookup.ran == [_SHOW, _CALL]
    assert st.session_state[sess._LOOKUP_SQL_KEY] == _CALL
    assert sess.role_grant_members(rows) == (frozenset({_DSA_USER}), ("SOME_NESTED_ROLE",))


def test_the_call_answer_makes_a_dsa_member_an_admin(lookup):
    access = sess.viewer_access()
    assert access["operator"] is True and access["source"] == "role"
    assert lookup.ran == [_CALL]
    info = sess.access_info()
    assert info["roster_lookup"] == _CALL
    # review r1 #2: the healthy verdict says which statement answered (the only in-app sign of the switch)
    summary = access_review.roster_summary(info, now=0.0)
    assert summary["state"] == "ok" and summary["headline"].endswith(f"by {_CALL}).")


def test_quoted_column_names_from_the_call_still_parse():
    # V175 declares RETURNS TABLE ("granted_to" VARCHAR, "grantee_name" VARCHAR); a driver may hand the keys back quoted
    rows = [{'"granted_to"': "USER", '"grantee_name"': _DSA_USER}]
    assert sess.role_grant_members(rows)[0] == frozenset({_DSA_USER})


def test_a_failed_call_fails_closed_and_names_the_call(lookup):
    lookup.raise_ = RuntimeError("SQL compilation error: Unknown function SP_ADMIN_ROLE_MEMBERS")
    access = sess.viewer_access()
    assert access["operator"] is False and access["source"] == "lookup_failed"
    assert access["profile"] != cfg.OPERATOR_PROFILES[0]
    assert len(lookup.errors) == 1 and _CALL in lookup.errors[0][2]
    info = sess.access_info()
    assert info["roster_status"] == "lookup_failed" and info["roster_lookup"] == _CALL
    summary = access_review.roster_summary(info, now=0.0)
    assert summary["state"] == "unavailable" and _CALL in summary["headline"]
    hint = access_review.lookup_check_hint(info)
    assert hint.startswith(f"Check: run {_CALL}") and "re-run V175 as SNOW_ACCOUNTADMINS" in hint
    assert "named admin" not in hint.lower()          # v4.611.0: nobody is exempt from the lookup


def test_a_call_with_no_user_row_is_unverified_never_an_admin(lookup):
    lookup.rows = [{"granted_to": "ROLE", "grantee_name": "SOME_NESTED_ROLE"}]
    access = sess.viewer_access()
    assert access["operator"] is False and access["source"] == "unverified"
    info = sess.access_info()
    assert _CALL in info["roster_error"]
    assert _CALL in access_review.roster_summary(info, now=0.0)["headline"]


def test_a_failed_call_never_passes_the_write_time_recheck(lookup):
    assert sess.viewer_access()["operator"] is True
    lookup.raise_ = RuntimeError("Insufficient privileges to operate on procedure")
    assert sess.reverify_role_admin() is False


def test_the_show_hint_is_unchanged_before_v175():
    info = {"admin_role": cfg.ADMIN_ACCESS_ROLE, "roster_lookup": _SHOW}
    hint = access_review.lookup_check_hint(info)
    assert hint.startswith(f"Check: run SHOW GRANTS OF ROLE {cfg.ADMIN_ACCESS_ROLE} as SNOW_ACCOUNTADMINS")
    assert access_review.lookup_check_hint({"admin_role": cfg.ADMIN_ACCESS_ROLE}) == hint
    assert "named admin" not in hint.lower()


def test_the_call_is_a_read_not_a_privileged_write():
    # it decides entitlement, so it must never route through the executor's entitlement check: the lookup
    # collects it directly (the source lock below) and the executor would refuse it for a non-admin
    src = sess._admin_role_rows.__code__.co_names
    assert "submit_collect" in src and "execute_action" not in src and "run" not in src
    assert q._is_privileged(_CALL) is True   # why the lookup must stay outside the executor
