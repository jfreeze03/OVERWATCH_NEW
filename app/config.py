"""Central constants. Pure module: no Streamlit, no Snowflake imports.

Rates and thresholds here are OFFLINE FALLBACKS ONLY — the live values come
from OVERWATCH.CORE.SETTINGS (seeded by V001) and are edited on the Admin
page, not in code.
"""

from __future__ import annotations

APP_NAME = "OVERWATCH"
APP_VERSION = "4.0.0"

# ---------------------------------------------------------------------------
# Snowflake object locations (must match snowflake/migrations/V001__core.sql)
# ---------------------------------------------------------------------------
OVERWATCH_DB = "OVERWATCH"
CORE_SCHEMA = "CORE"
MART_SCHEMA = "MART"
APP_WAREHOUSE = "OVERWATCH_WH"
APP_QUERY_TAG_PREFIX = "OVERWATCH"


def core_object(name: str) -> str:
    return f"{OVERWATCH_DB}.{CORE_SCHEMA}.{name}"


def mart_object(name: str) -> str:
    return f"{OVERWATCH_DB}.{MART_SCHEMA}.{name}"


# ---------------------------------------------------------------------------
# Rates — fallback defaults; CORE.SETTINGS is authoritative at runtime.
# Contract rates confirmed 2026-07: $3.68 compute, $2.20 Cortex.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "CREDIT_PRICE_USD": 3.68,
    "AI_CREDIT_PRICE_USD": 2.20,
    "STORAGE_USD_PER_TB_MONTH": 23.00,
    "MONTHLY_BUDGET_USD": 0.0,       # 0 = not configured; UI must not invent one
    "CONTRACT_CREDITS": 0.0,         # 0 = not configured
    "CONTRACT_START_DATE": "",
    "CONTRACT_END_DATE": "",
}

# ---------------------------------------------------------------------------
# Windows and guardrails
# ---------------------------------------------------------------------------
DAY_WINDOW_OPTIONS = (7, 14, 30, 60, 90)
DEFAULT_DAY_WINDOW = 7
MAX_LIVE_WINDOW_DAYS = 90          # hard clamp for live ACCOUNT_USAGE scans
DEFAULT_MAX_ROWS = 5_000           # visible-truncation cap for detail tables

THRESHOLDS = {
    "query_fail_pct_warn": 2.0,
    "task_fail_pct_warn": 1.0,
    "queue_minutes_warn": 10.0,
    "spill_gb_warn": 5.0,
    "anomaly_z": 3.5,
    "stale_fact_hours": 3.0,        # hourly facts older than this are stale
    "stale_daily_fact_hours": 30.0, # daily facts older than this are stale
}

ACCOUNT_USAGE_LAG_NOTE = "Account telemetry can lag up to ~45 min (metering-daily up to 24h)."

# ---------------------------------------------------------------------------
# Role -> navigation profile (page FILTERING only; Snowflake RBAC is the
# actual security boundary under Streamlit-in-Snowflake).
# ---------------------------------------------------------------------------
ROLE_PROFILE_OVERRIDES = {
    "SNOW_PRI_GFR_PRD_ALFA_PDMWMGMT": "EXECUTIVE",
    "SNOW_PRI_GFR_PRD_ALFA_DSA": "MANAGER",
    "SNOW_PRI_GFR_PRD_ALFA_DTI": "ANALYST",
    "SNOW_PRI_GFR_NONPRD_ALFA_PDMWMGMT": "EXECUTIVE",
    "SNOW_PRI_GFR_NONPRD_ALFA_DSA": "MANAGER",
    "SNOW_PRI_GFR_NONPRD_ALFA_DTI": "ANALYST",
    "SNOW_ACCOUNTADMINS": "DBA",
    "SNOW_SYSADMINS": "DBA",
    "ACCOUNTADMIN": "DBA",
    "SYSADMIN": "DBA",
    "OVERWATCH_OPERATOR": "DBA",
    "OVERWATCH_MONITOR": "ANALYST",
}

PAGES_BY_PROFILE = {
    "EXECUTIVE": ("Overview", "Cost & Contract", "Alerts"),
    "ANALYST": ("Overview", "Control Room", "Cost & Contract", "Operations", "Alerts", "Security"),
    "MANAGER": ("Overview", "Control Room", "Cost & Contract", "Operations", "Alerts", "Security"),
    "DBA": ("Overview", "Control Room", "Cost & Contract", "Operations", "Alerts", "Security", "Admin"),
}
DEFAULT_PROFILE = "ANALYST"

OPERATOR_PROFILES = ("DBA",)  # profiles allowed to execute state-changing SQL in-app


def resolve_role_profile(role: str) -> str:
    """Map a Snowflake role name to a navigation profile."""
    normalized = str(role or "").strip().upper()
    if not normalized:
        return DEFAULT_PROFILE
    if normalized in ROLE_PROFILE_OVERRIDES:
        return ROLE_PROFILE_OVERRIDES[normalized]
    if normalized.endswith("_DSA") or "_DSA_" in normalized:
        return "MANAGER"
    if normalized.endswith("_DTI") or "_DTI_" in normalized:
        return "ANALYST"
    if normalized.endswith("_PDMWMGMT") or "_PDMWMGMT_" in normalized:
        return "EXECUTIVE"
    if "ACCOUNTADMIN" in normalized or "SYSADMIN" in normalized or "DBA" in normalized:
        return "DBA"
    return DEFAULT_PROFILE


def clamp_days(days: object, maximum: int = MAX_LIVE_WINDOW_DAYS) -> int:
    """Clamp a day window to a safe integer range for live scans."""
    try:
        value = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = DEFAULT_DAY_WINDOW
    return max(1, min(value, maximum))
