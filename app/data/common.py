"""Shared helpers for SQL builders."""

from __future__ import annotations

from app.config import APP_QUERY_TAG_PREFIX, clamp_days


def and_where(*clauses: str) -> str:
    """Join non-empty clauses with AND; always returns a valid predicate."""
    parts = [c.strip() for c in clauses if c and c.strip()]
    return " AND ".join(parts) if parts else "1 = 1"


def bounded_days(days: object, maximum: int = 90) -> int:
    """Every live ACCOUNT_USAGE builder must run through this clamp."""
    return clamp_days(days, maximum)


def resolve_effective_window(days: object, column: str = "DAY",
                             max_days: int | None = None,
                             *, bounds: tuple | None = None) -> tuple[int, str]:
    """rec 4 / added rec #3: the ONE half-open effective window the warehouse dollar
    POOL and every allocation DENOMINATOR must share, so per-entity allocation shares
    reconcile to the dollar pool instead of applying a 365-day share to a 182-day pool
    (or including today's partial on one side and not the other).

    Clamps ``days`` to ``max_days`` (default ``MAX_MART_WINDOW_DAYS // 2`` — the
    vs-prior half-window cap the pool uses so its current+prior pair fits in retention)
    and EXCLUDES today (partial metering). Returns ``(eff_days, where_fragment)`` where
    the fragment bounds ``column`` to the half-open ``[today - eff_days, today)``.

    ``bounds`` (start, end_exclusive dates — from date_windows.window_bounds for the
    'Last month' calendar window) overrides the trailing form with an explicit half-open
    ``[start, end)`` calendar range and returns the span as ``eff_days``. The bounded
    range is naturally today-excluded (it ends at the first of this month), so it keeps
    the same today-excluded, half-open contract the pool and shares rely on."""
    if bounds is not None:
        start, end = bounds
        eff = (end - start).days
        frag = f"{column} >= '{start.isoformat()}' AND {column} < '{end.isoformat()}'"
        return eff, frag
    from app.config import MAX_MART_WINDOW_DAYS
    cap = (MAX_MART_WINDOW_DAYS // 2) if max_days is None else max_days
    eff = bounded_days(days, cap)
    frag = (f"{column} >= DATEADD('day', -{eff}, CURRENT_DATE()) "
            f"AND {column} < CURRENT_DATE()")
    return eff, frag


def scope_window_where(column: str, days: int, *, bounds: tuple | None = None,
                       exclude_today: bool = False) -> str:
    """The scope-filter date predicate for a day-grain ``column`` — trailing OR bounded.

    Default (bounds=None) reproduces the app's trailing convention EXACTLY:
    ``{column} >= DATEADD('day', -{days}, CURRENT_DATE())`` (plus ``AND {column} <
    CURRENT_DATE()`` when ``exclude_today``). Given ``bounds`` (the 'Last month' calendar
    range, start inclusive / end exclusive) it emits ``{column} >= 'start' AND {column} <
    'end'`` — a bounded previous-calendar-month instead of a today-anchored window. This
    is the one edit almost every scope-driven cost builder needs to honor Last month; the
    trailing branch stays byte-identical so no existing window changes."""
    if bounds is not None:
        return resolve_effective_window(days, column, bounds=bounds)[1]
    frag = f"{column} >= DATEADD('day', -{days}, CURRENT_DATE())"
    return f"{frag} AND {column} < CURRENT_DATE()" if exclude_today else frag


def account_month_start_sql() -> str:
    """SQL DATE for the first of THIS month in the ACCOUNT timezone.

    ``CURRENT_DATE()`` resolves in the Snowflake session timezone, but the app's
    canonical "today" is ``logic.formulas.account_today()`` (America/Chicago).
    Near a month boundary the two clocks pick different day sets, so a SQL
    ``DATE_TRUNC('month', CURRENT_DATE())`` MTD disagreed with the
    ``account_today()``-anchored MTD on the Overview (rec #2). This anchors the
    SQL month-start to the same account clock so both surfaces select one month.
    """
    from app.logic.formulas import ACCOUNT_TIMEZONE
    return (
        "DATE_TRUNC('month', "
        f"CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', CURRENT_TIMESTAMP())::DATE)"
    )


def account_today_sql() -> str:
    """SQL DATE for 'today' in the ACCOUNT timezone (America/Chicago) — the same clock
    as logic.formulas.account_today(). Session-tz CURRENT_DATE() drifts from it near
    midnight and month/quarter boundaries; use this wherever a SQL date bound must agree
    with the app's Python 'today' (calendar-month/quarter windows especially)."""
    from app.logic.formulas import ACCOUNT_TIMEZONE
    return f"CONVERT_TIMEZONE('{ACCOUNT_TIMEZONE}', CURRENT_TIMESTAMP())::DATE"


def lag_offset_start(days: int, lag_hours: int = 24) -> str:
    """Window start that ends before the ACCOUNT_USAGE completeness horizon.

    Comparing a complete prior window to a still-filling current window is the
    classic latency mistake; offsetting both windows by the lag avoids it.
    """
    return f"DATEADD('day', -{int(days)}, DATEADD('hour', -{int(lag_hours)}, CURRENT_TIMESTAMP()))"


# ---------------------------------------------------------------------------
# Window-anchoring convention (review #9: "N days" must mean one thing):
# - Rolling live scans over event streams anchor CURRENT_TIMESTAMP() when
#   hours matter (queued minutes, spill, logins): exactly N*24h.
# - Day-grain sources (facts keyed by DAY, USAGE_DATE dailies) anchor
#   CURRENT_DATE(): "N days" = N complete days plus today-so-far.
# Every panel keeps ONE anchor family across the queries it displays
# together; new builders follow this table rather than personal taste.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TIMEZONE STANDARD (see the timezone-consistency audit). OVERWATCH runs in the
# Snowflake ACCOUNT default TIMEZONE, which IS 'America/Chicago' (verified
# 2026-09-21: SHOW PARAMETERS ... IN ACCOUNT -> value=America/Chicago, level=
# ACCOUNT). The deployed Streamlit-in-Snowflake app CANNOT ALTER SESSION (an
# owner's-rights no-op), so that account default is the ONLY lever keeping the
# app's clock Central -- every session-tz read (CURRENT_DATE()/CURRENT_TIMESTAMP()/
# a bare ts::DATE, and any raw timestamp rendered to a user) resolves in Central
# ONLY because the account default is Central. Rules for NEW builders:
#   * A displayed timestamp or a day/month BOUNDARY that must be account-correct
#     REGARDLESS of the session zone MUST pin Central explicitly -- use
#     account_today_sql() / account_month_start_sql(), or wrap the column in
#     CONVERT_TIMEZONE('America/Chicago', ts) -- never a bare CURRENT_DATE()/
#     CURRENT_TIMESTAMP()/ts::DATE, which resolve in the session zone.
#   * scope_window_where()/resolve_effective_window() intentionally use session-tz
#     CURRENT_DATE() for rolling trailing windows (the convention above); that is
#     correct while the account is Central and is the accepted pattern.
# tests/test_timezone_standard.py locks ACCOUNT_TIMEZONE and the Central-pinned
# helpers so a future edit can't silently re-anchor the app's clock.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# OVERWATCH SELF-TRAFFIC MARKER (Next-Fifty #7) - ONE definition every self-noise predicate
# and the Admin self-cost split key on. Owner's-rights SiS rejects ALTER SESSION, so
# core.session.apply_query_tag() never tags production app queries; since v4.590.0 the app
# tags its own statements PER STATEMENT instead (QUERY_TAG via Snowpark statement_params -
# core.session.statement_params; the owner probe chose it over an appended SQL comment) and
# these predicates accept EITHER signal, so pre-release history and the comment leg stay
# correct. APP_SQL_MARKER
# deliberately contains 'OVERWATCH_APP' so the legacy '%OVERWATCH_APP%' text filters
# (ops/chatter/workbench builders and the DB-side V147 collector) also exclude
# comment-marked statements with no migration. Locked by tests/test_app_self_marker.py.
# ---------------------------------------------------------------------------
APP_QUERY_TAG_LIKE = f"{APP_QUERY_TAG_PREFIX}%"
APP_SQL_MARKER = "OVERWATCH_APP|"
APP_SQL_MARKER_OPEN = f"/* {APP_SQL_MARKER}"
# the predicate spells the marker as a SPLIT constant ('/* OVERWATCH' || '_APP|'): Snowflake folds it, but
# QUERY_TEXT records it split, so these builders' own statements never match their own predicate.
_MARKER_SQL = "'/* OVERWATCH' || '_APP|'"


def app_self_sql(alias: str = "", *, text: bool = True) -> str:
    """TRUE for OVERWATCH's own statements (tagged OR comment-marked). A NULL tag with no
    marker is NULL -> the IFF else-branch, so untagged rows never read as app traffic."""
    p = f"{alias}." if alias else ""
    tag = f"{p}QUERY_TAG LIKE '{APP_QUERY_TAG_LIKE}'"
    return f"({tag} OR CONTAINS({p}QUERY_TEXT, {_MARKER_SQL}))" if text else f"({tag})"


def not_app_self_sql(alias: str = "", *, text: bool = True) -> str:
    """NULL-safe exclusion of OVERWATCH's own statements. text=False skips the QUERY_TEXT
    read for builders that do not otherwise read it (detect_release_days)."""
    p = f"{alias}." if alias else ""
    tag = f"COALESCE({p}QUERY_TAG, '') NOT LIKE '{APP_QUERY_TAG_LIKE}'"
    if not text:
        return tag
    return f"({tag} AND NOT CONTAINS(COALESCE({p}QUERY_TEXT, ''), {_MARKER_SQL}))"


# ---------------------------------------------------------------------------
# Canonical AI/Cortex service-type predicate (v4.158.0). MUST stay byte-equal
# to app.logic.cost_coverage._is_ai_family — the SQL predicates had diverged and
# dropped SNOWFLAKE_COCO_SNOWSIGHT (Cortex Code / CoWork), so CoCo was excluded
# from the "account-wide" Cortex/AI chart and priced at the compute rate ($3.68)
# instead of the AI rate ($2.20) in every billed AI/OTHER split. One source now;
# every metering builder references these so the two layers cannot re-diverge.
# ---------------------------------------------------------------------------
AI_SERVICE_TOKENS = ("%CORTEX%", "AI%", "%INTELLIGENCE%", "%COCO%", "%COWORK%")


def ai_service_predicate(col: str = "SERVICE_TYPE") -> str:
    """SQL predicate true for Cortex/AI service types (incl. Cortex Code/CoWork).

    Mirrors app.logic.cost_coverage._is_ai_family: CORTEX / AI-prefix /
    INTELLIGENCE / COCO / COWORK. Kept in one place so the SQL and Python
    classifications never drift again.
    """
    return "(" + " OR ".join(f"{col} ILIKE '{t}'" for t in AI_SERVICE_TOKENS) + ")"


def not_ai_service_predicate(col: str = "SERVICE_TYPE") -> str:
    """NULL-safe negation of ai_service_predicate (NULL -> not AI -> compute rate)."""
    safe = f"COALESCE({col}, '')"
    return " AND ".join(f"{safe} NOT ILIKE '{t}'" for t in AI_SERVICE_TOKENS)


def day_literal(day: object) -> str:
    """Validated DATE literal for day-scoped builders (deep-linked replay).

    Accepts a datetime.date or ISO string; raises ValueError on anything
    else — a date picker value should never smuggle SQL.
    """
    from datetime import date as _date

    if isinstance(day, _date):
        return f"'{day.isoformat()}'::DATE"
    parsed = _date.fromisoformat(str(day).strip())
    return f"'{parsed.isoformat()}'::DATE"


def cs_by_query_type_projection(count_expr: str, credits_expr: str,
                                source: str, where: str) -> str:
    """Shared SELECT tail for the cs_by_query_type live/mart twins (dedup C4).

    The two builders emit a byte-identical projection + ORDER/LIMIT tail; they
    differ only in the count expr (``COUNT(*)`` live vs ``SUM(RUNS)`` mart), the
    credits expr (``SUM(CREDITS_USED_CLOUD_SERVICES)`` vs ``SUM(CS_CREDITS)``),
    the FROM object, and the WHERE clause. Keep this string byte-for-byte — it
    is a documented live/mart twin contract (tests/test_perf_group_a.py).
    """
    return f"""
SELECT
    QUERY_TYPE,
    {count_expr} AS QUERIES,
    ROUND({credits_expr}, 4) AS CS_CREDITS,
    ROUND({credits_expr} / NULLIF({count_expr}, 0) * 1000, 4) AS CS_CREDITS_PER_1K
FROM {source}
WHERE {where}
GROUP BY QUERY_TYPE
ORDER BY CS_CREDITS DESC
LIMIT 12
"""
