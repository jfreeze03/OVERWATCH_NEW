"""Pure logic for the ported insight features (1-7)."""

from __future__ import annotations

import re
from math import ceil

import pandas as pd

from .formulas import humanize_duration, safe_div, safe_float

# ---- 1. Idle warehouse advisor ---------------------------------------------

IDLE_PCT_FLAG = 20.0     # flag warehouses wasting >=20% of credits idle
IDLE_MIN_CREDITS = 1.0   # and at least 1 idle credit in the window
# A3: the suspend target the advisor steers toward. A warehouse already at or
# below this is NOT a tuning target — never propose raising it (the old hardcoded
# "e.g. 60s" wording did exactly that to a 30s warehouse).
IDLE_TARGET_SUSPEND_SEC = 60

# B3 (audit 2026-07-31): idle credits are NOT 100% recoverable. Snowflake bills a
# 60-second minimum on every resume, and a warehouse tuned to AUTO_SUSPEND=60s
# still burns the suspend timer itself after the last query of each active hour.
# The ledger used to book the whole idle number as savings, which is a promise the
# change cannot keep. The haircut charges back one target-suspend tail per ACTIVE
# metered hour at the warehouse's own credits/hour, leaving the portion a tighter
# timer can genuinely reclaim. Deliberately an estimate on the conservative side:
# a warehouse resuming many times inside one hour pays more tails than this.
IDLE_RESUME_TAIL_SEC = IDLE_TARGET_SUSPEND_SEC


def with_auto_suspend_settings(idle: pd.DataFrame, warehouses: pd.DataFrame) -> pd.DataFrame:
    """Attach case-insensitive SHOW WAREHOUSES auto-suspend evidence.

    Missing metadata stays explicitly unknown. AUTO_SUSPEND=0 is a known,
    disabled setting and must not be conflated with a failed metadata read.
    """
    if idle is None or idle.empty:
        return pd.DataFrame() if idle is None else idle.copy()
    out = idle.copy()
    out["AUTO_SUSPEND"] = pd.Series(pd.NA, index=out.index, dtype="Float64")
    out["AUTO_SUSPEND_KNOWN"] = False
    if warehouses is None or warehouses.empty or "WAREHOUSE_NAME" not in out.columns:
        return out
    settings = warehouses.copy()
    settings.columns = [str(column).lower() for column in settings.columns]
    if not {"name", "auto_suspend"}.issubset(settings.columns):
        return out
    values = {
        str(name).strip().upper(): pd.to_numeric(value, errors="coerce")
        for name, value in zip(settings["name"], settings["auto_suspend"], strict=False)
    }
    mapped = out["WAREHOUSE_NAME"].astype(str).str.strip().str.upper().map(values)
    out["AUTO_SUSPEND"] = pd.to_numeric(mapped, errors="coerce").astype("Float64")
    out["AUTO_SUSPEND_KNOWN"] = out["AUTO_SUSPEND"].notna()
    return out


def idle_advisor(df: pd.DataFrame, credit_rate_usd: float, window_days: int) -> pd.DataFrame:
    """Add idle %, idle $, projected monthly waste, and a recommendation."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("TOTAL_CREDITS", "IDLE_CREDITS", "METERED_HOURS", "IDLE_HOURS"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    rate = safe_float(credit_rate_usd, 3.68)
    days = max(int(window_days or 1), 1)
    out["IDLE_PCT"] = (out["IDLE_CREDITS"] / out["TOTAL_CREDITS"].replace(0, pd.NA) * 100).fillna(0.0).round(1)
    out["IDLE_USD"] = (out["IDLE_CREDITS"] * rate).round(2)
    out["PROJECTED_MONTHLY_IDLE_USD"] = (out["IDLE_USD"] / days * 30).round(2)
    out["FLAGGED"] = (out["IDLE_PCT"] >= IDLE_PCT_FLAG) & (out["IDLE_CREDITS"] >= IDLE_MIN_CREDITS)

    # B3: what a tighter AUTO_SUSPEND can actually reclaim = idle minus the
    # resume/suspend tail it cannot avoid (one IDLE_RESUME_TAIL_SEC per ACTIVE
    # metered hour, priced at this warehouse's own credits/hour). Never negative,
    # never above the idle itself. Both readers (live builder and efficiency
    # mart) supply METERED_HOURS/IDLE_HOURS; without them there is no basis for a
    # haircut and the recoverable figure degrades to the full idle number.
    if {"METERED_HOURS", "IDLE_HOURS"}.issubset(out.columns):
        metered = out["METERED_HOURS"].clip(lower=0)
        active_hours = (metered - out["IDLE_HOURS"]).clip(lower=0)
        credits_per_hour = (out["TOTAL_CREDITS"] / metered.replace(0, pd.NA)).fillna(0.0)
        tail_credits = active_hours * (IDLE_RESUME_TAIL_SEC / 3600.0) * credits_per_hour
        recoverable = (out["IDLE_CREDITS"] - tail_credits).clip(lower=0.0)
    else:
        recoverable = out["IDLE_CREDITS"]
    out["RECOVERABLE_IDLE_USD"] = (recoverable * rate).round(2)
    out["RECOVERABLE_MONTHLY_USD"] = (out["RECOVERABLE_IDLE_USD"] / days * 30).round(2)

    # A3 (audit 2026-07-31): the advice must respect the warehouse's CURRENT
    # AUTO_SUSPEND. The old text hardcoded "e.g. 60s" for everything, so a
    # warehouse already tuned to 30s got told to RAISE it to 60 — the generated
    # ALTER made things worse — and a fully-tuned warehouse still ranked #1 with
    # no way to tell it apart. When the current setting is already at/below the
    # 60s target, the residual is resume overhead, not a tuning gap. Callers pass
    # SHOW WAREHOUSES evidence; missing settings are verification work, never an
    # executable recommendation or a booked saving.
    has_current = "AUTO_SUSPEND" in out.columns
    if has_current:
        raw_current = pd.to_numeric(out["AUTO_SUSPEND"], errors="coerce")
        known = raw_current.notna()
        if "AUTO_SUSPEND_KNOWN" in out.columns:
            known &= out["AUTO_SUSPEND_KNOWN"].fillna(False).astype(bool)
        out["AUTO_SUSPEND"] = raw_current
    else:
        known = pd.Series(False, index=out.index, dtype=bool)
        out["AUTO_SUSPEND"] = pd.Series(pd.NA, index=out.index, dtype="Float64")
    out["AUTO_SUSPEND_KNOWN"] = known
    current = out["AUTO_SUSPEND"].fillna(0.0)
    out["ACTIONABLE"] = (
        out["FLAGGED"]
        & out["AUTO_SUSPEND_KNOWN"]
        & ((current <= 0) | (current > IDLE_TARGET_SUSPEND_SEC))
    )
    out["ACTIONABLE_MONTHLY_USD"] = out["RECOVERABLE_MONTHLY_USD"].where(
        out["ACTIONABLE"], 0.0
    ).round(2)

    def _status(r) -> str:
        if not r["FLAGGED"]:
            return "WITHIN TOLERANCE"
        if not bool(r["AUTO_SUSPEND_KNOWN"]):
            return "VERIFY SETTING"
        cur = safe_float(r.get("AUTO_SUSPEND"), 0.0)
        if 0 < cur <= IDLE_TARGET_SUSPEND_SEC:
            return "ALREADY TUNED"
        return "ACTIONABLE"

    out["ACTION_STATUS"] = out.apply(_status, axis=1)
    out["SAVINGS_CONFIDENCE"] = (
        "MEDIUM" if {"METERED_HOURS", "IDLE_HOURS"}.issubset(out.columns) else "LOW"
    )

    def _advice(r) -> str:
        if not r["FLAGGED"]:
            return "Idle share within tolerance."
        if not bool(r["AUTO_SUSPEND_KNOWN"]):
            return (f"Verify current AUTO_SUSPEND on {r['WAREHOUSE_NAME']}; measured idle "
                    "warrants review before generating a change.")
        cur = safe_float(r.get("AUTO_SUSPEND"), 0.0)
        if 0 < cur <= IDLE_TARGET_SUSPEND_SEC:
            return (f"{r['WAREHOUSE_NAME']} is already at AUTO_SUSPEND={cur:.0f}s — "
                    f"~{r['IDLE_PCT']:.0f}% idle here is resume overhead, not a tuning gap. "
                    "Look at query cadence/scheduling instead of the suspend timer.")
        if cur <= 0:
            return (f"Enable AUTO_SUSPEND={IDLE_TARGET_SUSPEND_SEC}s on "
                    f"{r['WAREHOUSE_NAME']}: ~{r['IDLE_PCT']:.0f}% of its credits burn "
                    "in hours with zero queries.")
        target = int(min(cur, IDLE_TARGET_SUSPEND_SEC)) if cur > 0 else IDLE_TARGET_SUSPEND_SEC
        now = f" (currently {cur:.0f}s)" if cur > 0 else ""
        return (f"Reduce AUTO_SUSPEND to {target}s on {r['WAREHOUSE_NAME']}{now}: "
                f"~{r['IDLE_PCT']:.0f}% of its credits burn in hours with zero queries.")

    out["RECOMMENDATION"] = out.apply(_advice, axis=1)
    # Rank executable actions first, then settings that need verification, then
    # already-tuned residual waste and within-tolerance rows.
    status_order = {"ACTIONABLE": 0, "VERIFY SETTING": 1, "ALREADY TUNED": 2,
                    "WITHIN TOLERANCE": 3}
    out["_ACTION_ORDER"] = out["ACTION_STATUS"].map(status_order).fillna(9)
    return (out.sort_values(["_ACTION_ORDER", "ACTIONABLE_MONTHLY_USD", "IDLE_USD"],
                            ascending=[True, False, False])
            .drop(columns="_ACTION_ORDER").reset_index(drop=True))


def idle_suspend_sql(warehouse: str, seconds: int = 60) -> str:
    """Generated (not executed) remediation for a flagged warehouse."""
    from app.core.sqlsafe import safe_identifier

    wh = safe_identifier(str(warehouse))
    seconds = max(30, min(int(seconds), 3600))
    return f"ALTER WAREHOUSE {wh} SET AUTO_SUSPEND = {seconds};"


# ---- 2. Repeat-query candidates ---------------------------------------------

# D3: the gate is "half an hour of compute PER 30 DAYS", not "half an hour in
# whatever window happens to be selected". The old absolute threshold made the
# same fingerprint a candidate at 365d and invisible at 7d — the recommendation
# moved with the window picker instead of with the workload. The constant keeps
# its calibrated value; window_days normalizes to it.
REPEAT_MIN_ELAPSED_HOURS = 0.5      # per REPEAT_GATE_BASE_DAYS
REPEAT_GATE_BASE_DAYS = 30
REPEAT_MIN_RUNS_PER_30D = 10.0
REPEAT_LOW_CACHE_PCT = 25.0


def repeat_min_runs(window_days: int) -> int:
    """SQL prefilter matching the 10-runs-per-30d recurrence contract."""
    days = max(int(window_days or REPEAT_GATE_BASE_DAYS), 1)
    return max(2, ceil(REPEAT_MIN_RUNS_PER_30D * days / REPEAT_GATE_BASE_DAYS))


def flag_repeat_candidates(df: pd.DataFrame, window_days: int = REPEAT_GATE_BASE_DAYS) -> pd.DataFrame:
    """Flag fingerprints worth materializing/caching: heavy + cache-poor.

    Ranked by normalized 30-day money at stake x cache-MISS share when the reader supplies
    EST_CREDITS (D3): wall-clock hours rank an X-Small hour equal to a 4X-Large
    hour, and a family that is already 95% cached has nothing left to reclaim
    however many hours it burns. The efficiency mart has no per-size column, so
    that path keeps the hours ordering — labeled by the caller.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("RUNS", "TOTAL_ELAPSED_HOURS", "AVG_CACHE_PCT", "TOTAL_TB_SCANNED", "EST_CREDITS"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    days = max(int(window_days or REPEAT_GATE_BASE_DAYS), 1)
    out["HOURS_PER_30D"] = (out["TOTAL_ELAPSED_HOURS"] / days * REPEAT_GATE_BASE_DAYS).round(2)
    out["RUNS_PER_30D"] = (out["RUNS"] / days * REPEAT_GATE_BASE_DAYS).round(1)
    out["CANDIDATE"] = (
        (out["HOURS_PER_30D"] >= REPEAT_MIN_ELAPSED_HOURS)
        & (out["RUNS_PER_30D"] >= REPEAT_MIN_RUNS_PER_30D)
        & (out["AVG_CACHE_PCT"] <= REPEAT_LOW_CACHE_PCT)
    )
    out["WHY"] = out.apply(
        lambda r: (
            f"{r['RUNS_PER_30D']:.1f} runs/30d, {r['HOURS_PER_30D']:.1f}h compute/30d, "
            f"{r['AVG_CACHE_PCT']:.0f}% cache — consider a materialized/refreshed table or schedule change."
        ) if r["CANDIDATE"] else "",
        axis=1,
    )
    if "EST_CREDITS" in out.columns:
        out["EST_CREDITS_PER_30D"] = (
            out["EST_CREDITS"] / days * REPEAT_GATE_BASE_DAYS
        ).round(3)
        out["AVOIDABLE_CREDITS_PER_30D"] = (
            out["EST_CREDITS_PER_30D"] * (1 - out["AVG_CACHE_PCT"].clip(0, 100) / 100)
        ).round(3)
        out = out.sort_values(
            ["CANDIDATE", "AVOIDABLE_CREDITS_PER_30D"], ascending=[False, False]
        ).reset_index(drop=True)
    else:
        out = out.sort_values(
            ["CANDIDATE", "HOURS_PER_30D"], ascending=[False, False]
        ).reset_index(drop=True)
    return out


# ---- 3. Storage growth movers ------------------------------------------------

MOVERS_MIN_CONFIDENT_DAYS = 7   # below this a slope is noise, not a trend


def storage_movers(df: pd.DataFrame, usd_per_tb_month: float) -> pd.DataFrame:
    """Growth per database with projected monthly $ delta.

    D4 (audit 2026-07-31): the 30-day projection prefers the least-squares slope
    over every observed day (SLOPE_BYTES_PER_DAY) instead of the endpoint diff —
    one reload or purge on the first/last day used to become the whole trend, and
    x30 multiplied it. Series shorter than MOVERS_MIN_CONFIDENT_DAYS are marked
    LOW_CONFIDENCE rather than silently projected; the caller says so on screen.
    Rows are ordered by projected DOLLARS, because that is what the chart and the
    top-10 cut are about (a 3 TB database growing slowly is not the mover).
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("FIRST_BYTES", "LAST_BYTES", "FAILSAFE_BYTES", "SPAN_DAYS",
                "DAYS_OBSERVED", "SLOPE_BYTES_PER_DAY"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    tb = 1024.0**4
    rate = safe_float(usd_per_tb_month, 23.0)
    out["CURRENT_TB"] = (out["LAST_BYTES"] / tb).round(3)
    out["GROWTH_TB"] = ((out["LAST_BYTES"] - out["FIRST_BYTES"]) / tb).round(3)
    span = out["SPAN_DAYS"].clip(lower=1)
    endpoint_30d = out["GROWTH_TB"] / span * 30
    if "SLOPE_BYTES_PER_DAY" in out.columns:
        slope_30d = out["SLOPE_BYTES_PER_DAY"] / tb * 30
        # A regression needs points; fall back to the endpoint diff when the
        # reader (or an older mart) gave us none.
        usable = out["SLOPE_BYTES_PER_DAY"].notna() & (out.get(
            "DAYS_OBSERVED", pd.Series(0, index=out.index)) >= 2)
        out["GROWTH_TB_30D"] = slope_30d.where(usable, endpoint_30d).round(3)
        out["TREND_BASIS"] = usable.map({True: "regression", False: "endpoints"})
    else:
        out["GROWTH_TB_30D"] = endpoint_30d.round(3)
        out["TREND_BASIS"] = "endpoints"
    observed = out["DAYS_OBSERVED"] if "DAYS_OBSERVED" in out.columns else span
    out["LOW_CONFIDENCE"] = (span < MOVERS_MIN_CONFIDENT_DAYS) | (observed < MOVERS_MIN_CONFIDENT_DAYS)
    out["GROWTH_USD_30D"] = (out["GROWTH_TB_30D"] * rate).round(2)
    out["PROJECTABLE"] = ~out["LOW_CONFIDENCE"]
    out["PROJECTABLE_GROWTH_USD_30D"] = out["GROWTH_USD_30D"].where(
        out["PROJECTABLE"] & (out["GROWTH_USD_30D"] > 0), 0.0
    ).round(2)
    # E6: AVERAGE_FAILSAFE_BYTES is reported ALONGSIDE AVERAGE_DATABASE_BYTES,
    # not inside it, so failsafe / database could (and did) exceed 100%. Share of
    # the database's TOTAL billed bytes is the number the label promises.
    total_bytes = (out["LAST_BYTES"] + out["FAILSAFE_BYTES"]).replace(0, pd.NA)
    out["FAILSAFE_SHARE_PCT"] = (
        out["FAILSAFE_BYTES"] / total_bytes * 100
    ).fillna(0.0).clip(0, 100).round(1)
    return out.sort_values("GROWTH_USD_30D", ascending=False).reset_index(drop=True)


# ---- 4. Release compare --------------------------------------------------------

_RELEASE_METRICS = (
    # column, label, lower_is_better
    ("QUERY_COUNT", "Queries", None),
    ("FAIL_PCT", "Failure %", True),
    ("P95_ELAPSED_SEC", "p95 runtime (s)", True),
    ("QUEUED_SEC", "Queued (s)", True),
    ("SPILL_REMOTE_GB", "Remote spill (GB)", True),
)
_FLAT_TOLERANCE_PCT = 10.0


def compare_release_periods(df: pd.DataFrame) -> list[dict]:
    """Turn BEFORE/AFTER rows into verdict rows (Better/Worse/Flat)."""
    if df is None or df.empty or "PERIOD" not in df.columns:
        return []
    periods = {str(r["PERIOD"]).upper(): r for _, r in df.iterrows()}
    before, after = periods.get("BEFORE"), periods.get("AFTER")
    if before is None or after is None:
        return []

    def _fail_pct(row) -> float:
        return safe_div(safe_float(row.get("FAILED_COUNT")), safe_float(row.get("QUERY_COUNT"))) * 100

    def _display(col: str, v: float) -> str:
        # Before/After share one column across metrics of different units, so
        # per-column number-formatting can't express them — format to a display
        # string here where the unit is still known per metric (durations
        # humanize to Hr/Min/Sec, matching the rest of the app).
        if col in ("P95_ELAPSED_SEC", "QUEUED_SEC"):
            return humanize_duration(v, "s")
        if col == "SPILL_REMOTE_GB":
            return f"{v:,.2f} GB"
        if col == "FAIL_PCT":
            return f"{v:,.1f}%"
        return f"{v:,.0f}"

    rows = []
    for col, label, lower_better in _RELEASE_METRICS:
        b = _fail_pct(before) if col == "FAIL_PCT" else safe_float(before.get(col))
        a = _fail_pct(after) if col == "FAIL_PCT" else safe_float(after.get(col))
        delta_pct = None if b == 0 else round((a - b) / abs(b) * 100, 1)
        if lower_better is None or delta_pct is None:
            verdict = "n/a"
        elif abs(delta_pct) <= _FLAT_TOLERANCE_PCT:
            verdict = "Flat"
        elif (delta_pct < 0) == lower_better:
            verdict = "Better"
        else:
            verdict = "Worse"
        rows.append({"Metric": label, "Before": _display(col, b), "After": _display(col, a),
                     "Delta %": delta_pct, "Verdict": verdict})
    return rows


# O16: auto-detect deploy days for the Release compare picker. Deploys show up
# as DDL spikes, so the biggest-count days are the candidates.
RELEASE_CANDIDATE_LIMIT = 8


def rank_release_candidates(df: pd.DataFrame, limit: int = RELEASE_CANDIDATE_LIMIT) -> pd.DataFrame:
    """Rank detected deploy days (one row per day with DDL_COUNT) for the picker.

    Keep the top ``limit`` days by DDL volume (the notable deploys), then order
    them most-recent-first so the default pick is the latest notable deploy.
    Days with no DDL are dropped. DAY is normalised to a ``YYYY-MM-DD`` string —
    the release-compare readers validate exactly that shape before embedding it.
    """
    if df is None or df.empty or "DDL_COUNT" not in df.columns or "DAY" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["DDL_COUNT"] = out["DDL_COUNT"].map(safe_float)
    out["DAY"] = pd.to_datetime(out["DAY"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out[(out["DDL_COUNT"] > 0) & out["DAY"].notna()]
    if out.empty:
        return pd.DataFrame()
    out = out.sort_values("DDL_COUNT", ascending=False).head(max(1, int(limit)))
    return out.sort_values("DAY", ascending=False).reset_index(drop=True)


_ERROR_FAMILIES = (
    ("Permission / auth", r"not authorized|insufficient privilege|does not exist or not authorized|access denied"),
    # v4.154: three families for the messages that dominated the "Other" bucket
    # (73% of task failures in the live account — the top one, "already a live
    # version", alone logged 929 fails). First match wins, so these sit before
    # the broader Missing-object / Data-quality patterns they must not fall into.
    ("Concurrency / live version", r"already a live version|please commit it first|already an? (running|active) version"),
    ("Session not set up", r"does not have a current (database|schema|warehouse|role)|call use (database|schema|warehouse|role)|no active warehouse"),
    ("Metadata not ready", r"not yet available"),
    ("Missing object", r"does not exist\b|invalid identifier|unknown (table|view|function)"),
    ("Timeout / cancelled", r"timeout|timed out|statement reached its statement or warehouse timeout|cancelled"),
    ("Resource / memory", r"out of memory|resource|exceeded|quota"),
    ("Data quality", r"numeric value|conversion|null result|duplicate|constraint|division by zero|is not recognized"),
    ("Syntax / SQL", r"syntax error|unexpected|compilation error"),
)


def task_release_deltas(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot per-task BEFORE/AFTER rows; surface tasks that got worse."""
    if df is None or df.empty:
        return pd.DataFrame()
    pivot = df.pivot_table(
        index=["DATABASE_NAME", "TASK_NAME"], columns="PERIOD",
        values=["RUNS", "FAILED", "AVG_SEC"], aggfunc="sum",
    )
    pivot.columns = [f"{m}_{p}" for m, p in pivot.columns]
    pivot = pivot.reset_index().fillna(0)
    for col in ("FAILED_BEFORE", "FAILED_AFTER", "AVG_SEC_BEFORE", "AVG_SEC_AFTER"):
        if col not in pivot.columns:
            pivot[col] = 0.0
    pivot["NEW_FAILURES"] = (pivot["FAILED_AFTER"] - pivot["FAILED_BEFORE"]).clip(lower=0)
    pivot["RUNTIME_DELTA_PCT"] = pivot.apply(
        lambda r: round((safe_float(r["AVG_SEC_AFTER"]) - safe_float(r["AVG_SEC_BEFORE"]))
                        / abs(safe_float(r["AVG_SEC_BEFORE"])) * 100, 1)
        if safe_float(r["AVG_SEC_BEFORE"]) else 0.0,
        axis=1,
    )
    pivot["GOT_WORSE"] = (pivot["NEW_FAILURES"] > 0) | (pivot["RUNTIME_DELTA_PCT"] > 25)
    return pivot.sort_values(["GOT_WORSE", "NEW_FAILURES", "RUNTIME_DELTA_PCT"],
                             ascending=[False, False, False]).reset_index(drop=True)


def classify_task_error(message: object) -> str:
    text = str(message or "").lower()
    if not text.strip():
        return "No error text"
    for family, pattern in _ERROR_FAMILIES:
        if re.search(pattern, text):
            return family
    return "Other"


# ---- 7. Dormant users ----------------------------------------------------------

def build_failure_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Mark the first failure per graph run (root cause) vs cascade, and
    attach an error family for grouping."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["ERROR_FAMILY"] = out.get("ERROR_MESSAGE", "").map(classify_task_error)
    out["QUERY_START_TIME"] = pd.to_datetime(out["QUERY_START_TIME"], errors="coerce")
    group_col = "GRAPH_RUN_GROUP_ID" if "GRAPH_RUN_GROUP_ID" in out.columns else None
    if group_col and out[group_col].notna().any():
        firsts = out.sort_values("QUERY_START_TIME").groupby(group_col, dropna=False).head(1).index
        out["ROLE_IN_GRAPH"] = "Cascade"
        out.loc[firsts, "ROLE_IN_GRAPH"] = "Root cause"
    else:
        out["ROLE_IN_GRAPH"] = "Root cause"
    return out.sort_values("QUERY_START_TIME", ascending=False).reset_index(drop=True)


def dormant_severity(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["DAYS_DORMANT"] = out["DAYS_DORMANT"].map(safe_float)
    out["ROLE_COUNT"] = out["ROLE_COUNT"].map(safe_float)
    out["SEVERITY"] = out.apply(
        lambda r: "High" if r["DAYS_DORMANT"] >= 180 or r["ROLE_COUNT"] >= 5
        else "Medium" if r["DAYS_DORMANT"] >= 90
        else "Low",
        axis=1,
    )
    return out


def reawakening_severity(df: pd.DataFrame) -> pd.DataFrame:
    """Sec5: rank dormant-then-active users — the longer the silence and the more
    roles they hold, the more a sudden login warrants a look."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["GAP_DAYS"] = out["GAP_DAYS"].map(safe_float)
    out["ROLE_COUNT"] = out["ROLE_COUNT"].map(safe_float)
    out["SEVERITY"] = out.apply(
        lambda r: "High" if r["GAP_DAYS"] >= 180 or r["ROLE_COUNT"] >= 5
        else "Medium" if r["GAP_DAYS"] >= 90
        else "Low",
        axis=1,
    )
    return out


def pipeline_sla_forecast(df: pd.DataFrame, *, overdue_k: float = 1.5) -> pd.DataFrame:
    """Turn the reactive freshness SLA into a forward-looking tier (O10).

    ``pipeline_sla_status`` answers 'is this table fresh right now?'. Given each
    table's runway to its deadline (MAX_AGE_HOURS - HOURS_SINCE) and its typical
    refresh cadence (MEDIAN_GAP_MIN), forecast the ones trending toward a miss.
    Adds ``FORECAST``, ``SEVERITY`` and a human ``DETAIL``. Rules, worst-first:

      * Breached — SLA_MET is already false (reactive; kept for context).
      * Overdue  — meets SLA now, but it has been more than ``overdue_k`` x its
        own median refresh gap since the last update: the refresh is materially
        late, so a stalled pipeline is the likely cause.
      * At risk  — meets SLA now, but the deadline is within one typical refresh
        cycle, so a single skipped refresh breaches it. Tables with no cadence
        history fall back to a runway-proximity check (deadline within 15% of the
        SLA horizon).
      * On track — comfortably fresh.

    Returns a copy; empty in -> empty out, never raises.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy().reset_index(drop=True)
    met_col = out.get("SLA_MET")
    met = (met_col.fillna(False).astype(bool)
           if met_col is not None else pd.Series(False, index=out.index))
    hours_since = pd.to_numeric(out.get("HOURS_SINCE"), errors="coerce")
    max_age = pd.to_numeric(out.get("MAX_AGE_HOURS"), errors="coerce")
    runway = pd.to_numeric(out.get("RUNWAY_HOURS"), errors="coerce")
    if runway.isna().all():
        runway = max_age - hours_since
    median_gap = pd.to_numeric(out.get("MEDIAN_GAP_MIN"), errors="coerce")
    k = float(overdue_k)

    forecasts: list[str] = []
    severities: list[str] = []
    details: list[str] = []
    for i in range(len(out)):
        is_met = bool(met.iloc[i])
        hs = safe_float(hours_since.iloc[i])
        rw_raw = runway.iloc[i]
        rw = safe_float(rw_raw)
        gap_raw = median_gap.iloc[i]
        has_cadence = bool(pd.notna(gap_raw)) and safe_float(gap_raw) > 0
        cycle_hours = safe_float(gap_raw) / 60.0 if has_cadence else None
        if not is_met:
            forecasts.append("Breached")
            severities.append("High")
            details.append(f"already {hs:.1f}h old (past its {safe_float(max_age.iloc[i]):.0f}h limit)")
            continue
        if has_cadence and hs * 60.0 > k * safe_float(gap_raw):
            forecasts.append("Overdue")
            severities.append("High")
            details.append(
                f"last refresh {hs:.1f}h ago vs ~{cycle_hours:.1f}h typical — refresh is late"
            )
            continue
        proximity = cycle_hours if cycle_hours is not None else safe_float(max_age.iloc[i]) * 0.15
        if bool(pd.notna(rw_raw)) and rw <= proximity:
            forecasts.append("At risk")
            severities.append("Medium")
            eta = f"breaches in ~{rw:.1f}h" if rw > 0 else "at the limit now"
            details.append(eta + (f"; ~{cycle_hours:.1f}h typical cadence" if has_cadence else ""))
            continue
        forecasts.append("On track")
        severities.append("OK")
        details.append(f"~{rw:.1f}h runway" if bool(pd.notna(rw_raw)) else "fresh")
    out["FORECAST"] = forecasts
    out["SEVERITY"] = severities
    out["DETAIL"] = details
    return out
