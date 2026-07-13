"""Central constants. Pure module: no Streamlit, no Snowflake imports.

Rates and thresholds here are OFFLINE FALLBACKS ONLY — the live values come
from DBA_MAINT_DB.OVERWATCH.SETTINGS (seeded by V001) and are edited on the Admin
page, not in code.
"""

from __future__ import annotations

APP_NAME = "OVERWATCH"
APP_VERSION = "4.43.0"

# ---------------------------------------------------------------------------
# Snowflake object locations (must match snowflake/migrations/V001__core.sql)
# ---------------------------------------------------------------------------
# Owner decision 2026-07: all OVERWATCH objects live in the existing
# DBA_MAINT_DB.OVERWATCH schema (shared with the previous app's objects).
OVERWATCH_DB = "DBA_MAINT_DB"
CORE_SCHEMA = "OVERWATCH"
MART_SCHEMA = "OVERWATCH"
APP_WAREHOUSE = "WH_ALFA_OVERWATCH"
APP_QUERY_TAG_PREFIX = "OVERWATCH"


def core_object(name: str) -> str:
    return f"{OVERWATCH_DB}.{CORE_SCHEMA}.{name}"


def mart_object(name: str) -> str:
    return f"{OVERWATCH_DB}.{MART_SCHEMA}.{name}"


# ---------------------------------------------------------------------------
# Rates — fallback defaults; SETTINGS is authoritative at runtime.
# Contract rates confirmed 2026-07: $3.68 compute, $2.20 Cortex.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "CREDIT_PRICE_USD": 3.68,
    "AI_CREDIT_PRICE_USD": 2.20,
    "STORAGE_USD_PER_TB_MONTH": 23.00,
    "MONTHLY_BUDGET_USD": 0.0,       # 0 = not configured; UI must not invent one
    "AI_MONTHLY_BUDGET_USD": 0.0,    # 0 = not configured; gates Cortex user severities
    "CORTEX_MODEL": "llama3.1-8b",   # model for in-app AI evaluations (Admin-editable)
    # Platform-score weights (per-unit penalties; caps fixed in scoring.py).
    # Uncalibrated starting points - tune against incident history.
    "SCORE_PTS_BUDGET_PER_PCT": "0.5",
    "SCORE_PTS_PER_CRITICAL": "6",
    "SCORE_PTS_PER_HIGH": "2",
    "SCORE_PTS_QUERY_FAIL_PER_PCT": "1.5",
    "SCORE_PTS_QUEUE_PER_MIN": "0.3",
    "SCORE_PTS_SPILL_PER_GB": "0.5",
    "SCORE_PTS_PER_STALE_SOURCE": "4",
    "SCORE_PTS_PER_OPEN_ACTION": "1.5",
    # Fact retention (SP_PURGE_FACTS, monthly). Floors in the proc: 90/180/30.
    "FACT_RETENTION_DAYS_HOURLY": "400",
    "FACT_RETENTION_DAYS_DAILY": "800",
    "ERROR_LOG_RETENTION_DAYS": "180",
    "APP_USAGE_RETENTION_DAYS": "365",
    # Forecast engine: linear | seasonal | ml_forecast (needs the opt-in
    # snowflake/ml_forecast_option.sql; falls back to seasonal when absent).
    "FORECAST_ENGINE": "linear",
    # Governance-drift weights (per-unit penalties; caps fixed in governance.py).
    "GOV_PTS_MFA_GAP": "5",
    "GOV_PTS_EXPIRED_CRED": "8",
    "GOV_PTS_EXPIRING_CRED": "2",
    "GOV_PTS_BREAKGLASS_GRANT": "6",
    "GOV_PTS_NO_MONITOR": "4",
    "GOV_PTS_NO_AUTOSUSPEND": "3",
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

# Only knobs that CODE actually reads live here (review #8: five decorative
# entries removed — alert thresholds are data in ALERT_CONFIG, score weights
# live in SETTINGS, the anomaly z default lives in logic/anomaly.py).
THRESHOLDS = {
    "stale_fact_hours": 3.0,        # hourly facts older than this are stale
    "stale_daily_fact_hours": 30.0, # daily facts older than this are stale
}

ACCOUNT_USAGE_LAG_NOTE = "Account telemetry can lag up to ~45 min (metering-daily up to 24h)."

# ---------------------------------------------------------------------------
# Role -> navigation profile (page FILTERING only; Snowflake RBAC is the
# actual security boundary under Streamlit-in-Snowflake).
# ---------------------------------------------------------------------------
ROLE_PROFILE_OVERRIDES = {
    # r27 #8: the SNOW_PRI_* viewer-role overrides were traces of roles
    # with no app access (owner 2026-07-13). Only the two real roles map;
    # the profile machinery stays for operator-UI gating.
    "SNOW_ACCOUNTADMINS": "DBA",
    "SNOW_SYSADMINS": "DBA",
}

PAGES_BY_PROFILE = {
    "EXECUTIVE": ("Brief", "Overview", "Cost & Contract", "Alerts"),
    "ANALYST": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Alerts", "Security"),
    "MANAGER": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Alerts", "Security"),
    "DBA": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Alerts", "Security", "Admin"),
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
        value = int(days)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        value = DEFAULT_DAY_WINDOW
    return max(1, min(value, maximum))
