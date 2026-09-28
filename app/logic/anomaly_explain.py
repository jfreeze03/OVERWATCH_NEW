"""Explain WHY a flagged spend day spiked — a contribution waterfall over the
robust baseline. Pure functions over pandas frames; no Streamlit.

`anomaly.py` DETECTS outliers; this module DECOMPOSES a flagged day's spend delta
across the warehouses that drove it, so triage jumps from "something spiked" to
"warehouse X ran $Y over its usual — that's 80% of the move" (rec#5). The
contributions sum to the total delta by construction, so nothing hides in a
residual.

Next-Fifty #27 goes one level down: ``explain_below_warehouse`` splits ONE warehouse's
flagged day by user and by database (mart27_sql.alloc_xdim_day_drivers), and
``changes_near_day`` lists that warehouse's setting changes around the day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from app.logic.formulas import ACCOUNT_TIMEZONE, format_usd, safe_float
from app.logic.rca import candidates_from_changes

_DEFAULT_BASELINE_DAYS = 14
_MAX_DRIVERS = 6
# Next-Fifty #27: the below-warehouse drill refuses an average over fewer loaded days.
_MIN_BASELINE_DAYS = 7
UNALLOCATED_LABEL = "Not allocated to a query (idle / carry-over hours)"
# Synthetic buckets mart27_sql.alloc_xdim_day_drivers emits: the top-N fold and the
# other-company mask. Never named as a driver in the narrative.
_SQL_OTHER = {"USER": "(all other users)", "DATABASE": "(all other databases)"}
_MASKED = {"USER": "(other-company users)", "DATABASE": "(other-company databases)"}


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


@dataclass(frozen=True)
class BelowWarehouseExplanation:
    """One warehouse's flagged day split by user and by database (Next-Fifty #27). Each of
    ``by_user`` / ``by_database`` ends with an UNALLOCATED_LABEL row and sums EXACTLY (to the
    cent) to ``metered_delta_usd``. ``ok`` False carries a plain-language ``reason``."""
    ok: bool
    reason: str
    warehouse: str
    flagged_day: date | None
    baseline_days: int              # loaded spine days actually averaged (<= the asked window)
    metered_actual_usd: float
    metered_baseline_usd: float
    metered_delta_usd: float
    unallocated_delta_usd: float
    by_user: tuple[DriverContribution, ...]
    by_database: tuple[DriverContribution, ...]
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

    # Per-driver "share of the net move" is only meaningful when the net move dominates the
    # gross churn. On an offsetting/redistribution day (one WH spikes, another collapses) the
    # net delta is tiny while individual deltas are large, so delta/net_delta blows past
    # +/-100% (e.g. +3000%). Suppress the share there rather than show a nonsense percentage.
    _gross = sum(abs(d) for _, _, _, d in rows) or 0.0
    _material_move = bool(total_delta) and abs(total_delta) >= 0.5 * _gross
    drivers = tuple(
        DriverContribution(
            name=name, baseline_usd=round(base, 2), actual_usd=round(act, 2),
            delta_usd=round(delta, 2),
            share_pct=round(100.0 * delta / total_delta, 1) if _material_move else 0.0)
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
    # Omit the "% of the move" clause on an offsetting day (share suppressed to 0) — the
    # dollar figures still name the biggest mover honestly without a nonsense percentage.
    tail = (f"Top driver: {top.name}, {format_usd(abs(top.delta_usd))} "
            f"{'over' if top.delta_usd >= 0 else 'under'} its usual"
            + (f" ({top.share_pct:+.0f}% of the move)" if top.share_pct else ""))
    if len(drivers) > 1 and abs(drivers[1].delta_usd) >= 0.01:
        second = drivers[1]
        tail += (f", then {second.name} ({format_usd(abs(second.delta_usd))} "
                 f"{'over' if second.delta_usd >= 0 else 'under'})")
    return lead + tail + "."


# ---------------------------------------------------------------------------------------
# Next-Fifty #27: below the warehouse — who and which database moved, and what changed.
# ---------------------------------------------------------------------------------------

def is_bucket_row(name: object) -> bool:
    """True for a synthetic row of the below-warehouse tables — the 'All other …' fold, the
    SQL top-N bucket, an other-company mask, the not-allocated residual. Never a login or a
    database name: the page skips the directory lookup for it and the narrative never names it."""
    text = str(name or "")
    return (text == UNALLOCATED_LABEL or text in _SQL_OTHER.values() or text in _MASKED.values()
            or text.startswith("All other "))


def _key_text(value: object) -> str:
    # The reader COALESCEs a missing user/database to 'NONE'; mirror it for any stray NULL
    # (None, NaN, pd.NA, NaT).
    if value is None or (not isinstance(value, str) and bool(pd.isna(value))):
        return "NONE"
    return str(value).strip() or "NONE"


def _dim_split(work: pd.DataFrame, dim: str, fday: date, spine: set,
               n: int) -> dict[str, tuple[float, float]]:
    """{key: (baseline, actual)} for one DIMENSION: actual = the key's sum on the flagged day;
    baseline = its sum over the loaded spine / len(spine) — a ZERO-FILLED MEAN (a key silent on
    a loaded day counts that day as 0), which is what makes the split exactly additive."""
    part = work[work["_DIM"] == dim]
    act = part[part["_D"] == fday].groupby("_K")["_V"].sum()
    base = part[part["_D"].isin(spine)].groupby("_K")["_V"].sum() / n
    return {str(k): (float(base.get(k, 0.0)), float(act.get(k, 0.0)))
            for k in sorted(set(act.index) | set(base.index))}


def _below_table(dim: str, rows: dict[str, tuple[float, float]], m_base: float, m_act: float,
                 max_rows: int) -> tuple[tuple[DriverContribution, ...], float]:
    """One dimension's table + its raw not-allocated delta. Rows: the top ``max_rows`` real keys
    by |delta| (the other-company mask always its own row, sorted in), then 'All other … (n)'
    (the rest plus the SQL top-N bucket; 'n+' when that bucket hides more keys), then the
    UNALLOCATED_LABEL residual. Each row is rounded to cents and the residual row BALANCES the
    rounded rows to the rounded metered figures, so a table adds up to metered_delta_usd to the
    cent. Share % is suppressed on an offsetting day (|net| < half the gross churn, the
    explain_by_warehouse rule, with the residual counted in the gross)."""
    noun = "users" if dim == "USER" else "databases"
    sql_other, masked = _SQL_OTHER[dim], _MASKED[dim]
    deltas = {k: a - b for k, (b, a) in rows.items()}
    m_delta = m_act - m_base
    unallocated = m_delta - sum(deltas.values())
    gross = sum(abs(d) for d in deltas.values()) + abs(unallocated)
    real = sorted((k for k in rows if k not in (sql_other, masked)), key=lambda k: (-abs(deltas[k]), k))
    shown = real[:max(1, int(max_rows))]
    rest = real[len(shown):]
    if masked in rows:
        shown = sorted([*shown, masked], key=lambda k: (-abs(deltas[k]), k))
    lines = [(k, rows[k][0], rows[k][1]) for k in shown]
    folded = rest + ([sql_other] if sql_other in rows else [])
    if folded:
        count = f"{len(rest)}{'+' if sql_other in rows else ''}"
        lines.append((f"All other {noun} ({count})" if rest else f"All other {noun}",
                      sum(rows[k][0] for k in folded), sum(rows[k][1] for k in folded)))
    tot_b, tot_a = round(m_base, 2), round(m_act, 2)
    tot_d = round(tot_a - tot_b, 2)
    material = bool(tot_d) and abs(m_delta) >= 0.5 * gross
    cells: list[tuple[str, float, float]] = []
    for name, b, a in lines:
        cells.append((name, round(b, 2), round(a, 2)))
    cells.append((UNALLOCATED_LABEL, round(tot_b - sum(c[1] for c in cells), 2),
                  round(tot_a - sum(c[2] for c in cells), 2)))
    table = tuple(
        DriverContribution(name=name, baseline_usd=b, actual_usd=a, delta_usd=round(a - b, 2),
                           share_pct=round(100.0 * round(a - b, 2) / tot_d, 1) if material else 0.0)
        for name, b, a in cells)
    return table, unallocated


def _top_clause(label: str, rows: tuple[DriverContribution, ...], *, second: bool) -> str:
    real = [r for r in rows if not is_bucket_row(r.name) and abs(r.delta_usd) >= 0.01]
    if not real:
        return ""
    top = real[0]
    text = (f" {label}: {top.name} {format_usd(abs(top.delta_usd))} "
            f"{'over' if top.delta_usd >= 0 else 'under'} its average"
            + (f" ({top.share_pct:+.0f}% of the move)" if top.share_pct else ""))
    if second and len(real) > 1:
        nxt = real[1]
        text += (f", then {nxt.name} ({format_usd(abs(nxt.delta_usd))} "
                 f"{'over' if nxt.delta_usd >= 0 else 'under'})")
    return text + "."


def _below_narrative(wh: str, fday: date, n: int, actual: float, baseline: float, delta: float,
                     unallocated: float, by_user: tuple[DriverContribution, ...],
                     by_database: tuple[DriverContribution, ...]) -> str:
    if abs(delta) < 0.01:
        return (f"{wh} on {fday}: {format_usd(actual)} metered, in line with its {n}-day average "
                f"of {format_usd(baseline)}.")
    lead = (f"{wh} on {fday}: {format_usd(actual)} metered vs a {n}-day average of "
            f"{format_usd(baseline)} — {format_usd(abs(delta))} {'above' if delta > 0 else 'below'} usual.")
    idle = ""
    if abs(unallocated) >= max(1.0, 0.1 * abs(delta)):
        idle = (f" {format_usd(abs(unallocated))} {'more' if unallocated > 0 else 'less'} than usual "
                "was not tied to a query (idle or long-query carry-over hours).")
    return (lead + _top_clause("By user", by_user, second=True)
            + _top_clause("By database", by_database, second=False) + idle)


def explain_below_warehouse(frame: pd.DataFrame | None, flagged_day: object, warehouse: str, *,
                            value_col: str = "USD", baseline_days: int = _DEFAULT_BASELINE_DAYS,
                            max_rows: int = _MAX_DRIVERS) -> BelowWarehouseExplanation:
    """Split ONE warehouse's flagged-day move by user and by database (Next-Fifty #27).

    ``frame`` is mart27_sql.alloc_xdim_day_drivers' long shape (DAY, DIMENSION in {USER,
    DATABASE, METERED, SPINE}, KEY_NAME) priced into ``value_col``. The baseline is the
    ZERO-FILLED MEAN over the last ``baseline_days`` SPINE days before the flagged day (the days
    the allocation fact loaded): every user/database — and the warehouse's metered total — is
    averaged over the same loaded days, a day without activity counting as 0. That makes it
    exactly additive: the keys' deltas plus the not-allocated residual (metered minus allocated:
    warehouse-hours in which no query started — idle time or a long query carrying over) equal
    the warehouse's metered delta. explain_by_warehouse's row-tail MEDIAN is deliberately not
    reused: it is not additive and hides a sporadic heavy user (a key absent from most baseline
    days gets its own spend as its "usual").

    Refuses (ok False + reason) when the flagged day is not in the spine (the fact has not loaded
    it), fewer than 7 baseline days are loaded, the window holds no user/database rows, or the
    warehouse has no metered row on the day in this company view.
    """
    wh = str(warehouse or "").strip()
    fday = _to_date(flagged_day)

    def _refuse(reason: str, n: int = 0) -> BelowWarehouseExplanation:
        return BelowWarehouseExplanation(False, reason, wh, fday, n, 0.0, 0.0, 0.0, 0.0, (), (), "")

    need = ["DAY", "DIMENSION", "KEY_NAME", value_col]
    if frame is None or frame.empty or fday is None or not set(need).issubset(frame.columns):
        return _refuse("No allocation detail is available for this warehouse and day.")
    work = frame[need].copy()
    work["_D"] = pd.to_datetime(work["DAY"], errors="coerce").dt.date
    work["_V"] = work[value_col].map(safe_float)
    work["_DIM"] = work["DIMENSION"].map(lambda v: str(v or "").strip().upper())
    work["_K"] = work["KEY_NAME"].map(_key_text)
    work = work.dropna(subset=["_D"])

    spine_all = set(work.loc[work["_DIM"] == "SPINE", "_D"])
    if fday not in spine_all:
        return _refuse(f"The allocation fact has not loaded {fday} yet (it loads once a day), so the "
                       "day can't be split by user or database until the next load.")
    spine = sorted(d for d in spine_all if d < fday)[-max(1, int(baseline_days)):]
    n = len(spine)
    if n < _MIN_BASELINE_DAYS:
        return _refuse(f"Only {n} baseline day(s) before {fday} are loaded in the allocation fact; "
                       f"at least {_MIN_BASELINE_DAYS} are needed for a fair average.", n)
    spine_set = set(spine)
    scoped = work[work["_D"].isin(spine_set | {fday})]
    if not scoped["_DIM"].isin(("USER", "DATABASE")).any():
        return _refuse(f"No query-allocated rows for {wh} in this window — a warehouse that ran only "
                       "cloud-services or serverless work has none, and one whose company mapping "
                       "changed recently may not match this company view.", n)
    if not ((scoped["_DIM"] == "METERED") & (scoped["_D"] == fday)).any():
        return _refuse(f"The warehouse fact has no metered credits for {wh} on {fday} in this company "
                       "view, so a split could not be reconciled to it.", n)

    metered = _dim_split(scoped, "METERED", fday, spine_set, n)
    m_base = sum(b for b, _ in metered.values())
    m_act = sum(a for _, a in metered.values())
    by_user, unallocated = _below_table("USER", _dim_split(scoped, "USER", fday, spine_set, n),
                                        m_base, m_act, max_rows)
    by_database, _ = _below_table("DATABASE", _dim_split(scoped, "DATABASE", fday, spine_set, n),
                                  m_base, m_act, max_rows)
    actual, baseline = round(m_act, 2), round(m_base, 2)
    delta = round(actual - baseline, 2)
    return BelowWarehouseExplanation(
        ok=True, reason="", warehouse=wh, flagged_day=fday, baseline_days=n,
        metered_actual_usd=actual, metered_baseline_usd=baseline, metered_delta_usd=delta,
        unallocated_delta_usd=round(unallocated, 2), by_user=by_user, by_database=by_database,
        narrative=_below_narrative(wh, fday, n, actual, baseline, delta, unallocated,
                                   by_user, by_database))


def _account_local(value: object) -> pd.Timestamp | None:
    """A registry timestamp as ACCOUNT-local time: a tz-aware value (the connector's LTZ, or a UTC
    value) converts to ACCOUNT_TIMEZONE; a naive value is already session (= account) time."""
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.tz_convert(ACCOUNT_TIMEZONE) if ts.tzinfo is not None else ts


def changes_near_day(frame: pd.DataFrame | None, warehouse: str, flagged_day: object, *,
                     days_before: int = 1, days_after: int = 1) -> list[dict]:
    """Warehouse setting changes the daily change scan saw within [D - days_before, D + days_after]
    (account-local CHANGE_SEEN_AT dates) on EXACTLY ``warehouse`` (case-insensitive; the
    change_impact_sql.warehouse_change_registry filter is a substring match), normalized through
    rca.candidates_from_changes — titles read "…: SIZE: MEDIUM → LARGE", ``when`` is account-local.
    CHANGE_SEEN_AT is the scan's clock (daily 06:40 CT), not the ALTER time."""
    fday = _to_date(flagged_day)
    want = str(warehouse or "").strip().upper()
    if (frame is None or frame.empty or fday is None or not want
            or not {"WAREHOUSE_NAME", "CHANGE_SEEN_AT"}.issubset(frame.columns)):
        return []
    lo, hi = fday - timedelta(days=int(days_before)), fday + timedelta(days=int(days_after))
    seen = frame["CHANGE_SEEN_AT"].map(_account_local)
    keep = [
        _key_text(name).upper() == want and ts is not None and lo <= ts.date() <= hi
        for name, ts in zip(frame["WAREHOUSE_NAME"], seen, strict=True)
    ]
    out = frame[keep].copy()
    out["CHANGE_SEEN_AT"] = [ts for ts, k in zip(seen, keep, strict=True) if k]
    return candidates_from_changes(out)
