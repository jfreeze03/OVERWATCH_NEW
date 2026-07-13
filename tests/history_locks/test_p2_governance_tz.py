"""P2 items 14a + 15: governance drift score, display timezone."""

import pandas as pd
import pytest

from app.data import prefs_sql, security_sql
from app.logic.governance import governance_drift


def test_governance_drift_named_deductions():
    clean = governance_drift({})
    assert clean.score == 100 and clean.state == "Healthy" and not clean.drivers

    dirty = governance_drift({
        "mfa_gap_users": 3, "expired_credentials": 1, "expiring_credentials": 2,
        "breakglass_grants_30d": 1, "warehouses_no_monitor": 2,
        "warehouses_no_autosuspend": 1,
    })
    # v4.45: the no-monitor deduction retired with resource monitors
    # (owner runs none): 15 + 8 + 6 + 8 + 3 = 40 -> score 60... the input
    # dict still carries warehouses_no_monitor and it is IGNORED (36 -> 64).
    assert dirty.score == 64 and dirty.state == "Act"
    assert {d.driver for d in dirty.drivers} == {
        "MFA gaps", "Expired credentials", "Expiring credentials",
        "Break-glass grants", "No auto-suspend"}


def test_governance_drift_caps_hold():
    flooded = governance_drift({"mfa_gap_users": 1000})
    assert flooded.score == 75  # capped at 25


def test_governance_counts_builder():
    sql = security_sql.governance_counts()
    for marker in ("MFA_GAP_USERS", "EXPIRED_CREDENTIALS", "BREAKGLASS_GRANTS_30D",
                   "HAS_MFA", "GRANTS_TO_USERS",
                   "ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')"):
        assert marker in sql, marker
    assert "LIMIT" in security_sql.show_warehouses_sql()


def test_display_tz_pref_key_accepted():
    sql = prefs_sql.upsert_pref_sql("DISPLAY_TZ", "UTC")
    assert "'DISPLAY_TZ'" in sql
    with pytest.raises(ValueError):
        prefs_sql.upsert_pref_sql("DISPLAY_TZ2", "UTC")
    assert "Account (America/Chicago)" in prefs_sql.DISPLAY_TIMEZONES


def test_localize_timestamps_is_display_only():
    import streamlit as st

    from app.ui.components import localize_timestamps

    df = pd.DataFrame({"AT": pd.to_datetime(["2026-07-07 12:00:00"]), "X": [1]})
    st.session_state["_ow_display_tz"] = "UTC"
    try:
        out, note = localize_timestamps(df, ["AT"])
        assert "UTC" in note
        assert str(out["AT"].iloc[0]) == "2026-07-07 17:00:00"  # CDT+5
        assert str(df["AT"].iloc[0]) == "2026-07-07 12:00:00"   # original untouched
        st.session_state["_ow_display_tz"] = "Account (America/Chicago)"
        same, note2 = localize_timestamps(df, ["AT"])
        assert note2 == "" and str(same["AT"].iloc[0]) == "2026-07-07 12:00:00"
    finally:
        st.session_state.pop("_ow_display_tz", None)


def test_run_batch_degrades_per_key_without_session():
    """v4.20 contract evolution (was: returns None): the parallel path now
    degrades PER KEY through run() — every key present, failures as
    ok=False results, and it still never raises."""
    from app.core.query import run_batch

    out = run_batch([{"key": "a", "sql": "SELECT 1 AS X"}], page="Test", tier="recent")
    assert out is not None and set(out) == {"a"}   # every key present
    assert out["a"].ok is False                    # no Snowflake session in CI


def test_run_batch_contract_in_source():
    import inspect

    from app.core import query

    src = inspect.getsource(query.run_batch)
    assert "None" in src and "fallback" in src.lower()
    batch_src = inspect.getsource(query._execute_batch)
    assert "block=False" in batch_src            # true server-side async
    # v4.24 evolution (Codex r9 #3): any failure STILL raises — so a partial
    # batch is never cached — but survivors ride out on the exception now
    # instead of being re-executed (duplicated scans were real credits).
    assert "raise _BatchPartial(frames, errors)" in batch_src
    assert "any failure raises" in batch_src or "if errors:" in batch_src
