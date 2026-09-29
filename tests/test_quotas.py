"""Locks for app/logic/quotas.py — per-user AI-quota block-history normalization.

The QUOTA_ACCESS_BLOCK_HISTORY view's SQL-reference page 404s; its real columns (ACTION_AT, QUOTA_NAME, USER_NAME,
CYCLE, ACTION, PER_USER_LIMIT, CREDITS, BLOCKED_UNTIL, ...) come from the owner's Snowsight preview (v4.601.1).
block_history binds them at runtime, keeps older guessed spellings as fallbacks, derives IS_ACTIVE from
BLOCKED_UNTIL on the account clock (or, on an older shape, from a release timestamp), and falls back to the raw
frame when nothing maps (never a blank one).
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


def test_builder_reads_the_block_view_and_windows_on_action_at():
    """v4.601.1: the view has no CREATED_ON (the owner's Snowsight run: 'invalid identifier CREATED_ON'); its
    event timestamp is ACTION_AT. The v4.543 reader failed on every account, silently (probe)."""
    from app.data import cortex_sql
    sql = cortex_sql.quota_access_block_history(30)
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY" in sql
    assert "ACTION_AT >= DATEADD('day', -30" in sql
    assert "ORDER BY ACTION_AT DESC" in sql
    assert "CREATED_ON" not in sql
    # days is clamped to >= 1 so a zero/negative window never becomes a future filter
    assert "-1," in cortex_sql.quota_access_block_history(0)


def test_builder_honors_last_month_bounds():
    import datetime as dt

    from app.data import cortex_sql
    b = cortex_sql.quota_access_block_history(30, bounds=(dt.date(2026, 8, 1), dt.date(2026, 9, 1)))
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY" in b
    assert "ACTION_AT" in b and "CREATED_ON" not in b
    # the bounded window is not the trailing-days form
    assert "DATEADD('day', -30" not in b


def test_builder_parses_as_snowflake_sql():
    import sqlglot

    from app.data import cortex_sql
    expr = sqlglot.parse_one(cortex_sql.quota_access_block_history(7), read="snowflake")
    assert expr.find(sqlglot.exp.Column) is not None


# --- v4.601.1: the view's real columns (the owner's Snowsight data preview, 2026-09-29) -------------------------

def _real_row(**over) -> dict:
    """One row exactly as QUOTA_ACCESS_BLOCK_HISTORY returned it on the account (TIMESTAMP_LTZ in UTC)."""
    base = {"ACTION_AT": pd.Timestamp("2026-09-21T17:41:11.000", tz="UTC"), "QUOTA_ID": 310,
            "QUOTA_NAME": "AI_USER_USAGE", "USER_ID": 1555, "USER_NAME": "LE7765", "CYCLE": "DAILY",
            "ACTION": "BLOCKED", "PER_USER_LIMIT": 15.0, "CREDITS": 15.230623640,
            "BLOCKED_UNTIL": pd.Timestamp("2026-09-22T00:00:00.000", tz="UTC")}
    return {**base, **over}


def test_real_view_shape_maps_every_column():
    import datetime as dt
    out, mapped = block_history(pd.DataFrame([_real_row()]), now=dt.datetime(2026, 9, 21, 13, 0))
    assert mapped
    assert list(out.columns) == ["USER", "QUOTA", "CYCLE", "ACTION", "CREDITS", "PER_USER_LIMIT",
                                 "BLOCKED_ON", "BLOCKED_UNTIL", "IS_ACTIVE"]
    row = out.iloc[0]
    assert (row["USER"], row["QUOTA"], row["CYCLE"], row["ACTION"]) == ("LE7765", "AI_USER_USAGE", "DAILY", "BLOCKED")
    assert row["CREDITS"] > row["PER_USER_LIMIT"] == 15.0


def test_a_block_is_active_until_blocked_until_on_the_account_clock():
    """BLOCKED_UNTIL 2026-09-22 00:00 UTC is 2026-09-21 19:00 Central (CDT). ``now`` is account (Central) time."""
    import datetime as dt
    frame = pd.DataFrame([_real_row()])
    during, _ = block_history(frame, now=dt.datetime(2026, 9, 21, 18, 59))
    after, _ = block_history(frame, now=dt.datetime(2026, 9, 21, 19, 1))
    assert bool(during.iloc[0]["IS_ACTIVE"]) and not bool(after.iloc[0]["IS_ACTIVE"])
    # a tz-NAIVE BLOCKED_UNTIL is taken as account time already
    naive = pd.DataFrame([_real_row(BLOCKED_UNTIL=pd.Timestamp("2026-09-21 19:00"))])
    assert bool(block_history(naive, now=dt.datetime(2026, 9, 21, 18, 0))[0].iloc[0]["IS_ACTIVE"])
    assert not bool(block_history(naive, now=dt.datetime(2026, 9, 21, 19, 30))[0].iloc[0]["IS_ACTIVE"])


def test_without_now_a_blocked_until_frame_derives_no_active_flag():
    out, mapped = block_history(pd.DataFrame([_real_row()]))
    assert mapped and "IS_ACTIVE" not in out.columns


def test_a_later_non_block_action_ends_the_block_and_is_not_counted_as_a_block():
    import datetime as dt

    from app.logic.quotas import block_events
    frame = pd.DataFrame([
        _real_row(),
        # an admin reset the same user's quota before the cycle ended
        _real_row(ACTION_AT=pd.Timestamp("2026-09-21T18:30:00", tz="UTC"), ACTION="UNBLOCKED"),
        # another user, still blocked
        _real_row(USER_NAME="QZ1234", USER_ID=77, ACTION_AT=pd.Timestamp("2026-09-21T19:00:00", tz="UTC")),
    ])
    out, _ = block_history(frame, now=dt.datetime(2026, 9, 21, 15, 0))
    active = out.set_index(["USER", "ACTION"])["IS_ACTIVE"]
    assert not bool(active.loc[("LE7765", "BLOCKED")]) and not bool(active.loc[("LE7765", "UNBLOCKED")])
    assert bool(active.loc[("QZ1234", "BLOCKED")])
    assert block_events(out) == 2 and block_events(pd.DataFrame()) == 0
    # an older shape without ACTION counts every row
    assert block_events(pd.DataFrame({"USER": ["A", "B"]})) == 2


def test_quota_panel_never_reports_no_blocks_when_the_read_failed():
    """v4.601.1: the probe read's failure used to fall through to 'No per-user AI-quota blocks' plus 'No per-user AI
    credit quota is enforcing here'. A failed read now says unavailable and stops."""
    from tests._source import read
    src = read("app/ui/pages/cost_parts/ai_chargeback.py")
    body = src.split("def _ai_quota_panel(", 1)[1].split("\ndef ", 1)[0]
    fail = body.index("if not blk.ok:")
    assert fail < body.index("block_history(blk.df, now=account_now())") < body.index('empty_state("clean"')
    branch = body[fail:body.index("block_history(blk.df")]
    assert 'empty_state("unavailable"' in branch and "return" in branch
    assert 'blk.error_kind == "absent"' in branch
    assert 'f"{block_events(blocks):,}"' in body
