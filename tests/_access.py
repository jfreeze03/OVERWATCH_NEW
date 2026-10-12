"""Shared helper for scripting the admin-role (SNOW_PRI_GFR_PRD_ALFA_DSA) roster (v4.611.0).

Since v4.611.0 roles alone decide who is an OVERWATCH admin (owner 2026-10-07): no username is hard-coded, so
a test makes someone an admin only by listing them in a scripted DSA roster, never by a name. Not collected
(no ``test_`` prefix). Import as ``from tests._access import ...``.

``dsa_rows(*pairs)`` builds SHOW GRANTS OF ROLE rows for config.ADMIN_ACCESS_ROLE from (granted_to,
grantee_name) pairs. ``script_dsa_roster(monkeypatch, viewer=..., members=..., nested=..., raise_=...)``
clears st.session_state, makes ``viewer`` the identified Streamlit-in-Snowflake viewer and answers the
admin-role lookup from ``members`` (USER rows) and ``nested`` (ROLE rows). It returns the mutable state:
``state['lookups']`` counts the lookups, setting ``state['raise']`` makes the next one raise,
``state['errors']`` collects the APP_ERROR_LOG rows (errors.record_error is captured, never persisted), and
``state['viewer']`` / ``state['members']`` / ``state['nested']`` can be changed between calls.
"""

from __future__ import annotations

import streamlit as st

import app.config as cfg


def dsa_rows(*pairs: tuple[str, str]) -> list[dict]:
    """SHOW GRANTS OF ROLE rows for the admin role: (granted_to, grantee_name) -> one row each."""
    return [{"created_on": "2026-10-07", "role": cfg.ADMIN_ACCESS_ROLE, "granted_to": kind,
             "grantee_name": name, "granted_by": "SNOW_ACCOUNTADMINS"} for kind, name in pairs]


def script_dsa_roster(monkeypatch, *, viewer: str, members: tuple[str, ...] = ("DSA_PERSON1",),
                      nested: tuple[str, ...] = (), raise_: BaseException | None = None) -> dict:
    """Make ``viewer`` an identified SiS viewer whose admin-role lookup answers ``members`` / ``nested``."""
    import app.core.errors as errors_mod
    import app.core.identity as ident
    import app.core.session as sess

    st.session_state.clear()
    state: dict = {"viewer": viewer, "members": tuple(members), "nested": tuple(nested), "raise": raise_,
                   "lookups": 0, "errors": []}

    def _lookup() -> list[dict]:
        state["lookups"] += 1
        if state["raise"] is not None:
            raise state["raise"]
        return dsa_rows(*(("USER", m) for m in state["members"]), *(("ROLE", r) for r in state["nested"]))

    monkeypatch.setattr(ident, "viewer_name", lambda: state["viewer"])
    monkeypatch.setattr(sess, "is_sis", lambda: True)
    monkeypatch.setattr(sess, "_admin_role_rows", _lookup)
    monkeypatch.setattr(errors_mod, "record_error",
                        lambda page, exc, context="": state["errors"].append((page, exc, context)) or "ref")
    return state
