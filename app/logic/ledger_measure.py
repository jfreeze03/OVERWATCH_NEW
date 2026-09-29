"""Measured before/after for a MANUAL savings-ledger verify (Next-Fifty #46(d)). Pure module.

The ledger's hand verify used to take a free-typed monthly figure. For the finding types the app books
itself, the marts already hold a before/after: this module turns one row of
``mart_sql.ledger_before_after`` into a monthly figure the verify form prefills (the operator can still
overwrite it) and into the PROOF_RESULT snapshot stamped on the row.

Three bases, by FINDING_TYPE (``ledger_basis``):
  WAREHOUSE — FACT_WAREHOUSE_DAILY credits/day, 14 days before vs up to 30 complete days after the booking
              day, plus the query-volume ratio (MART_WAREHOUSE_EFFICIENCY_DAILY) with the V153 0.7-1.3x
              confounded band (disclosed, never adjusted — the change scan's stance);
  OBJECT    — FACT_OBJECT_COST_DAILY maintenance credits (clustering / search optimization / MV refresh) on
              the object, same windows (the unread-maintenance ESTIMATED rows);
  TABLE     — MART_TABLE_STORAGE_DAILY time-travel bytes: the last snapshot before vs the latest after,
              priced at the storage $/TB-month.
The booking day itself is partial and skipped. The after window ends at the earliest of the mart's last
loaded day, yesterday (account time) and booking + 30, so a stalled loader never reads as a saving.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta

WAREHOUSE_FINDING_TYPES = frozenset({"SCHEDULE", "AUTO_SUSPEND", "STATEMENT_TIMEOUT", "RESIZE", "MAX_CLUSTERS"})
TABLE_FINDING_TYPES = frozenset({"RETENTION"})
# == the unread-maintenance lever's ARM_FINDING_TYPE values (the integration test pins the two together)
OBJECT_FINDING_TYPES = frozenset({"SUSPEND_RECLUSTER", "DROP_SEARCH_OPTIMIZATION", "SUSPEND_MV_REFRESH"})
OBJECT_COST_ARMS = ("CLUSTERING", "SEARCH_OPT", "MV_REFRESH")   # FACT_OBJECT_COST_DAILY maintenance arms
BASES = ("WAREHOUSE", "TABLE", "OBJECT")
BEFORE_DAYS, MAX_AFTER_DAYS, MIN_AFTER_DAYS = 14, 30, 7
VOLUME_BAND = (0.7, 1.3)          # the V153 confounded band (mart_sql.savings_ledger's VOLUME_CONFOUNDED)
MONTH_DAYS = 30
TB = 1024 ** 4
MEASURED, TOO_EARLY, NO_DATA = "MEASURED", "TOO_EARLY", "NO_DATA"
PROOF_RESULT_MAX_CHARS = 16_000   # SAVINGS_LEDGER.PROOF_RESULT is VARCHAR(16000) (V053)


def ledger_basis(finding_type: object) -> str | None:
    """'WAREHOUSE' | 'TABLE' | 'OBJECT' for a finding type the marts can measure; None otherwise
    ('unclassified', 'EXPERIMENT', free text, blank)."""
    kind = str(finding_type or "").strip().upper()
    if kind in WAREHOUSE_FINDING_TYPES:
        return "WAREHOUSE"
    if kind in TABLE_FINDING_TYPES:
        return "TABLE"
    if kind in OBJECT_FINDING_TYPES:
        return "OBJECT"
    return None


def _num(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _day(value: object) -> date | None:
    """A calendar day from a date / datetime / pandas Timestamp / ISO string; None for NULL/NaT/garbage."""
    if value is None:
        return None
    try:
        if value != value:            # NaN / pandas NaT
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        try:
            return value.date()
        except ValueError:            # pandas NaT subclasses datetime and cannot produce a date
            return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.upper() in ("NAT", "NAN", "NONE"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _int(value: object, default: int) -> int:
    v = _num(value)
    return default if v is None or v <= 0 else int(v)


def _fmt_day(d: date | None) -> str:
    return "—" if d is None else f"{d:%b} {d.day}"


def ledger_measurement(row: Mapping[str, object], *, basis: str, rate: float,
                       storage_usd_per_tb: float, today: date) -> dict:
    """One ``ledger_before_after`` row -> the measured monthly saving.

    Keys: state (MEASURED / TOO_EARLY / NO_DATA), monthly_usd (raw, may be negative: the level rose),
    prefill_usd (= max(0, round(monthly_usd, 2)); 0 unless MEASURED), before_per_day / after_per_day
    (credits per day; None on TABLE), before_bytes / after_bytes (TABLE only), after_days, after_end,
    volume_ratio (WAREHOUSE only; None when unmeasurable), confounded (volume outside 0.7-1.3x), note."""
    kind = str(basis or "").strip().upper()
    if kind not in BASES:
        raise ValueError(f"No measured basis for {kind or 'blank'}")
    booked = _day(row.get("BOOKED_DAY"))
    out: dict = {"state": NO_DATA, "basis": kind, "monthly_usd": None, "prefill_usd": 0.0,
                 "before_per_day": None, "after_per_day": None, "before_bytes": None, "after_bytes": None,
                 "after_days": 0, "after_end": None, "volume_ratio": None, "confounded": False, "note": ""}
    if booked is None:
        out["note"] = "The booking day is unknown, so nothing can be measured around it."
        return out
    if kind == "TABLE":
        return _table_measurement(row, booked, out, storage_usd_per_tb)
    before_days = _int(row.get("BEFORE_DAYS"), BEFORE_DAYS)
    max_after = _int(row.get("MAX_AFTER_DAYS"), MAX_AFTER_DAYS)
    loaded = _day(row.get("LOADED_THROUGH"))
    before = _num(row.get("BEFORE_CREDITS"))
    what = "warehouse credits" if kind == "WAREHOUSE" else "maintenance credits"
    if before is None or loaded is None:
        out["note"] = f"No {what} in the mart for the {before_days} days before the booking."
        return out
    end = min(loaded, today - timedelta(days=1), booked + timedelta(days=max_after))
    after_days = max(0, (end - booked).days)
    after = _num(row.get("AFTER_CREDITS")) or 0.0
    before_pd = before / before_days
    out.update(before_per_day=round(before_pd, 4), after_days=after_days, after_end=end)
    if after_days < MIN_AFTER_DAYS:
        out["state"] = TOO_EARLY
        out["note"] = (f"Only {after_days} complete day(s) since the booking (through {_fmt_day(end)}); "
                       f"a measured figure needs {MIN_AFTER_DAYS}.")
        return out
    after_pd = after / after_days
    monthly = (before_pd - after_pd) * MONTH_DAYS * float(rate)
    out.update(state=MEASURED, after_per_day=round(after_pd, 4), monthly_usd=round(monthly, 2),
               prefill_usd=max(0.0, round(monthly, 2)))
    note = (f"{what.capitalize()}: {before_days} days before vs {after_days} complete days after "
            f"(through {_fmt_day(end)}), x {MONTH_DAYS} days x the credit rate.")
    if kind == "WAREHOUSE":
        bq, aq = _num(row.get("BEFORE_QUERIES")), _num(row.get("AFTER_QUERIES"))
        if bq and aq is not None:
            ratio = (aq / after_days) / (bq / before_days)
            out["volume_ratio"] = round(ratio, 2)
            out["confounded"] = not (VOLUME_BAND[0] <= ratio <= VOLUME_BAND[1])
            if out["confounded"]:
                note += (f" Query volume after is {ratio:.2f}x the baseline (outside "
                         f"{VOLUME_BAND[0]}-{VOLUME_BAND[1]}x): the figure is volume-confounded, not adjusted.")
    if monthly < 0:
        note += " The level ROSE after the change, so the prefill is 0."
    out["note"] = note
    return out


def _table_measurement(row: Mapping[str, object], booked: date, out: dict, storage_usd_per_tb: float) -> dict:
    before, after = _num(row.get("BEFORE_TT_BYTES")), _num(row.get("AFTER_TT_BYTES"))
    after_day = _day(row.get("AFTER_SNAPSHOT_DAY"))
    if before is None:
        out["note"] = ("No time-travel snapshot before the booking: the table-storage mart keeps about two "
                       "weeks of daily snapshots, so a change booked earlier cannot be measured here.")
        return out
    if after is None or after_day is None:
        out["note"] = "No time-travel snapshot after the booking yet."
        return out
    after_days = max(0, (after_day - booked).days)
    out.update(before_bytes=before, after_bytes=after, after_days=after_days, after_end=after_day)
    if after_days < MIN_AFTER_DAYS:
        out["state"] = TOO_EARLY
        out["note"] = (f"The latest snapshot is {after_days} day(s) after the booking "
                       f"({_fmt_day(after_day)}); a measured figure needs {MIN_AFTER_DAYS}.")
        return out
    monthly = (before - after) / TB * float(storage_usd_per_tb)
    out.update(state=MEASURED, monthly_usd=round(monthly, 2), prefill_usd=max(0.0, round(monthly, 2)))
    note = (f"Time-travel bytes: the snapshot of {_fmt_day(_day(row.get('BEFORE_SNAPSHOT_DAY')))} vs "
            f"{_fmt_day(after_day)}, x the storage rate per TB-month (fail-safe bytes are not counted).")
    rb, ra = _num(row.get("BEFORE_RETENTION_DAYS")), _num(row.get("AFTER_RETENTION_DAYS"))
    if rb is not None and ra is not None and rb == ra:
        note += f" Retention reads {int(ra)} day(s) on both snapshots: check that the ALTER ran."
    if monthly < 0:
        note += " Time-travel bytes ROSE, so the prefill is 0."
    out["note"] = note
    return out


def _jsonable(value: object) -> object:
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def proof_result_json(measure: Mapping[str, object], *, target: str, basis: str, entered_usd: float,
                      sql_hash: str) -> str:
    """The PROOF_RESULT snapshot stamped on a measured verify: the measurement, what the operator entered
    and the read's SQL hash (joins APP_QUERY_TELEMETRY when the query id is blank). JSON, <= 16000 chars."""
    payload = {"kind": "ledger_before_after", "basis": str(basis), "target": str(target)[:600],
               "entered_usd": _jsonable(float(entered_usd)), "sql_hash": str(sql_hash)[:64]}
    for key in ("state", "monthly_usd", "prefill_usd", "before_per_day", "after_per_day", "before_bytes",
                "after_bytes", "after_days", "after_end", "volume_ratio", "confounded"):
        payload[key] = _jsonable(measure.get(key))
    payload["note"] = str(measure.get("note") or "")[:4000]
    text = json.dumps(payload, sort_keys=True)
    if len(text) > PROOF_RESULT_MAX_CHARS:            # defensive: every field above is already bounded
        payload["note"] = ""
        text = json.dumps(payload, sort_keys=True)[:PROOF_RESULT_MAX_CHARS]
    return text
