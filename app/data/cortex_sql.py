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


# The Cortex Code scans are window-FLAT: measured 22.1s at 7d, 27.3s at 30d,
# 25.6s at 365d. The cost is secure-view expansion plus the per-call
# SYSTEM$GET_CORTEX_CODE_*_SUBSCRIPTION probe, not rows — so a narrow window
# buys nothing and a days-keyed cache re-pays the whole 22s every time the
# window picker moves. LIVE_DERIVE_DAYS fetches the full retention ONCE.
LIVE_DERIVE_DAYS = 365


def cortex_code_user_daily(company: str = "ALL") -> str:
    """The ONE live Cortex Code scan: 365d at user-day-source grain.

    This is the fallback leg for BOTH ai_chargeback panels (the user rollup
    and the daily-by-source chart). It is deliberately days-INDEPENDENT so
    every window the picker offers shares a single cache entry and a single
    22s payment per TTL; app/logic/cortex.py slices the window and derives
    both aggregates in pandas (cortex_code_user_rollup / cortex_code_daily
    remain the tested contract those derivations reproduce).

    Company scope is applied POST-aggregation over the ~50 distinct grouped
    users. cortex_code_daily's live form called COMPANY_FOR_USER on every
    RAW usage row (one UDF invocation per Cortex request) — that is pure
    waste when the answer only varies per user.

    Honors the long window (v4.54): the owner-named live exception to the
    90d ACCOUNT_USAGE cap. Per-user token telemetry is low-volume, unlike a
    QUERY_HISTORY-scale read (still capped at 90).
    """
    outer_scope = companies.user_clause(company, "USER_NAME")
    return f"""
WITH combined AS ({_COMBINED_CODE_USAGE.format(days=LIVE_DERIVE_DAYS)}),
user_daily AS (
    SELECT
        COALESCE(U.NAME, 'UNKNOWN (' || C.USER_ID || ')') AS USER_NAME,
        U.EMAIL,
        U.FIRST_NAME,
        U.LAST_NAME,
        C.SOURCE,
        C.USAGE_TIME::DATE AS USAGE_DATE,
        COUNT(*) AS REQUESTS,
        SUM(COALESCE(C.TOKEN_CREDITS, 0)) AS CREDITS,
        SUM(COALESCE(C.TOKENS, 0)) AS TOKENS,
        MIN(C.USAGE_TIME) AS FIRST_TS,
        MAX(C.USAGE_TIME) AS LAST_TS
    FROM combined C
    LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U ON C.USER_ID = U.USER_ID
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT * FROM user_daily
WHERE {outer_scope if outer_scope else '1 = 1'}
ORDER BY USAGE_DATE, USER_NAME
LIMIT 200000
"""


def cortex_code_user_rollup(days: int, company: str = "ALL") -> str:
    """Per-user Cortex Code rollup: requests, token credits, usage intensity.

    Credits are exact (token metering). Projection to 30 days and dollar
    classification happen in app/logic/cortex.py, not in SQL.

    Honors the long window (v4.54): the owner-named live exception. Cortex Code
    usage views are per-user token telemetry — low-volume, so 180/365 is cheap
    to scan, unlike a QUERY_HISTORY-scale live read (still capped at 90).
    """
    days = bounded_days(days, 365)
    # Company scope is applied ONCE per grouped user in the outer WHERE (a
    # ~50-row set), not per raw usage row — COMPANY_FOR_USER stays cheap.
    outer_scope = companies.user_clause(company, "USER_NAME")
    return f"""
WITH combined AS ({_COMBINED_CODE_USAGE.format(days=days)}),
user_daily AS (
    SELECT
        COALESCE(U.NAME, 'UNKNOWN (' || C.USER_ID || ')') AS USER_NAME,
        U.EMAIL,
        U.FIRST_NAME,
        U.LAST_NAME,
        C.SOURCE,
        C.USAGE_TIME::DATE AS USAGE_DATE,
        COUNT(*) AS REQUESTS,
        SUM(COALESCE(C.TOKEN_CREDITS, 0)) AS CREDITS,
        SUM(COALESCE(C.TOKENS, 0)) AS TOKENS,
        MIN(C.USAGE_TIME) AS FIRST_TS,
        MAX(C.USAGE_TIME) AS LAST_TS
    FROM combined C
    LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U ON C.USER_ID = U.USER_ID
    GROUP BY 1, 2, 3, 4, 5, 6
),
by_user AS (
SELECT
    USER_NAME,
    EMAIL,
    FIRST_NAME,
    LAST_NAME,
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
GROUP BY USER_NAME, EMAIL, FIRST_NAME, LAST_NAME, SOURCE
)
SELECT * FROM by_user
WHERE {outer_scope if outer_scope else '1 = 1'}
ORDER BY TOTAL_CREDITS DESC
LIMIT 500
"""


def cortex_code_daily(days: int, company: str = "ALL") -> str:
    """Daily Cortex Code usage by source (requests, credits, active users).

    Honors the long window (v4.54) with cortex_code_user_rollup — same
    low-volume telemetry, the owner-named live exception to the 90d cap."""
    days = bounded_days(days, 365)
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


def guardrails_daily(days: int = 30) -> str:
    """Cortex Guardrails flag telemetry by day (repo review 2026-08-17).

    OPTIONAL view — CORTEX_AI_GUARDRAILS_USAGE_HISTORY exists only on accounts
    with Cortex Guardrails enabled; callers MUST pass probe=True and render an
    honest "not enabled" state on the error path (the CORTEX_AI_FUNCTIONS
    pattern above). Column set kept minimal so schema drift lands in the same
    honest-degrade path, never in wrong data."""
    days = bounded_days(days)
    return f"""
SELECT
    START_TIME::DATE AS DAY,
    COUNT(*) AS REQUESTS,
    COUNT_IF(COALESCE(GUARDRAILS_RESPONSE, '') <> '') AS FLAGGED
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_GUARDRAILS_USAGE_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY DAY
"""


def cortex_code_token_types(days: int = 30) -> str:
    """Per-user token-TYPE decomposition (repo review wave 2: TOKENS_GRANULAR)
    — input / output / cache_read / cache_write — the prompt-cache-efficiency
    lens raw token totals can't show.

    OPTIONAL column (newer view versions; VARIANT shape may drift) — callers
    MUST pass probe=True and degrade honestly to the token-total view."""
    days = bounded_days(days)
    return f"""
WITH combined AS (
    SELECT USER_ID, USAGE_TIME, TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT USER_ID, USAGE_TIME, TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
)
SELECT
    COALESCE(U.NAME, 'UNKNOWN (' || C.USER_ID || ')') AS USER_NAME,
    LOWER(F.VALUE:token_type::VARCHAR) AS TOKEN_TYPE,
    SUM(COALESCE(TRY_TO_NUMBER(TO_VARCHAR(F.VALUE:tokens)), 0)) AS TOKENS
FROM combined C,
     LATERAL FLATTEN(INPUT => C.TOKENS_GRANULAR) F
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U ON C.USER_ID = U.USER_ID
GROUP BY 1, 2
ORDER BY USER_NAME, TOKEN_TYPE
LIMIT 20000
"""
