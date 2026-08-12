"""Cost by CLIENT APPLICATION x USER (V077).

Measured warehouse-compute credits attributed to the program a query ran under
(SESSIONS.CLIENT_ENVIRONMENT:APPLICATION when the client self-reports it, else
the CLIENT_APPLICATION_ID driver family, else '(unknown)') and to the user.

Mart-first: ``app_cost_mart`` reads FACT_APP_COST_DAILY (fast, once the owner
applies V077 and the daily task loads); ``app_cost_live`` is the SESSIONS x
QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY join that works before the fact exists.
Measured = warehouse compute + query acceleration; excludes idle, serverless and
storage. No dollar rates in SQL — app/logic dollarizes at the compute rate.
GET_PATH (not the ':' path variant) keeps the live builder canary-parse-clean.
"""

from __future__ import annotations

from app import companies
from app.config import core_object
from app.core.sqlsafe import sql_literal
from app.data.common import and_where, bounded_days

# The application identifier, matching V077's SP_LOAD_APP_COST: the self-reported
# program, else the driver family (version stripped), else '(unknown)'.
_APP_EXPR = (
    "COALESCE("
    "NULLIF(GET_PATH(TRY_PARSE_JSON(CLIENT_ENVIRONMENT), 'APPLICATION')::STRING, ''), "
    "NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''), "
    "'(unknown)')"
)


def _company_col_clause(company: str) -> str:
    """Filter the mart's pre-stamped COMPANY column ('' = ALL)."""
    c = str(company or "ALL")
    if c.upper() == "ALL":
        return ""
    return f"UPPER(COMPANY) = {sql_literal(c.upper(), 40)}"


def app_cost_mart(days: int = 30, company: str = "ALL") -> str:
    """Measured cost by application x user from FACT_APP_COST_DAILY (V077)."""
    days = bounded_days(days, 365)
    where = and_where(
        f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        _company_col_clause(company),
    )
    return f"""
SELECT APPLICATION, USER_NAME, COMPANY,
       SUM(QUERIES) AS QUERIES, SUM(CREDITS) AS CREDITS
FROM {core_object('FACT_APP_COST_DAILY')}
WHERE {where}
GROUP BY APPLICATION, USER_NAME, COMPANY
HAVING SUM(CREDITS) > 0
ORDER BY CREDITS DESC
LIMIT 1000
"""


def app_cost_live(days: int = 30, company: str = "ALL") -> str:
    """Live fallback: SESSIONS x QUERY_HISTORY (SESSION_ID) x
    QUERY_ATTRIBUTION_HISTORY (QUERY_ID). Heavier than the mart (a 3-way join over
    ACCOUNT_USAGE, capped to 90d); serves until FACT_APP_COST_DAILY loads."""
    days = bounded_days(days, 90)
    q_where = and_where(
        f"q.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.warehouse_clause(company, "q.WAREHOUSE_NAME"),
    )
    return f"""
WITH cred AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days + 1}, CURRENT_TIMESTAMP())
    GROUP BY QUERY_ID
    HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
),
q AS (
    SELECT q.QUERY_ID, q.SESSION_ID, COALESCE(q.USER_NAME, 'UNKNOWN') AS USER_NAME, q.WAREHOUSE_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {q_where}
),
sess AS (
    SELECT SESSION_ID, {_APP_EXPR} AS APPLICATION
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE CREATED_ON >= DATEADD('day', -{days + 7}, CURRENT_TIMESTAMP())
    QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
)
SELECT COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION,
       q.USER_NAME,
       {companies.company_case_sql('q.WAREHOUSE_NAME')} AS COMPANY,
       COUNT(*) AS QUERIES, SUM(c.CREDITS) AS CREDITS
FROM q
JOIN cred c ON c.QUERY_ID = q.QUERY_ID
LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
GROUP BY 1, 2, 3
HAVING SUM(c.CREDITS) > 0
ORDER BY CREDITS DESC
LIMIT 1000
"""
