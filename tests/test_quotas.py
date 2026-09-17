"""Locks for app/logic/quotas.py — per-user AI-quota block-history normalization.

The QUOTA_ACCESS_BLOCK_HISTORY view's columns are undocumented, so block_history
binds them at runtime: maps common spellings, derives IS_ACTIVE from a release
timestamp, and falls back to the raw frame when nothing maps (never a blank one).
"""

from __future__ import annotations

import pandas as pd

from app.logic.quotas import block_history


def test_maps_common_columns_and_derives_active():
    df = pd.DataFrame([
        # blocked, not yet released -> active
        {"USER_NAME": "ALICE", "QUOTA_NAME": "AI_CAP", "DOMAIN": "AI FUNCTION",
         "BLOCK_REASON": "MONTHLY_LIMIT", "BLOCKED_ON": "2026-09-16 10:00", "RELEASED_ON": ""},
        # blocked then released -> not active
        {"USER_NAME": "BOB", "QUOTA_NAME": "AI_CAP", "DOMAIN": "AI FUNCTION",
         "BLOCK_REASON": "DAILY_LIMIT", "BLOCKED_ON": "2026-09-10 09:00",
         "RELEASED_ON": "2026-09-11 00:00"},
    ])
    out, mapped = block_history(df)
    assert mapped
    assert list(out.columns) == ["USER", "QUOTA", "DOMAIN", "REASON",
                                 "BLOCKED_ON", "RELEASED_ON", "IS_ACTIVE"]
    assert out.set_index("USER").loc["ALICE", "IS_ACTIVE"] is True or \
        bool(out.set_index("USER").loc["ALICE", "IS_ACTIVE"])
    assert not bool(out.set_index("USER").loc["BOB", "IS_ACTIVE"])
    assert int(out["IS_ACTIVE"].sum()) == 1


def test_active_when_release_is_null():
    df = pd.DataFrame([{"user_name": "X", "quota_name": "Q", "blocked_on": "t", "released_on": None}])
    out, mapped = block_history(df)
    assert mapped and bool(out.iloc[0]["IS_ACTIVE"])


def test_is_active_handles_datetime_nat_the_snowpark_shape():
    """Snowpark returns a NULL TIMESTAMP as pd.NaT inside a datetime64 column, so a
    still-blocked user (no release) arrives as NaT, NOT an empty string. IS_ACTIVE
    must still be True for that row — the whole point of the panel is surfacing live
    blocks. (Regression: _txt('NaT') != '' had inverted this to all-clear.)"""
    df = pd.DataFrame({
        "USER_NAME": ["ALICE", "BOB"],
        "QUOTA_NAME": ["AI_CAP", "AI_CAP"],
        "BLOCKED_ON": pd.to_datetime(["2026-09-16", "2026-09-10"]),
        "RELEASED_ON": pd.to_datetime([None, "2026-09-11"]),  # NaT = still blocked
    })
    out, mapped = block_history(df)
    assert mapped
    assert int(out["IS_ACTIVE"].sum()) == 1
    assert bool(out.set_index("USER").loc["ALICE", "IS_ACTIVE"])      # NaT -> active
    assert not bool(out.set_index("USER").loc["BOB", "IS_ACTIVE"])


def test_no_release_column_means_no_is_active_flag():
    # only user + a start timestamp -> maps, but IS_ACTIVE cannot be derived
    df = pd.DataFrame([{"USER_NAME": "A", "CREATED_ON": "2026-09-16"}])
    out, mapped = block_history(df)
    assert mapped
    assert "USER" in out.columns and "BLOCKED_ON" in out.columns  # created_on -> BLOCKED_ON
    assert "IS_ACTIVE" not in out.columns
    assert "RELEASED_ON" not in out.columns


def test_drifted_columns_fall_back_to_raw_not_blank():
    df = pd.DataFrame([{"WIDGET": 1, "SPROCKET": 2}])
    out, mapped = block_history(df)
    assert mapped is False
    assert list(out.columns) == ["WIDGET", "SPROCKET"]  # raw frame handed back
    assert len(out) == 1


def test_empty_in_empty_out_mapped_true():
    out, mapped = block_history(pd.DataFrame())
    assert mapped and out.empty and "IS_ACTIVE" in out.columns
    out2, mapped2 = block_history(None)
    assert mapped2 and out2.empty


def test_builder_reads_the_block_view_and_windows_on_created_on():
    from app.data import cortex_sql
    sql = cortex_sql.quota_access_block_history(30)
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY" in sql
    assert "CREATED_ON >= DATEADD('day', -30" in sql
    assert "ORDER BY CREATED_ON DESC" in sql
    # days is clamped to >= 1 so a zero/negative window never becomes a future filter
    assert "-1," in cortex_sql.quota_access_block_history(0)


def test_builder_honors_last_month_bounds():
    import datetime as dt

    from app.data import cortex_sql
    b = cortex_sql.quota_access_block_history(30, bounds=(dt.date(2026, 8, 1), dt.date(2026, 9, 1)))
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY" in b
    assert "CREATED_ON" in b
    # the bounded window is not the trailing-days form
    assert "DATEADD('day', -30" not in b
