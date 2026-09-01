"""Robust warehouse-capacity pressure forecasts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

import pandas as pd

from app.logic.formulas import account_today, safe_float

MAX_CAPACITY_STALE_DAYS = 2


@dataclass(frozen=True)
class _ChannelFit:
    """One normalized pressure channel's robust forecast (rec#39)."""

    current: float
    slope: float
    r2: float
    residual_mad: float
    backtest_mae: float | None
    eta: float | None

_OUTPUT_COLUMNS = [
    "WAREHOUSE_NAME",
    "STATUS",
    "DAYS_TO_PRESSURE",
    "ETA_LOW_DAYS",
    "ETA_HIGH_DAYS",
    "CURRENT_PRESSURE_INDEX",
    "SLOPE_PER_DAY",
    "R2",
    "BACKTEST_MAE",
    "WORKLOAD_GROWTH_PCT",
    "RECENT_PRESSURE_DAYS",
    "OBSERVED_DAYS",
    "COVERAGE_PCT",
    "SOURCE_LATEST_DAY",
    "LATEST_DAY",
    "STALE_DAYS",
    "ACTIVITY_GAP_DAYS",
    "BASIS",
]


def _theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float]:
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    slope = float(median(slopes)) if slopes else 0.0
    intercept = float(median([y - slope * x for x, y in zip(xs, ys, strict=True)]))
    return slope, intercept


def _quality(ys: list[float], predicted: list[float]) -> tuple[float, float]:
    if not ys:
        return 0.0, 0.0
    errors = [actual - estimate for actual, estimate in zip(ys, predicted, strict=True)]
    mean_y = sum(ys) / len(ys)
    total = sum((value - mean_y) ** 2 for value in ys)
    residual = sum(error ** 2 for error in errors)
    r2 = 1.0 - residual / total if total > 0 else (1.0 if residual == 0 else 0.0)
    error_median = median(errors)
    mad = float(median([abs(error - error_median) for error in errors]))
    return r2, mad


def _growth_pct(values: pd.Series, window: int = 30) -> float:
    """Recent workload growth: last ``window`` days vs the ``window`` before them.

    rec#39: the old head-vs-tail over the FULL (up to 365d) series read big growth
    for a warehouse whose demand rose months ago and has since gone flat — the
    growth gate then corroborated an ETA that recent demand no longer supports.
    A fixed recent sub-window (default last-30 vs prior-30) only fires on growth
    that is actually happening now.
    """
    numeric = pd.to_numeric(values, errors="coerce").fillna(0.0)
    if len(numeric) < 14:
        return 0.0
    width = min(window, len(numeric) // 2)   # never let the two windows overlap
    prior = float(numeric.iloc[-2 * width:-width].median())
    recent = float(numeric.tail(width).median())
    if prior <= 0:
        return 100.0 if recent > 0 else 0.0
    return (recent - prior) / prior * 100.0


def _holdout_mae(xs: list[float], ys_smooth: list[float], ys_raw: list[float]) -> float | None:
    """7-day holdout MAE: fit on the smoothed train, score against RAW holdout.

    rec#14: the error is measured against the raw pressure the smoothed line
    understates, so the backtest reflects real forecast error, not fit to noise.
    """
    if len(xs) < 30:
        return None
    train_x, train_y = xs[:-7], ys_smooth[:-7]
    if len(train_x) < 20:
        return None
    test_x, test_y = xs[-7:], ys_raw[-7:]
    slope, intercept = _theil_sen(train_x, train_y)
    return sum(
        abs(actual - (intercept + slope * x)) for actual, x in zip(test_y, test_x, strict=True)
    ) / len(test_y)


def _fit_channel(days: pd.Series, raw_norm: pd.Series) -> _ChannelFit:
    """Independently forecast one normalized pressure channel (rec#39).

    ``raw_norm`` is the channel scaled so 1.0 is the intervention line (queue
    minutes / 30, or remote-spill GB / 1). Fitting the queue and spill channels
    separately — rather than one Theil-Sen slope through their max()-composite,
    which is non-physical when the two cross — lets the caller take the sooner,
    correctly-attributed ETA. Trend fit on the 7-day median, quality against the
    RAW channel (rec#14). ``eta`` is days for the smoothed level to reach 1.0.
    """
    smooth = raw_norm.rolling(7, min_periods=4).median()
    keep = smooth.notna()
    ys = [safe_float(value) for value in smooth[keep]]
    if len(ys) < 2:
        return _ChannelFit(current=max(0.0, ys[-1]) if ys else 0.0, slope=0.0,
                           r2=0.0, residual_mad=0.0, backtest_mae=None, eta=None)
    origin = days[keep].iloc[0]
    xs = [float((day - origin).days) for day in days[keep]]
    ys_raw = [safe_float(value) for value in raw_norm[keep]]
    slope, intercept = _theil_sen(xs, ys)
    predicted = [intercept + slope * x for x in xs]
    r2, residual_mad = _quality(ys_raw, predicted)
    current = max(0.0, ys[-1])
    backtest_mae = _holdout_mae(xs, ys, ys_raw)
    eta = (1.0 - current) / slope if slope > 0 else None
    return _ChannelFit(current=current, slope=slope, r2=r2,
                       residual_mad=residual_mad, backtest_mae=backtest_mae, eta=eta)


def capacity_forecasts(
    frame: pd.DataFrame,
    min_days: int = 30,
    *,
    as_of: date | None = None,
    max_stale_days: int = MAX_CAPACITY_STALE_DAYS,
) -> pd.DataFrame:
    """Forecast days until queue/spill pressure reaches the intervention line.

    The pressure index is max(queue minutes / 30, remote spill GB / 1). An ETA
    is emitted only with dense history, recurring pressure, corroborating
    workload growth, no recent capacity change, and a useful holdout backtest.
    """
    required = {
        "DAY", "WAREHOUSE_NAME", "QUERY_COUNT", "QUEUED_MIN", "SPILL_REMOTE_GB",
        "CREDITS_TOTAL", "CHANGE_EVENTS",
    }
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    anchor = as_of or account_today()
    expected_latest = pd.Timestamp(anchor - timedelta(days=1))
    source_values = (
        pd.to_datetime(frame["SOURCE_LATEST_DAY"], errors="coerce")
        if "SOURCE_LATEST_DAY" in frame.columns
        else pd.to_datetime(frame["DAY"], errors="coerce")
    )
    source_latest = source_values.max()
    source_stale_days = (
        max(0, int((expected_latest - source_latest.normalize()).days))
        if pd.notna(source_latest) else 0
    )
    rows: list[dict[str, object]] = []
    for warehouse, source in frame.groupby("WAREHOUSE_NAME", dropna=False):
        group = source.copy()
        group["DAY"] = pd.to_datetime(group["DAY"], errors="coerce")
        group = group.dropna(subset=["DAY"]).sort_values("DAY").drop_duplicates("DAY", keep="last")
        for column in required - {"DAY", "WAREHOUSE_NAME"}:
            group[column] = pd.to_numeric(group[column], errors="coerce").fillna(0.0)
        observed = len(group)
        calendar_days = ((group["DAY"].max() - group["DAY"].min()).days + 1) if observed else 0
        coverage = observed / calendar_days * 100.0 if calendar_days else 0.0
        latest_day = group["DAY"].max().normalize() if observed else pd.NaT
        activity_gap_days = max(0, int((expected_latest - latest_day).days)) if observed else 0
        base: dict[str, object] = {
            "WAREHOUSE_NAME": str(warehouse),
            "STATUS": "INSUFFICIENT",
            "DAYS_TO_PRESSURE": None,
            "ETA_LOW_DAYS": None,
            "ETA_HIGH_DAYS": None,
            "CURRENT_PRESSURE_INDEX": 0.0,
            "SLOPE_PER_DAY": 0.0,
            "R2": 0.0,
            "BACKTEST_MAE": None,
            "WORKLOAD_GROWTH_PCT": 0.0,
            "RECENT_PRESSURE_DAYS": 0,
            "OBSERVED_DAYS": observed,
            "COVERAGE_PCT": round(coverage, 1),
            "SOURCE_LATEST_DAY": source_latest.date() if pd.notna(source_latest) else None,
            "LATEST_DAY": latest_day.date() if observed else None,
            "STALE_DAYS": source_stale_days,
            "ACTIVITY_GAP_DAYS": activity_gap_days,
            "BASIS": "Needs at least 30 observed complete days with 80% calendar coverage.",
        }
        if source_stale_days > max(0, int(max_stale_days)):
            base.update({
                "STATUS": "STALE",
                "BASIS": (
                    f"Latest complete fact is {source_stale_days} day(s) behind the expected "
                    f"{expected_latest.date()} boundary; refresh telemetry before forecasting."
                ),
            })
            rows.append(base)
            continue
        if activity_gap_days > max(0, int(max_stale_days)):
            base.update({
                "STATUS": "INACTIVE",
                "BASIS": (
                    f"No query activity for this warehouse in {activity_gap_days} day(s); "
                    "an old pressure endpoint is not a current capacity forecast."
                ),
            })
            rows.append(base)
            continue
        if observed < max(30, int(min_days)) or coverage < 80.0:
            rows.append(base)
            continue

        # rec#39: forecast the queue and remote-spill channels INDEPENDENTLY, then
        # let the one that reaches the 1.0 intervention line SOONEST drive the ETA.
        # The old max()-composite fit a single Theil-Sen slope to two series that
        # can cross over the window, yielding a slope — and an ETA — matching
        # neither. Each channel is scaled so 1.0 == its threshold.
        channels = {
            "queue": _fit_channel(group["DAY"], group["QUEUED_MIN"] / 30.0),
            "remote spill": _fit_channel(group["DAY"], group["SPILL_REMOTE_GB"] / 1.0),
        }
        current = max(fit.current for fit in channels.values())   # closest to threshold now
        rising = {name: fit for name, fit in channels.items()
                  if fit.slope > 0 and fit.eta is not None and fit.eta > 0}
        if rising:   # driver = the soonest-to-breach rising channel
            driver_name = min(rising, key=lambda name: rising[name].eta or math.inf)
        else:        # nothing rising — keep the strongest slope for reporting; gates refuse it
            driver_name = max(channels, key=lambda name: channels[name].slope)
        driver = channels[driver_name]
        slope = driver.slope
        r2 = driver.r2
        residual_mad = driver.residual_mad
        backtest_mae = driver.backtest_mae
        driver_current = driver.current

        recent = group.tail(14)
        recent_pressure_days = int(
            ((recent["QUEUED_MIN"] >= 1.0) | (recent["SPILL_REMOTE_GB"] >= 0.05)).sum()
        )
        recent_changes = int(recent["CHANGE_EVENTS"].sum())
        workload_growth = max(_growth_pct(group["QUERY_COUNT"]), _growth_pct(group["CREDITS_TOTAL"]))

        base.update({
            "CURRENT_PRESSURE_INDEX": round(current, 3),
            "SLOPE_PER_DAY": round(slope, 5),
            "R2": round(r2, 3),
            "BACKTEST_MAE": round(backtest_mae, 3) if backtest_mae is not None else None,
            "WORKLOAD_GROWTH_PCT": round(workload_growth, 1),
            "RECENT_PRESSURE_DAYS": recent_pressure_days,
        })
        if current >= 1.0:
            base.update({
                "STATUS": "PRESSURE NOW",
                "DAYS_TO_PRESSURE": 0,
                "ETA_LOW_DAYS": 0,
                "ETA_HIGH_DAYS": 0,
                "BASIS": "The 7-day median already exceeds 30 queue-min/day or 1 GB spill/day.",
            })
        elif recent_changes > 0:
            base.update({
                "STATUS": "UNSTABLE",
                "BASIS": "A capacity setting changed in the last 14 days; wait for a stable post-change baseline.",
            })
        elif recent_pressure_days < 5 or slope <= 0:
            base.update({
                "STATUS": "STABLE",
                "BASIS": "Pressure is not recurring or its robust trend is flat/down; no ETA is justified.",
            })
        elif workload_growth <= 0:
            base.update({
                "STATUS": "WATCH",
                "BASIS": "Pressure trends upward, but query/credit demand does not corroborate the trend.",
            })
        elif backtest_mae is None or r2 < 0.35 or backtest_mae > max(0.20, driver_current * 0.6):
            base.update({
                "STATUS": "WATCH",
                "BASIS": "Upward pressure is visible, but the 7-day holdout error is too high for an ETA.",
            })
        else:
            eta = driver.eta if driver.eta is not None else math.inf
            uncertainty = 1.96 * 1.4826 * residual_mad + backtest_mae
            low = max(0.0, (1.0 - driver_current - uncertainty) / slope)
            high = max(low, (1.0 - driver_current + uncertainty) / slope)
            if 0 < eta <= 180 and high <= 365:
                base.update({
                    "STATUS": "FORECAST",
                    # Report the DRIVER channel's current index here, not the cross-channel
                    # max: the ETA below is derived from driver_current, so a row that showed
                    # a near-1.0 max from a FLAT channel next to a far ETA from a different
                    # rising channel read as self-contradictory (round-2 bug hunt). Now the
                    # shown current, the ETA, and the BASIS all describe the same channel.
                    "CURRENT_PRESSURE_INDEX": round(driver_current, 3),
                    "DAYS_TO_PRESSURE": round(eta),
                    "ETA_LOW_DAYS": round(low),
                    "ETA_HIGH_DAYS": round(high),
                    "BASIS": f"Theil-Sen trend on the {driver_name} channel (7-day median); "
                             "interval includes residual and holdout error.",
                })
            else:
                base.update({
                    "STATUS": "WATCH",
                    "BASIS": "Pressure is rising, but the threshold is outside the reliable 180-day horizon.",
                })
        rows.append(base)

    result = pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)
    rank = {"PRESSURE NOW": 0, "FORECAST": 1, "WATCH": 2, "UNSTABLE": 3,
            "STALE": 4, "STABLE": 5, "INACTIVE": 6, "INSUFFICIENT": 7}
    result["_RANK"] = result["STATUS"].map(rank).fillna(9)
    return result.sort_values(
        ["_RANK", "DAYS_TO_PRESSURE", "CURRENT_PRESSURE_INDEX"],
        ascending=[True, True, False],
        na_position="last",
    ).drop(columns="_RANK").reset_index(drop=True)
