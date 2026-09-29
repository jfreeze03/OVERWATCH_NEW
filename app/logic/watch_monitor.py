"""Watched-entity monitoring (owner ask 2026-08-17).

Watching an entity in Entity 360 used to be a passive bookmark — it pinned the
entity in tables and showed an SLO badge only when you opened the Watchlist tab.
Nothing ran; it waited for you. This turns "watch" into "surface this entity's
status where I'll see it first": for each watched WAREHOUSE, evaluate a cost
spike/drop (the same robust anomaly sweep the Cost page uses) and a health-grade
drop (the per-warehouse health chip), so the Brief and the Watchlist tab flag a
watched entity that moved — without the operator hunting for it.

Read-only: this detects and surfaces, it never acts. Next-Fifty #46 adds two
arms read from the entity's own daily marts (workbench_sql.entity_daily_signals,
passed in as ``entity_daily``): a watched TASK flags failed runs since yesterday
and a P95 runtime spike; a watched QUERY_FINGERPRINT (query family) flags an
attributed-cost spike/drop and a P95 spike. Other watches (product, user, a bare
'QUERY') pass through with no proactive signal here — the pin + SLO badge still
cover them elsewhere. Pure pandas; tested in tests/test_watch_monitor.py.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

import pandas as pd

from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    anomaly_summary,
    complete_days_only,
    flag_anomalies,
    suppress_expected_spikes,
)
from app.logic.formulas import account_today, humanize_duration, safe_float

_ATTENTION_GRADES = {"WATCH", "DEGRADED", "AT RISK"}
_BAD_GRADES = {"DEGRADED", "AT RISK"}

# Next-Fifty #46 watch arms. The family cost floor is its own, lower materiality gate: at the shared
# ANOMALY_MIN_USD ($50/day) a query family's cost spike would almost never fire.
WATCH_SIGNAL_TYPES = ("TASK", "QUERY_FINGERPRINT")
WATCH_TASK_MIN_P95_SEC = 60.0
WATCH_FAMILY_MIN_P95_SEC = 10.0
WATCH_FAMILY_MIN_USD = 10.0


def _signal_rows(entity_daily: pd.DataFrame, etype: str, ekey: str) -> pd.DataFrame:
    """One watched entity's rows from entity_daily_signals, DAY parsed to a date (case-insensitive key)."""
    if entity_daily.empty or not {"ENTITY_TYPE", "ENTITY_KEY_U", "DAY"}.issubset(entity_daily.columns):
        return pd.DataFrame(columns=["ENTITY_TYPE", "ENTITY_KEY_U", "DAY"])
    mask = ((entity_daily["ENTITY_TYPE"].astype(str).str.strip().str.upper() == etype)
            & (entity_daily["ENTITY_KEY_U"].astype(str).str.strip().str.upper() == ekey))
    rows = entity_daily[mask].copy()
    rows["DAY"] = pd.to_datetime(rows["DAY"], errors="coerce").dt.date
    return rows[rows["DAY"].notna()].assign(ENTITY_KEY_U=ekey)


def _recent_hits(rows: pd.DataFrame, value_col: str, today: date, *, min_value: float,
                 calendar: str = "") -> list[dict]:
    """Anomalies of ``value_col`` over COMPLETE days, kept only on the last two complete loaded days (the
    warehouse arm's r6-bug5 recency cut, anchored on the mart's own LOADED_THROUGH so a mart lag keeps its
    grace and a weeks-old spike never re-fires)."""
    if rows.empty or value_col not in rows.columns:
        return []
    complete = rows[rows["DAY"] < today].copy()
    if complete.empty:
        return []
    complete[value_col] = pd.to_numeric(complete[value_col], errors="coerce")
    flagged = flag_anomalies(complete, value_col, group_col="ENTITY_KEY_U", min_value=min_value,
                             min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
    if calendar:
        flagged = suppress_expected_spikes(flagged, calendar)
    anchor = today - timedelta(days=1)
    if "LOADED_THROUGH" in rows.columns:
        loaded = pd.to_datetime(rows["LOADED_THROUGH"], errors="coerce").max()
        if pd.notna(loaded):
            anchor = min(anchor, loaded.date())
    flagged = flagged[flagged["DAY"] >= anchor - timedelta(days=1)]
    return anomaly_summary(flagged, "ENTITY_KEY_U", value_col)


def _task_arm(rows: pd.DataFrame, today: date) -> tuple[list[str], str, str]:
    """(parts, severity, status-when-quiet) for a watched TASK."""
    if rows.empty:
        return [], "", "no runs in the last 30 days"
    parts: list[str] = []
    severity = ""
    fails = pd.to_numeric(rows.get("FAILS"), errors="coerce") if "FAILS" in rows.columns else None
    if fails is not None:
        n = int(fails[rows["DAY"] >= today - timedelta(days=1)].fillna(0).sum())
        if n > 0:
            parts.append(f"{n:,} failed run{'' if n == 1 else 's'} since yesterday")
            severity = "warn"
    for h in _recent_hits(rows, "P95_SEC", today, min_value=WATCH_TASK_MIN_P95_SEC):
        z = safe_float(h.get("z"))
        if z > 0:
            parts.append(f"runtime spike (P95 {humanize_duration(h.get('value'), 's')}, z {z:+.1f})")
            severity = severity or "watch"
            break
    return parts, severity, "steady"


def _family_arm(rows: pd.DataFrame, today: date, rate: float, calendar: str) -> tuple[list[str], str, str]:
    """(parts, severity, status-when-quiet) for a watched QUERY_FINGERPRINT (query family)."""
    if rows.empty:
        return [], "", "not in the family marts in the last 30 days"
    parts: list[str] = []
    severity = ""
    if "CREDITS" in rows.columns:
        priced = rows.assign(USD=pd.to_numeric(rows["CREDITS"], errors="coerce").fillna(0.0) * safe_float(rate, 3.68))
        for h in _recent_hits(priced, "USD", today, min_value=WATCH_FAMILY_MIN_USD, calendar=calendar):
            z = safe_float(h.get("z"))
            parts.append(f"spend {'spike' if z > 0 else 'drop'} (z {z:+.1f})")
            severity = "warn" if z > 0 else "watch"
            break
    for h in _recent_hits(rows, "P95_SEC", today, min_value=WATCH_FAMILY_MIN_P95_SEC):
        z = safe_float(h.get("z"))
        if z > 0:
            parts.append(f"P95 spike ({humanize_duration(h.get('value'), 's')}, z {z:+.1f})")
            severity = severity or "watch"
            break
    return parts, severity, "steady"


def watched_status(watchlist: pd.DataFrame | None,
                   wh_daily: pd.DataFrame | None,
                   health: pd.DataFrame | None,
                   rate: float = 3.68,
                   calendar: str = "",
                   *,
                   entity_daily: pd.DataFrame | None = None,
                   signal_keys: Iterable[tuple[str, str]] | None = None,
                   today: date | None = None) -> pd.DataFrame:
    """Per watched entity: does it need attention, and why.

    ``watchlist``: USER_WATCHLIST rows (ENTITY_TYPE, ENTITY_KEY, LABEL).
    ``wh_daily``: FACT_WAREHOUSE_DAILY (WAREHOUSE_NAME, DAY, CREDITS_TOTAL) — the
      cost-anomaly input; priced with ``rate``.
    ``health``: warehouse_health output (WAREHOUSE_NAME, GRADE).
    ``calendar``: EXPECTED_SPIKE_CALENDAR — known month/quarter-end spikes are labeled
      expected, not flagged (same hygiene as the Cost surfaces).
    ``entity_daily``: entity_daily_signals rows for the watched TASK / QUERY_FINGERPRINT
      entities (None = not read: those watches pass through blank, the pre-#46 behaviour).
    ``signal_keys``: the (TYPE, KEY) pairs that read covered (None = every such watch);
      a watch outside it stays blank rather than reading as 'no runs'.
    ``today``: the account day (default formulas.account_today()).
    Returns ENTITY_TYPE, ENTITY_KEY, LABEL, ATTENTION (bool), STATUS, SEVERITY —
    attention rows first. Empty watchlist -> empty frame."""
    cols = ["ENTITY_TYPE", "ENTITY_KEY", "LABEL", "ATTENTION", "STATUS", "SEVERITY"]
    if watchlist is None or watchlist.empty or "ENTITY_KEY" not in watchlist.columns:
        return pd.DataFrame(columns=cols)
    day = today or account_today()
    covered = (None if signal_keys is None
               else {(str(t).strip().upper(), str(k).strip().upper()) for t, k in signal_keys})

    # Cost spike/drop per warehouse — the SAME robust sweep the Cost page runs.
    cost_hits: dict[str, dict] = {}
    if wh_daily is not None and not wh_daily.empty and "CREDITS_TOTAL" in wh_daily.columns:
        priced = complete_days_only(wh_daily.copy())
        priced["USD"] = pd.to_numeric(priced["CREDITS_TOTAL"], errors="coerce").fillna(0.0) * safe_float(rate, 3.68)
        flagged = flag_anomalies(priced, "USD", group_col="WAREHOUSE_NAME",
                                 min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
        # Same anomaly hygiene as the Cost decision surfaces: (1) a known month/quarter-end
        # spike is 'expected', not flagged; (2) only the last TWO complete days count as CURRENT
        # attention — else a weeks-old spike still inside the 30d window re-fires as 'moved'
        # every day (r6-bug5). Restrict to that recent window BEFORE anomaly_summary so a genuine
        # current spike is never crowded out of its top-10 by stronger HISTORICAL spikes
        # elsewhere in the account; the 2-day grace survives mart-lag and a day-missed Brief.
        flagged = suppress_expected_spikes(flagged, calendar)
        if "DAY" in flagged.columns:
            _fdays = pd.to_datetime(flagged["DAY"], errors="coerce")
            _latest = _fdays.max()
            if pd.notna(_latest):
                # datetime.timedelta, NOT pd.Timedelta: subtracting a pd.Timedelta from a
                # Timestamp and comparing to a datetime64 Series triggers numpy's
                # "generic unit timedelta is deprecated ... will raise in the future"
                # (numpy 2.5+); datetime.timedelta is exempt. (bug-hunt round 10)
                flagged = flagged[_fdays >= (_latest.normalize() - timedelta(days=1))]
        for h in anomaly_summary(flagged, "WAREHOUSE_NAME", "USD"):
            # anomaly_summary is strongest-first; keep the STRONGEST day per warehouse.
            cost_hits.setdefault(str(h.get("label", "")).upper(), h)

    grade_by_wh: dict[str, str] = {}
    if health is not None and not health.empty and "WAREHOUSE_NAME" in health.columns:
        grade_by_wh = {str(r["WAREHOUSE_NAME"]).upper(): str(r.get("GRADE", ""))
                       for _, r in health.iterrows()}

    rows = []
    for _, w in watchlist.iterrows():
        etype = str(w.get("ENTITY_TYPE", "")).upper()
        ekey = str(w.get("ENTITY_KEY", "")).upper()
        parts: list[str] = []
        severity = ""
        quiet = ""                  # the status when nothing moved ('' = not evaluated here)
        is_warehouse = etype == "WAREHOUSE"
        if is_warehouse:
            quiet = "steady"        # evaluated, nothing moved
            hit = cost_hits.get(ekey)
            if hit:
                z = safe_float(hit.get("z"))
                parts.append(f"spend {'spike' if z > 0 else 'drop'} (z {z:+.1f})")
                severity = "warn"
            grade = grade_by_wh.get(ekey, "")
            if grade.upper() in _ATTENTION_GRADES:
                parts.append(f"health: {grade}")   # keep canonical casing ("At risk")
                if grade.upper() in _BAD_GRADES:
                    severity = "warn"
                elif not severity:
                    severity = "watch"
        elif (etype in WATCH_SIGNAL_TYPES and entity_daily is not None
              and (covered is None or (etype, ekey) in covered)):
            rows_e = _signal_rows(entity_daily, etype, ekey)
            parts, severity, quiet = (_task_arm(rows_e, day) if etype == "TASK"
                                      else _family_arm(rows_e, day, rate, calendar))
        status = "; ".join(parts) if parts else quiet
        rows.append({
            "ENTITY_TYPE": str(w.get("ENTITY_TYPE", "")),
            "ENTITY_KEY": str(w.get("ENTITY_KEY", "")),
            "LABEL": str(w.get("LABEL", "") or w.get("ENTITY_KEY", "")),
            "ATTENTION": bool(parts),
            "STATUS": status,
            "SEVERITY": severity,
        })
    out = pd.DataFrame(rows)
    return out.sort_values("ATTENTION", ascending=False, kind="stable").reset_index(drop=True)[cols]


def watch_summary(status: pd.DataFrame | None) -> dict:
    """Headline for the Brief callout: how many watched entities need attention."""
    if status is None or status.empty:
        return {"watched": 0, "attention": 0, "warn": 0}
    att = status[status["ATTENTION"]]
    return {"watched": len(status),
            "attention": len(att),
            "warn": int((att["SEVERITY"] == "warn").sum())}
