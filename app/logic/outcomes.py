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
lags the real break by up to ROLL_DAYS - 1 days). Never dropping is "Not fixed". The failure rule is
the same rule on the failure RATE (failed / runs): the trailing ROLL_DAYS rate must fall MIN_DROP below
the baseline rate, and climbing back to REGAIN x that rate (with a failed run in the week) is
"Re-broke" -- so one stray failure on a fixed high-volume family is not a re-break, while a daily task
that fails again is (review C5).

Control Room triage items (SOURCE = fix_queue.TRIAGE_TRACK_SOURCE) are judged on the signal they were
tracked for, never on a level they were not tracked to lower (review C4): a task-failure item always
takes the failure rule, whatever its baseline rate; a warehouse-spend item re-breaks only when the
triage scan's own spend test (robust |z| >= anomaly.DEFAULT_THRESHOLD over the warehouse's trailing
TRIAGE_WINDOW_DAYS, with its $ and active-day floors and the known-spike calendar) fires again in the
direction it was tracked for (a spike, or a collapse), read back from the TITLE triage_track_item wrote.
A quiet signal reads "Held". A triage warehouse item never reads "Not fixed" (only Held / Re-broke / Too
early / not measurable); a triage task item can, when its failure rate never fell 20% below the pre-done
rate (review r2).

"Re-broke" and "Not fixed" lift Operations > Optimize's 90-day Track-all cooldown (OVERRIDES_COOLDOWN);
"Too early", "Unavailable" and "Not checked" never do, and a dismissal is never lifted. The thresholds
are uncalibrated: a month-end or volume swing can read as "Re-broke", which only re-admits a family to
the capped, idempotent Track all and changes a display label.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.logic import fix_queue
from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    DEFAULT_SPIKE_CALENDAR,
    DEFAULT_THRESHOLD,
    expected_spike_labels,
)
from app.logic.formulas import DEFAULT_CREDIT_PRICE_USD, format_usd, humanize_duration, safe_float

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
SIGNAL_SPEND_ANOMALY = "spend anomaly"

# The Control Room triage spend scan (control_room.py): robust z per warehouse over the trailing 30 complete
# days (mart_sql.fact_warehouse_daily(30) minus today), surfaced for the latest complete day.
TRIAGE_WINDOW_DAYS = 30
# fix_queue.triage_track_item writes TITLE = f"{KIND}: {key}", KIND from actions.triage_queue
SPEND_SPIKE_PREFIX = "Spend anomaly:"
SPEND_COLLAPSE_PREFIX = "Spend collapse:"
# anomaly.robust_zscores' Iglewicz-Hoaglin constants (a parity test pins _robust_last_z to that function)
_MAD_K = 0.6745
_MEANAD_K = 0.7979


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
         runs_before: float | None = None, fails_after: float | None = None, runs_after: float | None = None,
         z: float | None = None, direction: int = 0) -> dict:
    return {"state": state, "label": label, "signal": signal, "since": since, "after_days": int(after_days),
            "before": before, "after": after, "fails_before": fails_before, "runs_before": runs_before,
            "fails_after": fails_after, "runs_after": runs_after, "z": z, "direction": int(direction)}


def is_triage_source(source: object) -> bool:
    """True for an item Control Room triage Track wrote (SOURCE = fix_queue.TRIAGE_TRACK_SOURCE)."""
    return _text(source).upper() == fix_queue.TRIAGE_TRACK_SOURCE.upper()


def spend_direction(title: object) -> int:
    """The triage spend row a warehouse item was tracked from, read back from its TITLE: +1 a spike
    ('Spend anomaly: WH'), -1 a collapse ('Spend collapse: WH'), 0 unknown."""
    text = _text(title)
    if text.startswith(SPEND_SPIKE_PREFIX):
        return 1
    if text.startswith(SPEND_COLLAPSE_PREFIX):
        return -1
    return 0


def _robust_last_z(values: np.ndarray) -> float:
    """anomaly.robust_zscores(values).iloc[-1] for a NaN-free array, without building a Series per day
    (Held? scores up to LOOKBACK_DAYS windows per item on every rerun)."""
    if len(values) < 5:
        return 0.0
    med = float(np.median(values))
    dev = np.abs(values - med)
    mad = float(np.median(dev))
    if mad > 0:
        return float(_MAD_K * (values[-1] - med) / mad)
    mean_ad = float(dev.mean())
    return float(_MEANAD_K * (values[-1] - med) / mean_ad) if mean_ad > 0 else 0.0


def _spend_recurrence(per_day: pd.DataFrame, after_idx: list[date], *, title: object, rate: float,
                      spike_calendar: str | None) -> dict:
    """A triage warehouse-spend item: did the triage scan's spend test fire again, in the direction the
    item was tracked for? Each after-day that has a spend row is scored as the triage scan would score it
    the next morning -- robust z over the TRIAGE_WINDOW_DAYS ending that day (days with a row only),
    flagged at |z| >= DEFAULT_THRESHOLD with the same floors (a spike day >= ANOMALY_MIN_USD, a collapse
    on a window median >= ANOMALY_MIN_USD, >= ANOMALY_MIN_ACTIVE_DAYS active days) and the known-spike
    calendar clearing an upward hit. The first such day is "Re-broke"; none is "Held" (or "Too early")."""
    direction = spend_direction(title)
    n = len(after_idx)
    if direction == 0:
        # not a title triage Track wrote: which way the spend broke is unknown, so no honest test exists
        return _res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL, SIGNAL_SPEND_ANOMALY)
    price = safe_float(rate, DEFAULT_CREDIT_PRICE_USD)
    price = price if price > 0 else DEFAULT_CREDIT_PRICE_USD
    usd = (per_day["CREDITS"].dropna() * price).sort_index()
    days = list(usd.index)
    vals = usd.to_numpy(dtype=float)
    present = [d for d in after_idx if d in usd.index]
    if direction < 0 and n >= MIN_AFTER_DAYS and not present:
        # no spend row at all since done: a stalled pipeline and a retired warehouse look the same
        return _res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL, SIGNAL_SPEND_ANOMALY, after_days=n, direction=direction)
    cal = DEFAULT_SPIKE_CALENDAR if spike_calendar is None else str(spike_calendar)
    labels = (expected_spike_labels(pd.Series(present, dtype=object), cal).tolist() if present else [])
    for day, label in zip(present, labels, strict=True):
        pos = bisect_left(days, day)
        start = bisect_left(days, day - timedelta(days=TRIAGE_WINDOW_DAYS - 1))
        window = vals[start:pos + 1]
        if int((window > 0).sum()) < ANOMALY_MIN_ACTIVE_DAYS:
            continue
        z = _robust_last_z(window)
        if direction > 0:
            hit = z >= DEFAULT_THRESHOLD and window[-1] >= ANOMALY_MIN_USD and not label
        else:
            hit = z <= -DEFAULT_THRESHOLD and float(np.median(window)) >= ANOMALY_MIN_USD
        if hit:
            return _res(REBROKE, _rebroke_label(day), SIGNAL_SPEND_ANOMALY, since=day, after_days=n,
                        after=float(window[-1]), z=z, direction=direction)
    state, text = (TOO_EARLY, _too_early(n)) if n < MIN_AFTER_DAYS else (HELD, f"Held {n} days")
    return _res(state, text, SIGNAL_SPEND_ANOMALY, after_days=n, direction=direction)


def _failure_arm(per_day: pd.DataFrame, before_idx: list[date], after_idx: list[date], *,
                 forced: bool) -> dict | None:
    """The failure rule on the failure RATE, or None when it does not apply (not ``forced`` and fewer
    than FAIL_ARM_PCT of the baseline runs failed). The level rule's shape on failed / runs: the trailing
    ROLL_DAYS rate (calendar days, zero-filled) must fall MIN_DROP below the baseline rate to count as
    fixed; after that, a week back at REGAIN x the baseline rate WITH a failed run in it is "Re-broke",
    dated the day that week crossed back; never falling is "Not fixed"; < MIN_AFTER_DAYS is "Too early".
    A week with no run has no rate; when no week since done had a run the item is "Not measurable".
    A zero baseline rate (a forced triage task with no failed run in the baseline) makes any failed run
    in a week after the first clean one a re-break."""
    runs_b = float(per_day["RUNS"].reindex(before_idx).fillna(0.0).sum())
    fails_b = float(per_day["FAILS"].reindex(before_idx).fillna(0.0).sum())
    rate_b = 100.0 * fails_b / runs_b if runs_b > 0 else 0.0
    if not forced and not (runs_b > 0 and rate_b >= FAIL_ARM_PCT):
        return None
    fails_a = per_day["FAILS"].reindex(after_idx).fillna(0.0)
    runs_a = per_day["RUNS"].reindex(after_idx).fillna(0.0)
    n = len(after_idx)
    fails_sum, runs_sum = float(fails_a.sum()), float(runs_a.sum())

    def res(state: str, label: str, *, since: date | None = None, after: float | None = None) -> dict:
        return _res(state, label, SIGNAL_FAILURES, since=since, after_days=n, before=rate_b, after=after,
                    fails_before=fails_b, runs_before=runs_b, fails_after=fails_sum, runs_after=runs_sum)

    roll_f = fails_a.rolling(ROLL_DAYS, min_periods=ROLL_DAYS).sum().dropna()
    roll_r = runs_a.rolling(ROLL_DAYS, min_periods=ROLL_DAYS).sum().reindex(roll_f.index)
    # R1-084: a week with no run has NO failure rate (house law 8) -- 0 of 0 is not a measured 0%, or a
    # task that was suspended / unscheduled after done (no mart rows; LOADED_THROUGH is mart-wide) read
    # "Held". Those weeks drop out; a failed run with no recorded run stays the inconsistent 100%.
    rate = pd.Series([(100.0 * f / r) if r > 0 else (100.0 if f > 0 else float("nan"))
                      for f, r in zip(roll_f.tolist(), roll_r.tolist(), strict=True)],
                     index=roll_f.index, dtype=float).dropna()
    if n < MIN_AFTER_DAYS or roll_f.empty:
        return res(TOO_EARLY, _too_early(n))
    if rate.empty:
        # a full week has elapsed but no run since done: nothing to measure the fix on
        return res(NOT_MEASURABLE, NOT_MEASURABLE_LABEL)
    last = float(rate.iloc[-1])
    fixed = rate <= rate_b * (1 - MIN_DROP)
    if not bool(fixed.any()):
        return res(NOT_FIXED, "Not fixed", after=last)
    first_fixed = fixed.idxmax()
    later = [d for d in rate.index if d > first_fixed]
    regained = [d for d in later if rate[d] >= rate_b * REGAIN and roll_f[d] > 0]
    if regained:
        since = regained[0]
        return res(REBROKE, _rebroke_label(since), since=since, after=last)
    return res(HELD, f"Held {n} days", after=last)


def _rebroke_label(since: date) -> str:
    return f"Re-broke {since:%b} {since.day}"


def _too_early(n: int) -> str:
    return f"Too early ({n} of {MIN_AFTER_DAYS} days)"


def _pct(part: float, whole: float) -> str:
    """A failure rate for the basis line: '2.7%', '0.037%', '0%', '—' with no runs."""
    if not whole > 0:
        return "—"
    pct = 100.0 * part / whole
    if pct <= 0:
        return "0%"
    if pct >= 10:
        return f"{pct:.0f}%"
    if pct >= 1:
        return f"{pct:.1f}%"
    if pct < 0.001:
        return "<0.001%"
    return f"{pct:.2g}%"


def action_held(entity_type: object, entity_key: object, done: date | None,
                daily: pd.DataFrame | None, today: date, *, source: object = "", title: object = "",
                rate: float = DEFAULT_CREDIT_PRICE_USD, spike_calendar: str | None = None) -> dict:
    """The measured outcome of one completed item. Keys: state, label, signal, since, after_days, before,
    after, fails_before, runs_before, fails_after, runs_after, z, direction. Never raises.

    ``source`` / ``title`` are the item's SOURCE and TITLE: a Control Room triage item is judged on the
    signal it was tracked for (a task on its failure rate, a warehouse on the triage spend test in the
    direction of its TITLE), never on a level rule. ``rate`` prices that spend test's $ floor and
    ``spike_calendar`` is EXPECTED_SPIKE_CALENDAR (None = the shipped default, '' = none)."""
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
    triage = is_triage_source(source)
    if triage and kind == "WAREHOUSE":
        return _spend_recurrence(per_day, after_idx, title=title, rate=rate, spike_calendar=spike_calendar)
    if kind != "WAREHOUSE":
        failures = _failure_arm(per_day, before_idx, after_idx, forced=triage and kind == "TASK")
        if failures is not None:
            return failures
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
        fb, rb = safe_float(result.get("fails_before")), safe_float(result.get("runs_before"))
        fa, ra = safe_float(result.get("fails_after")), safe_float(result.get("runs_after"))
        return (f"{int(fb):,} failed of {int(rb):,} runs in the {BASELINE_DAYS} days before "
                f"({_pct(fb, rb)}); {int(fa):,} of {int(ra):,} since ({_pct(fa, ra)})")
    if signal == SIGNAL_SPEND_ANOMALY:
        word = {1: "spike", -1: "collapse"}.get(int(safe_float(result.get("direction"))), "")
        if not word:
            return ""
        since = result.get("since")
        if _text(result.get("state")) == REBROKE and isinstance(since, date):
            return (f"a spend {word} again on {since:%b} {since.day}: "
                    f"{format_usd(safe_float(after))}/day, z {safe_float(result.get('z')):+.1f} against its "
                    f"own {TRIAGE_WINDOW_DAYS} days (the triage scan's test)")
        return (f"no spend {word} the triage scan would raise (|z| >= {DEFAULT_THRESHOLD:g} against its own "
                f"{TRIAGE_WINDOW_DAYS} days) in the {n} day{'' if n == 1 else 's'} since")
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
                 key_col: str = "SOURCE_ENTITY_KEY", source_col: str = "SOURCE", title_col: str = "TITLE",
                 spike_calendar: str | None = None,
                 lookback_days: int = LOOKBACK_DAYS) -> tuple[pd.Series, pd.Series]:
    """(Held? label, HELD_BASIS) per row: None for a row that is not DONE; 'Not measurable' for a type
    with no signal; 'Unavailable' when the signals read failed; 'Not checked' outside the evaluated set
    (the newest MAX_ENTITIES completions within the lookback); else the measured label -- a Control Room
    triage row (``source_col``) on the signal it was tracked for (see action_held)."""
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
            res = action_held(kind, key, done, daily, today, source=rec.get(source_col),
                              title=rec.get(title_col), rate=rate, spike_calendar=spike_calendar)
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
