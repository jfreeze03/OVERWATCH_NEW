"""Cloud-services metadata-chatter attribution to the client APPLICATION / driver.

Phase 1b of the Cloud Services Driver Intelligence work. Answers "WHICH client
application / driver is generating the metadata / compile chatter on this account" by
joining ACCOUNT_USAGE.QUERY_HISTORY to ACCOUNT_USAGE.SESSIONS on SESSION_ID and grouping
the chatter footprint by the self-reported application (else the driver family) -- the
WHO axis that complements the Cost > Spend compile-heavy family panel (the WHAT axis,
Phase 0).

LIVE, no mart / no migration dependency: the SESSION_ID join is valid at the live
QUERY_HISTORY grain, and IS_CLIENT_GENERATED_STATEMENT is a live QUERY_HISTORY column, so
this works today -- V149's OW_QH_EXTRACT columns only matter for a future MART-backed cut.

ACCOUNT-WIDE by nature: metadata statements carry WAREHOUSE_NAME NULL, so the chatter
cannot be company-scoped by warehouse; a warehouse filter instead narrows to that
warehouse's compile-heavy (warehouse-backed) portion. Every cloud-services credit here is
GROSS USAGE (never billable -- the ~10% rebate is account+day, not decomposable to an app
or query), and the application name is SELF-REPORTED (spoofable; ODBC/legacy tools land in
'(unknown)'). SESSIONS is scanned a few days WIDER than the query window (it lags ~3h) so
an in-window statement never loses its application for lack of a session match.
"""

from __future__ import annotations

from app.core.sqlsafe import sql_literal
from app.data.app_cost_sql import _APP_EXPR  # the canonical application identifier (V077)
from app.data.common import and_where, bounded_days

# chatter = metadata-only (no warehouse) OR compile-dominated (compile >= half the elapsed).
_CHATTER_PREDICATE = (
    "(q.WAREHOUSE_NAME IS NULL "
    "OR (COALESCE(q.TOTAL_ELAPSED_TIME, 0) > 0 "
    "AND q.COMPILATION_TIME >= 0.5 * q.TOTAL_ELAPSED_TIME))"
)
# exclude OVERWATCH's own statements (the shared self-noise set the triage builder uses).
_SELF_NOISE = (
    "COALESCE(q.QUERY_TAG, '') NOT LIKE 'OVERWATCH%' "
    "AND UPPER(COALESCE(q.QUERY_TEXT, '')) NOT LIKE 'EXECUTE STREAMLIT%' "
    "AND UPPER(COALESCE(q.QUERY_TEXT, '')) NOT LIKE '%OVERWATCH_APP%'"
)


def _windows(days: int, bounds: tuple | None) -> tuple[str, str]:
    """(q_scope, sess_scope). SESSIONS is padded -7d wider (it lags ~3h) so an in-window
    query keeps its application; 'Last month' applies the bounded [start, end)."""
    if bounds is not None:
        _si, _ei = f"'{bounds[0].isoformat()}'", f"'{bounds[1].isoformat()}'"
        return (f"q.START_TIME >= {_si} AND q.START_TIME < {_ei}",
                f"CREATED_ON >= DATEADD('day', -7, {_si}) AND CREATED_ON < {_ei}")
    return (f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
            f"CREATED_ON >= DATEADD('day', -{days + 7}, CURRENT_TIMESTAMP())")


def _scope_filters(warehouse_contains: str, user_contains: str) -> list[str]:
    out = []
    if warehouse_contains:
        out.append(f"q.WAREHOUSE_NAME ILIKE {sql_literal('%' + warehouse_contains + '%')}")
    if user_contains:
        out.append(f"q.USER_NAME ILIKE {sql_literal('%' + user_contains + '%')}")
    return out


def chatter_by_application(days: int = 30, warehouse_contains: str = "", user_contains: str = "",
                           min_runs: int = 20, *, bounds: tuple | None = None) -> str:
    """Metadata / compile chatter grouped by the client application / driver.

    One row per application: RUNS, footprint-weighted COMPILE_PCT, total compile seconds,
    gross cloud-services credits, distinct users & sessions, and CLIENT_GEN_PCT (the share
    issued by the driver/UI itself). ``warehouse_contains`` narrows to a warehouse's
    compile-heavy portion (drops the no-warehouse metadata); default is account-wide."""
    days = bounded_days(days, 90)
    q_scope, sess_scope = _windows(days, bounds)
    q_where = and_where(q_scope, _CHATTER_PREDICATE, _SELF_NOISE,
                        *_scope_filters(warehouse_contains, user_contains))
    min_runs = max(1, min(int(min_runs), 1000))
    return f"""
WITH q AS (
    SELECT q.SESSION_ID, COALESCE(q.USER_NAME, 'UNKNOWN') AS USER_NAME,
           q.COMPILATION_TIME, q.TOTAL_ELAPSED_TIME,
           COALESCE(q.CREDITS_USED_CLOUD_SERVICES, 0) AS CS_CREDITS,
           IFF(q.IS_CLIENT_GENERATED_STATEMENT, 1, 0) AS CLIENT_GEN
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {q_where}
),
sess AS (
    SELECT SESSION_ID, {_APP_EXPR} AS APPLICATION
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE {sess_scope}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
)
SELECT COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION,
       COUNT(*) AS RUNS,
       ROUND(SUM(q.COMPILATION_TIME) / NULLIF(SUM(q.TOTAL_ELAPSED_TIME), 0) * 100, 1) AS COMPILE_PCT,
       ROUND(SUM(q.COMPILATION_TIME) / 1000, 1) AS TOTAL_COMPILE_SEC,
       ROUND(SUM(q.CS_CREDITS), 4) AS CS_CREDITS,
       COUNT(DISTINCT q.USER_NAME) AS USERS,
       COUNT(DISTINCT q.SESSION_ID) AS SESSIONS,
       ROUND(SUM(q.CLIENT_GEN) / NULLIF(COUNT(*), 0) * 100, 0) AS CLIENT_GEN_PCT
FROM q
LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
GROUP BY 1
HAVING COUNT(*) >= {min_runs}
ORDER BY RUNS DESC
LIMIT 100
"""


def chatter_families_for_application(application: str = "", days: int = 30,
                                     warehouse_contains: str = "", user_contains: str = "",
                                     min_runs: int = 5, *, bounds: tuple | None = None) -> str:
    """The chatter query FAMILIES a selected application generates -- output shaped for
    app.logic.cs_driver.classify_families (SAMPLE_TEXT / QUERY_TYPE / COMPILE_PCT /
    AVG_TOTAL_S / RUNS) so the panel can label each family (JDBC discovery, INFORMATION_SCHEMA,
    metadata chatter, ...) and say whether a resize could help."""
    days = bounded_days(days, 90)
    q_scope, sess_scope = _windows(days, bounds)
    q_where = and_where(q_scope, _CHATTER_PREDICATE, _SELF_NOISE,
                        "q.QUERY_PARAMETERIZED_HASH IS NOT NULL",
                        *_scope_filters(warehouse_contains, user_contains))
    min_runs = max(1, min(int(min_runs), 1000))
    return f"""
WITH q AS (
    SELECT q.SESSION_ID, q.QUERY_PARAMETERIZED_HASH, LEFT(q.QUERY_TEXT, 90) AS QUERY_TEXT,
           q.QUERY_TYPE, q.COMPILATION_TIME, q.TOTAL_ELAPSED_TIME,
           COALESCE(q.CREDITS_USED_CLOUD_SERVICES, 0) AS CS_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {q_where}
),
sess AS (
    SELECT SESSION_ID, {_APP_EXPR} AS APPLICATION
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE {sess_scope}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
)
SELECT q.QUERY_PARAMETERIZED_HASH,
       ANY_VALUE(q.QUERY_TEXT) AS SAMPLE_TEXT,
       ANY_VALUE(q.QUERY_TYPE) AS QUERY_TYPE,
       COUNT(*) AS RUNS,
       ROUND(AVG(q.COMPILATION_TIME) / 1000, 2) AS AVG_COMPILE_S,
       ROUND(AVG(q.TOTAL_ELAPSED_TIME) / 1000, 2) AS AVG_TOTAL_S,
       ROUND(SUM(q.COMPILATION_TIME) / NULLIF(SUM(q.TOTAL_ELAPSED_TIME), 0) * 100, 1) AS COMPILE_PCT,
       ROUND(SUM(q.CS_CREDITS), 4) AS CS_CREDITS
FROM q
LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
WHERE COALESCE(s.APPLICATION, '(unknown)') = {sql_literal(application)}
GROUP BY 1
HAVING COUNT(*) >= {min_runs}
ORDER BY SUM(q.COMPILATION_TIME) DESC
LIMIT 25
"""
