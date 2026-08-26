"""Pure logic for the ported insight features (1-7)."""

from __future__ import annotations

import re
from math import ceil

import pandas as pd

from app.logic.anomaly import flag_anomalies

from .formulas import credits_to_usd, humanize_duration, safe_div, safe_float

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


def idle_waste_summary(df: pd.DataFrame, credit_rate_usd: float, window_days: int) -> dict:
    """Account/company roll-up of idle warehouse waste (repo review wave 3) — the
    single headline "$ burned in warehouse-hours with zero queries" number, priced.

    ``df`` is the RAW idle frame (idle_warehouse_analysis / eff_idle_analysis):
    TOTAL_CREDITS + IDLE_CREDITS per warehouse. GROSS idle $ (never call it
    "savings" — the recoverable net, after the resume/suspend tail, is idle_advisor's
    ACTIONABLE). ``window_days`` should be served_days(res, days) so the /day*30
    projection isn't ~4x low when the live path clamped to 90d. Cheap + testable."""
    empty = {"IDLE_USD": 0.0, "IDLE_SHARE_PCT": 0.0, "PROJECTED_MONTHLY_USD": 0.0,
             "TOTAL_USD": 0.0, "WAREHOUSES": 0}
    if df is None or df.empty:
        return empty
    idle_cr = float(pd.to_numeric(df.get("IDLE_CREDITS"), errors="coerce").fillna(0).sum())
    total_cr = float(pd.to_numeric(df.get("TOTAL_CREDITS"), errors="coerce").fillna(0).sum())
    rate = safe_float(credit_rate_usd, 3.68)
    days = max(int(window_days or 1), 1)
    idle_usd = round(credits_to_usd(idle_cr, rate), 2)
    return {
        "IDLE_USD": idle_usd,
        "IDLE_SHARE_PCT": round(safe_div(idle_cr, total_cr) * 100, 1),
        "PROJECTED_MONTHLY_USD": round(idle_usd / days * 30, 2),
        "TOTAL_USD": round(credits_to_usd(total_cr, rate), 2),
        "WAREHOUSES": len(df),
    }


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


def product_retirement(cost_df: pd.DataFrame, reads_df: pd.DataFrame,
                       credit_rate_usd: float, *, min_cost_usd: float = 100.0,
                       decline_pct: float = -50.0, min_window_reads: int = 5) -> pd.DataFrame:
    """#28: cost-per-consumer + retirement candidates per data product.

    Joins per-product measured object cost (``data_product_economics``) with per-product
    consumer reach + reads trend (``product_consumer_reads``) to answer "what does this
    product cost per person who actually uses it, and is anyone still using it?".

    COST_USD = MEASURED_OBJECT_CREDITS * rate. COST_PER_CONSUMER_USD = COST_USD /
    DISTINCT_CONSUMERS with a NULLIF-style guard so 0 consumers => NA, never inf (a
    costly product with 0 consumers is the STRONGEST retire signal, routed to the
    verdict, not a divide-by-zero). READ_TREND_PCT = (RECENT-PRIOR)/PRIOR*100, NA when
    PRIOR==0 (a brand-new product has no trend, not a -100%).

    RETIREMENT_VERDICT (advisory only, evidence-gated like decision.prioritize_workloads):
      * INSUFFICIENT_DATA — reads unavailable/degraded, or a product has SOME but fewer
        than ``min_window_reads`` reads across the window: too little to judge, so never
        recommend retiring off it.
      * RETIRE_CANDIDATE — COST_USD>=min_cost_usd AND (0 consumers OR 0 recent reads OR
        READ_TREND_PCT<=decline_pct): costing real money, usage gone or collapsing.
      * REVIEW — COST_USD>=min_cost_usd AND declining (READ_TREND_PCT<0): worth a look.
      * KEEP — otherwise. Sorted by COST_USD desc. Empty cost_df -> empty; never raises."""
    if cost_df is None or cost_df.empty or "DATA_PRODUCT" not in cost_df.columns:
        return pd.DataFrame()
    out = cost_df.copy()
    rate = max(safe_float(credit_rate_usd, 3.68), 0.0)
    reads = reads_df.copy() if (reads_df is not None and not reads_df.empty) else pd.DataFrame()
    reads_available = not reads.empty and "DATA_PRODUCT" in reads.columns
    if reads_available:
        keep_cols = ["DATA_PRODUCT", "DISTINCT_CONSUMERS", "TOTAL_READS", "RECENT_READS",
                     "PRIOR_READS", "LAST_READ"]
        out = out.merge(reads[[c for c in keep_cols if c in reads.columns]],
                        on="DATA_PRODUCT", how="left")
    for col in ("MEASURED_OBJECT_CREDITS", "DISTINCT_CONSUMERS", "TOTAL_READS",
                "RECENT_READS", "PRIOR_READS"):
        out[col] = out[col].map(safe_float) if col in out.columns else 0.0

    out["COST_USD"] = (out["MEASURED_OBJECT_CREDITS"] * rate).round(2)
    # replace(0, NA) can yield an object-dtype divisor, so coerce the quotient back to a
    # numeric Series before rounding (0 consumers / 0 prior -> NA, never inf, never raise).
    consumers = out["DISTINCT_CONSUMERS"].replace(0, pd.NA)
    out["COST_PER_CONSUMER_USD"] = pd.to_numeric(
        out["COST_USD"] / consumers, errors="coerce").round(2)
    prior = out["PRIOR_READS"].replace(0, pd.NA)
    out["READ_TREND_PCT"] = pd.to_numeric(
        (out["RECENT_READS"] - out["PRIOR_READS"]) / prior * 100, errors="coerce").round(1)

    window_reads = out["RECENT_READS"] + out["PRIOR_READS"]
    costly = out["COST_USD"] >= float(min_cost_usd)
    no_usage = (out["DISTINCT_CONSUMERS"] <= 0) | (out["RECENT_READS"] <= 0)
    trend = out["READ_TREND_PCT"].fillna(0.0)   # NA trend (new product) is not a decline
    collapsing = trend <= float(decline_pct)
    declining = trend < 0

    verdict = pd.Series("KEEP", index=out.index)
    verdict = verdict.mask(costly & declining, "REVIEW")
    verdict = verdict.mask(costly & (no_usage | collapsing), "RETIRE_CANDIDATE")
    # INSUFFICIENT overrides: reads absent entirely, OR present-but-too-sparse to judge.
    # A measured 0 (reads available, window_reads==0) is NOT insufficient — it is a real
    # 'nobody touched it' signal and stays RETIRE_CANDIDATE.
    if reads_available:
        sparse = (window_reads > 0) & (window_reads < float(min_window_reads))
    else:
        sparse = pd.Series(True, index=out.index)
    verdict = verdict.mask(sparse, "INSUFFICIENT_DATA")
    out["RETIREMENT_VERDICT"] = verdict
    return out.sort_values("COST_USD", ascending=False).reset_index(drop=True)


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
    # Key by SCHEMA_NAME too so two same-named tasks in different schemas of one
    # database don't blend into a single before/after row (kept optional so
    # pre-SCHEMA_NAME callers/frames still pivot on DB+task).
    index = [c for c in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME") if c in df.columns]
    pivot = df.pivot_table(
        index=index, columns="PERIOD",
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
        # Standalone failures carry no graph-run group id; each one is its own
        # root cause. Collapsing every NULL-group row into a single pseudo-group
        # would keep only the earliest as 'Root cause' and mark the rest
        # 'Cascade' (which route_incidents(root_only=True) then drops), so give
        # NULL-group rows a unique synthetic key while real graph groups stay
        # grouped for genuine cascade detection.
        grp = out[group_col]
        synthetic = grp.where(grp.notna(), "\x00standalone\x00" + out.index.to_series().astype(str))
        firsts = out.sort_values("QUERY_START_TIME").groupby(synthetic, dropna=False).head(1).index
        out["ROLE_IN_GRAPH"] = "Cascade"
        out.loc[firsts, "ROLE_IN_GRAPH"] = "Root cause"
    else:
        out["ROLE_IN_GRAPH"] = "Root cause"
    return out.sort_values("QUERY_START_TIME", ascending=False).reset_index(drop=True)


SYSTEMIC_TASK_THRESHOLD = 3   # >= 3 distinct tasks hit by one family => one cause, not N bugs


def cluster_failures_by_family(timeline: pd.DataFrame) -> pd.DataFrame:
    """Systemic error roll-up (repo wave-2 #14): which error families hit MANY
    distinct tasks — usually one revoked grant / dead source, not N separate bugs.

    From ``build_failure_timeline`` output (ERROR_FAMILY + DATABASE_NAME /
    SCHEMA_NAME / TASK_NAME / ERROR_MESSAGE). Per family: DISTINCT_TASKS (over the
    composite DB.SCHEMA.TASK key, so a task name reused across schemas isn't
    undercounted), FAILURES, SAMPLE_TASK, SAMPLE_ERROR, and SYSTEMIC (>= 3 distinct
    tasks). 'No error text' is listed but never flagged systemic. Sorted systemic
    first, then by breadth. Empty in -> empty out; never raises."""
    if timeline is None or timeline.empty or "ERROR_FAMILY" not in timeline.columns:
        return pd.DataFrame()
    work = timeline.copy()
    for c in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME"):
        if c not in work.columns:
            work[c] = ""
    work["_TASK_KEY"] = (work["DATABASE_NAME"].astype(str) + "." +
                         work["SCHEMA_NAME"].astype(str) + "." + work["TASK_NAME"].astype(str))
    rows: list[dict] = []
    for family, grp in work.groupby("ERROR_FAMILY", dropna=False):
        distinct = int(grp["_TASK_KEY"].nunique())
        fam = str(family)
        errs = [str(e) for e in grp.get("ERROR_MESSAGE", pd.Series(dtype=str)).tolist()
                if str(e).strip()]
        rows.append({
            "ERROR_FAMILY": fam,
            "DISTINCT_TASKS": distinct,
            "FAILURES": len(grp),
            "SAMPLE_TASK": str(grp["_TASK_KEY"].iloc[0]),
            "SAMPLE_ERROR": errs[0][:200] if errs else "",
            "SYSTEMIC": bool(distinct >= SYSTEMIC_TASK_THRESHOLD and fam != "No error text"),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["SYSTEMIC", "DISTINCT_TASKS", "FAILURES"],
        ascending=[False, False, False]).reset_index(drop=True)


DURATION_MIN_SEC = 30.0        # ignore trivially-short tasks (1s->4s is z-huge but immaterial)
DURATION_MIN_ACTIVE_DAYS = 7   # need a real baseline before calling drift


def task_duration_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Tasks running slower than their OWN daily baseline (repo wave-2 #15).

    From FACT_TASK_DAILY rows (DAY, DATABASE_NAME[, SCHEMA_NAME], TASK_NAME, AVG_SEC,
    RUNS): a task that is 100% successful yet quietly 3x slower is otherwise
    unflagged. Each task is identified by its full grain (DB, schema, name, and
    company when the frame carries them) and reduced to one value per calendar day,
    then its daily AVG_SEC is fed into the robust-z engine (``flag_anomalies``,
    median/MAD). Keeps the SLOW side and reduces to one row per task = its worst
    flagged day, with BASELINE_SEC (the task's median) and SLOWER_X (AVG_SEC /
    baseline). Empty in -> empty out; never raises."""
    cols = ["DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME", "DAY", "AVG_SEC",
            "BASELINE_SEC", "SLOWER_X", "Z_SCORE", "RUNS"]
    if (df is None or df.empty
            or not {"DAY", "AVG_SEC", "TASK_NAME", "DATABASE_NAME"}.issubset(df.columns)):
        return pd.DataFrame(columns=cols)
    work = df.copy()
    work["AVG_SEC"] = pd.to_numeric(work["AVG_SEC"], errors="coerce")
    work["DAY"] = pd.to_datetime(work["DAY"], errors="coerce").dt.normalize()
    work = work.dropna(subset=["AVG_SEC", "DAY"])
    if work.empty:
        return pd.DataFrame(columns=cols)
    # Full-grain identity + one row per calendar day: keying on DB.TASK_NAME alone
    # interleaves same-named tasks from different schemas/companies into one series,
    # and a duplicate mart row would inflate the active-day count.
    grain = (["DATABASE_NAME"]
             + (["SCHEMA_NAME"] if "SCHEMA_NAME" in work.columns else [])
             + ["TASK_NAME"]
             + (["COMPANY"] if "COMPANY" in work.columns else []))
    agg: dict[str, str] = {"AVG_SEC": "mean"}
    if "RUNS" in work.columns:
        agg["RUNS"] = "sum"
    daily = work.groupby([*grain, "DAY"], as_index=False).agg(agg)
    daily["TASK_KEY"] = daily[grain].astype(str).agg(".".join, axis=1)
    flagged = flag_anomalies(daily, "AVG_SEC", group_col="TASK_KEY",
                             min_value=DURATION_MIN_SEC, min_active_days=DURATION_MIN_ACTIVE_DAYS)
    if flagged is None or flagged.empty or "IS_ANOMALY" not in flagged.columns:
        return pd.DataFrame(columns=cols)
    slow = flagged[flagged["IS_ANOMALY"]
                   & (pd.to_numeric(flagged["Z_SCORE"], errors="coerce") > 0)].copy()
    if slow.empty:
        return pd.DataFrame(columns=cols)
    baseline = daily.groupby("TASK_KEY")["AVG_SEC"].median().rename("BASELINE_SEC")
    slow = slow.join(baseline, on="TASK_KEY")
    slow["SLOWER_X"] = (pd.to_numeric(slow["AVG_SEC"], errors="coerce")
                        / slow["BASELINE_SEC"].where(slow["BASELINE_SEC"] > 0)).round(1)
    slow["_absz"] = pd.to_numeric(slow["Z_SCORE"], errors="coerce").abs()
    worst = (slow.sort_values("_absz", ascending=False)
             .groupby("TASK_KEY", as_index=False).head(1))
    keep = [c for c in cols if c in worst.columns]
    return worst.sort_values("SLOWER_X", ascending=False).reset_index(drop=True)[keep]


DURATION_FORECAST_RECENT_DAYS = 3   # trailing days that form the "recent level"
DURATION_FORECAST_AT_RISK_X = 1.3   # recent-median >= this x baseline (+ climb) -> At risk
DURATION_FORECAST_MISS_X = 2.0      # recent-median >= this x baseline (+ climb) -> Predicted miss


def duration_sla_forecast(df: pd.DataFrame) -> pd.DataFrame:
    """Tasks whose daily runtime is CLIMBING toward a likely SLA miss (Upgrade Board
    #5) — the leading half of the duration signal, complementary to
    ``task_duration_anomalies`` (which flags a task that was ALREADY an outlier on
    some day).

    From FACT_TASK_DAILY rows (DAY, DATABASE_NAME[, SCHEMA_NAME], TASK_NAME, AVG_SEC):
    each task (identified by its full grain — DB, schema, name, and company when the
    frame carries them — deduped to one value per calendar day) needs
    >= DURATION_MIN_ACTIVE_DAYS distinct days and a baseline (median of the days BEFORE
    the last DURATION_FORECAST_RECENT_DAYS) of >= DURATION_MIN_SEC. It is flagged when
    the last day is above the start of the recent window (a climb, not a recovery) AND
    the recent-window MEDIAN — robust, so a single spike on any one day cannot fake a
    trend — is materially above the baseline: 'Predicted miss' at >= MISS_X, 'At risk'
    at >= AT_RISK_X. The tier is taken from the SAME rounded ratio shown as SLOWER_X,
    so the label never contradicts the number. One row per flagged task (its latest
    day), worst first. ACCOUNT_USAGE only sees COMPLETED runs, so this is a
    trailing-window forecast, not a live in-flight detector. Empty in -> empty out.
    """
    cols = ["DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME", "DAY", "BASELINE_SEC",
            "LATEST_SEC", "SLOWER_X", "FORECAST", "SEVERITY"]
    if (df is None or df.empty
            or not {"DAY", "AVG_SEC", "TASK_NAME", "DATABASE_NAME"}.issubset(df.columns)):
        return pd.DataFrame(columns=cols)
    work = df.copy()
    work["AVG_SEC"] = pd.to_numeric(work["AVG_SEC"], errors="coerce")
    work["DAY"] = pd.to_datetime(work["DAY"], errors="coerce").dt.normalize()
    work = work.dropna(subset=["AVG_SEC", "DAY"])
    if work.empty:
        return pd.DataFrame(columns=cols)
    # Full-grain identity: the same task name can exist across schemas/companies —
    # keying only on DB.TASK_NAME would interleave their series into one. Use every
    # grain column the frame carries, then reduce to one value per calendar day so a
    # duplicate mart row can neither inflate the day count nor distort the window.
    grain = (["DATABASE_NAME"]
             + (["SCHEMA_NAME"] if "SCHEMA_NAME" in work.columns else [])
             + ["TASK_NAME"]
             + (["COMPANY"] if "COMPANY" in work.columns else []))
    daily = work.groupby([*grain, "DAY"], as_index=False)["AVG_SEC"].mean()
    rows = []
    for _key, group in daily.groupby(grain):
        g = group.sort_values("DAY")
        series = g["AVG_SEC"].to_numpy()
        if len(series) < DURATION_MIN_ACTIVE_DAYS:      # distinct days after the dedup
            continue
        recent = series[-DURATION_FORECAST_RECENT_DAYS:]
        prior = series[:-DURATION_FORECAST_RECENT_DAYS]
        baseline = float(pd.Series(prior).median())
        if baseline < DURATION_MIN_SEC:
            continue
        recent_level = float(pd.Series(recent).median())   # robust to a one-day spike
        latest = float(recent[-1])
        climbing = latest > float(recent[0])               # ended above the window start
        ratio = round(recent_level / baseline, 1) if baseline > 0 else 0.0
        if not (climbing and ratio >= DURATION_FORECAST_AT_RISK_X):
            continue
        forecast, severity = (("Predicted miss", "High") if ratio >= DURATION_FORECAST_MISS_X
                              else ("At risk", "Medium"))
        last = g.iloc[-1]
        rows.append({
            "DATABASE_NAME": last["DATABASE_NAME"],
            "SCHEMA_NAME": (last["SCHEMA_NAME"] if "SCHEMA_NAME" in work.columns else None),
            "TASK_NAME": last["TASK_NAME"],
            "DAY": last["DAY"],
            "BASELINE_SEC": round(baseline, 1),
            "LATEST_SEC": round(latest, 1),
            "SLOWER_X": ratio,
            "FORECAST": forecast,
            "SEVERITY": severity,
        })
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("SLOWER_X", ascending=False).reset_index(drop=True)[cols]


def flag_clustering_churn(df: pd.DataFrame, *, rate: float | None = None,
                          min_credits: float = 1.0) -> pd.DataFrame:
    """Auto-clustering churn (repo wave-2 #6): tables paying credits to recluster
    ~nothing — the poor-cluster-key signal the raw spend table can't name.

    From ``clustering_by_table`` (TABLE_FQN, CREDITS, TB_RECLUSTERED,
    RECLUSTER_RUNS): adds CREDITS_PER_TB_RECLUSTERED (NaN when 0 TB reclustered),
    SPEND_USD (when a rate is given), and CHURNY = material credits spent to
    recluster ~0 TB (the sharpest 'paying to reorganize nothing' case). Sorted
    churny-first, then by credits-per-TB. Empty in -> empty out; never raises."""
    if df is None or df.empty or "CREDITS" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["CREDITS"] = pd.to_numeric(out["CREDITS"], errors="coerce").fillna(0.0)
    tb = pd.to_numeric(out.get("TB_RECLUSTERED", 0.0), errors="coerce").fillna(0.0)
    out["TB_RECLUSTERED"] = tb
    # ratio is undefined at 0 TB -> NaN (blank in the table); the CHURNY flag, not the
    # ratio, carries the zero-TB signal so a div-by-zero can't mis-sort it to the bottom.
    out["CREDITS_PER_TB_RECLUSTERED"] = (out["CREDITS"] / tb.where(tb > 0)).round(2)
    if rate is not None:
        out["SPEND_USD"] = (out["CREDITS"] * safe_float(rate)).round(2)
    out["CHURNY"] = (out["CREDITS"] >= float(min_credits)) & (tb <= 0)
    if "SPEND_USD" in out.columns:
        # #31: recoverable $ = the CHURNY spend that SUSPEND RECLUSTER would stop
        # (paying to recluster ~0 TB); zero for non-churny rows.
        out["RECOVERABLE_USD"] = out["SPEND_USD"].where(out["CHURNY"], 0.0).round(2)
    return out.sort_values(["CHURNY", "CREDITS_PER_TB_RECLUSTERED", "CREDITS"],
                           ascending=[False, False, False],
                           na_position="last").reset_index(drop=True)


def suspend_recluster_sql(table_fqn: str) -> str:
    """Generated (not executed) SUSPEND RECLUSTER candidate for a churny table."""
    from app.core.sqlsafe import safe_identifier
    return f"ALTER TABLE {safe_identifier(str(table_fqn), allow_qualified=True)} SUSPEND RECLUSTER;"


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


def task_failure_streaks(df: pd.DataFrame) -> pd.DataFrame:
    """Per-task leading FAILED streak since the last SUCCEEDED (repo wave-2).

    Input: ``ops_sql.task_recent_states`` rows (DATABASE_NAME, SCHEMA_NAME,
    TASK_NAME, SCHEDULED_TIME, STATE, ERROR_MESSAGE), newest first. A task whose
    newest runs are failing in a row is 'actively broken now' — a sharper on-call
    signal than a lifetime failure count, which ``task_runs`` (MAX_BY newest state)
    cannot express. Returns one row per task with a leading failure streak >= 1:
    FAIL_STREAK, ACTIVELY_BROKEN (>= 2 consecutive), LAST_RUN, LAST_ERROR, SEVERITY
    (High >= 3, Medium == 2, Low == 1). Empty in -> empty out; never raises."""
    if df is None or df.empty or "STATE" not in df.columns:
        return pd.DataFrame()
    keys = ["DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME"]
    if any(k not in df.columns for k in keys):
        return pd.DataFrame()
    work = df.copy()
    work["SCHEDULED_TIME"] = pd.to_datetime(work.get("SCHEDULED_TIME"), errors="coerce")
    work["STATE"] = work["STATE"].astype(str).str.upper()
    rows: list[dict] = []
    for key_vals, grp in work.groupby(keys, dropna=False):
        ordered = grp.sort_values("SCHEDULED_TIME", ascending=False)
        streak = 0
        last_error = ""
        for _, r in ordered.iterrows():
            if r["STATE"] == "FAILED":
                streak += 1
                if not last_error:
                    last_error = str(r.get("ERROR_MESSAGE") or "")
            else:
                break  # newest SUCCEEDED reached — the failing streak ends here
        if streak < 1:
            continue
        db, sch, task = key_vals
        rows.append({
            "DATABASE_NAME": db, "SCHEMA_NAME": sch, "TASK_NAME": task,
            "FAIL_STREAK": int(streak),
            "ACTIVELY_BROKEN": bool(streak >= 2),
            "LAST_RUN": ordered.iloc[0]["SCHEDULED_TIME"],
            "LAST_ERROR": last_error,
            "SEVERITY": "High" if streak >= 3 else "Medium" if streak >= 2 else "Low",
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    return out.sort_values(["FAIL_STREAK", "LAST_RUN"],
                           ascending=[False, False]).reset_index(drop=True)


# A task silent only within the ACCOUNT_USAGE reporting lag (~45 min) is not late —
# it may simply not have been reported yet; require overdue beyond it before flagging.
_TASK_LAG_MIN = 45.0


def task_freshness_status(df: pd.DataFrame) -> pd.DataFrame:
    """Classify each task On-time / Late / Stale against its own cadence (repo wave-2).

    Input: ``ops_sql.task_freshness_sla`` rows (MEDIAN_GAP_MIN, MINS_SINCE_SUCCESS,
    ...). A task silent well past its expected cadence has quietly stopped being
    scheduled — invisible to the run/failure views, which only show runs that
    happened. Stale ~ 2x cadence overdue; Late ~ 1x; and nothing is flagged unless
    overdue by more than the ~45-min ACCOUNT_USAGE lag, so a task merely inside the
    telemetry window is not a false positive. A task with no success in the window
    reads as Stale (silent). Adds OVERDUE_MIN, STATUS, SEVERITY. Empty in -> empty
    out; never raises."""
    if df is None or df.empty or "MEDIAN_GAP_MIN" not in df.columns:
        return pd.DataFrame()
    out = df.copy().reset_index(drop=True)

    def _num(name: str) -> pd.Series:
        if name in out.columns:
            return pd.to_numeric(out[name], errors="coerce")
        return pd.Series([float("nan")] * len(out), index=out.index)  # absent -> all-NaN, never raises

    gap = _num("MEDIAN_GAP_MIN")
    mins = _num("MINS_SINCE_SUCCESS")
    statuses: list[str] = []
    severities: list[str] = []
    overdue: list[float] = []
    for i in range(len(out)):
        g = safe_float(gap.iloc[i])
        m_raw = mins.iloc[i]
        if pd.isna(m_raw):
            statuses.append("Stale")
            severities.append("High")
            overdue.append(float("nan"))
            continue
        m = safe_float(m_raw)
        over = m - g
        overdue.append(round(over, 1))
        if g > 0 and m >= 2 * g and over >= _TASK_LAG_MIN:
            statuses.append("Stale")
            severities.append("High")
        elif g > 0 and m >= g and over >= _TASK_LAG_MIN:
            statuses.append("Late")
            severities.append("Medium")
        else:
            statuses.append("On-time")
            severities.append("Low")
    out["OVERDUE_MIN"] = overdue
    out["STATUS"] = statuses
    out["SEVERITY"] = severities
    return out.sort_values("OVERDUE_MIN", ascending=False,
                           na_position="first").reset_index(drop=True)


def takeover_severity(df: pd.DataFrame) -> pd.DataFrame:
    """Rank account-takeover candidates (``security_sql.login_takeover_candidates``).

    A failure burst FOLLOWED BY a success (SUCCEEDED_AFTER) is the dangerous case —
    a burst with no later success is a locked-out user. High when a breakthrough
    pairs with volume or spread (10+ failures, or 3+ distinct source IPs); Medium
    for any breakthrough; Low for a failure burst that never succeeded. Adds
    SEVERITY, sorted worst-first. Empty in -> empty out; never raises."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for _c in ("FAILURES", "FAIL_IPS"):
        # absent -> 0.0 column (never raises: out.get(_c, 0) would be a bare int with no .map)
        out[_c] = (pd.to_numeric(out[_c], errors="coerce").fillna(0.0)
                   if _c in out.columns else 0.0)
    broke_col = out.get("SUCCEEDED_AFTER")
    broke = (broke_col.fillna(False).astype(bool)
             if broke_col is not None else pd.Series(False, index=out.index))
    out["SUCCEEDED_AFTER"] = broke
    out["SEVERITY"] = out.apply(
        lambda r: ("Low" if not bool(r["SUCCEEDED_AFTER"])
                   else "High" if (r["FAILURES"] >= 10 or r["FAIL_IPS"] >= 3)
                   else "Medium"),
        axis=1,
    )
    rank = {"High": 0, "Medium": 1, "Low": 2}
    return (out.assign(_o=out["SEVERITY"].map(rank))
            .sort_values(["_o", "FAILURES"], ascending=[True, False])
            .drop(columns="_o").reset_index(drop=True))


# ---- #18. Data-exfiltration composite behavioral score ----------------------

# Whole-token markers of an automated (non-human) principal. Extends the account's
# existing LIKE 'TF~_%' convention: a role or user is a service account when, after
# splitting ROLE_NAME/USER_NAME on non-alphanumeric boundaries, ANY token EXACTLY
# matches one of these. Whole-token (not substring) matching is deliberate and
# load-bearing — an unanchored `"SERVICE" in text` wrongly caps a human whose role
# is CUSTOMER_SERVICE_LEAD or PROF_SERVICES_ANALYST, and the false negative (a real
# human exfil filed as Low) is the worst error this detector can make. Generic
# dictionary words that collide with human departments (SERVICE, SYSTEM, PIPELINE)
# are therefore intentionally NOT here.
_EXFIL_SERVICE_TOKENS = frozenset({
    "TF", "SVC", "ETL", "DBT", "SNOWPIPE", "FIVETRAN", "AIRFLOW", "AIRBYTE",
    "MELTANO", "STITCH", "MATILLION", "INGEST", "INGESTION", "LOADER", "LOAD",
    "PIPE",
})
# Prefix markers: a token STARTING with one of these is a service principal, which
# covers a family without listing every variant (REPLICATION / REPLICATOR / ...).
# Kept short on purpose — a prefix is looser than a whole token, so only unambiguous
# stems belong here (not 'SVC', which would also catch a human like 'SVCARSON').
_EXFIL_SERVICE_PREFIXES = ("REPLICAT",)

# Weighted contribution of each sub-signal to the 0-100 score. Deliberately a
# transparent additive model (not an opaque ML score) so every point on the board
# is explainable from the REASON column — an auditor can see exactly which factors
# fired. Volume is the heaviest single factor; the human/service split is a GATE
# rather than just a weight (see below).
_EXFIL_W_VOLUME = 35
_EXFIL_W_OFFHOURS = 25
_EXFIL_W_DEST = 25
_EXFIL_W_HUMAN = 15
# A service (automated) principal is capped strictly below the Medium band, so
# routine ETL egress can never rank above Low no matter how big or how off-hours —
# "does a nightly bulk load look like an incident?" answers "no" by construction.
_EXFIL_SERVICE_CAP = 35


def _exfil_is_service(role: object, user: object) -> bool:
    # Join with a separator so a marker sitting at the role/user boundary can't be
    # merged into a neighbouring token, then match whole tokens (+ a few prefixes).
    text = (str(role or "") + "_" + str(user or "")).upper()
    tokens = [t for t in re.split(r"[^A-Z0-9]+", text) if t]
    if _EXFIL_SERVICE_TOKENS.intersection(tokens):
        return True
    return any(t.startswith(_EXFIL_SERVICE_PREFIXES) for t in tokens)


def _exfil_dest_class(target: object) -> str:
    """Classify a COPY INTO target. 'personal' = a user (personal) stage or an
    ad-hoc cloud URL — data leaving to a location no curated pipeline owns.
    'named' = a named stage (the normal, governed path). 'unknown' = NULL/unparsed
    (the regex found nothing) — treated as neither safe nor damning."""
    # NULL destination (regex found nothing) arrives as None or a float NaN.
    if target is None or (not isinstance(target, str) and pd.isna(target)):
        return "unknown"
    # Snowflake external locations are SINGLE-QUOTED literals (COPY INTO 's3://…'),
    # and the SQL regex captures the token verbatim — so the value arrives WITH its
    # surrounding quotes. Strip them (and whitespace) before the prefix checks, or
    # the entire cloud-URL arm silently never fires on real unload syntax (the exact
    # data-leaving-the-account case this feature exists to catch).
    t = str(target).strip().strip("'\"").strip().lower()
    if not t:
        return "unknown"
    if t.startswith("@~"):
        return "personal"
    if t.startswith(("s3://", "azure://", "gcs://", "gs://", "https://", "http://")):
        return "personal"
    return "named"


def egress_exfil_severity(
    df: pd.DataFrame,
    *,
    high_gb: float = 50.0,
    spike_mult: float = 3.0,
    business_start_hour: int = 7,
    business_end_hour: int = 19,
) -> pd.DataFrame:
    """#18: composite behavioral exfiltration score for unload events (heuristic).

    ``egress_review`` already answers "who unloaded, and how much?". A big number
    there is not automatically an incident — a scheduled export is supposed to be
    big. This fuses four independently auditable sub-signals per event —

      * VOL_UNUSUAL  — GB_OUT clears an absolute floor (``high_gb``) OR is
        ``spike_mult``x this user's own median (the OR matters: a user whose median
        is tiny is caught by the multiple; a whale with a huge median is still
        caught by the floor).
      * OFF_HOURS    — the account-local event hour is a weekend or outside
        ``business_start_hour``..``business_end_hour``.
      * PERSONAL_DEST — the COPY INTO target is a personal stage or ad-hoc cloud
        URL rather than a governed named stage.
      * HUMAN_USER   — the principal is not an automated service/ETL role.

    into a 0-100 ``SCORE`` (transparent weighted sum), a ``SEVERITY``
    (High>=70 / Medium>=40 / Low), and a ``REASON`` enumerating every factor that
    fired. HUMAN_USER is a GATE, not just a weight: a service principal is capped at
    ``_EXFIL_SERVICE_CAP`` (Low), so routine ETL egress never outranks a human doing
    the same thing. Empty in -> empty out; never raises."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()

    def _num(col: str, default: float) -> pd.Series:
        # Absence-safe: out.get(missing) is None, and pd.to_numeric(None) returns a
        # bare scalar NaN whose .fillna would raise — so fall back to an index-aligned
        # Series first. Mirrors the defensive column handling below (honors the
        # "never raises" contract even if the SQL projection ever changes).
        s = out.get(col)
        if s is None:
            s = pd.Series(default, index=out.index)
        return pd.to_numeric(s, errors="coerce").fillna(default)

    gb = _num("GB_OUT", 0.0)
    med = _num("USER_MEDIAN_GB", 0.0)
    hour = _num("HOUR_OF_DAY", 12.0)
    dow = _num("DOW_ISO", 1.0)
    out["GB_OUT"] = gb

    spike = (med > 0) & (gb >= spike_mult * med)
    out["VOL_UNUSUAL"] = (gb >= high_gb) | spike
    out["OFF_HOURS"] = (dow >= 6) | (hour < business_start_hour) | (hour >= business_end_hour)
    dest = out.get("TARGET_LOCATION")
    out["DEST_CLASS"] = (dest.map(_exfil_dest_class) if dest is not None
                         else pd.Series("unknown", index=out.index))
    out["PERSONAL_DEST"] = out["DEST_CLASS"] == "personal"
    role_s = (out["ROLE_NAME"].astype(str) if "ROLE_NAME" in out.columns
              else pd.Series("", index=out.index))
    user_s = (out["USER_NAME"].astype(str) if "USER_NAME" in out.columns
              else pd.Series("", index=out.index))
    out["HUMAN_USER"] = [not _exfil_is_service(r, u)
                         for r, u in zip(role_s, user_s, strict=False)]

    score = (_EXFIL_W_VOLUME * out["VOL_UNUSUAL"].astype(int)
             + _EXFIL_W_OFFHOURS * out["OFF_HOURS"].astype(int)
             + _EXFIL_W_DEST * out["PERSONAL_DEST"].astype(int)
             + _EXFIL_W_HUMAN * out["HUMAN_USER"].astype(int))
    # Service-role GATE: cap strictly below Medium so ETL can't rank as an incident.
    score = score.where(out["HUMAN_USER"], score.clip(upper=_EXFIL_SERVICE_CAP))
    out["SCORE"] = score.clip(0, 100).round().astype(int)
    out["SEVERITY"] = out["SCORE"].map(
        lambda s: "High" if s >= 70 else "Medium" if s >= 40 else "Low")
    out["REASON"] = out.apply(_exfil_reason, axis=1)

    rank = {"High": 0, "Medium": 1, "Low": 2}
    return (out.assign(_o=out["SEVERITY"].map(rank))
            .sort_values(["_o", "SCORE", "GB_OUT"], ascending=[True, False, False])
            .drop(columns="_o").reset_index(drop=True))


def _exfil_reason(r: pd.Series) -> str:
    parts: list[str] = []
    gb = safe_float(r.get("GB_OUT"))
    med = safe_float(r.get("USER_MEDIAN_GB"))
    if bool(r.get("VOL_UNUSUAL")):
        if med > 0 and gb >= 3 * med:
            parts.append(f"{gb:,.0f} GB ({gb / med:.0f}x user median)")
        else:
            parts.append(f"{gb:,.0f} GB")
    if bool(r.get("OFF_HOURS")):
        parts.append(f"{int(safe_float(r.get('HOUR_OF_DAY'))):02d}:00 local, off-hours")
    if bool(r.get("PERSONAL_DEST")):
        dest = str(r.get("TARGET_LOCATION") or "")[:40]
        parts.append(f"personal/ad-hoc destination {dest}".rstrip())
    parts.append("human user" if bool(r.get("HUMAN_USER")) else "service role (capped)")
    return "; ".join(parts)


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
