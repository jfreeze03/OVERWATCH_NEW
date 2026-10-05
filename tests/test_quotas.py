"""Locks for app/logic/quotas.py — per-user AI-quota block-history normalization.

The QUOTA_ACCESS_BLOCK_HISTORY view's SQL-reference page 404s; its real columns (ACTION_AT, QUOTA_NAME, USER_NAME,
CYCLE, ACTION, PER_USER_LIMIT, CREDITS, BLOCKED_UNTIL, ...) come from the owner's Snowsight preview (v4.601.1).
block_history binds them at runtime, keeps older guessed spellings as fallbacks, derives IS_ACTIVE from
BLOCKED_UNTIL on the account clock (or, on an older shape, from a release timestamp), and falls back to the raw
frame when nothing maps (never a blank one).

Next-Fifty #37b (bottom half): the review-only quota recommender (user_day_totals,
runaway_days, recommend_quotas, quota_summary), anomaly.robust_z_vs_history, and the
Chargeback & AI panel that renders them without a new read. The SQL-arm parity of
runaway_days lives in tests/test_ai_runaway_parity.py.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.logic import anomaly, quotas
from app.logic.quotas import block_history
from tests._source import read


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
    # review r1: the read also takes the last BLOCK_STATE_DAYS for "currently blocked", marking the window's rows
    assert cortex_sql.BLOCK_STATE_DAYS == 32
    assert "SELECT *, (ACTION_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS IN_WINDOW" in sql
    assert ("WHERE (ACTION_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())) "
            "OR ACTION_AT >= DATEADD('day', -32, CURRENT_TIMESTAMP())") in sql
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
    # the window marks IN_WINDOW; the 32-day state read is added whatever the bounds
    assert ") AS IN_WINDOW" in b and "OR ACTION_AT >= DATEADD('day', -32, CURRENT_TIMESTAMP())" in b


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


def test_an_unblock_on_one_quota_never_ends_a_block_on_another():
    """IS_ACTIVE is 'the user's latest action ON THAT QUOTA' (merge review): a DAILY reset leaves the MONTHLY block
    in force. Grouping on USER alone would drop it."""
    import datetime as dt
    frame = pd.DataFrame([
        _real_row(QUOTA_NAME="AI_MONTHLY", QUOTA_ID=311, CYCLE="MONTHLY",
                  BLOCKED_UNTIL=pd.Timestamp("2026-10-01T00:00", tz="UTC")),
        _real_row(ACTION_AT=pd.Timestamp("2026-09-21T18:30:00", tz="UTC"), ACTION="UNBLOCKED"),
    ])
    out, _ = block_history(frame, now=dt.datetime(2026, 9, 21, 15, 0))
    active = out.set_index(["QUOTA", "ACTION"])["IS_ACTIVE"]
    assert bool(active.loc[("AI_MONTHLY", "BLOCKED")]) and not bool(active.loc[("AI_USER_USAGE", "UNBLOCKED")])


def test_quota_panel_never_reports_no_blocks_when_the_read_failed():
    """v4.601.1: the probe read's failure used to fall through to the clean 'no blocks' state plus a claim that no
    quota was enforcing. A failed read now says so, with no early return (review r1: wave 4 renders a table after
    both branches), and an absent view is a setup state, not a red error."""
    src = read("app/ui/pages/cost_parts/ai_chargeback.py")
    body = src.split("def _ai_quota_panel(", 1)[1].split("\ndef ", 1)[0]
    fail = body.index("if not blk.ok:")
    assert fail < body.index("block_history(blk.df, now=account_now())") < body.index('empty_state("clean"')
    branch = body[fail:body.index("block_history(blk.df")]
    assert 'empty_state("unavailable"' in branch and 'empty_state("needs_setup"' in branch
    assert "is_setup_absence(blk.error_kind)" in branch and "return" not in branch
    assert "detail=blk.error" in branch
    assert "No per-user AI credit quota is enforcing here" not in body
    assert 'f"{block_events(in_win):,}{_plus}"' in body


# =====================================================================================================
# #37b: robust_z_vs_history
# =====================================================================================================
def test_robust_z_vs_history_uses_mad_then_mean_ad_then_the_no_dispersion_sentinel():
    hist = [1.0, 2.0, 3.0, 4.0, 10.0]                     # median 3, |dev| 2,1,0,1,7 -> MAD 1
    assert anomaly.robust_z_vs_history(13.0, hist) == pytest.approx(0.6745 * 10 / 1)
    # MAD 0 (most points equal) -> mean absolute deviation around the median (0.7979)
    flat = [5.0, 5.0, 5.0, 5.0, 7.0]                      # median 5, |dev| 0,0,0,0,2 -> MAD 0, mean-AD 0.4
    assert anomaly.robust_z_vs_history(9.0, flat) == pytest.approx(0.7979 * 4 / 0.4)
    # no dispersion at all -> 999 above the median, 0 at or below it
    same = [5.0] * 6
    assert anomaly.robust_z_vs_history(5.1, same) == anomaly.NO_DISPERSION_Z == 999.0
    assert anomaly.robust_z_vs_history(5.0, same) == 0.0
    assert anomaly.robust_z_vs_history(1.0, same) == 0.0
    # below the onset floor there is no baseline (None, never 0: the caller owns the onset rule)
    assert anomaly.ROBUST_Z_MIN_HISTORY == 5
    assert anomaly.robust_z_vs_history(50.0, [1.0, 2.0, 3.0, 4.0]) is None
    assert anomaly.robust_z_vs_history(50.0, []) is None
    # NaN / None history points are dropped before the count; a junk value is no score
    assert anomaly.robust_z_vs_history(50.0, [1.0, 2.0, 3.0, 4.0, None, float("nan")]) is None
    assert anomaly.robust_z_vs_history(float("nan"), hist) is None
    assert anomaly.robust_z_vs_history("x", hist) is None     # type: ignore[arg-type]
    # the constants are the V150 / robust_zscores engine
    assert (anomaly._MAD_K, anomaly._MEANAD_K) == (0.6745, 0.7979)


def test_robust_z_vs_history_never_scores_the_value_into_its_own_baseline():
    hist = [10.0, 11.0, 12.0, 13.0, 14.0]                 # median 12, MAD 1
    alone = anomaly.robust_z_vs_history(40.0, hist)
    assert alone == pytest.approx(0.6745 * 28 / 1)
    # robust_zscores scores a point WITH itself in the series: a different (smaller) number
    with_self = float(anomaly.robust_zscores(pd.Series([*hist, 40.0])).iloc[-1])
    assert alone is not None and alone > with_self


# =====================================================================================================
# #37b: knobs and user_day_totals
# =====================================================================================================
@pytest.mark.parametrize(("raw", "cap"), [("15.0", 15.0), (12, 12.0), ("0", 15.0), ("-3", 15.0),
                                          ("abc", 15.0), (None, 15.0), (float("nan"), 15.0), ("7.5", 7.5)])
def test_effective_cap_mirrors_the_arm_fallbacks(raw, cap):
    assert quotas.effective_cap(raw) == cap


@pytest.mark.parametrize(("raw", "z"), [("3.5", 3.5), ("2", 2.0), ("0", 0.0), ("x", 3.5), (None, 3.5)])
def test_effective_z_min_mirrors_the_arm_fallback(raw, z):
    assert quotas.effective_z_min(raw) == z


def _ud(rows) -> pd.DataFrame:
    return pd.DataFrame([{"USER_NAME": u, "SOURCE": s, "USAGE_DATE": d, "CREDITS": c} for u, s, d, c in rows])


_T = date(2026, 9, 29)


def _d(k: int) -> date:
    return _T - timedelta(days=k)


def test_user_day_totals_collapses_sources_and_drops_non_users():
    ud = _ud([
        ("ALICE", "Snowsight", _d(1), 4.0), ("ALICE", "CLI", _d(1), 6.0),       # one day, two sources
        ("ALICE", "CLI", _d(2), 0.0),                                          # zero day: not active
        ("ACCOUNT", "Functions", _d(1), 99.0),                                 # the mart's Functions row
        ("UNKNOWN", "CLI", _d(1), 5.0), ("UNKNOWN (4242)", "CLI", _d(1), 5.0),  # mart + live no-match
        ("ACCOUNTING_SVC", "CLI", _d(1), 3.0),                                 # a REAL user, not a prefix drop
        ("BOB", "Functions", _d(1), 7.0),                                      # Functions naming a user
        ("BOB", "CLI", "not a date", 1.0),
    ])
    out = quotas.user_day_totals(ud)
    assert list(out.columns) == quotas.TOTALS_COLS
    got = {(r.USER_NAME, r.DAY): r.CREDITS for r in out.itertuples()}
    assert got == {("ACCOUNTING_SVC", _d(1)): 3.0, ("ALICE", _d(1)): 10.0}
    assert all(isinstance(d, date) for d in out["DAY"])
    # the AI_RUNAWAY_INCLUDE_FUNCTIONS switch: a Functions row counts only when it names a user
    on = quotas.user_day_totals(ud, include_functions=True)
    assert ("BOB", _d(1)) in {(r.USER_NAME, r.DAY) for r in on.itertuples()}
    assert "ACCOUNT" not in set(on["USER_NAME"])


@pytest.mark.parametrize("frame", [None, pd.DataFrame(), pd.DataFrame({"USER_NAME": ["A"]})])
def test_empty_or_unusable_input_gives_empty_frames_with_columns(frame):
    assert list(quotas.user_day_totals(frame).columns) == quotas.TOTALS_COLS
    rec = quotas.recommend_quotas(frame, 15.0, 2.2, today=_T)
    assert rec.empty and list(rec.columns) == quotas.QUOTA_COLS
    run = quotas.runaway_days(quotas.user_day_totals(frame), 15.0, today=_T)
    assert run.empty and list(run.columns) == quotas.RUNAWAY_COLS
    assert quotas.quota_summary(rec, 15.0)["users"] == 0
    assert quotas.quota_summary(None, 15.0)["backtest_usd_over"] == 0.0


# =====================================================================================================
# #37b: runaway_days (the arm's twin; executed SQL parity is in test_ai_runaway_parity.py)
# =====================================================================================================
def _steady(user: str, credits: float, ks, source: str = "CLI"):
    return [(user, source, _d(k), credits + (0.5 if k % 2 else 0.0)) for k in ks]


def test_runaway_needs_both_legs_and_scores_only_the_last_three_complete_days():
    rows = (_steady("BOB", 12.0, range(5, 60))                    # a ~12/day habit
            + [("BOB", "CLI", _d(2), 70.0)]                        # > 30 AND a huge z -> raised
            + [("BOB", "CLI", _d(4), 90.0)]                        # outside [today-3, today): never scored
            + [("BOB", "CLI", _d(0), 95.0)]                        # today: partial, never scored
            + [("HEAVY", "CLI", _d(k), (20.0, 24.0, 26.0, 28.0, 32.0)[k % 5]) for k in range(5, 60)]
            + [("HEAVY", "CLI", _d(1), 31.0)]                      # > 30 but no z outlier -> held
            + [("EVE", "CLI", _d(3), 35.0)])                       # onset, oldest scored day -> raised
    got = quotas.runaway_days(quotas.user_day_totals(_ud(rows)), 15.0, today=_T)
    keyed = {(r.USER_NAME, r.DAY): r for r in got.itertuples()}
    assert set(keyed) == {("BOB", _d(2)), ("EVE", _d(3))}
    bob, eve = keyed[("BOB", _d(2))], keyed[("EVE", _d(3))]
    assert not bob.NO_BASELINE and bob.ROBUST_Z >= quotas.RUNAWAY_ROBUST_Z
    assert eve.NO_BASELINE and math.isnan(eve.ROBUST_Z) and eve.BASELINE_DAYS == 0
    metric = bob.CAP_MULTIPLE                                      # the event's METRIC_VALUE, 4 dp
    assert metric == round(70.0 / 15.0, 4)
    # the multiple is strict: exactly 2x the cap never raises; z_min 0 lets the habitual user through
    exact = quotas.runaway_days(quotas.user_day_totals(_ud([("X", "CLI", _d(1), 30.0)])), 15.0, today=_T)
    assert exact.empty
    loose = quotas.runaway_days(quotas.user_day_totals(_ud(rows)), 15.0, z_min=0.0, today=_T)
    assert ("HEAVY", _d(1)) in {(r.USER_NAME, r.DAY) for r in loose.itertuples()}


def test_runaway_baseline_is_the_prior_ninety_days_only():
    # 5 active days 91+ days before the scored day do not count -> still no baseline (onset)
    rows = [("OLD", "CLI", _d(1 + 91 + k), 5.0) for k in range(6)] + [("OLD", "CLI", _d(1), 40.0)]
    got = quotas.runaway_days(quotas.user_day_totals(_ud(rows)), 15.0, today=_T)
    assert bool(got.iloc[0]["NO_BASELINE"]) and int(got.iloc[0]["BASELINE_DAYS"]) == 0
    # the same days inside the window form a (flat) baseline -> the no-dispersion sentinel
    rows = [("NEW", "CLI", _d(2 + k), 5.0) for k in range(6)] + [("NEW", "CLI", _d(1), 40.0)]
    got = quotas.runaway_days(quotas.user_day_totals(_ud(rows)), 15.0, today=_T)
    assert got.iloc[0]["ROBUST_Z"] == anomaly.NO_DISPERSION_Z and int(got.iloc[0]["BASELINE_DAYS"]) == 6


# =====================================================================================================
# #37b: recommend_quotas
# =====================================================================================================
def test_p95_is_ceiled_to_a_whole_credit_and_never_bumped_by_float_fuzz():
    rows = [("U", "CLI", _d(k), float(k)) for k in range(1, 21)]          # 1..20 credits, 20 active days
    rec = quotas.recommend_quotas(_ud(rows), 15.0, 2.2, today=_T).set_index("USER_NAME")
    assert rec.loc["U", "P95_DAILY_CREDITS"] == round(float(np.quantile(range(1, 21), 0.95)), 2) == 19.05
    assert rec.loc["U", "SUGGESTED_DAILY_CREDITS"] == 20.0
    flat = [("F", "CLI", _d(k), 0.1 * 70) for k in range(1, 21)]          # 7.000000000000001
    rec = quotas.recommend_quotas(_ud(flat), 15.0, 2.2, today=_T).set_index("USER_NAME")
    assert rec.loc["F", "SUGGESTED_DAILY_CREDITS"] == 7.0


def test_below_min_active_days_the_suggestion_and_backtest_are_nan_but_counts_are_facts():
    rows = [("THIN", "CLI", _d(k), 20.0 if k == 1 else 3.0) for k in range(1, 10)]   # 9 active days
    rec = quotas.recommend_quotas(_ud(rows), 15.0, 2.2, today=_T).set_index("USER_NAME")
    for col in ("P95_DAILY_CREDITS", "SUGGESTED_DAILY_CREDITS", "SUGGESTED_MONTHLY_CREDITS",
                "BACKTEST_DAYS", "BACKTEST_DAYS_OVER", "BACKTEST_CREDITS_OVER", "BACKTEST_USD_OVER"):
        assert math.isnan(rec.loc["THIN", col]), col
    assert rec.loc["THIN", "ACTIVE_DAYS"] == 9 and rec.loc["THIN", "DAYS_OVER_CAP"] == 1
    summary = quotas.quota_summary(rec.reset_index(), 15.0)
    assert summary["users"] == 1 and summary["with_history"] == 0


def test_days_over_cap_is_strict_and_the_history_is_the_fixed_ninety_complete_days():
    rows = ([("U", "CLI", _d(k), 15.0) for k in range(1, 11)]             # AT the cap: not over
            + [("U", "CLI", _d(11), 15.01)]                               # over
            + [("U", "CLI", _d(0), 99.0)]                                 # today: partial, excluded
            + [("U", "CLI", _d(91), 99.0), ("U", "CLI", _d(200), 99.0)])  # older than 90 days: excluded
    rec = quotas.recommend_quotas(_ud(rows), 15.0, 2.2, today=_T).set_index("USER_NAME")
    assert rec.loc["U", "DAYS_OVER_CAP"] == 1 and rec.loc["U", "ACTIVE_DAYS"] == 11
    assert quotas.QUOTA_LOOKBACK_DAYS == 90
    # a user active only before the history has no row (not a current AI user)
    gone = quotas.recommend_quotas(_ud([("GONE", "CLI", _d(120 + k), 5.0) for k in range(20)]),
                                   15.0, 2.2, today=_T)
    assert gone.empty


def test_monthly_suggestion_is_the_p95_of_zero_filled_rolling_thirty_day_totals():
    rng = np.random.default_rng(3)
    rows = [("M", "CLI", _d(k), float(rng.integers(1, 9))) for k in range(1, 91) if rng.random() < 0.6]
    rec = quotas.recommend_quotas(_ud(rows), 15.0, 2.2, today=_T).set_index("USER_NAME")
    by_day = {d: c for _, _, d, c in rows}
    grid = [_T - timedelta(days=90) + timedelta(days=k) for k in range(90)]     # idle days are 0
    sums = [sum(by_day.get(grid[i + j], 0.0) for j in range(30)) for i in range(90 - 30 + 1)]
    assert len(sums) == 61
    assert rec.loc["M", "SUGGESTED_MONTHLY_CREDITS"] == math.ceil(round(float(np.quantile(sums, 0.95)), 6))


def test_walk_forward_backtest_has_no_look_ahead():
    """Ten prior days at 5 then a 50-credit spike: the spike day is judged against the limit its PRIOR days
    set (ceil p95 of ten 5s = 5), so it is over by 45. With leakage (the spike inside its own window) the
    limit would be ceil(np.quantile([5]*10 + [50], 0.95)) = 28 and the overage only 22."""
    rows = [("S", "CLI", _d(k), 5.0) for k in range(2, 12)] + [("S", "CLI", _d(1), 50.0)]
    rec = quotas.recommend_quotas(_ud(rows), 15.0, 2.2, today=_T).set_index("USER_NAME")
    leaky = math.ceil(float(np.quantile([5.0] * 10 + [50.0], 0.95)))
    assert leaky == 28
    assert rec.loc["S", "BACKTEST_DAYS"] == 1              # only the spike day has 10 prior active days
    assert rec.loc["S", "BACKTEST_DAYS_OVER"] == 1
    assert rec.loc["S", "BACKTEST_CREDITS_OVER"] == 45.0
    assert rec.loc["S", "BACKTEST_USD_OVER"] == round(45.0 * 2.2, 2)
    # a LATER day never changes an earlier day's verdict
    later = rows + [("S", "CLI", _d(k), 500.0) for k in (0,)]            # today: excluded anyway
    again = quotas.recommend_quotas(_ud(later), 15.0, 2.2, today=_T).set_index("USER_NAME")
    assert again.loc["S", "BACKTEST_CREDITS_OVER"] == 45.0


def test_runaway_days_column_is_the_rule_replayed_over_the_history_and_rows_sort_by_p95():
    rows = [*_steady("BOB", 12.0, range(5, 90)), ("BOB", "CLI", _d(40), 80.0), ("BOB", "CLI", _d(2), 70.0),
            *_steady("CAROL", 2.0, range(1, 90))]
    ud = _ud(rows)
    rec = quotas.recommend_quotas(ud, 15.0, 2.2, today=_T)
    replay = quotas.runaway_days(quotas.user_day_totals(ud), 15.0, today=_T, scored_days=90)
    assert rec.set_index("USER_NAME")["RUNAWAY_DAYS"].to_dict() == {
        "BOB": int((replay["USER_NAME"] == "BOB").sum()), "CAROL": 0}
    assert int((replay["USER_NAME"] == "BOB").sum()) == 2
    assert list(rec["USER_NAME"]) == ["BOB", "CAROL"]                   # P95 descending
    assert rec["ACTIVE_DAYS"].dtype.kind == "i" and rec["RUNAWAY_DAYS"].dtype.kind == "i"
    s = quotas.quota_summary(rec, 15.0)
    assert s["runaway_days"] == 2 and s["runaway_users"] == 1 and s["users"] == 2 and s["with_history"] == 2


# =====================================================================================================
# #37b: the Chargeback & AI panel (no new read, fixed history, honest captions)
# =====================================================================================================
_CB = "app/ui/pages/cost_parts/ai_chargeback.py"


def _fn(src: str, name: str) -> str:
    i = src.index(f"def {name}(")
    j = src.find("\ndef ", i + 5)
    return src[i:j if j > 0 else len(src)]


def test_panel_source_locks():
    src = read(_CB)
    assert "No per-user AI credit quota is enforcing here" not in src
    panel, table = _fn(src, "_ai_quota_panel"), _fn(src, "_suggested_quota_table")
    assert "recommend_quotas(user_daily, cap_credits, ai_rate, today=account_today(), z_min=z_min)" in panel
    assert len(re.findall(r"\brun\(", panel)) == 1 and "run(cortex_sql.quota_access_block_history(" in panel
    for banned in ("run(", "run_mart", "execute_statement", "SNOWFLAKE.CORE.QUOTA", "CREATE ", "ACCOUNT_USAGE"):
        assert banned not in table, banned
    assert not re.search(r"CREATE\s+(OR\s+REPLACE\s+)?(SNOWFLAKE\.CORE\.)?QUOTA", src)
    assert "_suggested_quota_table(rec, enriched, cap_credits, z_min)" in panel
    # the old early return after the blocks branch is gone: the suggestions render after EITHER branch
    assert panel.index("_suggested_quota_table(") > panel.index('    elif blk.ok or blk.error_kind == "absent":')
    assert "\n    _suggested_quota_table(rec, enriched, cap_credits, z_min)\n" in panel   # not inside a branch
    tab = _fn(src, "_ai_users_tab")
    assert "user_daily=(live_res.df if live_res is not None else None)" in tab
    assert 'effective_cap(settings.get("COCO_DAILY_CAP_CREDITS"))' in tab
    assert 'effective_z_min(settings.get("AI_RUNAWAY_ROBUST_Z"))' in tab
    assert "_token_economics_panel(company, days, _coco_cap, bounds=bounds)" in tab
    assert '"**Suggested per-user AI quotas (review only)**"' in table
    assert "-day history (the last {QUOTA_LOOKBACK_DAYS} complete" in table


class _FakeSt:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def markdown(self, text, *_a, **_k):
        self.calls.append(("markdown", str(text)))

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


_SUGGEST_NEEDS_LIVE = "Suggested quotas need the live Cortex Code user-day scan"
_SUGGEST_HEADING = "**Suggested per-user AI quotas (review only)**"


def _ok(df, truncated=False):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, truncated=truncated, error="", error_kind="")


def _failed(kind):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), truncated=False, error="boom", error_kind=kind)


def _block_states(seen) -> list[tuple[str, str]]:
    """The blocks half's empty states: the suggestions table below adds its own when user_daily is None."""
    return [(k, m) for k, m in seen["empty"] if not m.startswith(_SUGGEST_NEEDS_LIVE)]


def _render(monkeypatch, result=None, *, user_daily=None, bounds=None, now=None, applied=True, days=7,
            alerts=True):
    """The whole panel with fakes: ONE block read returning ``result`` (default: no rows), then the #37b suggestions
    over ``user_daily``. Clocks pinned: account_today = _T (the suggestions), account_now (the live-block test)."""
    import datetime as dt

    from app.ui.pages.cost_parts import ai_chargeback as cb
    fake = _FakeSt()
    seen: dict = {"run": 0, "kpis": [], "tables": [], "empty": []}
    res = result if result is not None else _ok(pd.DataFrame())

    def fake_run(*_a, **_k):
        seen["run"] += 1
        return res

    monkeypatch.setattr(cb, "st", fake)
    monkeypatch.setattr(cb, "run", fake_run)
    monkeypatch.setattr(cb, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(cb, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    monkeypatch.setattr(cb, "styled_table", lambda df, **k: seen["tables"].append((k.get("slug"), df)))
    monkeypatch.setattr(cb, "empty_state", lambda kind, msg, *_a, **k: (
        seen["empty"].append((kind, msg)), seen.setdefault("detail", []).append(k.get("detail"))))
    monkeypatch.setattr(cb, "with_user_names", lambda df, *_a, **_k: df)
    monkeypatch.setattr(cb, "has_migration", lambda v, _p: applied and v == 163)
    monkeypatch.setattr(cb, "can_open", lambda page: alerts or page != "Alerts")    # alerts=False: MONITOR
    monkeypatch.setattr(cb, "account_today", lambda: _T)
    monkeypatch.setattr(cb, "account_now", lambda: now or dt.datetime(2026, 9, 21, 13, 0))
    enriched = pd.DataFrame({"USER_NAME": ["BOB", "CAROL"], "DISPLAY_NAME": ["Bob B", "Carol C"],
                             "SPEND_USD": [300.0, 20.0]})
    cb._ai_quota_panel(enriched, {"spend_usd": 320.0, "active_users": 2}, days, bounds=bounds,
                       user_daily=user_daily, cap_credits=15.0, ai_rate=2.2, z_min=3.5)
    seen["kpi"] = {k["label"]: k for k in (seen["kpis"][0] if seen["kpis"] else [])}
    return fake, seen


def _frame_for_ui() -> pd.DataFrame:
    rows = [*_steady("BOB", 12.0, range(5, 90)), ("BOB", "CLI", _d(2), 70.0),
            *_steady("CAROL", 2.0, range(1, 90)), ("DAVE", "CLI", _d(3), 1.0)]
    return _ud(rows)


def test_panel_renders_suggestions_with_one_read_and_a_fixed_history_caption(monkeypatch):
    fake, seen = _render(monkeypatch, user_daily=_frame_for_ui(), days=30)
    assert seen["run"] == 1                                            # only the block-history read
    assert ("clean", "No per-user AI-quota blocks in the last 30 days.") in seen["empty"]
    caps = fake.text("caption")
    assert "No per-user AI credit quota is enforcing here" not in caps
    assert "AI exposure:" in caps and "Suggested per-user limits are in the table below." in caps
    assert "walk-forward back-test" in caps and "\\$" in caps               # md_dollars at the sink
    assert "**Suggested per-user AI quotas (review only)**" in fake.text("markdown")
    ((slug, table),) = seen["tables"]
    assert slug == "ai-quota-suggestions"
    assert list(table.columns) == ["USER_NAME", "DISPLAY_NAME", *quotas.QUOTA_COLS[1:]]
    assert dict(zip(table["USER_NAME"], table["DISPLAY_NAME"], strict=True)) == {
        "BOB": "Bob B", "CAROL": "Carol C", "DAVE": "DAVE"}
    ((kpis),) = seen["kpis"]
    assert [k["label"] for k in kpis] == ["Users with 10+ active days", "p95 day above the daily cap",
                                          "Runaway-rule days, 90d", "Back-test: USD above the limits"]
    assert kpis[0]["value"] == "2 of 3" and kpis[2]["value"] == "1"
    assert "90-day history (the last 90 complete days, not the page Window)" in caps
    assert "seed multiple 2x; the live rule may be tuned in Alerts > Rules." in caps
    assert "Review only" in caps and "creates no quota" in caps


def test_panel_before_v163_says_the_rule_is_not_installed_yet(monkeypatch):
    for alerts in (True, False):
        fake, _ = _render(monkeypatch, user_daily=_frame_for_ui(), applied=False, alerts=alerts)
        caps = fake.text("caption")
        assert "the rule itself arrives with migration V163" in caps and "Alerts > Rules" not in caps


def test_panel_for_a_viewer_without_alerts_names_who_tunes_the_rule(monkeypatch):
    # v4.610.0 final review: MONITOR opens Cost Intelligence but not Alerts, so the caption names who tunes the
    # rule instead of pointing at a page it cannot open
    fake, _ = _render(monkeypatch, user_daily=_frame_for_ui(), alerts=False)
    caps = fake.text("caption")
    assert "seed multiple 2x; an OVERWATCH admin may tune the live rule." in caps
    assert "Alerts" not in caps


def test_panel_on_the_fact_fallback_leg_names_the_missing_live_scan(monkeypatch):
    fake, seen = _render(monkeypatch, user_daily=None)
    assert seen["run"] == 1 and seen["tables"] == [] and seen["kpis"] == []
    assert any(kind == "needs_setup" and "live Cortex Code user-day scan" in msg for kind, msg in seen["empty"])
    assert "held back" not in fake.text("caption")


def test_suggestions_render_after_the_blocks_branch_too(monkeypatch):
    blocks = pd.DataFrame([{"USER_NAME": "BOB", "QUOTA_NAME": "AI_CAP", "BLOCKED_ON": "2026-09-20",
                            "RELEASED_ON": None}])
    _, seen = _render(monkeypatch, _ok(blocks), user_daily=_frame_for_ui())
    assert [slug for slug, _ in seen["tables"]] == ["ai-quota-blocks", "ai-quota-suggestions"]
    assert len(seen["kpis"]) == 2


def test_panel_absent_view_is_a_setup_state_and_still_states_the_exposure(monkeypatch):
    fake, seen = _render(monkeypatch, _failed("absent"))
    kinds = [k for k, _ in _block_states(seen)]
    assert kinds == ["needs_setup"] and "clean" not in kinds
    assert "AI exposure:" in fake.text("caption") and not seen["tables"]
    assert _SUGGEST_HEADING in fake.text("markdown")                  # the suggestions still render


def test_panel_privilege_error_is_a_setup_state_without_the_exposure(monkeypatch):
    """v4.605 review r3: an "Insufficient privileges" error is a setup state like an absent view (the view exists,
    so the blocks are unknown): needs_setup, never clean, and no exposure line (that is for a view not enabled)."""
    fake, seen = _render(monkeypatch, _failed("privilege"))
    assert [k for k, _ in _block_states(seen)] == ["needs_setup"]
    assert "AI exposure:" not in fake.text("caption") and not seen["tables"]
    assert _SUGGEST_HEADING in fake.text("markdown")


def test_panel_failed_read_is_unavailable_and_never_clean(monkeypatch):
    for kind in ("missing_column", "timeout", "other"):
        fake, seen = _render(monkeypatch, _failed(kind))
        assert [k for k, _ in _block_states(seen)] == ["unavailable"], kind
        assert seen["detail"][0] == "boom", kind                       # the error text reaches the owner
        assert "AI exposure:" not in fake.text("caption") and not seen["kpis"] and not seen["tables"]
        # no early return: wave 4's suggestions render after a failed block read too
        assert _SUGGEST_HEADING in fake.text("markdown"), kind


def test_panel_computes_the_suggestions_after_a_failed_or_absent_block_read(monkeypatch):
    """Merge review: the heading alone also renders on the rec=None 'needs the live scan' leg, so pass a real
    user-day frame and require the table itself -- a regression that computed rec only on blk.ok fails here."""
    for res in (_failed("absent"), _failed("timeout"), _failed("missing_column")):
        fake, seen = _render(monkeypatch, res, user_daily=_frame_for_ui())
        assert [slug for slug, _ in seen["tables"]] == ["ai-quota-suggestions"], res.error_kind
        assert len(seen["kpis"]) == 1, res.error_kind
        assert not any(m.startswith(_SUGGEST_NEEDS_LIVE) for _, m in seen["empty"]), res.error_kind
        assert fake.text("markdown").count(_SUGGEST_HEADING) == 1, res.error_kind


def test_panel_window_blocks_count_and_the_live_block(monkeypatch):
    fake, seen = _render(monkeypatch, _ok(pd.DataFrame([_real_row(IN_WINDOW=True)])))
    assert seen["run"] == 1
    assert seen["kpi"]["AI-quota blocks (7d)"]["value"] == "1"
    assert seen["kpi"]["Currently blocked"]["value"] == "1" and seen["kpi"]["Currently blocked"]["severity"] == "warn"
    slug, table = seen["tables"][0]
    assert slug == "ai-quota-blocks" and "IN_WINDOW" not in table.columns and "USER_NAME" in table.columns
    assert "No per-user AI credit quota is enforcing here" not in fake.text("caption")
    assert "NOT filtered to this tab's company scope" in fake.text("caption")   # the blocks are account-wide


def test_panel_currently_blocked_counts_distinct_users_not_rows(monkeypatch):
    """One user holding a daily AND a monthly block is one live incident, not two (the R1 fix)."""
    until = pd.Timestamp("2026-10-01T00:00", tz="UTC")
    rows = [_real_row(IN_WINDOW=True, BLOCKED_UNTIL=until),
            _real_row(IN_WINDOW=True, QUOTA_NAME="AI_MONTHLY", QUOTA_ID=311, CYCLE="MONTHLY", BLOCKED_UNTIL=until)]
    _f, seen = _render(monkeypatch, _ok(pd.DataFrame(rows)))
    assert seen["kpi"]["Currently blocked"]["value"] == "1"
    assert seen["kpi"]["Users affected"]["value"] == "1"


def test_panel_last_month_still_shows_a_user_blocked_today(monkeypatch):
    """Review r1: under 'Last month' the window holds only last month's (expired) blocks; a block recorded today is
    outside the window but in the 32-day state read, so Currently blocked is 1, not 0."""
    import datetime as dt
    today = _real_row(IN_WINDOW=False)                                   # blocked Sep 21, until Sep 22 00:00 UTC
    august = _real_row(USER_NAME="QZ1234", IN_WINDOW=True, ACTION_AT=pd.Timestamp("2026-08-14T10:00", tz="UTC"),
                       BLOCKED_UNTIL=pd.Timestamp("2026-08-15T00:00", tz="UTC"))
    _fake, seen = _render(monkeypatch, _ok(pd.DataFrame([today, august])),
                         bounds=(dt.date(2026, 8, 1), dt.date(2026, 9, 1)))
    assert seen["kpi"]["Currently blocked"]["value"] == "1"
    assert seen["kpi"]["Users affected"]["value"] == "1"            # QZ1234, last month
    assert seen["tables"][0][1]["USER_NAME"].tolist() == ["QZ1234"]
    # no block in the window, one in force now: the table lists the live block and says why
    # an expired block earlier this month is in the 32-day state read but not in force: never listed
    old = _real_row(USER_NAME="OLD1", IN_WINDOW=False, ACTION_AT=pd.Timestamp("2026-09-03T10:00", tz="UTC"),
                    BLOCKED_UNTIL=pd.Timestamp("2026-09-04T00:00", tz="UTC"))
    fake2, seen2 = _render(monkeypatch, _ok(pd.DataFrame([today, old])),
                           bounds=(dt.date(2026, 8, 1), dt.date(2026, 9, 1)))
    assert seen2["kpi"]["Currently blocked"]["value"] == "1" and seen2["kpi"]["AI-quota blocks (last month)"]["value"] == "0"
    assert seen2["tables"][0][1]["USER_NAME"].tolist() == ["LE7765"]
    assert "IN_WINDOW" not in seen2["tables"][0][1].columns
    assert "the table lists the blocks still in force" in fake2.text("caption")
    assert "clean" not in [k for k, _ in _block_states(seen2)]


def test_panel_discloses_the_row_cap(monkeypatch):
    fake, seen = _render(monkeypatch, _ok(pd.DataFrame([_real_row(IN_WINDOW=True)]), truncated=True))
    assert seen["kpi"]["AI-quota blocks (7d)"]["value"] == "1+"
    assert seen["kpi"]["Users affected"]["value"] == "1+"
    assert seen["kpi"]["Currently blocked"]["value"] == "1+"
    assert "Only the newest 1,000 block rows were read" in fake.text("caption")


def test_panel_last_month_capped_read_is_never_clean(monkeypatch):
    """Merge review: under 'Last month' the newest rows are this month's state rows, so a capped read can cut
    every last-month row. That is unknown, not 'no blocks'."""
    import datetime as dt
    lm = (dt.date(2026, 8, 1), dt.date(2026, 9, 1))
    expired = _real_row(IN_WINDOW=False, ACTION_AT=pd.Timestamp("2026-09-10T10:00", tz="UTC"),
                        BLOCKED_UNTIL=pd.Timestamp("2026-09-11T00:00", tz="UTC"))
    _f, seen = _render(monkeypatch, _ok(pd.DataFrame([expired]), truncated=True), bounds=lm)
    states = _block_states(seen)
    assert "clean" not in [k for k, _ in states]
    assert states and states[0][0] == "unavailable" and "newest 1,000 block rows" in states[0][1]
    # someone blocked now: the table lists them, the caption says why the window is empty, the count is '1+'
    fake2, seen2 = _render(monkeypatch, _ok(pd.DataFrame([_real_row(IN_WINDOW=False)]), truncated=True), bounds=lm)
    assert seen2["kpi"]["Currently blocked"]["value"] == "1+"
    assert "The newest 1,000 block rows all fall outside last month" in fake2.text("caption")
    assert "No block was recorded in" not in fake2.text("caption")


def test_panel_no_blocks_is_clean_with_a_window_scoped_exposure_caption(monkeypatch):
    fake, seen = _render(monkeypatch, _ok(pd.DataFrame()))
    assert _block_states(seen) == [("clean", "No per-user AI-quota blocks in the last 7 days.")]
    caps = fake.text("caption")
    assert "AI exposure:" in caps and "in the last 7 days" in caps
    assert "No per-user AI credit quota is enforcing here" not in caps


def test_in_window_rows_keeps_the_window_and_drops_the_helper():
    from app.logic.quotas import in_window_rows
    out, _ = block_history(pd.DataFrame([_real_row(IN_WINDOW=True), _real_row(USER_NAME="X", IN_WINDOW=False)]))
    kept = in_window_rows(out)
    assert kept["USER"].tolist() == ["LE7765"] and "IN_WINDOW" not in kept.columns
    older, _ = block_history(pd.DataFrame([_real_row()]))
    assert len(in_window_rows(older)) == 1                           # no IN_WINDOW column: every row
    assert in_window_rows(None).empty
