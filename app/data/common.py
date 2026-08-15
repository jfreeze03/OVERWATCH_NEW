"""Shared helpers for SQL builders."""

from __future__ import annotations

from app.config import clamp_days


def and_where(*clauses: str) -> str:
    """Join non-empty clauses with AND; always returns a valid predicate."""
    parts = [c.strip() for c in clauses if c and c.strip()]
    return " AND ".join(parts) if parts else "1 = 1"


def bounded_days(days: object, maximum: int = 90) -> int:
    """Every live ACCOUNT_USAGE builder must run through this clamp."""
    return clamp_days(days, maximum)


def resolve_effective_window(days: object, column: str = "DAY",
                             max_days: int | None = None) -> tuple[int, str]:
    """rec 4 / added rec #3: the ONE half-open effective window the warehouse dollar
    POOL and every allocation DENOMINATOR must share, so per-entity allocation shares
    reconcile to the dollar pool instead of applying a 365-day share to a 182-day pool
    (or including today's partial on one side and not the other).

    Clamps ``days`` to ``max_days`` (default ``MAX_MART_WINDOW_DAYS // 2`` — the
    vs-prior half-window cap the pool uses so its current+prior pair fits in retention)
    and EXCLUDES today (partial metering). Returns ``(eff_days, where_fragment)`` where
    the fragment bounds ``column`` to the half-open ``[today - eff_days, today)``."""
    from app.config import MAX_MART_WINDOW_DAYS
    cap = (MAX_MART_WINDOW_DAYS // 2) if max_days is None else max_days
    eff = bounded_days(days, cap)
    frag = (f"{column} >= DATEADD('day', -{eff}, CURRENT_DATE()) "
            f"AND {column} < CURRENT_DATE()")
    return eff, frag


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
