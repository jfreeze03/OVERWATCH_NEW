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
MAX_OVERLAPS_LISTED = 20          # overlapping ledger items named in the warning / PROOF_RESULT


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
                      sql_hash: str, overlaps: Mapping[str, object] | None = None) -> str:
    """The PROOF_RESULT snapshot stamped on a measured verify: the measurement, what the operator entered
    and the read's SQL hash (joins APP_QUERY_TELEMETRY when the query id is blank). JSON, <= 16000 chars.

    ``overlaps`` (a ``ledger_overlaps`` result) adds ``overlapping_items`` / ``overlap_check`` ONLY when
    another booked change shares the measured window or the check could not be completed, so a clean
    verify's JSON is unchanged."""
    payload = {"kind": "ledger_before_after", "basis": str(basis), "target": str(target)[:600],
               "entered_usd": _jsonable(float(entered_usd)), "sql_hash": str(sql_hash)[:64]}
    for key in ("state", "monthly_usd", "prefill_usd", "before_per_day", "after_per_day", "before_bytes",
                "after_bytes", "after_days", "after_end", "volume_ratio", "confounded"):
        payload[key] = _jsonable(measure.get(key))
    payload["note"] = str(measure.get("note") or "")[:4000]
    if overlaps is not None:
        items = overlaps.get("items")
        labels = [str(i.get("label") or "")[:160] for i in items if isinstance(i, Mapping)] \
            if isinstance(items, list) else []
        if labels:
            payload["overlapping_items"] = labels[:MAX_OVERLAPS_LISTED]
        if not overlaps.get("complete", True):
            payload["overlap_check"] = "incomplete"
    text = json.dumps(payload, sort_keys=True)
    if len(text) > PROOF_RESULT_MAX_CHARS:            # defensive: every field above is already bounded
        payload["note"] = ""
        text = json.dumps(payload, sort_keys=True)[:PROOF_RESULT_MAX_CHARS]
    return text


def _norm_target(value: object) -> str:
    """A ledger target as ledger_before_after keys it: upper-case, trimmed, quotes stripped; '' for NULL."""
    if value is None:
        return ""
    try:
        if value != value:            # NaN / NaT
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().upper().replace('"', "")


def _blank(value: object) -> bool:
    return _norm_target(value) == ""


def ledger_overlaps(ledger: object, *, item_id: object, target: object, booked_day: date,
                    window_end: date | None = None, row_cap: int | None = None) -> dict:
    """Other savings-ledger rows booked on the SAME target inside this item's measured window (review C22).

    ``ledger_before_after`` is keyed on the target and the booking day only, so every other change booked on
    that warehouse / object / table between ``booked_day - BEFORE_DAYS`` and ``window_end`` (the measurement's
    after_end; ``booked_day + MAX_AFTER_DAYS`` when unknown) moves the same before/after delta. Prefilling it
    would credit the whole change to this one item, and verifying two such items counts it twice.

    ``ledger`` is the savings_ledger() frame (any object with ``to_dict('records')``). A row matches on its
    TARGET_OBJECT, else (a change-scan row carries no target) on its registry CHANGE_WAREHOUSE. Excluded: the
    item itself, REJECTED rows, and superseded manual twins (their settled change-scan row stands for the same
    change). A row's day is its CREATED_AT day, the frame's own sort key, so the coverage test is exact.

    ``row_cap``: the frame is the ledger PAGE (newest CREATED_AT first, LIMIT row_cap). When it came back full
    and its oldest row is not older than the window start, rows past the cap could overlap: ``complete`` is
    then False and the caller must not treat the item as alone.

    Returns {"items": [{"item_id", "label", "description", "day"}] (by day, at most MAX_OVERLAPS_LISTED),
             "count": every overlap, "complete": bool, "start": date, "end": date}."""
    start = booked_day - timedelta(days=BEFORE_DAYS)
    end = window_end if isinstance(window_end, date) else booked_day + timedelta(days=MAX_AFTER_DAYS)
    end = max(end, booked_day)
    out: dict = {"items": [], "count": 0, "complete": True, "start": start, "end": end}
    to_records = getattr(ledger, "to_dict", None)
    records = to_records("records") if callable(to_records) else []
    key = _norm_target(target)
    me = str(item_id or "").strip()
    found: list[dict] = []
    oldest: date | None = None
    for rec in records:
        day = _day(rec.get("CREATED_AT"))
        if day is not None and (oldest is None or day < oldest):
            oldest = day
        rid = str(rec.get("ITEM_ID") or "").strip()
        if not key or day is None or rid == me:
            continue
        if str(rec.get("STATE") or "").strip().upper() == "REJECTED":
            continue
        if not _blank(rec.get("SUPERSEDED_BY_CHANGE_ID")):
            continue
        tgt = _norm_target(rec.get("TARGET_OBJECT")) or _norm_target(rec.get("CHANGE_WAREHOUSE"))
        if tgt != key or not (start <= day <= end):
            continue
        finding = str(rec.get("FINDING_TYPE") or "").strip() or "unclassified"
        source = str(rec.get("SOURCE") or "").strip() or "manual"
        state = str(rec.get("STATE") or "").strip().upper() or "?"
        found.append({"item_id": rid, "day": day,
                      "description": str(rec.get("DESCRIPTION") or "").strip()[:120],
                      "label": f"{rid[:8]} {finding} ({source}, {state}, booked {_fmt_day(day)})"})
    found.sort(key=lambda r: (r["day"], r["item_id"]))
    out["count"] = len(found)
    out["items"] = found[:MAX_OVERLAPS_LISTED]
    if row_cap is not None and len(records) >= int(row_cap) and (oldest is None or oldest >= start):
        out["complete"] = False
    return out


def verify_prefill(*, item_id: str, target: float | None, last: Mapping[str, object] | None,
                   widget_value: object, clicked: bool) -> dict:
    """What the verify form's amount widget should hold this run (review C12 / C17). Pure.

    ``target``: the measured prefill; None when there is nothing to prefill (not measured, or another booked
    change shares the measured window). ``last``: the previous run's state, {"item": the item id, "val": the
    amount OVERWATCH last left in the widget, 0.0 being the widget's own default; None = unknown}.
    ``widget_value``: the widget's current value, None when its key is ABSENT (never rendered yet, or
    Streamlit dropped it after the operator left the section). ``clicked``: this is the Verify click's rerun.

    Rules:
      * the Verify click's rerun never moves the amount (the UPDATE writes the widget's value: the one st.code
        showed, or one the operator typed in the same rerun);
      * another item takes its own prefill, or 0.0 when it has none, so one item's amount never carries over;
      * a dropped widget re-arms the prefill (the operator's value went with the widget state anyway);
      * an untouched widget (still holding what OVERWATCH left there) follows the measurement, back to 0.0
        when the prefill is withdrawn;
      * an edited widget is never overwritten.

    Returns {"write": the value to put in the widget, None = leave it alone; "state": the new ``last``;
             "kept_edit": True when the operator's own entry differs from the measured prefill}."""
    tgt = None if target is None else round(float(target), 2)
    prev = dict(last) if isinstance(last, Mapping) else {}
    prev_item = prev.get("item")
    prev_val = _num(prev.get("val"))
    present = widget_value is not None
    cur = _num(widget_value) if present else None
    keep = {"write": None, "state": {"item": prev_item, "val": prev_val}, "kept_edit": False}
    if clicked:
        return keep
    if prev_item != item_id:
        if tgt is not None:
            return {"write": tgt, "state": {"item": item_id, "val": tgt}, "kept_edit": False}
        if prev_item is not None and present:
            return {"write": 0.0, "state": {"item": item_id, "val": 0.0}, "kept_edit": False}
        return {"write": None, "state": {"item": item_id, "val": None if present else 0.0}, "kept_edit": False}
    if not present:
        if tgt is not None:
            return {"write": tgt, "state": {"item": item_id, "val": tgt}, "kept_edit": False}
        return {"write": None, "state": {"item": item_id, "val": 0.0}, "kept_edit": False}
    untouched = prev_val is not None and cur is not None and round(cur, 2) == round(prev_val, 2)
    if untouched:
        new = tgt if tgt is not None else 0.0
        if cur is not None and round(new, 2) != round(cur, 2):
            return {"write": new, "state": {"item": item_id, "val": new}, "kept_edit": False}
        return {"write": None, "state": {"item": item_id, "val": prev_val}, "kept_edit": False}
    kept = tgt is not None and cur is not None and round(cur, 2) != tgt
    return {"write": None, "state": {"item": item_id, "val": prev_val}, "kept_edit": kept}
