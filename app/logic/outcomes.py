"""Measured outcomes for completed work (Next-Fifty #46): did the fix hold? Pure module.

A work item marked DONE is measured against its entity's OWN daily mart signal after the day it was
marked done (``workbench_sql.entity_daily_signals``):

- WAREHOUSE: metered credits per day.
- TASK: failed runs when the task was failing before (at least FAIL_ARM_PCT of its runs in the
  BASELINE_DAYS before), else its P95 runtime.
- QUERY_FINGERPRINT: failed runs on the same rule, else attributed credits (what Optimize ranks on).

The level rule: the baseline is the BASELINE_DAYS before the done day (credits: the zero-filled daily
mean; P95: the median over run days). The after window runs from the day after the done day (that day
is partial) to min(today - 1, the mart's LOADED_THROUGH), so a stalled loader never reads as a fix. The
trailing ROLL_DAYS level must fall at least MIN_DROP below the baseline to count as fixed; climbing back
to REGAIN x the baseline after that is "Re-broke", dated the day the trailing week crossed back (so it
lags the real break by up to ROLL_DAYS - 1 days). Never dropping is "Not fixed". The failure rule: any
failed run after the done day is "Re-broke" on that day.

"Re-broke" and "Not fixed" lift Operations > Optimize's 90-day Track-all cooldown (OVERRIDES_COOLDOWN);
"Too early", "Unavailable" and "Not checked" never do, and a dismissal is never lifted. The thresholds
are uncalibrated: a month-end or volume swing can read as "Re-broke", which only re-admits a family to
the capped, idempotent Track all and changes a display label.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

import pandas as pd

from app.logic import fix_queue
from app.logic.formulas import format_usd, humanize_duration, safe_float

HELD_TYPES = ("WAREHOUSE", "TASK", "QUERY_FINGERPRINT")
BASELINE_DAYS = 28
MIN_AFTER_DAYS = 7
ROLL_DAYS = 7                  # one week, so weekday / weekend seasonality cancels
MIN_DROP = 0.20
REGAIN = 0.90
FAIL_ARM_PCT = 2.0             # the Optimize 'Failure risk' line
LOOKBACK_DAYS = fix_queue.TRACK_COOLDOWN_DAYS
MAX_ENTITIES = 40              # x (BASELINE_DAYS + LOOKBACK_DAYS + 1) rows stays under DEFAULT_MAX_ROWS
HELD_COL = "Held?"             # mixed case, so the header prettifier leaves it alone

HELD = "HELD"
REBROKE = "REBROKE"
NOT_FIXED = "NOT_FIXED"
TOO_EARLY = "TOO_EARLY"
NOT_MEASURABLE = "NOT_MEASURABLE"
OVERRIDES_COOLDOWN = frozenset({REBROKE, NOT_FIXED})

UNAVAILABLE_LABEL = "Unavailable"     # the signals read failed
NOT_CHECKED_LABEL = "Not checked"     # beyond the newest MAX_ENTITIES completions or the lookback
NOT_MEASURABLE_LABEL = "Not measurable"

SIGNAL_CREDITS = "credits"
SIGNAL_P95 = "P95 runtime"
SIGNAL_FAILURES = "failures"


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value).strip()


def _day(value: object) -> date | None:
    """A timestamp / date / text cell as a date; None for NULL or garbage."""
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError):
        return None
    if ts is None or pd.isna(ts):
        return None
    return ts.date()


def done_day(row: Mapping[str, object]) -> date | None:
    """The day a work item was marked done: COMPLETED_AT, else UPDATED_AT (the house COALESCE)."""
    return _day(row.get("COMPLETED_AT")) or _day(row.get("UPDATED_AT"))


def _res(state: str, label: str, signal: str = "", *, since: date | None = None, after_days: int = 0,
         before: float | None = None, after: float | None = None, fails_before: float | None = None,
         runs_before: float | None = None, fails_after: float | None = None) -> dict:
    return {"state": state, "label": label, "signal": signal, "since": since, "after_days": int(after_days),
            "before": before, "after": after, "fails_before": fails_before, "runs_before": runs_before,
            "fails_after": fails_after}


def _rebroke_label(since: date) -> str:
    return f"Re-broke {since:%b} {since.day}"


def _too_early(n: int) -> str:
    return f"Too early ({n} of {MIN_AFTER_DAYS} days)"


def action_held(entity_type: object, entity_key: object, done: date | None,
                daily: pd.DataFrame | None, today: date) -> dict:
    """The measured outcome of one completed item. Keys: state, label, signal, since, after_days, before,
    after, fails_before, runs_before, fails_after. Never raises."""
    kind = _text(entity_type).upper()
    key = _text(entity_key).upper()
    if kind not in HELD_TYPES or not key or done is None:
        return _res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL)
    if daily is None or daily.empty or not {"ENTITY_TYPE", "ENTITY_KEY_U", "DAY"}.issubset(daily.columns):
        return _res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL)
    of_kind = daily[daily["ENTITY_TYPE"].map(lambda v: _text(v).upper()) == kind]
    rows = of_kind[of_kind["ENTITY_KEY_U"].map(lambda v: _text(v).upper()) == key].copy()
    if rows.empty:
        return _res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL)
    end = today - timedelta(days=1)
    if "LOADED_THROUGH" in of_kind.columns:
        loaded = pd.to_datetime(of_kind["LOADED_THROUGH"], errors="coerce").max()
        if pd.notna(loaded):
            end = min(end, loaded.date())
    start = done - timedelta(days=BASELINE_DAYS)
    rows["DAY"] = pd.to_datetime(rows["DAY"], errors="coerce")
    rows = rows[rows["DAY"].notna()]
    rows["DAY"] = rows["DAY"].dt.date
    for col in ("CREDITS", "RUNS", "FAILS", "P95_SEC"):
        rows[col] = pd.to_numeric(rows[col], errors="coerce") if col in rows.columns else float("nan")
    per_day = rows.groupby("DAY").agg({"CREDITS": "sum", "RUNS": "sum", "FAILS": "sum", "P95_SEC": "max"})
    # sum() turns an all-NaN group into 0: keep a day with no measurement NaN (NULL is never 0)
    counted = rows.groupby("DAY")[["CREDITS", "RUNS", "FAILS"]].count()
    for col in ("CREDITS", "RUNS", "FAILS"):
        per_day[col] = per_day[col].where(counted[col] > 0)
    calendar = [start + timedelta(days=i) for i in range(max(0, (end - start).days + 1))]
    before_idx = [d for d in calendar if d < done]
    after_idx = [d for d in calendar if d > done]
    runs_b = float(per_day["RUNS"].reindex(before_idx).fillna(0.0).sum())
    fails_b = float(per_day["FAILS"].reindex(before_idx).fillna(0.0).sum())
    if kind != "WAREHOUSE" and runs_b > 0 and 100.0 * fails_b / runs_b >= FAIL_ARM_PCT:
        fails_a = per_day["FAILS"].reindex(after_idx).fillna(0.0)
        bad = fails_a[fails_a > 0]
        n = len(after_idx)
        fails_after = float(fails_a.sum())
        if len(bad):
            since = bad.index[0]
            return _res(REBROKE, _rebroke_label(since), SIGNAL_FAILURES, since=since, after_days=n,
                        fails_before=fails_b, runs_before=runs_b, fails_after=fails_after)
        state, label = (TOO_EARLY, _too_early(n)) if n < MIN_AFTER_DAYS else (HELD, f"Held {n} days")
        return _res(state, label, SIGNAL_FAILURES, after_days=n, fails_before=fails_b, runs_before=runs_b,
                    fails_after=fails_after)
    if kind == "TASK":
        signal = SIGNAL_P95
        series = per_day["P95_SEC"]
        before = series.reindex(before_idx).dropna()
        after = series.reindex(after_idx).dropna()
        base = float(before.median()) if len(before) else 0.0
        roll = after.rolling(ROLL_DAYS, min_periods=3).median().dropna()
    else:
        signal = SIGNAL_CREDITS
        series = per_day["CREDITS"]
        before = series.reindex(before_idx).fillna(0.0)
        after = series.reindex(after_idx).fillna(0.0)
        base = float(before.mean()) if len(before) else 0.0
        roll = after.rolling(ROLL_DAYS, min_periods=ROLL_DAYS).mean().dropna()
    if not len(before) or not base > 0:
        return _res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL, signal)
    n_after = len(after)
    if n_after < MIN_AFTER_DAYS or roll.empty:
        return _res(TOO_EARLY, _too_early(n_after), signal, after_days=n_after, before=base)
    last = float(roll.iloc[-1])
    fixed = roll <= base * (1 - MIN_DROP)
    if not bool(fixed.any()):
        return _res(NOT_FIXED, "Not fixed", signal, after_days=n_after, before=base, after=last)
    first_fixed = fixed.idxmax()
    later = roll[[d > first_fixed for d in roll.index]]
    regained = later[later >= base * REGAIN]
    if len(regained):
        since = regained.index[0]
        return _res(REBROKE, _rebroke_label(since), signal, since=since, after_days=n_after, before=base,
                    after=last)
    return _res(HELD, f"Held {n_after} days", signal, after_days=n_after, before=base, after=last)


def held_basis(result: Mapping[str, object], rate: float) -> str:
    """The one-line evidence behind a Held? label ('' when there is nothing measured). Credit figures
    are priced at ``rate``; the caller wraps any markdown sink in md_dollars."""
    signal = _text(result.get("signal"))
    n = int(safe_float(result.get("after_days")))
    before, after = result.get("before"), result.get("after")
    if signal == SIGNAL_FAILURES:
        return (f"{int(safe_float(result.get('fails_before'))):,} failed of "
                f"{int(safe_float(result.get('runs_before'))):,} runs in the {BASELINE_DAYS} days before; "
                f"{int(safe_float(result.get('fails_after'))):,} since")
    if before is None:
        return ""
    if signal == SIGNAL_CREDITS:
        head = f"{format_usd(safe_float(before) * safe_float(rate))}/day before"
        tail = (f" → {format_usd(safe_float(after) * safe_float(rate))}/day after" if after is not None else "")
    elif signal == SIGNAL_P95:
        head = f"P95 {humanize_duration(before, 's')} before"
        tail = f" → {humanize_duration(after, 's')} after" if after is not None else ""
    else:
        return ""
    return f"{head}{tail}, {n} day{'' if n == 1 else 's'} measured"


def done_entities(actions: pd.DataFrame | None, today: date, *, type_col: str = "SOURCE_ENTITY_TYPE",
                  key_col: str = "SOURCE_ENTITY_KEY", lookback_days: int = LOOKBACK_DAYS,
                  limit: int = MAX_ENTITIES) -> list[tuple[str, str, date]]:
    """(TYPE, KEY_UPPER, earliest done - BASELINE_DAYS) for the DONE rows of HELD_TYPES completed within the
    lookback: newest completion first, one entry per entity, at most ``limit`` entities."""
    if actions is None or actions.empty or not {"STATUS", type_col, key_col}.issubset(actions.columns):
        return []
    floor = today - timedelta(days=int(lookback_days))
    candidates: list[tuple[date, str, str]] = []
    for rec in actions.to_dict("records"):
        if _text(rec.get("STATUS")).upper() != "DONE":
            continue
        kind, key = _text(rec.get(type_col)).upper(), _text(rec.get(key_col)).upper()
        done = done_day(rec)
        if kind not in HELD_TYPES or not key or done is None or done < floor:
            continue
        candidates.append((done, kind, key))
    candidates.sort(key=lambda c: c[0], reverse=True)
    earliest: dict[tuple[str, str], date] = {}
    for done, kind, key in candidates:
        ent = (kind, key)
        if ent in earliest:
            earliest[ent] = min(earliest[ent], done)
        elif len(earliest) < int(limit):
            earliest[ent] = done
    return [(kind, key, d - timedelta(days=BASELINE_DAYS)) for (kind, key), d in earliest.items()]


def held_columns(actions: pd.DataFrame | None, daily: pd.DataFrame | None, today: date, *, read_ok: bool,
                 evaluated: Iterable[tuple[str, str]], rate: float, type_col: str = "SOURCE_ENTITY_TYPE",
                 key_col: str = "SOURCE_ENTITY_KEY",
                 lookback_days: int = LOOKBACK_DAYS) -> tuple[pd.Series, pd.Series]:
    """(Held? label, HELD_BASIS) per row: None for a row that is not DONE; 'Not measurable' for a type
    with no signal; 'Unavailable' when the signals read failed; 'Not checked' outside the evaluated set
    (the newest MAX_ENTITIES completions within the lookback); else the measured label."""
    if actions is None:
        return pd.Series(dtype=object), pd.Series(dtype=object)
    seen = {(_text(t).upper(), _text(k).upper()) for t, k in evaluated}
    floor = today - timedelta(days=int(lookback_days))
    labels: list[str | None] = []
    bases: list[str | None] = []
    for rec in actions.to_dict("records"):
        if _text(rec.get("STATUS")).upper() != "DONE":
            labels.append(None)
            bases.append(None)
            continue
        kind, key = _text(rec.get(type_col)).upper(), _text(rec.get(key_col)).upper()
        done = done_day(rec)
        basis: str | None = None
        if kind not in HELD_TYPES or not key or done is None:
            label = NOT_MEASURABLE_LABEL
        elif not read_ok:
            label = UNAVAILABLE_LABEL
        elif (kind, key) not in seen or done < floor:
            label = NOT_CHECKED_LABEL
        else:
            res = action_held(kind, key, done, daily, today)
            label = str(res["label"])
            basis = held_basis(res, rate) or None
        labels.append(label)
        bases.append(basis)
    return (pd.Series(labels, index=actions.index, dtype=object),
            pd.Series(bases, index=actions.index, dtype=object))


def _done_family_rows(tracked: pd.DataFrame | None, keys: Iterable[object], today: date, *,
                      limit: int = MAX_ENTITIES) -> list[tuple[str, date]]:
    """(KEY_UPPER, done day) for tracked families whose only decided work in the lookback is DONE (DONE_N > 0,
    no open item, no dismissal) and a real LAST_DECIDED, in ``keys`` order, de-duplicated, capped."""
    if tracked is None or tracked.empty or "ENTITY_KEY_U" not in tracked.columns:
        return []
    floor = today - timedelta(days=LOOKBACK_DAYS)
    by_key: dict[str, date] = {}
    for rec in tracked.to_dict("records"):
        key = _text(rec.get("ENTITY_KEY_U")).upper()
        status = _text(rec.get("ACTION_STATUS")).upper()
        last = _day(rec.get("LAST_DECIDED"))
        if (not key or last is None or last < floor or safe_float(rec.get("DONE_N")) <= 0
                or safe_float(rec.get("OPEN_N")) > 0 or safe_float(rec.get("DROPPED_N")) > 0
                or status in ("OPEN", "IN_PROGRESS", "DROPPED")):
            continue
        by_key[key] = last
    out: list[tuple[str, date]] = []
    for k in keys:
        key = _text(k).upper()
        if key in by_key and all(key != o for o, _ in out):
            out.append((key, by_key[key]))
            if len(out) >= int(limit):
                break
    return out


def done_family_entities(tracked: pd.DataFrame | None, keys: Iterable[object], today: date, *,
                         limit: int = MAX_ENTITIES) -> list[tuple[str, str, date]]:
    """The entity_daily_signals request for Optimize's listed Done families (``keys`` in display order)."""
    return [(fix_queue.TRACK_ENTITY_TYPE, key, done - timedelta(days=BASELINE_DAYS))
            for key, done in _done_family_rows(tracked, keys, today, limit=limit)]


def family_outcomes(tracked: pd.DataFrame | None, daily: pd.DataFrame | None, today: date,
                    keys: Iterable[object]) -> dict[str, dict]:
    """KEY_UPPER -> the action_held result for every evaluated Done family (the done_family_entities set)."""
    return {key: action_held(fix_queue.TRACK_ENTITY_TYPE, key, done, daily, today)
            for key, done in _done_family_rows(tracked, keys, today)}


def rebroke_families(tracked: pd.DataFrame | None, daily: pd.DataFrame | None, today: date,
                     keys: Iterable[object]) -> dict[str, dict]:
    """The evaluated Done families whose measured outcome lifts the Track-all cooldown."""
    return {k: r for k, r in family_outcomes(tracked, daily, today, keys).items()
            if r["state"] in OVERRIDES_COOLDOWN}
