"""Explain WHY a flagged spend day spiked — a contribution waterfall over the
robust baseline. Pure functions over pandas frames; no Streamlit.

`anomaly.py` DETECTS outliers; this module DECOMPOSES a flagged day's spend delta
across the warehouses that drove it, so triage jumps from "something spiked" to
"warehouse X ran $Y over its usual — that's 80% of the move" (rec#5). The
contributions sum to the total delta by construction, so nothing hides in a
residual.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.logic.formulas import format_usd, safe_float

_DEFAULT_BASELINE_DAYS = 14
_MAX_DRIVERS = 6


@dataclass(frozen=True)
class DriverContribution:
    name: str
    baseline_usd: float
    actual_usd: float
    delta_usd: float
    share_pct: float   # signed share of the total delta (drivers sum to ~100%)


@dataclass(frozen=True)
class AnomalyExplanation:
    ok: bool
    flagged_day: date | None
    total_actual_usd: float
    total_baseline_usd: float
    total_delta_usd: float
    drivers: tuple[DriverContribution, ...]
    narrative: str


def _to_date(value: object) -> date | None:
    ts = pd.to_datetime(value, errors="coerce")
    return ts.date() if pd.notna(ts) else None


def explain_by_warehouse(frame: pd.DataFrame, flagged_day: object, *,
                         value_col: str = "USD", group_col: str = "WAREHOUSE_NAME",
                         day_col: str = "DAY",
                         baseline_days: int = _DEFAULT_BASELINE_DAYS) -> AnomalyExplanation:
    """Decompose the flagged day's total spend delta across warehouses.

    The baseline for each warehouse is the median of the last ``baseline_days``
    COMPLETE days BEFORE the flagged day; a warehouse's contribution is its
    flagged-day spend minus that baseline. A warehouse active on the flagged day
    with no prior history contributes its whole spend (a brand-new driver); one
    with history but silent on the flagged day contributes a negative delta (it
    went quiet). Contributions are ranked by absolute size and sum to the total
    delta.
    """
    fday = _to_date(flagged_day)
    if (frame is None or frame.empty or fday is None
            or not {day_col, group_col, value_col}.issubset(frame.columns)):
        return AnomalyExplanation(False, fday, 0.0, 0.0, 0.0, (), "No detail available.")

    work = frame[[day_col, group_col, value_col]].copy()
    work["_D"] = pd.to_datetime(work[day_col], errors="coerce").dt.date
    work["_V"] = work[value_col].map(safe_float)
    work = work.dropna(subset=["_D"])

    actual = work[work["_D"] == fday].groupby(group_col)["_V"].sum()
    before = work[work["_D"] < fday]
    baseline: dict[object, float] = {}
    for warehouse, group in before.groupby(group_col):
        recent = group.sort_values("_D").tail(baseline_days)
        baseline[warehouse] = float(recent["_V"].median()) if not recent.empty else 0.0

    names = set(actual.index) | set(baseline)
    rows = [(str(wh), float(baseline.get(wh, 0.0)), float(actual.get(wh, 0.0)),
             float(actual.get(wh, 0.0)) - float(baseline.get(wh, 0.0)))
            for wh in names]
    total_actual = sum(a for _, _, a, _ in rows)
    total_baseline = sum(b for _, b, _, _ in rows)
    total_delta = total_actual - total_baseline
    rows.sort(key=lambda r: abs(r[3]), reverse=True)

    drivers = tuple(
        DriverContribution(
            name=name, baseline_usd=round(base, 2), actual_usd=round(act, 2),
            delta_usd=round(delta, 2),
            share_pct=round(100.0 * delta / total_delta, 1) if total_delta else 0.0)
        for name, base, act, delta in rows[:_MAX_DRIVERS] if abs(delta) >= 0.005
    )
    return AnomalyExplanation(
        ok=True, flagged_day=fday, total_actual_usd=round(total_actual, 2),
        total_baseline_usd=round(total_baseline, 2), total_delta_usd=round(total_delta, 2),
        drivers=drivers, narrative=_narrative(fday, total_actual, total_baseline, total_delta, drivers))


def _narrative(fday: date | None, actual: float, baseline: float, delta: float,
               drivers: tuple[DriverContribution, ...]) -> str:
    if not drivers or abs(delta) < 0.01:
        return f"Spend on {fday} was {format_usd(actual)}, in line with the recent median."
    direction = "above" if delta >= 0 else "below"
    lead = (f"Spend on {fday} was {format_usd(actual)} — {format_usd(abs(delta))} {direction} "
            f"the recent median of {format_usd(baseline)}. ")
    top = drivers[0]
    tail = (f"Top driver: {top.name}, {format_usd(abs(top.delta_usd))} "
            f"{'over' if top.delta_usd >= 0 else 'under'} its usual "
            f"({top.share_pct:+.0f}% of the move)")
    if len(drivers) > 1 and abs(drivers[1].delta_usd) >= 0.01:
        second = drivers[1]
        tail += (f", then {second.name} ({format_usd(abs(second.delta_usd))} "
                 f"{'over' if second.delta_usd >= 0 else 'under'})")
    return lead + tail + "."
