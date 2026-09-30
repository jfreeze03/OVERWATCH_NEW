"""Company all-in showback (#42 Part 1): pure logic for Cost > Chargeback & AI.

Splits the window's billed metering plus estimated storage by company wherever a fact
carries a company key, and keeps everything without one on explicit account-level rows,
so the rows always add up to the all-in total:

- company rows: warehouse metering (FACT_WAREHOUSE_DAILY, by the warehouse's company,
  before the cloud-services adjustment), serverless maintenance from the object-cost
  ledger's non-query arms (by database), Cortex Code token credits in Snowsight and the
  CLI (by user) and estimated database + fail-safe storage (by database);
- account rows: the cloud-services adjustment (a credit, never taken out of a company)
  and the unattributed remainder (billed metering + estimated storage minus everything
  with a key).

Shares are on one basis: the spend BEFORE the cloud-services adjustment (the all-in total
with the adjustment added back = the company rows + the unattributed row). The company rows
are metering before the adjustment, so dividing them by the after-adjustment all-in total
could read over 100% (R1-12); the adjustment row carries no share.

Nothing is allocated by a share. Input is the one long frame
``app.data.chargeback_sql.company_allin_showback`` returns (LINE_KIND rows). Pure: no
``app.data`` import and no clock; the builder fixes the span in SQL.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta

import pandas as pd

from app.config import DEFAULT_SETTINGS
from app.logic.cost_coverage import service_category
from app.logic.formulas import DEFAULT_STORAGE_USD_PER_TB_MONTH, credits_to_usd, format_usd, safe_float

# The non-query arms of the V139 SP_LOAD_OBJECT_COST, each stamped COMPANY_FOR_DATABASE.
# The QUERY_COMPUTE* arms are measured slices of warehouse compute and
# QUERY_COMPUTE_RESIDUAL is the same compute with no object, so adding them to a
# company's warehouse dollars would double count.
SERVERLESS_ARMS: tuple[str, ...] = ("CLUSTERING", "MV_REFRESH", "SEARCH_OPT", "SERVERLESS_TASK", "SNOWPIPE")

# The SOURCE literals of loader arm [9] (SP_LOAD_MARTS_V27, the Cortex Code views). Cortex
# Code Desktop is not loaded, so it stays on the unattributed row; the panel copy says so
# and tests/test_company_showback_sql.py ties the two together.
COCO_SOURCES: tuple[str, ...] = ("Snowsight", "CLI")

# FACT_STORAGE_ACCOUNT_DAILY tiers (V046); the builder reads f"{tier}_BYTES".
STORAGE_TIERS: tuple[str, ...] = ("TABLE", "STAGE", "FAILSAFE", "HYBRID", "ARCHIVE_COOL", "ARCHIVE_COLD")

FRAME_COLUMNS: tuple[str, ...] = (
    "LINE_KIND", "COMPANY", "SERVICE_TYPE", "CREDITS", "CREDITS_ADJUSTMENT", "TIB_MO",
    "FIRST_DAY", "LAST_DAY", "DAYS_IN_SPAN", "LOADED_AT",
)
TABLE_COLUMNS: tuple[str, ...] = (
    "COMPANY", "WAREHOUSE_USD", "SERVERLESS_USD", "AI_USD", "OTHER_METERED_USD",
    "STORAGE_EST_USD", "TOTAL_USD", "SHARE_OF_TOTAL_PCT",
)
BREAKDOWN_COLUMNS: tuple[str, ...] = (
    "SERVICE_FAMILY", "ACCOUNT_USD", "COMPANY_ROWS_USD", "ADJUSTMENT_USD", "UNATTRIBUTED_USD", "CONTENTS",
)

UNKNOWN_ROW = "UNKNOWN (unmapped)"
ADJUSTMENT_ROW = "Account-level: cloud-services adjustment"
UNATTRIBUTED_ROW = "Account-level: unattributed (no company key)"
STORAGE_FAMILY = "Storage (estimate)"

# (table, Line, line): the fact behind each company line, named in the coverage notes.
_SOURCE_LINES: tuple[tuple[str, str, str], ...] = (
    ("FACT_WAREHOUSE_DAILY", "Warehouse", "warehouse"),
    ("FACT_OBJECT_COST_DAILY", "Serverless", "serverless"),
    ("FACT_AI_USAGE_DAILY", "Cortex Code", "Cortex Code"),
    ("FACT_STORAGE_DAILY", "Storage", "storage"),
)

_FAMILY_CONTENTS: dict[str, str] = {
    "Warehouse": ("Cloud services outside any warehouse (the CLOUD_SERVICES_ONLY pseudo-warehouse, which the "
                  "warehouse fact skips) and day-boundary offset."),
    "Warehouse (reader)": "Reader-account metering: no company key.",
    "Serverless": ("Serverless with no object-cost ledger arm (for example query acceleration, container "
                   "services, Snowpipe Streaming, serverless alerts) and day-boundary offset."),
    "AI / Cortex": ("AI services other than Cortex Code in Snowsight and the CLI (for example AI functions, "
                    "Cortex Code Desktop, Analyst, Search, Intelligence) and day-boundary offset."),
    "Replication": "Replication metering: no company key in metering.",
    "Hybrid requests (historical)": "Hybrid-table request metering (historical): no company key.",
    STORAGE_FAMILY: ("Stage, hybrid-table and archive storage (no per-database split) and any difference "
                     "between the account and per-database storage views."),
}
_DEFAULT_CONTENTS = "Metered services with no company key."

# metering bucket -> the company-row column it reconciles against
_BUCKET_COLUMN = {"WAREHOUSE": "WAREHOUSE_USD", "SERVERLESS": "SERVERLESS_USD", "AI": "AI_USD",
                  "OTHER": "OTHER_METERED_USD"}
# metering category -> the company-row column that holds its keyed dollars
_KEYED_FAMILY_COLUMN = {"Warehouse": "WAREHOUSE_USD", "Serverless": "SERVERLESS_USD", "AI / Cortex": "AI_USD"}
# a negative family residual smaller than this is rounding noise, not worth a note
_NEGATIVE_NOTE_FLOOR_USD = 1.0
# R1-13: the object-cost and Cortex Code facts reload in their own daily runs, after the 06:45 CT
# metering load has already moved yesterday into the span. A fact whose last load (the COVERAGE
# row's LOADED_AT, MAX(LOAD_TS): Central wall time) ran before its newest span day ended, plus
# this allowance for the ACCOUNT_USAGE views' latency (up to ~3 h), holds that day only in part.
LOAD_LATENCY_HOURS = 3


def storage_tier_rates(settings: Mapping[str, object]) -> dict[str, float]:
    """$/TiB-month per FACT_STORAGE_ACCOUNT_DAILY tier from the Admin SETTINGS (the same
    defaults the Spend & Attribution storage-tier panel used inline). Table and fail-safe
    bill at the standard rate; stage defaults to it."""
    std = safe_float(settings.get("STORAGE_USD_PER_TB_MONTH"), DEFAULT_STORAGE_USD_PER_TB_MONTH)

    def _rate(key: str) -> float:
        return safe_float(settings.get(key), safe_float(DEFAULT_SETTINGS[key]))

    return {
        "TABLE": std,
        "STAGE": safe_float(settings.get("STORAGE_STAGE_USD_PER_TB_MONTH"), std),
        "FAILSAFE": std,
        "HYBRID": _rate("STORAGE_HYBRID_USD_PER_TB_MONTH"),
        "ARCHIVE_COOL": _rate("STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH"),
        "ARCHIVE_COLD": _rate("STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH"),
    }


def metering_bucket(service_type: object) -> str:
    """WAREHOUSE / SERVERLESS / AI / OTHER: which company-row column a metering service
    reconciles against. Reader, replication, historical hybrid requests, a metered
    STORAGE line and anything unknown carry no company key, so they are OTHER."""
    cat = service_category(service_type)
    if cat == "Warehouse":
        return "WAREHOUSE"
    if cat == "Serverless":
        return "SERVERLESS"
    if cat == "AI / Cortex":
        return "AI"
    return "OTHER"


def span_label(first: date, last: date) -> str:
    """'Aug 31 – Sep 29'; the year is added to both ends when the span crosses a year."""
    if first.year == last.year:
        return f"{first:%b} {first.day} – {last:%b} {last.day}"
    return f"{first:%b} {first.day}, {first.year} – {last:%b} {last.day}, {last.year}"


def day_label(d: date) -> str:
    """'Jun 30, 2026' (always with the year: notes can name a day far from the span)."""
    return f"{d:%b} {d.day}, {d.year}"


def _as_date(value: object) -> date | None:
    """A mart DATE cell as a date; NULL / NaT / junk -> None (never raises)."""
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if ts is None or not isinstance(ts, pd.Timestamp) or pd.isna(ts):
        return None
    return ts.date()


def _as_timestamp(value: object) -> datetime | None:
    """A LOAD_TS cell as a naive datetime (the builder sends TIMESTAMP_NTZ Central wall time; an
    aware value keeps its wall time, which the connector gives in the session's Central zone);
    NULL / NaT / junk -> None (never raises)."""
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if ts is None or not isinstance(ts, pd.Timestamp) or pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.to_pydatetime()


def loaded_before_day_complete(loaded_at: datetime, day: date) -> bool:
    """True when a load at ``loaded_at`` ran before ``day`` had fully landed: before the day's
    end (Central) plus LOAD_LATENCY_HOURS. A load then holds that day only in part."""
    return loaded_at < datetime.combine(day + timedelta(days=1), time()) + timedelta(hours=LOAD_LATENCY_HOURS)


def _is_null(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):   # a list-like cell is not a scalar NULL
        return False


def _num(value: object) -> float:
    return safe_float(value)


def _empty_table() -> pd.DataFrame:
    return pd.DataFrame(columns=list(TABLE_COLUMNS))


def _empty_breakdown() -> pd.DataFrame:
    return pd.DataFrame(columns=list(BREAKDOWN_COLUMNS))


def _row_dates(frame: pd.DataFrame, kind: str, service: str | None = None) -> tuple[date | None, date | None, int]:
    rows = frame[frame["LINE_KIND"] == kind]
    if service is not None:
        rows = rows[rows["SERVICE_TYPE"].astype(str).str.upper() == service.upper()]
    if rows.empty:
        return None, None, 0
    r = rows.iloc[0]
    return _as_date(r["FIRST_DAY"]), _as_date(r["LAST_DAY"]), int(_num(r["DAYS_IN_SPAN"]))


def coverage_notes(summary: Mapping[str, object],
                   coverage: Mapping[str, tuple[date | None, date | None]],
                   loaded: Mapping[str, datetime | None] | None = None) -> list[str]:
    """The named coverage gaps for a span (house rule: a late, stale or partly loaded source
    is said, and its dollars stay on the unattributed row). ``coverage`` maps a fact table to
    its ledger-wide (first, last) day; ``loaded`` maps a daily-reloaded keyed fact to its last
    load time (Central). Empty when there is no span to compare against."""
    return _coverage_review(summary, coverage, loaded)[0]


def keyed_gaps(summary: Mapping[str, object],
               coverage: Mapping[str, tuple[date | None, date | None]],
               loaded: Mapping[str, datetime | None] | None = None) -> list[str]:
    """The keyed facts (in line order) that do not cover the whole span in full: no rows, a
    late start, an early end, a newest span day loaded only in part, or storage on fewer days
    than the span. The same comparisons as coverage_notes. While any is listed, an empty
    company scope is unverified, never a clean verdict (R1-14)."""
    return _coverage_review(summary, coverage, loaded)[1]


def _coverage_review(summary: Mapping[str, object],
                     coverage: Mapping[str, tuple[date | None, date | None]],
                     loaded: Mapping[str, datetime | None] | None) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    gaps: list[str] = []
    loaded = loaded or {}
    span_first = summary.get("span_first")
    span_last = summary.get("span_last")
    span_days = int(_num(summary.get("span_days")))
    if not isinstance(span_first, date) or not isinstance(span_last, date) or span_days <= 0:
        return notes, gaps
    window_first = summary.get("window_first")
    window_last = summary.get("window_last")
    metering_first = summary.get("metering_first")
    metering_last = summary.get("metering_last")

    if isinstance(metering_first, date) and isinstance(window_first, date) and metering_first > window_first:
        notes.append(f"Daily metering starts {day_label(metering_first)}, so the span starts there, not at the "
                     f"Window's start ({day_label(window_first)}).")
    if isinstance(window_last, date) and span_last < window_last - timedelta(days=2):
        if isinstance(metering_last, date) and metering_last <= window_last:
            notes.append(f"The newest daily-metering day is {day_label(metering_last)}; it is left out as possibly "
                         f"unfinished, so the span ends {day_label(span_last)}.")
        else:
            # metering runs past the Window, but its last days inside the Window have no row
            notes.append(f"Daily metering has no row after {day_label(span_last)} inside the Window, so the span "
                         f"ends there.")
    k = (span_last - span_first).days + 1 - span_days
    if k > 0:
        notes.append(f"Daily metering has no row for {k} day{'s' if k != 1 else ''} inside the span; those "
                     f"days are left out of every row.")

    storage_first = summary.get("storage_first")
    storage_last = summary.get("storage_last")
    storage_days = int(_num(summary.get("storage_days")))
    for table, line_cap, line in _SOURCE_LINES:
        if table == "FACT_STORAGE_DAILY":
            # the per-database storage line is compared to the account-storage days; with no
            # account storage in the span, storage is in neither side (the note below says so)
            if storage_days <= 0 or not isinstance(storage_first, date) or not isinstance(storage_last, date):
                continue
            lo, hi = storage_first, storage_last
        else:
            lo, hi = span_first, span_last
        first, last = coverage.get(table, (None, None))
        if not isinstance(first, date) or not isinstance(last, date):
            notes.append(f"{line_cap}: {table} has no rows yet, so this line is empty and any {line} spend "
                         f"counts as unattributed.")
            gaps.append(table)
            continue
        if first > lo:
            notes.append(f"{line_cap}: {table} starts {day_label(first)}; any {line} spend before then counts as "
                         f"unattributed.")
            gaps.append(table)
        # the fact's newest day inside the span: loaded before it was complete -> held only in part
        newest = min(last, hi)
        loaded_at = loaded.get(table)
        partial = (isinstance(loaded_at, datetime) and newest >= lo
                   and loaded_before_day_complete(loaded_at, newest))
        if last < hi:
            if partial:
                notes.append(f"{line_cap}: {table}'s newest row is {day_label(last)}, loaded before that day was "
                             f"complete; the rest of that day's {line} spend and any after it counts as "
                             f"unattributed.")
            else:
                notes.append(f"{line_cap}: {table}'s newest row is {day_label(last)}; any {line} spend after it "
                             f"counts as unattributed.")
            gaps.append(table)
        elif partial:
            notes.append(f"{line_cap}: {table}'s rows for {day_label(newest)} were loaded before that day was "
                         f"complete, so part of that day's {line} spend counts as unattributed until the loader "
                         f"runs again.")
            gaps.append(table)

    if storage_days == 0 < span_days:
        notes.append("Storage: FACT_STORAGE_ACCOUNT_DAILY has no rows for these days, so storage is in neither "
                     "the company rows nor the all-in total.")
    elif (0 < storage_days < span_days and isinstance(storage_first, date)
          and isinstance(storage_last, date)):
        notes.append(f"Storage covers {storage_days} of the span's {span_days} days ({day_label(storage_first)} – "
                     f"{day_label(storage_last)}); storage on the other days is in neither the company rows nor "
                     f"the all-in total.")
    if storage_days < span_days:
        # the per-database storage legs ride the account-storage days, so a missing day is unverified too
        gaps.append("FACT_STORAGE_ACCOUNT_DAILY")
    return notes, list(dict.fromkeys(gaps))


def _base_summary(company: str, scoped: bool) -> dict[str, object]:
    return {
        "state": "shape", "scoped": scoped, "company": company,
        "window_first": None, "window_last": None,
        "span_first": None, "span_last": None, "span_days": 0,
        "storage_first": None, "storage_last": None, "storage_days": 0,
        "metering_first": None, "metering_last": None,
        "billed_credit_usd": 0.0, "storage_est_usd": 0.0, "allin_total_usd": 0.0,
        "company_usd": 0.0, "adjustment_usd": 0.0, "unattributed_usd": None, "share_basis_usd": 0.0,
        "company_share_pct": None, "span_label": None, "stall_day": None, "missing": [],
    }


def _result(state: str, summary: dict[str, object], *, table: pd.DataFrame | None = None,
            breakdown: pd.DataFrame | None = None, notes: list[str] | None = None,
            gaps: list[str] | None = None) -> dict[str, object]:
    summary["state"] = state
    return {"state": state,
            "table": table if table is not None else _empty_table(),
            "breakdown": breakdown if breakdown is not None else _empty_breakdown(),
            "summary": summary, "notes": list(notes or []), "keyed_gaps": list(gaps or [])}


def company_showback(frame: pd.DataFrame | None, *, rate: float, ai_rate: float,
                     storage_rates: Mapping[str, float], company: str = "ALL") -> dict[str, object]:
    """Company rows + account-level rows that tie out to billed metering + estimated storage.

    Returns ``{state, table, breakdown, summary, notes, keyed_gaps}``; ``state`` is ``ok``, ``shape`` (the
    frame lacks the contract columns or its WINDOW row), ``no_ledger`` (daily metering has
    never loaded) or ``no_basis`` (no complete metered day in the Window). Never raises."""
    company = str(company or "ALL")
    scoped = company.upper() not in ("ALL", "")
    summary = _base_summary(company, scoped)
    try:
        return _company_showback(frame, rate=rate, ai_rate=ai_rate, storage_rates=storage_rates,
                                 company=company, scoped=scoped, summary=summary)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError, OverflowError) as exc:
        # declared-exception guard: a junk frame renders 'unavailable' (shape), never crashes the tab
        summary["missing"] = [f"unreadable frame ({type(exc).__name__})"]
        return _result("shape", summary)


def _company_showback(frame: pd.DataFrame | None, *, rate: float, ai_rate: float,
                      storage_rates: Mapping[str, float], company: str, scoped: bool,
                      summary: dict[str, object]) -> dict[str, object]:
    if frame is None or getattr(frame, "empty", True):
        summary["missing"] = sorted(FRAME_COLUMNS)
        return _result("shape", summary)
    missing = sorted(set(FRAME_COLUMNS) - {str(c).upper() for c in frame.columns})
    if missing:
        summary["missing"] = missing
        return _result("shape", summary)
    df = frame.copy()
    df.columns = [str(c).upper() for c in df.columns]
    df["LINE_KIND"] = df["LINE_KIND"].astype(str).str.upper()
    if not (df["LINE_KIND"] == "WINDOW").any():
        summary["missing"] = ["WINDOW row"]
        return _result("shape", summary)
    if not ((df["LINE_KIND"] == "COVERAGE")
            & (df["SERVICE_TYPE"].astype(str).str.upper() == "FACT_METERING_DAILY")).any():
        summary["missing"] = ["FACT_METERING_DAILY coverage row"]
        return _result("shape", summary)
    # A NULL cell is data (an empty fact, an empty span); a non-NULL cell that does not parse is a
    # broken read, and it must never pass for "no metering yet" or a $0 row.
    unreadable = []
    for col in ("CREDITS", "CREDITS_ADJUSTMENT", "TIB_MO", "DAYS_IN_SPAN"):
        parsed = pd.to_numeric(df[col], errors="coerce")
        if (parsed.isna() & df[col].notna()).any():
            unreadable.append(col)
        df[col] = parsed.fillna(0.0)
    unreadable.extend(col for col in ("FIRST_DAY", "LAST_DAY")
                      if any(_as_date(v) is None for v in df[col] if not _is_null(v)))
    if any(_as_timestamp(v) is None for v in df["LOADED_AT"] if not _is_null(v)):
        unreadable.append("LOADED_AT")
    if unreadable:
        summary["missing"] = [f"{c} (unreadable values)" for c in unreadable]
        return _result("shape", summary)

    rate = safe_float(rate)
    ai_rate = safe_float(ai_rate)
    window_first, window_last, _ = _row_dates(df, "WINDOW")
    span_first, span_last, span_days = _row_dates(df, "SPAN")
    storage_first, storage_last, storage_days = _row_dates(df, "STORAGE_SPAN")
    coverage: dict[str, tuple[date | None, date | None]] = {}
    loaded: dict[str, datetime | None] = {}
    for _, r in df[df["LINE_KIND"] == "COVERAGE"].iterrows():
        coverage[str(r["SERVICE_TYPE"]).upper()] = (_as_date(r["FIRST_DAY"]), _as_date(r["LAST_DAY"]))
        loaded[str(r["SERVICE_TYPE"]).upper()] = _as_timestamp(r["LOADED_AT"])
    metering_first, metering_last = coverage.get("FACT_METERING_DAILY", (None, None))
    if span_first is None or span_last is None:
        span_days = 0
    if storage_first is None or storage_last is None:
        storage_days = 0
    summary.update({
        "window_first": window_first, "window_last": window_last,
        "span_first": span_first, "span_last": span_last, "span_days": span_days,
        "storage_first": storage_first, "storage_last": storage_last, "storage_days": storage_days,
        "metering_first": metering_first, "metering_last": metering_last,
        "span_label": span_label(span_first, span_last) if span_days > 0 and span_first and span_last else None,
        # the metering loader looks behind: its newest day is older than the Window's last day - 1
        "stall_day": (day_label(metering_last) if metering_last is not None and window_last is not None
                      and metering_last < window_last - timedelta(days=1) else None),
    })
    if metering_first is None:
        return _result("no_ledger", summary, notes=coverage_notes(summary, coverage, loaded))
    if span_days == 0:
        return _result("no_basis", summary, notes=coverage_notes(summary, coverage, loaded))

    # --- metering: billed dollars + the cloud-services adjustment, by family and bucket ---
    billed_cat: dict[str, float] = {}
    adj_cat: dict[str, float] = {}
    billed_bucket = dict.fromkeys(_BUCKET_COLUMN, 0.0)
    adj_bucket = dict.fromkeys(_BUCKET_COLUMN, 0.0)
    for _, r in df[df["LINE_KIND"] == "METERING"].iterrows():
        cat = service_category(r["SERVICE_TYPE"])
        cat_rate = ai_rate if cat == "AI / Cortex" else rate
        billed = credits_to_usd(r["CREDITS"], cat_rate, round_cents=False)
        adj = credits_to_usd(r["CREDITS_ADJUSTMENT"], cat_rate, round_cents=False)
        bucket = metering_bucket(r["SERVICE_TYPE"])
        billed_cat[cat] = billed_cat.get(cat, 0.0) + billed
        adj_cat[cat] = adj_cat.get(cat, 0.0) + adj
        billed_bucket[bucket] += billed
        adj_bucket[bucket] += adj

    # --- company rows ---
    keyed = df[df["LINE_KIND"].isin(("WAREHOUSE", "SERVERLESS", "COCO", "STORAGE_DB"))].copy()
    keyed["COMPANY"] = keyed["COMPANY"].where(keyed["COMPANY"].notna(), "UNKNOWN").astype(str)
    keyed.loc[keyed["COMPANY"].str.strip() == "", "COMPANY"] = "UNKNOWN"
    if scoped:
        keyed = keyed[keyed["COMPANY"] == company]
    std_rate = safe_float(storage_rates.get("TABLE"), DEFAULT_STORAGE_USD_PER_TB_MONTH)
    rows: list[dict[str, object]] = []
    for name, g in keyed.groupby("COMPANY", sort=False):
        wh = float(g.loc[g["LINE_KIND"] == "WAREHOUSE", "CREDITS"].sum()) * rate
        sl = float(g.loc[g["LINE_KIND"] == "SERVERLESS", "CREDITS"].sum()) * rate
        ai = float(g.loc[g["LINE_KIND"] == "COCO", "CREDITS"].sum()) * ai_rate
        st_usd = float(g.loc[g["LINE_KIND"] == "STORAGE_DB", "TIB_MO"].sum()) * std_rate
        rows.append({"COMPANY": UNKNOWN_ROW if str(name) == "UNKNOWN" else str(name),
                     "WAREHOUSE_USD": wh, "SERVERLESS_USD": sl, "AI_USD": ai,
                     "OTHER_METERED_USD": math.nan, "STORAGE_EST_USD": st_usd,
                     "TOTAL_USD": wh + sl + ai + st_usd})
    companies = pd.DataFrame(rows, columns=[c for c in TABLE_COLUMNS if c != "SHARE_OF_TOTAL_PCT"])
    if not companies.empty:
        companies["_UNK"] = (companies["COMPANY"] == UNKNOWN_ROW).astype(int)
        companies = (companies.sort_values(["_UNK", "TOTAL_USD"], ascending=[True, False])
                     .drop(columns="_UNK").reset_index(drop=True))

    # --- totals ---
    storage_est = 0.0
    for _, r in df[df["LINE_KIND"] == "STORAGE_ACCT"].iterrows():
        tier = str(r["SERVICE_TYPE"]).upper()
        storage_est += _num(r["TIB_MO"]) * safe_float(storage_rates.get(tier), std_rate)
    billed_total = sum(billed_cat.values())
    adj_total = sum(adj_cat.values())
    allin = billed_total + storage_est
    # R1-12: the share basis is the spend before the cloud-services adjustment (= company rows +
    # the unattributed row), the same basis as the company rows' own warehouse dollars
    gross = allin - adj_total
    co_sum = {col: float(companies[col].sum()) if not companies.empty else 0.0
              for col in ("WAREHOUSE_USD", "SERVERLESS_USD", "AI_USD", "STORAGE_EST_USD", "TOTAL_USD")}

    table = companies
    breakdown = _empty_breakdown()
    unattributed: float | None = None
    if not scoped:
        adj_row = {"COMPANY": ADJUSTMENT_ROW, "STORAGE_EST_USD": math.nan, "TOTAL_USD": adj_total}
        un_row: dict[str, object] = {"COMPANY": UNATTRIBUTED_ROW}
        for bucket, col in _BUCKET_COLUMN.items():
            adj_row[col] = adj_bucket[bucket]
            keyed_usd = co_sum.get(col, 0.0) if bucket != "OTHER" else 0.0
            un_row[col] = billed_bucket[bucket] - keyed_usd - adj_bucket[bucket]
        un_row["STORAGE_EST_USD"] = storage_est - co_sum["STORAGE_EST_USD"]
        unattributed = sum(_num(un_row[c]) for c in (*_BUCKET_COLUMN.values(), "STORAGE_EST_USD"))
        un_row["TOTAL_USD"] = unattributed
        account_rows = pd.DataFrame([adj_row, un_row])
        table = account_rows if companies.empty else pd.concat([companies, account_rows], ignore_index=True)

        fam_rows: list[dict[str, object]] = []
        families = list(billed_cat)
        for fam, col in _KEYED_FAMILY_COLUMN.items():
            # a keyed company column with dollars but no metering family still needs its row,
            # or the breakdown would stop summing to the unattributed row
            if fam not in families and abs(co_sum[col]) > 0:
                families.append(fam)
        for fam in families:
            account = billed_cat.get(fam, 0.0)
            keyed_usd = co_sum[_KEYED_FAMILY_COLUMN[fam]] if fam in _KEYED_FAMILY_COLUMN else 0.0
            adj = adj_cat.get(fam, 0.0)
            fam_rows.append({"SERVICE_FAMILY": fam, "ACCOUNT_USD": account, "COMPANY_ROWS_USD": keyed_usd,
                             "ADJUSTMENT_USD": adj, "UNATTRIBUTED_USD": account - keyed_usd - adj,
                             "CONTENTS": _FAMILY_CONTENTS.get(fam, _DEFAULT_CONTENTS)})
        fam_rows.append({"SERVICE_FAMILY": STORAGE_FAMILY, "ACCOUNT_USD": storage_est,
                         "COMPANY_ROWS_USD": co_sum["STORAGE_EST_USD"], "ADJUSTMENT_USD": 0.0,
                         "UNATTRIBUTED_USD": storage_est - co_sum["STORAGE_EST_USD"],
                         "CONTENTS": _FAMILY_CONTENTS[STORAGE_FAMILY]})
        breakdown = (pd.DataFrame(fam_rows, columns=list(BREAKDOWN_COLUMNS))
                     .sort_values("UNATTRIBUTED_USD", ascending=False).reset_index(drop=True))

    table = table.reindex(columns=list(TABLE_COLUMNS))
    for col in TABLE_COLUMNS[1:]:
        table[col] = pd.to_numeric(table[col], errors="coerce")
    table["SHARE_OF_TOTAL_PCT"] = (table["TOTAL_USD"] / gross * 100.0) if gross > 0 else math.nan
    # the adjustment is a credit on the whole bill, outside the share basis: no share, never a negative %
    table.loc[table["COMPANY"] == ADJUSTMENT_ROW, "SHARE_OF_TOTAL_PCT"] = math.nan
    table = table.reset_index(drop=True)

    summary.update({
        "billed_credit_usd": billed_total, "storage_est_usd": storage_est, "allin_total_usd": allin,
        "company_usd": co_sum["TOTAL_USD"], "adjustment_usd": adj_total, "unattributed_usd": unattributed,
        "share_basis_usd": gross,
        "company_share_pct": (co_sum["TOTAL_USD"] / gross * 100.0) if gross > 0 else None,
    })
    notes, gaps = _coverage_review(summary, coverage, loaded)
    for _, r in breakdown.iterrows():
        u = _num(r["UNATTRIBUTED_USD"])
        if u < -_NEGATIVE_NOTE_FLOOR_USD:
            notes.append(f"{r['SERVICE_FAMILY']}: the company rows exceed the family's dollars by "
                         f"{format_usd(-u)} in this span, so its unattributed amount is negative. The two "
                         f"sides come from different Snowflake views with different day boundaries, so they "
                         f"need not match exactly.")
    return _result("ok", summary, table=table, breakdown=breakdown, notes=notes, gaps=gaps)
