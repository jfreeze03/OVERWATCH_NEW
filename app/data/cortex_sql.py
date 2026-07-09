"""Cortex / AI usage SQL builders (user attribution).

Ported from the original OVERWATCH "AI & Cortex Monitor > User Attribution"
section, with the new app's contracts applied: no dollar rates baked into
SQL (dollarization lives in app/logic), every scan bounded, company scoping
via the shared clause builders (KEBARR1 override included).

Sources:
- CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY / CORTEX_CODE_CLI_USAGE_HISTORY:
  per-user, per-request TOKEN_CREDITS and TOKENS (exact attribution).
- CORTEX_AI_FUNCTIONS_USAGE_HISTORY: optional; not all accounts expose it —
  callers rely on the QueryResult error path when it is absent.
"""

from __future__ import annotations

from app import companies
from app.data.common import and_where, bounded_days

_COMBINED_CODE_USAGE = """
    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI' AS SOURCE
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
"""


def cortex_code_user_rollup(days: int, company: str = "ALL") -> str:
    """Per-user Cortex Code rollup: requests, token credits, usage intensity.

    Credits are exact (token metering). Projection to 30 days and dollar
    classification happen in app/logic/cortex.py, not in SQL.
    """
    days = bounded_days(days)
    # Company scope is applied ONCE per grouped user in the outer WHERE (a
    # ~50-row set), not per raw usage row — COMPANY_FOR_USER stays cheap.
    outer_scope = companies.user_clause(company, "USER_NAME")
    return f"""
WITH combined AS ({_COMBINED_CODE_USAGE.format(days=days)}),
user_daily AS (
    SELECT
        COALESCE(U.NAME, 'UNKNOWN (' || C.USER_ID || ')') AS USER_NAME,
        U.EMAIL,
        C.SOURCE,
        C.USAGE_TIME::DATE AS USAGE_DATE,
        COUNT(*) AS REQUESTS,
        SUM(COALESCE(C.TOKEN_CREDITS, 0)) AS CREDITS,
        SUM(COALESCE(C.TOKENS, 0)) AS TOKENS,
        MIN(C.USAGE_TIME) AS FIRST_TS,
        MAX(C.USAGE_TIME) AS LAST_TS
    FROM combined C
    LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U ON C.USER_ID = U.USER_ID
    GROUP BY 1, 2, 3, 4
),
by_user AS (
SELECT
    USER_NAME,
    EMAIL,
    SOURCE,
    COUNT(DISTINCT USAGE_DATE) AS ACTIVE_DAYS,
    SUM(REQUESTS) AS TOTAL_REQUESTS,
    SUM(CREDITS) AS TOTAL_CREDITS,
    SUM(TOKENS) AS TOTAL_TOKENS,
    MIN(FIRST_TS) AS FIRST_USAGE,
    MAX(LAST_TS) AS LAST_USAGE,
    SUM(CREDITS) / NULLIF(SUM(REQUESTS), 0) AS CREDITS_PER_REQUEST,
    SUM(CREDITS) / NULLIF(COUNT(DISTINCT USAGE_DATE), 0) AS AVG_DAILY_CREDITS
FROM user_daily
GROUP BY USER_NAME, EMAIL, SOURCE
)
SELECT * FROM by_user
WHERE {outer_scope if outer_scope else '1 = 1'}
ORDER BY TOTAL_CREDITS DESC
LIMIT 500
"""


def cortex_code_daily(days: int, company: str = "ALL") -> str:
    """Daily Cortex Code usage by source (requests, credits, active users)."""
    days = bounded_days(days)
    where = and_where("1 = 1", companies.user_clause(company, "U.NAME"))
    return f"""
WITH combined AS ({_COMBINED_CODE_USAGE.format(days=days)})
SELECT
    C.USAGE_TIME::DATE AS DAY,
    C.SOURCE,
    COUNT(DISTINCT C.USER_ID) AS ACTIVE_USERS,
    COUNT(*) AS TOTAL_REQUESTS,
    SUM(COALESCE(C.TOKEN_CREDITS, 0)) AS TOTAL_CREDITS,
    SUM(COALESCE(C.TOKENS, 0)) AS TOTAL_TOKENS
FROM combined C
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U ON C.USER_ID = U.USER_ID
WHERE {where}
GROUP BY 1, 2
ORDER BY DAY, SOURCE
"""


def cortex_ai_functions_daily(days: int) -> str:
    """Optional AI Functions daily credits (view absent in some accounts;
    the runtime error path is the compatibility guard)."""
    days = bounded_days(days)
    return f"""
SELECT
    F.START_TIME::DATE AS DAY,
    'AI Functions' AS SOURCE,
    COUNT(DISTINCT F.QUERY_ID) AS TOTAL_REQUESTS,
    SUM(COALESCE(F.CREDITS, 0)) AS TOTAL_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY F
WHERE F.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY DAY
"""


def cortex_model_costs(days: int) -> str:
    """AI credits by function and model, with a credits/1M-token unit rate.

    CORTEX_FUNCTIONS_USAGE_HISTORY carries no database dimension — this is
    account-wide by definition; per-user attribution stays in the rollup.
    View/column availability varies by account: the runtime error path is
    the compatibility guard (same pattern as cortex_ai_functions_daily).
    """
    days = bounded_days(days)
    return f"""
SELECT
    FUNCTION_NAME,
    COALESCE(MODEL_NAME, 'n/a') AS MODEL_NAME,
    SUM(COALESCE(TOKENS, 0)) AS TOKENS,
    ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 4) AS CREDITS,
    ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)) * 1000000
          / NULLIF(SUM(COALESCE(TOKENS, 0)), 0), 4) AS CREDITS_PER_1M_TOKENS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1, 2
ORDER BY CREDITS DESC
LIMIT 200
"""


def cortex_source_costs(days: int) -> str:
    """AI credits by SOURCE from the Cortex Code usage views — the views
    that actually bill this account (live finding 2026-07-08: the model
    view was empty while Snowsight/CLI code credits carried the AI spend)."""
    days = bounded_days(days)
    return f"""
SELECT
    SOURCE AS FUNCTION_NAME,
    'Cortex Code' AS MODEL_NAME,
    COUNT(*) AS REQUESTS,
    SUM(COALESCE(TOKENS, 0)) AS TOKENS,
    ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 4) AS CREDITS,
    ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)) * 1000000
          / NULLIF(SUM(COALESCE(TOKENS, 0)), 0), 4) AS CREDITS_PER_1M_TOKENS
FROM ({_COMBINED_CODE_USAGE.format(days=days)})
GROUP BY 1
ORDER BY CREDITS DESC
"""
