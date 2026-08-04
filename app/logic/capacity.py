"""Robust warehouse-capacity pressure forecasts."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import median

import pandas as pd

from app.logic.formulas import account_today, safe_float

MAX_CAPACITY_STALE_DAYS = 2

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


def _growth_pct(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").fillna(0.0)
    if len(numeric) < 14:
        return 0.0
    width = min(14, max(7, len(numeric) // 3))
    first = float(numeric.head(width).median())
    last = float(numeric.tail(width).median())
    if first <= 0:
        return 100.0 if last > 0 else 0.0
    return (last - first) / first * 100.0


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

        queue = group["QUEUED_MIN"] / 30.0
        spill = group["SPILL_REMOTE_GB"] / 1.0
        group["PRESSURE_INDEX"] = pd.concat([queue, spill], axis=1).max(axis=1)
        group["PRESSURE_SMOOTH"] = group["PRESSURE_INDEX"].rolling(7, min_periods=4).median()
        model = group.dropna(subset=["PRESSURE_SMOOTH"]).copy()
        xs = [(day - model["DAY"].iloc[0]).days for day in model["DAY"]]
        ys = [safe_float(value) for value in model["PRESSURE_SMOOTH"]]
        slope, intercept = _theil_sen([float(x) for x in xs], ys)
        predicted = [intercept + slope * x for x in xs]
        r2, residual_mad = _quality(ys, predicted)
        current = max(0.0, ys[-1])

        recent = group.tail(14)
        recent_pressure_days = int(
            ((recent["QUEUED_MIN"] >= 1.0) | (recent["SPILL_REMOTE_GB"] >= 0.05)).sum()
        )
        recent_changes = int(recent["CHANGE_EVENTS"].sum())
        workload_growth = max(_growth_pct(group["QUERY_COUNT"]), _growth_pct(group["CREDITS_TOTAL"]))

        backtest_mae: float | None = None
        if len(model) >= 30:
            train = model.iloc[:-7]
            holdout = model.iloc[-7:]
            tx = [(day - train["DAY"].iloc[0]).days for day in train["DAY"]]
            ty = [safe_float(value) for value in train["PRESSURE_SMOOTH"]]
            if len(tx) >= 20:
                test_x = [(day - train["DAY"].iloc[0]).days for day in holdout["DAY"]]
                test_y = [safe_float(value) for value in holdout["PRESSURE_SMOOTH"]]
                test_slope, test_intercept = _theil_sen([float(x) for x in tx], ty)
                backtest_mae = sum(
                    abs(actual - (test_intercept + test_slope * x))
                    for actual, x in zip(test_y, test_x, strict=True)
                ) / len(test_y)

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
        elif backtest_mae is None or r2 < 0.35 or backtest_mae > max(0.20, current * 0.6):
            base.update({
                "STATUS": "WATCH",
                "BASIS": "Upward pressure is visible, but the 7-day holdout error is too high for an ETA.",
            })
        else:
            eta = (1.0 - current) / slope if slope > 0 else math.inf
            uncertainty = 1.96 * 1.4826 * residual_mad + backtest_mae
            low = max(0.0, (1.0 - current - uncertainty) / slope)
            high = max(low, (1.0 - current + uncertainty) / slope)
            if 0 < eta <= 180 and high <= 365:
                base.update({
                    "STATUS": "FORECAST",
                    "DAYS_TO_PRESSURE": round(eta),
                    "ETA_LOW_DAYS": round(low),
                    "ETA_HIGH_DAYS": round(high),
                    "BASIS": "Theil-Sen trend on a 7-day median; interval includes residual and holdout error.",
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
