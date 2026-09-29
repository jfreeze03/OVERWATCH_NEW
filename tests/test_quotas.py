"""Locks for app/logic/quotas.py — per-user AI-quota block-history normalization.

The QUOTA_ACCESS_BLOCK_HISTORY view's columns are undocumented, so block_history
binds them at runtime: maps common spellings, derives IS_ACTIVE from a release
timestamp, and falls back to the raw frame when nothing maps (never a blank one).

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
    assert panel.index("_suggested_quota_table(") > panel.index("    else:\n")
    tab = _fn(src, "_ai_users_tab")
    assert "user_daily=(live_res.df if live_res is not None else None)" in tab
    assert 'effective_cap(settings.get("COCO_DAILY_CAP_CREDITS"))' in tab
    assert 'effective_z_min(settings.get("AI_RUNAWAY_ROBUST_Z"))' in tab
    assert "_token_economics_panel(company, days, _coco_cap, bounds=bounds)" in tab
    assert '"**Suggested per-user AI quotas (review only)**"' in table
    assert "-day history (the last {QUOTA_LOOKBACK_DAYS} complete" in table


class _FakeSt:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def markdown(self, text, **_k) -> None:
        self.calls.append(("markdown", str(text)))

    def caption(self, text, **_k) -> None:
        self.calls.append(("caption", str(text)))

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


def _render(monkeypatch, *, user_daily, blocks=None, applied=True):
    from app.ui.pages.cost_parts import ai_chargeback as cb

    fake, seen = _FakeSt(), {"run": 0, "kpis": [], "tables": [], "empty": []}

    def fake_run(*_a, **_k):
        seen["run"] += 1
        df = blocks if blocks is not None else pd.DataFrame()
        return SimpleNamespace(ok=True, empty=df.empty, df=df)

    monkeypatch.setattr(cb, "st", fake)
    monkeypatch.setattr(cb, "run", fake_run)
    monkeypatch.setattr(cb, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(cb, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    monkeypatch.setattr(cb, "styled_table", lambda df, **k: seen["tables"].append((k.get("slug"), df)))
    monkeypatch.setattr(cb, "empty_state", lambda kind, msg, *_a, **_k: seen["empty"].append((kind, msg)))
    monkeypatch.setattr(cb, "with_user_names", lambda df, *_a, **_k: df)
    monkeypatch.setattr(cb, "has_migration", lambda v, _p: applied and v == 163)
    monkeypatch.setattr(cb, "account_today", lambda: _T)
    enriched = pd.DataFrame({"USER_NAME": ["BOB", "CAROL"], "DISPLAY_NAME": ["Bob B", "Carol C"],
                             "SPEND_USD": [300.0, 20.0]})
    cb._ai_quota_panel(enriched, {"spend_usd": 320.0, "active_users": 2}, 30,
                       user_daily=user_daily, cap_credits=15.0, ai_rate=2.2, z_min=3.5)
    return fake, seen


def _frame_for_ui() -> pd.DataFrame:
    rows = [*_steady("BOB", 12.0, range(5, 90)), ("BOB", "CLI", _d(2), 70.0),
            *_steady("CAROL", 2.0, range(1, 90)), ("DAVE", "CLI", _d(3), 1.0)]
    return _ud(rows)


def test_panel_renders_suggestions_with_one_read_and_a_fixed_history_caption(monkeypatch):
    fake, seen = _render(monkeypatch, user_daily=_frame_for_ui())
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
    fake, _ = _render(monkeypatch, user_daily=_frame_for_ui(), applied=False)
    caps = fake.text("caption")
    assert "the rule itself arrives with migration V163" in caps and "Alerts > Rules" not in caps


def test_panel_on_the_fact_fallback_leg_names_the_missing_live_scan(monkeypatch):
    fake, seen = _render(monkeypatch, user_daily=None)
    assert seen["run"] == 1 and seen["tables"] == [] and seen["kpis"] == []
    assert any(kind == "needs_setup" and "live Cortex Code user-day scan" in msg for kind, msg in seen["empty"])
    assert "held back" not in fake.text("caption")


def test_suggestions_render_after_the_blocks_branch_too(monkeypatch):
    blocks = pd.DataFrame([{"USER_NAME": "BOB", "QUOTA_NAME": "AI_CAP", "BLOCKED_ON": "2026-09-20",
                            "RELEASED_ON": None}])
    _, seen = _render(monkeypatch, user_daily=_frame_for_ui(), blocks=blocks)
    assert [slug for slug, _ in seen["tables"]] == ["ai-quota-blocks", "ai-quota-suggestions"]
    assert len(seen["kpis"]) == 2
