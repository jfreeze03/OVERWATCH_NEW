"""rec#15: month-end forecast bands are honest.

Locks the four sub-fixes: a robust trend in the linear engine, a >= 4-week
seasonal floor over a widened baseline, a ddof=1 + inflation band, and a
7-day minimum before any projection is emitted.
"""

from datetime import date, timedelta

import pandas as pd

from app.logic.forecast import _band, month_end_projection


def _flat(days: int, usd: float, end: date) -> pd.DataFrame:
    return pd.DataFrame([{"DAY": end - timedelta(days=i), "USD": usd} for i in range(1, days + 1)])


def _ramp(days: int, start: float, step: float, end: date) -> pd.DataFrame:
    # oldest complete day (`days` back) = `start`, rising `step`/day up to yesterday
    rows = [{"DAY": end - timedelta(days=days - j), "USD": start + step * j}
            for j in range(days)]
    return pd.DataFrame(rows)


# --- minimum history (7-day floor) -----------------------------------------
def test_declines_below_seven_days():
    today = date(2026, 7, 20)
    assert not month_end_projection(_flat(6, 100.0, today), today).ok
    assert month_end_projection(_flat(7, 100.0, today), today).ok


# --- linear engine now carries a robust trend ------------------------------
def test_linear_engine_projects_the_trend_up():
    today = date(2026, 7, 20)
    rising = _ramp(14, start=40.0, step=10.0, end=today)   # 40 -> 170, mean 105
    flat = _flat(14, 105.0, today)                          # identical mean, no slope
    up = month_end_projection(rising, today, engine="linear")
    level = month_end_projection(flat, today, engine="linear")
    assert up.ok and level.ok
    # A flat mean would ignore the climb; the robust slope projects strictly above it.
    assert up.projected_usd > level.projected_usd
    # A perfectly linear history has no residual scatter, so the band is zero —
    # honest: there is nothing uncertain to widen for (noise -> a band; see below).
    assert up.low_usd == up.high_usd == up.projected_usd


def test_downtrend_never_inverts_the_band_or_projects_below_mtd():
    # A steep decline + a normal busy partial today: the robust slope extrapolates
    # remaining spend toward zero, which must NOT drive the point estimate below
    # money already spent this month (month-end is monotonic). Regression for the
    # inverted-interval bug (low > projected > high) a clean downslope produced.
    today = date(2026, 7, 25)
    declining = _ramp(14, start=200.0, step=-12.0, end=today)   # 200 -> ~44, slope -12/day
    rows = declining.to_dict("records")
    rows.append({"DAY": today, "USD": 150.0})                   # today, partial (busy)
    f = month_end_projection(pd.DataFrame(rows), today, engine="linear")
    assert f.ok
    assert f.low_usd <= f.projected_usd <= f.high_usd           # interval never inverts
    assert f.projected_usd >= f.mtd_usd                         # never below spend-to-date


def test_flat_history_keeps_a_zero_band():
    today = date(2026, 7, 20)
    f = month_end_projection(_flat(14, 100.0, today), today, engine="linear")
    assert f.ok and f.low_usd == f.high_usd == f.projected_usd


# --- seasonal floor + widened baseline -------------------------------------
def _weekday_frame(n: int, today: date) -> pd.DataFrame:
    rows = []
    for i in range(n, 0, -1):
        d = today - timedelta(days=i)
        rows.append({"DAY": d, "USD": 20.0 if d.weekday() >= 5 else 100.0})
    return pd.DataFrame(rows)


def test_seasonal_needs_four_weeks():
    today = date(2026, 7, 20)
    thin = month_end_projection(_weekday_frame(21, today), today, engine="seasonal")
    assert thin.ok and "Linear engine" in thin.basis   # 21d < 28 -> falls back
    ok = month_end_projection(_weekday_frame(28, today), today, engine="seasonal")
    assert ok.ok and "Seasonal engine" in ok.basis


def test_seasonal_uses_more_than_two_weeks_of_history():
    today = date(2026, 7, 20)
    fc = month_end_projection(_weekday_frame(35, today), today, engine="seasonal")
    assert fc.ok and "over 35d" in fc.basis   # widened past the old tail(14)


# --- band is wider than the old i.i.d. / ddof=0 band -----------------------
def test_band_inflates_over_iid_and_stays_zero_on_no_residual():
    import math

    resid_std, days, n = 10.0, 16, 14
    iid = resid_std * math.sqrt(days)   # the old band: no parameter/autocorr inflation
    assert _band(resid_std, days, n) > iid
    # a flat or perfectly periodic history (zero residual) has no band
    assert _band(0.0, days, n) == 0.0
    assert _band(resid_std, 0, n) == 0.0


def test_noisy_history_has_a_nondegenerate_band():
    today = date(2026, 7, 20)
    noisy = pd.DataFrame([
        {"DAY": today - timedelta(days=i), "USD": 100.0 + (15.0 if i % 2 else -15.0)}
        for i in range(1, 15)
    ])
    fc = month_end_projection(noisy, today, engine="linear")
    assert fc.ok and fc.high_usd > fc.projected_usd > fc.low_usd
