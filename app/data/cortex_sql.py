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
from app.data.common import and_where, bounded_days, resolve_effective_window, scope_window_where

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


def _user_daily_cte(days: int) -> str:
    """Shared ``combined`` + ``user_daily`` CTE for the Cortex Code user-grain
    builders (cortex_code_user_daily, cortex_code_user_rollup). Returns the
    fragment with NO leading/trailing newline so callers interpolate it as
    ``{_user_daily_cte(...)}`` on its own line; byte-identical to the inline
    text it replaces. cortex_code_daily uses a DIFFERENT (raw combined) grain
    and is intentionally not a caller.
    """
    return f"""WITH combined AS ({_COMBINED_CODE_USAGE.format(days=days)}),
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
)"""


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
{_user_daily_cte(LIVE_DERIVE_DAYS)}
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
{_user_daily_cte(days)},
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


def cortex_ai_functions_daily(days: int, *, bounds: tuple | None = None) -> str:
    """Optional AI Functions daily credits (view absent in some accounts;
    the runtime error path is the compatibility guard)."""
    days = bounded_days(days)
    scope = (resolve_effective_window(days, "F.START_TIME", bounds=bounds)[1]
             if bounds is not None
             else f"F.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    return f"""
SELECT
    F.START_TIME::DATE AS DAY,
    'AI Functions' AS SOURCE,
    COUNT(DISTINCT F.QUERY_ID) AS TOTAL_REQUESTS,
    SUM(COALESCE(F.CREDITS, 0)) AS TOTAL_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY F
WHERE {scope}
GROUP BY 1
ORDER BY DAY
"""


def cortex_model_costs(days: int, *, bounds: tuple | None = None) -> str:
    """AI credits by function and model, with a credits/1M-token unit rate.

    Reads CORTEX_AI_FUNCTIONS_USAGE_HISTORY — the canonical Cortex AI-functions usage
    view (GA; data from 2026-01-05). It is NOT a drop-in for the older
    CORTEX_FUNCTIONS_USAGE_HISTORY / CORTEX_AISQL_USAGE_HISTORY reads:
      * the credits column is CREDITS, not TOKEN_CREDITS;
      * the time column is START_TIME (TIMESTAMP_LTZ), windowed on the raw value;
      * there is NO scalar TOKENS column — token counts live inside the METRICS ARRAY,
        one element per metric as
        {"key":{"metric":"input"|"output","unit":"tokens"},"value":N}. LATERAL FLATTEN
        sums the value where unit='tokens' (so non-token metrics, e.g. a 'pages' unit
        from document parsing, are correctly excluded), and CREDITS is deduped to once
        per source row via COALESCE(M.INDEX,0)=0 so the FLATTEN fan-out can't multiply it
        (OUTER=>TRUE emits a NULL-index row for empty METRICS, which is still counted once).

    No database dimension — account-wide by definition; per-user attribution stays in the
    rollup. View/column availability varies by account: the runtime error path is the
    compatibility guard (same pattern as cortex_ai_functions_daily, which reads this view).
    """
    days = bounded_days(days)
    scope = (resolve_effective_window(days, "F.START_TIME", bounds=bounds)[1]
             if bounds is not None
             else f"F.START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    return f"""
SELECT
    F.FUNCTION_NAME,
    COALESCE(NULLIF(F.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
    SUM(CASE WHEN M.VALUE:key:unit::STRING = 'tokens'
             THEN M.VALUE:value::NUMBER ELSE 0 END) AS TOKENS,
    ROUND(SUM(CASE WHEN COALESCE(M.INDEX, 0) = 0
                   THEN COALESCE(F.CREDITS, 0) ELSE 0 END), 4) AS CREDITS,
    ROUND(SUM(CASE WHEN COALESCE(M.INDEX, 0) = 0 THEN COALESCE(F.CREDITS, 0) ELSE 0 END) * 1000000
          / NULLIF(SUM(CASE WHEN M.VALUE:key:unit::STRING = 'tokens'
                            THEN M.VALUE:value::NUMBER ELSE 0 END), 0), 4) AS CREDITS_PER_1M_TOKENS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY F,
     LATERAL FLATTEN(input => F.METRICS, OUTER => TRUE) M
WHERE {scope}
GROUP BY 1, 2
ORDER BY CREDITS DESC
LIMIT 200
"""


def cortex_source_costs(days: int, *, bounds: tuple | None = None) -> str:
    """AI credits by SOURCE from the Cortex Code usage views — the views
    that actually bill this account (live finding 2026-07-08: the model
    view was empty while Snowsight/CLI code credits carried the AI spend).

    Honors the 'Last month' calendar window (bounds) so this fallback — which is the
    ONLY AI-spend surface on the Unit-costs tab when the model view + mart are empty —
    shares the date range of the scope chip and its neighbors (R2 fix). The no-bounds
    path is byte-identical to before (reuses _COMBINED_CODE_USAGE)."""
    days = bounded_days(days)
    if bounds is not None:
        _win = scope_window_where("USAGE_TIME", days, bounds=bounds)
        combined = f"""
    SELECT USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY WHERE {_win}
    UNION ALL
    SELECT USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI' AS SOURCE
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY WHERE {_win}
"""
    else:
        combined = _COMBINED_CODE_USAGE.format(days=days)
    return f"""
SELECT
    SOURCE AS FUNCTION_NAME,
    'Cortex Code' AS MODEL_NAME,
    COUNT(*) AS REQUESTS,
    SUM(COALESCE(TOKENS, 0)) AS TOKENS,
    ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 4) AS CREDITS,
    ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)) * 1000000
          / NULLIF(SUM(COALESCE(TOKENS, 0)), 0), 4) AS CREDITS_PER_1M_TOKENS
FROM ({combined})
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


def cortex_code_token_types() -> str:
    """Per-user, per-day token-TYPE decomposition (repo review wave 2: TOKENS_GRANULAR)
    — input / output / cache_read / cache_write — the prompt-cache-efficiency lens raw
    token totals can't show.

    OPTIONAL column (newer view versions; VARIANT shape may drift) — callers MUST pass
    probe=True and degrade honestly to the token-total view.

    Days-independent (v4.528): scans the full LIVE_DERIVE_DAYS retention ONCE and keeps a
    USAGE_DATE column, so ONE (sql,scope) cache entry serves EVERY window — the caller
    slices the window in pandas via app.logic.cortex.token_types_window. The old form baked
    the window into the SQL text AND keyed the read per-window, so every window move was a
    fresh cache miss re-paying the whole secure-view UNION + RECURSIVE FLATTEN scan (whose
    dominant cost — secure-view expansion + the subscription probe — is window-FLAT, so a
    narrow window bought nothing while the churn cost a full re-scan each time). Per-user
    token telemetry is low-volume, like the sibling cortex_code_* scans, so the 365d fetch
    is cheap and shares its cache across every window and company (the grain is account-wide)."""
    return f"""
WITH combined AS (
    SELECT USER_ID, USAGE_TIME, TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -{LIVE_DERIVE_DAYS}, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT USER_ID, USAGE_TIME, TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -{LIVE_DERIVE_DAYS}, CURRENT_TIMESTAMP())
),
flat AS (
    -- TOKENS_GRANULAR on this account is nested BY MODEL: each model name maps to an
    -- object of token-type to count, e.g. claude-opus-4-6 -> input 6, output 210,
    -- cache_read_input 277061, cache_write_input 49119. A single FLATTEN yielded
    -- KEY=model, VALUE=the-type-object, so the old F.VALUE:token_type / F.VALUE:tokens
    -- read NULL and every count came out 0 (owner 2026-08-19). RECURSIVE flatten + a
    -- numeric-leaf filter pulls the token-type-to-count leaves regardless of nesting
    -- depth -- resilient to the model-nested shape here OR a flat type-to-count map --
    -- keyed by the leaf token-type name (input / output / cache_read_input /
    -- cache_write_input). FLATTEN stays in its own CTE, LEFT JOIN USERS on it (a LATERAL
    -- cannot sit on the LEFT of a LEFT JOIN -- Snowflake 001072).
    SELECT C.USER_ID,
           C.USAGE_TIME::DATE AS USAGE_DATE,
           LOWER(F.KEY::VARCHAR) AS TOKEN_TYPE,
           TRY_TO_NUMBER(TO_VARCHAR(F.VALUE)) AS TOKENS
    FROM combined C,
         LATERAL FLATTEN(INPUT => C.TOKENS_GRANULAR, RECURSIVE => TRUE) F
    WHERE TRY_TO_NUMBER(TO_VARCHAR(F.VALUE)) IS NOT NULL
)
SELECT
    COALESCE(U.NAME, 'UNKNOWN (' || flat.USER_ID || ')') AS USER_NAME,
    flat.USAGE_DATE,
    flat.TOKEN_TYPE,
    SUM(COALESCE(flat.TOKENS, 0)) AS TOKENS
FROM flat
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U ON flat.USER_ID = U.USER_ID
GROUP BY 1, 2, 3
ORDER BY USER_NAME, USAGE_DATE, TOKEN_TYPE
LIMIT 200000
"""


def quota_access_block_history(days: int, *, bounds: tuple | None = None) -> str:
    """Account-wide per-user AI-quota block history — who Snowflake blocked for
    hitting a per-user AI credit quota (SNOWFLAKE.CORE.QUOTA), and when.

    This is the ONE account-level, plain-SELECT read Snowflake exposes for native
    quotas; the per-quota config / limits / consumption are admin-scoped CALL methods
    on each quota object (no SQL enumeration, no read-only viewer path), so they are
    deliberately out of scope for a read-only console. The view's columns are
    UNDOCUMENTED as of 2026-09 (its SQL-reference page 404s) — SELECT * and bind
    client-side (logic/quotas.block_history); CREATED_ON is the one confirmed column,
    used to window and order. The reader passes probe=True: the view is absent on
    accounts without the feature, an EXPECTED absence, not an error to log. Honors the
    scope-bar 'Last month' bounds so the block window matches the tab's spend window."""
    d = max(1, int(days))
    scope = (resolve_effective_window(d, "CREATED_ON", bounds=bounds)[1]
             if bounds is not None
             else f"CREATED_ON >= DATEADD('day', -{d}, CURRENT_TIMESTAMP())")
    return (
        "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY\n"
        f"WHERE {scope}\n"
        "ORDER BY CREATED_ON DESC"
    )
