"""Central constants. Pure module: no Streamlit, no Snowflake imports.

Rates and thresholds here are OFFLINE FALLBACKS ONLY — the live values come
from DBA_MAINT_DB.OVERWATCH.SETTINGS (seeded by V001) and are edited on the Admin
page, not in code.
"""

from __future__ import annotations

APP_NAME = "OVERWATCH"
APP_VERSION = "4.375.0"

# ---------------------------------------------------------------------------
# Snowflake object locations (must match snowflake/migrations/V001__core.sql)
# ---------------------------------------------------------------------------
# Owner decision 2026-07: all OVERWATCH objects live in the existing
# DBA_MAINT_DB.OVERWATCH schema (shared with the previous app's objects).
OVERWATCH_DB = "DBA_MAINT_DB"
CORE_SCHEMA = "OVERWATCH"
MART_SCHEMA = "OVERWATCH"
APP_WAREHOUSE = "WH_ALFA_ADMIN"
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
    "STORAGE_USD_PER_TB_MONTH": 23.00,   # standard table/stage/failsafe; TB = binary TiB (see formulas.py F3 note)
    # Storage tier rates (V046 storage-truth). Estimates from AWS US-East list
    # pricing — EDIT on Admin to match your rate card. Hybrid/archive bill
    # differently from standard storage; these light up the account-tier panel.
    "STORAGE_STAGE_USD_PER_TB_MONTH": 23.00,
    "STORAGE_HYBRID_USD_PER_TB_MONTH": 348.16,   # ~$0.34/GB row store
    "STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH": 4.00,
    "STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH": 1.00,
    # rec#11: fallback egress rate for the data-transfer panel. Cross-region /
    # cross-cloud transfer OUT is billed per TB (same-region is free); the panel
    # PREFERS the org rate-card implied rate (TRANSFER_USD / billable TB) and only
    # falls back to this constant when org billing currency is not visible. AWS
    # cross-region ballpark — EDIT on Admin to match your rate card.
    "DATA_TRANSFER_USD_PER_TB": 20.00,
    "MONTHLY_BUDGET_USD": 0.0,       # 0 = not configured; UI must not invent one
    "AI_MONTHLY_BUDGET_USD": 0.0,    # 0 = not configured; gates Cortex user severities
    "CORTEX_MODEL": "llama3.1-8b",   # model for in-app AI evaluations (Admin-editable)
    # Platform-score weights (per-unit penalties; caps fixed in scoring.py).
    # Uncalibrated starting points - tune against incident history.
    "SCORE_PTS_BUDGET_PER_PCT": "0.5",
    "SCORE_PTS_PER_CRITICAL": "6",
    "SCORE_PTS_PER_HIGH": "2",
    "SCORE_PTS_QUERY_FAIL_PER_PCT": "1.5",
    "SCORE_PTS_TASK_FAIL_PER_PCT": "2",
    "SCORE_PTS_QUEUE_PER_MIN": "0.3",
    "SCORE_PTS_SPILL_PER_GB": "0.5",
    "SCORE_PTS_PER_STALE_SOURCE": "4",
    "SCORE_PTS_PER_OPEN_ACTION": "1.5",
    # Fact retention (SP_PURGE_FACTS, monthly). Floors in the proc: 90/365/30
    # (daily floor raised 180->365 in V054 so long windows always have history).
    "FACT_RETENTION_DAYS_HOURLY": "400",
    "FACT_RETENTION_DAYS_DAILY": "800",
    "ERROR_LOG_RETENTION_DAYS": "180",
    "APP_USAGE_RETENTION_DAYS": "365",
    # Forecast engine: linear | seasonal | ml_forecast (needs the opt-in
    # snowflake/ml_forecast_option.sql; falls back to seasonal when absent).
    "FORECAST_ENGINE": "linear",
    # Known-spike calendar (repo review 2026-08-17): predictable spend spikes the
    # anomaly panels label "expected" instead of flagging. Semicolon rules:
    # MONTH_END:<n> | QUARTER_END:<n> | YYYY-MM-DD..YYYY-MM-DD:<label>.
    # Collapses (z<0) are never suppressed. Empty = no suppression.
    "EXPECTED_SPIKE_CALENDAR": "MONTH_END:1;QUARTER_END:2",
    # Governance-drift weights (per-unit penalties; caps fixed in governance.py).
    "GOV_PTS_MFA_GAP": "5",
    "GOV_PTS_EXPIRED_CRED": "8",
    "GOV_PTS_EXPIRING_CRED": "2",
    "GOV_PTS_BREAKGLASS_GRANT": "6",
    "GOV_PTS_NO_AUTOSUSPEND": "3",
    "CONTRACT_CREDITS": 0.0,         # 0 = not configured
    "CONTRACT_START_DATE": "",
    "CONTRACT_END_DATE": "",
}

# ---------------------------------------------------------------------------
# Windows and guardrails
# ---------------------------------------------------------------------------
DAY_WINDOW_OPTIONS = (7, 14, 30, 60, 90, 180, 365)
DEFAULT_DAY_WINDOW = 7
CURRENT_MONTH_WINDOW = "CURRENT_MONTH"
CURRENT_YEAR_WINDOW = "CURRENT_YEAR"
# The fixed tuple remains the exec-board/retention contract. Calendar presets
# resolve to an account-time day offset at render time and V073 materializes
# those dynamic offsets in MART_EXEC_BOARD.
TRIAGE_WINDOW_OPTIONS = (*DAY_WINDOW_OPTIONS, CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW)
MAX_LIVE_WINDOW_DAYS = 90          # hard clamp for live ACCOUNT_USAGE scans
MAX_MART_WINDOW_DAYS = 365         # mart-backed facts (400-800d retention) honor the long window
# The 90d live cap bounds expensive QUERY_HISTORY-scale scans. The window
# picker offers 180/365 for mart-history (exec board, storage, chargeback)
# and the one low-volume live exception the owner named: Cortex user costs.
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
    "ANALYST": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Decision Studio", "Alerts", "Security"),
    "MANAGER": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Decision Studio", "Alerts", "Security"),
    # "Ask" (grounded Q&A, app/logic/ask + app/ui/pages/ask.py) is DBA-only for now
    # and sits in its own "Ask OVERWATCH" nav group, ordered below Govern (see NAV_GROUPS).
    # Brief is FIRST so the default landing (pages[0], when no saved view / deep link)
    # opens on Brief, not Ask — matching the nav display order (Ask trails last).
    "DBA": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Decision Studio", "Alerts", "Security", "Admin", "Ask"),
    # Read-only tier (owner ask 2026-08-31): the ETL team + any SiS viewer not
    # explicitly mapped. Everything EXCEPT Admin, Alerts, and Ask. Operations is
    # deliberately IN — ETL want its warehouse/task/pipeline health — but every
    # write control there (emergency levers, scans) is is_operator-gated, so a
    # READER sees it fully and can change nothing.
    "READER": ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations", "Decision Studio", "Security"),
}
DEFAULT_PROFILE = "ANALYST"

# rec14: group the sidebar by operator WORKFLOW, not a flat list. Watch = the
# always-on morning surfaces; Analyze = drill/investigate; Govern = security +
# admin. Ordering only — role visibility is still PAGES_BY_PROFILE, and any page a
# profile can see that is not listed here trails under "More" (so a new page is
# never hidden by omission).
NAV_GROUPS = {
    "Watch": ("Brief", "Overview", "Alerts"),
    "Analyze": ("Control Room", "Cost & Contract", "Operations", "Decision Studio"),
    "Govern": ("Security", "Admin"),
    # Ask sits below Govern for now (its own single-item group).
    "Ask OVERWATCH": ("Ask",),
}


def nav_groups_for(pages: tuple[str, ...] | list[str]) -> list[tuple[str, list[str]]]:
    """Partition an ORDERED page list into [(group, [pages])] by NAV_GROUPS,
    intersected with what the profile actually allows. Pages not in any group
    trail under 'More' so nothing is dropped. Empty groups are omitted."""
    allowed = list(pages)
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for group, group_pages in NAV_GROUPS.items():
        members = [p for p in allowed if p in group_pages]
        if members:
            out.append((group, members))
            seen.update(members)
    leftover = [p for p in allowed if p not in seen]
    if leftover:
        out.append(("More", leftover))
    return out


OPERATOR_PROFILES = ("DBA",)  # profiles allowed to execute state-changing SQL in-app

# In-app operator allowlist — the VIEWER usernames (st.user under owner's-rights
# Streamlit-in-Snowflake) permitted to execute state-changing SQL in the app.
# WHY (correctness #3): operator gating used to key off SQL CURRENT_ROLE(), but
# under an owner's-rights SiS app CURRENT_ROLE() is the app OWNER's role for
# EVERY viewer — so it never differentiates people, and an accidental app grant
# would expose DBA actions to any viewer. Entitle by the viewer's identity
# instead (session.is_operator()). Snowflake RBAC stays the REAL boundary (a
# non-privileged role's write still fails server-side); this only decides what
# the app OFFERS. Store bare Snowflake usernames; matching is case-insensitive.
# Empty tuple = no viewer is an in-app operator (secure default); the owner adds
# the specific usernames who may operate. Off-SiS (local dev/tests) there is no
# viewer identity, so session.is_operator() falls back to the role->profile check.
OPERATOR_USERS: tuple[str, ...] = ("H21427", "E22292", "KEBARR1", "CLROY", "N22514")  # the DBA/admin team


def is_operator_user(viewer: str) -> bool:
    """True when a VIEWER username is on the in-app operator allowlist.

    Case-insensitive; a blank viewer is never an operator (the caller falls back
    to role-based gating for that off-SiS case). Pure so it is unit-testable.
    """
    name = str(viewer or "").strip().upper()
    if not name:
        return False
    return name in {str(u).strip().upper() for u in OPERATOR_USERS}


# ---------------------------------------------------------------------------
# Per-viewer navigation profile (page visibility) — the READ analog of
# OPERATOR_USERS. Under owner's-rights SiS, CURRENT_ROLE() is the app OWNER's
# role for EVERY viewer, so resolve_role_profile(current_role()) (the off-SiS
# path) cannot scope who sees which pages. Key page visibility on the VIEWER
# (st.user) instead: the DBA/admin team sees the full DBA surface; the ETL team
# gets the read-only READER profile (no Admin/Alerts/Ask). Bare usernames,
# matched case-insensitively (same grain as OPERATOR_USERS). This decides only
# what the app OFFERS; writes remain independently gated by OPERATOR_USERS.
# ---------------------------------------------------------------------------
VIEWER_PROFILES: dict[str, str] = {
    # DBA/admin team — full surface incl. Admin + Ask (also in OPERATOR_USERS for write)
    "H21427": "DBA", "E22292": "DBA", "KEBARR1": "DBA", "CLROY": "DBA", "N22514": "DBA",
    # ETL team (_DTI_ roles) — read-only, no Admin/Alerts/Ask
    "GRTHOMP1": "READER", "SUDEVAX": "READER", "TV5073": "READER", "VS4229": "READER",
}
# Any identified SiS viewer NOT in VIEWER_PROFILES falls to this least-privilege
# read tier — NEVER the owner's DBA. Owner policy 2026-08-31: an unmapped viewer
# gets the same read-only surface as the ETL team.
VIEWER_UNKNOWN_PROFILE = "READER"


def resolve_viewer_profile(viewer: str) -> str | None:
    """Navigation profile for a VIEWER username, or None when unmapped/blank.

    Pure and case-insensitive (mirrors is_operator_user). The caller owns the
    unmapped policy: on SiS an identified-but-unmapped viewer maps to
    VIEWER_UNKNOWN_PROFILE (read-only); a blank viewer means off-SiS, where the
    caller falls back to the role->profile map instead.
    """
    name = str(viewer or "").strip().upper()
    if not name:
        return None
    return {str(k).strip().upper(): v for k, v in VIEWER_PROFILES.items()}.get(name)


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
    # Calendar MTD/YTD is a day OFFSET: on the first day of a month/year, zero
    # means CURRENT_DATE through CURRENT_DATE and must not widen into yesterday.
    minimum = 0 if getattr(days, "calendar_window", False) else 1
    clamped = max(minimum, min(value, maximum))
    if minimum == 0:
        from app.logic.date_windows import CalendarDayOffset

        return CalendarDayOffset(clamped)  # preserve marker through nested builders
    return clamped
