"""Pure logic for the ported insight features (1-7)."""

from __future__ import annotations

import re

import pandas as pd

from .formulas import safe_div, safe_float

# ---- 1. Idle warehouse advisor ---------------------------------------------

IDLE_PCT_FLAG = 20.0     # flag warehouses wasting >=20% of credits idle
IDLE_MIN_CREDITS = 1.0   # and at least 1 idle credit in the window


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
    out["RECOMMENDATION"] = out.apply(
        lambda r: (
            f"Reduce AUTO_SUSPEND (e.g. 60s) on {r['WAREHOUSE_NAME']}: "
            f"~{r['IDLE_PCT']:.0f}% of its credits burn in hours with zero queries."
        ) if r["FLAGGED"] else "Idle share within tolerance.",
        axis=1,
    )
    return out.sort_values("IDLE_USD", ascending=False).reset_index(drop=True)


def idle_suspend_sql(warehouse: str, seconds: int = 60) -> str:
    """Generated (not executed) remediation for a flagged warehouse."""
    from app.core.sqlsafe import safe_identifier

    wh = safe_identifier(str(warehouse))
    seconds = max(30, min(int(seconds), 3600))
    return f"ALTER WAREHOUSE {wh} SET AUTO_SUSPEND = {seconds};"


# ---- 2. Repeat-query candidates ---------------------------------------------

REPEAT_MIN_ELAPSED_HOURS = 0.5
REPEAT_LOW_CACHE_PCT = 25.0


def flag_repeat_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Flag fingerprints worth materializing/caching: heavy + cache-poor."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("RUNS", "TOTAL_ELAPSED_HOURS", "AVG_CACHE_PCT", "TOTAL_TB_SCANNED"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    out["CANDIDATE"] = (
        (out["TOTAL_ELAPSED_HOURS"] >= REPEAT_MIN_ELAPSED_HOURS)
        & (out["AVG_CACHE_PCT"] <= REPEAT_LOW_CACHE_PCT)
    )
    out["WHY"] = out.apply(
        lambda r: (
            f"{int(r['RUNS'])} runs, {r['TOTAL_ELAPSED_HOURS']:.1f}h compute, "
            f"{r['AVG_CACHE_PCT']:.0f}% cache — consider a materialized/refreshed table or schedule change."
        ) if r["CANDIDATE"] else "",
        axis=1,
    )
    return out


# ---- 3. Storage growth movers ------------------------------------------------

def storage_movers(df: pd.DataFrame, usd_per_tb_month: float) -> pd.DataFrame:
    """Growth per database with projected monthly $ delta."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("FIRST_BYTES", "LAST_BYTES", "FAILSAFE_BYTES", "SPAN_DAYS"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    tb = 1024.0**4
    rate = safe_float(usd_per_tb_month, 23.0)
    out["CURRENT_TB"] = (out["LAST_BYTES"] / tb).round(3)
    out["GROWTH_TB"] = ((out["LAST_BYTES"] - out["FIRST_BYTES"]) / tb).round(3)
    span = out["SPAN_DAYS"].clip(lower=1)
    out["GROWTH_TB_30D"] = (out["GROWTH_TB"] / span * 30).round(3)
    out["GROWTH_USD_30D"] = (out["GROWTH_TB_30D"] * rate).round(2)
    out["FAILSAFE_SHARE_PCT"] = (
        out["FAILSAFE_BYTES"] / out["LAST_BYTES"].replace(0, pd.NA) * 100
    ).fillna(0.0).round(1)
    return out.sort_values("GROWTH_TB", ascending=False).reset_index(drop=True)


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
        rows.append({"Metric": label, "Before": round(b, 2), "After": round(a, 2),
                     "Delta %": delta_pct, "Verdict": verdict})
    return rows



_ERROR_FAMILIES = (
    ("Permission / auth", r"not authorized|insufficient privilege|does not exist or not authorized|access denied"),
    ("Missing object", r"does not exist\b|invalid identifier|unknown (table|view|function)"),
    ("Timeout / cancelled", r"timeout|timed out|statement reached its statement or warehouse timeout|cancelled"),
    ("Resource / memory", r"out of memory|resource|exceeded|quota"),
    ("Data quality", r"numeric value|conversion|null result|duplicate|constraint|division by zero|is not recognized"),
    ("Syntax / SQL", r"syntax error|unexpected|compilation error"),
)


def classify_task_error(message: object) -> str:
    text = str(message or "").lower()
    if not text.strip():
        return "No error text"
    for family, pattern in _ERROR_FAMILIES:
        if re.search(pattern, text):
            return family
    return "Other"


# ---- 7. Dormant users ----------------------------------------------------------

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
