"""Cortex / AI usage SQL builders (user attribution).

Ported from the original OVERWATCH "AI & Cortex Monitor > User Attribution"
section, with the new app's contracts applied: no dollar rates baked into
SQL (dollarization lives in app/logic), every scan bounded, company scoping
via the shared clause builders (KEBARR1 override included).

Sources:
- CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY / CORTEX_CODE_CLI_USAGE_HISTORY:
  per-user, per-request TOKEN_CREDITS and TOKENS (exact attribution).
- SNOWFLAKE_COCO_USAGE_HISTORY (v4.612.0): the unified Cortex Code view
  (Snowsight, CLI and Desktop, with INTERFACE and METADATA); read ONLY by
  coco_model_usage_daily, the per-user model split (CREDITS_GRANULAR /
  TOKENS_GRANULAR by model). The per-user readers above stay on the two
  per-interface views until Phase 2.
- CORTEX_AI_FUNCTIONS_USAGE_HISTORY: optional; not all accounts expose it —
  callers rely on the QueryResult error path when it is absent.
"""

from __future__ import annotations

from app import companies
from app.data.common import and_where, bounded_days, resolve_effective_window, scope_window_where

# R2-052 (v4.609): USAGE_TIME is TIMESTAMP_TZ, so a bare USAGE_TIME::DATE took the date in the value's OWN
# stored offset, not the account (Central) day. Every day key below converts to Central first
# (CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE) -- correct for any stored offset, a no-op when the
# views already stamp Central -- matching V167's SP_LOAD_MARTS_V27 arm [9] fact keys. Ungated: these are the
# live legs. FIRST_TS / LAST_TS stay tz-aware (cortex._account_day / quotas._account_ts convert them).
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
        CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE AS USAGE_DATE,
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
    CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE AS DAY,
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

    OPTIONAL view — CORTEX_AI_GUARDRAILS_USAGE_HISTORY can be absent on some
    accounts/regions, but it EXISTS on this account whether or not Guardrails is
    enabled (owner probe 2026-09-29: created 2026-05-26). Callers MUST pass
    probe=True and branch on error_kind (v4.603): an absent object is a setup
    state ("not readable by this app", never "Guardrails is not enabled"), and
    any other failure — a missing column included, which a probe read does not
    log — renders 'unavailable' with the error. Column set kept minimal so schema
    drift fails loudly, never as wrong data.

    UNVERIFIED columns: START_TIME and GUARDRAILS_RESPONSE were never checked
    against the view. The owner's column list (S0b, 14 columns, cut off) starts
    USER_ID, USER_NAME, USER_TAGS, REQUEST_ID, PARENT_REQUEST_ID — the
    CORTEX_CODE_* shape, whose time column is USAGE_TIME. The canary
    ``cortex.guardrails_daily`` FAILs if either column is missing; rename them
    only from the owner's full column list, never by guessing."""
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
    probe=True and degrade honestly to the token-total view (only for the expected-absence
    kinds; a timeout is a failed read, v4.603). The canary ``cortex.code_token_types`` runs
    this exact text: a missing TOKENS_GRANULAR FAILs it, a missing subscription is a GAP.

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
           CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE AS USAGE_DATE,
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


def coco_model_usage_daily(company: str = "ALL") -> str:
    """Cortex Code usage by Central day x user x interface x role x MODEL, all three interfaces (v4.612.0, owner ask
    2026-10-08: "track and drill down by user which models they select when using coco"). The ONE live read behind
    Cost Intelligence > Chargeback & AI > Cortex Code models.

    Source: SNOWFLAKE_COCO_USAGE_HISTORY (GA 2026-08-17), the Snowsight, CLI AND Desktop views in one, with INTERFACE
    and METADATA; 365-day retention, latency up to 1 h. It probes the Cortex Code subscriptions like the per-interface
    views (002139 when absent), so callers pass probe=True and branch on error_kind (v4.603). Days-independent like
    cortex_code_user_daily: the full LIVE_DERIVE_DAYS once, one cache entry per company; app.logic.cortex slices the
    window (Last-month bounds included) and folds every drill, so a click costs no query.

    Columns (the contract app.logic.cortex.coco_* folds read):
    * MAIN_REQUESTS / REQUEST_TOKEN_CREDITS / REQUEST_TOKENS: each request ONCE, on its main model (the model with the
      most credits in the request; ties by name). Additive over any slice; REQUEST_TOKEN_CREDITS is the spend basis
      (= TOKEN_CREDITS, the number the AI users tab sums for Snowsight + CLI).
    * COCO_CREDITS(_INPUT/_CACHE_READ/_CACHE_WRITE/_OUTPUT/_OTHER): this model's CREDITS_GRANULAR leaves, every leaf
      COALESCEd and read with TRY_TO_DOUBLE (TRY_TO_NUMBER is scale 0: 0.0123 -> 0); _OTHER = an entry whose value is
      a bare number. The four named leaves are read by name, so a pre-computed 'total' leaf can never double count;
      any gap to TOKEN_CREDITS becomes a visible '(not attributed to a model)' row in coco_model_mix, never a drop.
    * REQUESTS_USING: requests that billed this model. NOT additive across models (one request can bill several).
    * TOKENS_*: TOKENS_GRANULAR flattened on its own, aggregated to the same grain, then FULL OUTER JOINed (two
      FLATTENs in one FROM multiply models x models; an inner join drops a model present on one side only).
    * MODEL_NAME '(no model breakdown)' = a request with a NULL / empty CREDITS_GRANULAR (OUTER => TRUE keeps it);
      app.logic.cortex.COCO_NO_BREAKDOWN must equal this literal.
    * ROLE_NAME = METADATA:role_name, '(not recorded)' before the field existed.
    * USER_NAME = USERS.NAME via USER_ID (USERS deduplicated per USER_ID), 'UNKNOWN (<id>)' otherwise: the key
      cortex_code_user_daily and COMPANY_FOR_USER use; the view's own USER_NAME (a login name) is not read.
    Each FLATTEN sits in its own CTE (a LATERAL cannot be on the left of a LEFT JOIN: 001072). Day keys convert to
    Central first (R2-052). Company scope runs COMPANY_FOR_USER once per DISTINCT grouped user, on a plain column
    (V030). No division, no correlated subquery; LIMIT 200000 (run(max_rows=200_000) discloses truncation)."""
    scope = companies.user_clause(company, "s.USER_NAME")
    in_scope = (f"n.USER_NAME IN (SELECT s.USER_NAME FROM (SELECT DISTINCT d.USER_NAME FROM named d) s "
                f"WHERE {scope})" if scope else "1 = 1")
    return f"""
WITH base AS (
    SELECT
        COALESCE(C.USER_ID, -1) AS USER_KEY,
        C.USAGE_TIME,
        CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE AS USAGE_DATE,
        CASE LOWER(C.INTERFACE)
            WHEN 'snowsight' THEN 'Snowsight'
            WHEN 'cli' THEN 'CLI'
            WHEN 'desktop' THEN 'Desktop'
            ELSE COALESCE(NULLIF(TRIM(C.INTERFACE), ''), '(unknown)')
        END AS SOURCE,
        COALESCE(NULLIF(TRIM(C.METADATA:role_name::STRING), ''), '(not recorded)') AS ROLE_NAME,
        COALESCE(C.TOKEN_CREDITS, 0) AS TOKEN_CREDITS,
        COALESCE(C.TOKENS, 0) AS TOKENS,
        C.CREDITS_GRANULAR,
        C.TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY C
    WHERE C.USAGE_TIME >= DATEADD('day', -{LIVE_DERIVE_DAYS}, CURRENT_TIMESTAMP())
),
cr AS (
    SELECT
        F.SEQ AS ROW_SEQ,
        B.USER_KEY, B.USAGE_TIME, B.USAGE_DATE, B.SOURCE, B.ROLE_NAME, B.TOKEN_CREDITS, B.TOKENS,
        COALESCE(NULLIF(TRIM(F.KEY::STRING), ''), '(no model breakdown)') AS MODEL_NAME,
        COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(F.VALUE:input)), 0) AS CR_INPUT,
        COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(F.VALUE:cache_read_input)), 0) AS CR_CACHE_READ,
        COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(F.VALUE:cache_write_input)), 0) AS CR_CACHE_WRITE,
        COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(F.VALUE:output)), 0) AS CR_OUTPUT,
        IFF(IS_OBJECT(F.VALUE), 0, COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(F.VALUE)), 0)) AS CR_OTHER
    FROM base B,
         LATERAL FLATTEN(INPUT => B.CREDITS_GRANULAR, OUTER => TRUE) F
),
ranked AS (
    SELECT
        cr.ROW_SEQ, cr.USER_KEY, cr.USAGE_TIME, cr.USAGE_DATE, cr.SOURCE, cr.ROLE_NAME,
        cr.TOKEN_CREDITS, cr.TOKENS, cr.MODEL_NAME,
        cr.CR_INPUT, cr.CR_CACHE_READ, cr.CR_CACHE_WRITE, cr.CR_OUTPUT, cr.CR_OTHER,
        ROW_NUMBER() OVER (
            PARTITION BY cr.ROW_SEQ
            ORDER BY cr.CR_INPUT + cr.CR_CACHE_READ + cr.CR_CACHE_WRITE + cr.CR_OUTPUT + cr.CR_OTHER DESC,
                     cr.MODEL_NAME
        ) AS MODEL_RANK
    FROM cr
),
cr_agg AS (
    SELECT
        R.USAGE_DATE, R.USER_KEY, R.SOURCE, R.ROLE_NAME, R.MODEL_NAME,
        COUNT(*) AS REQUESTS_USING,
        COUNT_IF(R.MODEL_RANK = 1) AS MAIN_REQUESTS,
        SUM(IFF(R.MODEL_RANK = 1, R.TOKEN_CREDITS, 0)) AS REQUEST_TOKEN_CREDITS,
        SUM(IFF(R.MODEL_RANK = 1, R.TOKENS, 0)) AS REQUEST_TOKENS,
        SUM(R.CR_INPUT + R.CR_CACHE_READ + R.CR_CACHE_WRITE + R.CR_OUTPUT + R.CR_OTHER) AS COCO_CREDITS,
        SUM(R.CR_INPUT) AS COCO_CREDITS_INPUT,
        SUM(R.CR_CACHE_READ) AS COCO_CREDITS_CACHE_READ,
        SUM(R.CR_CACHE_WRITE) AS COCO_CREDITS_CACHE_WRITE,
        SUM(R.CR_OUTPUT) AS COCO_CREDITS_OUTPUT,
        SUM(R.CR_OTHER) AS COCO_CREDITS_OTHER,
        MIN(R.USAGE_TIME) AS FIRST_TS,
        MAX(R.USAGE_TIME) AS LAST_TS
    FROM ranked R
    GROUP BY R.USAGE_DATE, R.USER_KEY, R.SOURCE, R.ROLE_NAME, R.MODEL_NAME
),
tk_agg AS (
    SELECT
        B.USAGE_DATE, B.USER_KEY, B.SOURCE, B.ROLE_NAME,
        COALESCE(NULLIF(TRIM(T.KEY::STRING), ''), '(no model breakdown)') AS MODEL_NAME,
        SUM(COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(T.VALUE:input)), 0)) AS TOKENS_INPUT,
        SUM(COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(T.VALUE:cache_read_input)), 0)) AS TOKENS_CACHE_READ,
        SUM(COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(T.VALUE:cache_write_input)), 0)) AS TOKENS_CACHE_WRITE,
        SUM(COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(T.VALUE:output)), 0)) AS TOKENS_OUTPUT,
        SUM(IFF(IS_OBJECT(T.VALUE), 0, COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(T.VALUE)), 0))) AS TOKENS_OTHER
    FROM base B,
         LATERAL FLATTEN(INPUT => B.TOKENS_GRANULAR) T
    GROUP BY 1, 2, 3, 4, 5
),
merged AS (
    SELECT
        COALESCE(c.USAGE_DATE, t.USAGE_DATE) AS USAGE_DATE,
        COALESCE(c.USER_KEY, t.USER_KEY) AS USER_KEY,
        COALESCE(c.SOURCE, t.SOURCE) AS SOURCE,
        COALESCE(c.ROLE_NAME, t.ROLE_NAME) AS ROLE_NAME,
        COALESCE(c.MODEL_NAME, t.MODEL_NAME) AS MODEL_NAME,
        COALESCE(c.REQUESTS_USING, 0) AS REQUESTS_USING,
        COALESCE(c.MAIN_REQUESTS, 0) AS MAIN_REQUESTS,
        COALESCE(c.REQUEST_TOKEN_CREDITS, 0) AS REQUEST_TOKEN_CREDITS,
        COALESCE(c.REQUEST_TOKENS, 0) AS REQUEST_TOKENS,
        COALESCE(c.COCO_CREDITS, 0) AS COCO_CREDITS,
        COALESCE(c.COCO_CREDITS_INPUT, 0) AS COCO_CREDITS_INPUT,
        COALESCE(c.COCO_CREDITS_CACHE_READ, 0) AS COCO_CREDITS_CACHE_READ,
        COALESCE(c.COCO_CREDITS_CACHE_WRITE, 0) AS COCO_CREDITS_CACHE_WRITE,
        COALESCE(c.COCO_CREDITS_OUTPUT, 0) AS COCO_CREDITS_OUTPUT,
        COALESCE(c.COCO_CREDITS_OTHER, 0) AS COCO_CREDITS_OTHER,
        COALESCE(t.TOKENS_INPUT, 0) AS TOKENS_INPUT,
        COALESCE(t.TOKENS_CACHE_READ, 0) AS TOKENS_CACHE_READ,
        COALESCE(t.TOKENS_CACHE_WRITE, 0) AS TOKENS_CACHE_WRITE,
        COALESCE(t.TOKENS_OUTPUT, 0) AS TOKENS_OUTPUT,
        COALESCE(t.TOKENS_OTHER, 0) AS TOKENS_OTHER,
        c.FIRST_TS,
        c.LAST_TS
    FROM cr_agg c
    FULL OUTER JOIN tk_agg t
      ON t.USAGE_DATE = c.USAGE_DATE AND t.USER_KEY = c.USER_KEY AND t.SOURCE = c.SOURCE
     AND t.ROLE_NAME = c.ROLE_NAME AND t.MODEL_NAME = c.MODEL_NAME
),
users1 AS (
    SELECT U.USER_ID, U.NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
    QUALIFY ROW_NUMBER() OVER (PARTITION BY U.USER_ID ORDER BY U.CREATED_ON DESC NULLS LAST) = 1
),
named AS (
    SELECT
        m.USAGE_DATE,
        COALESCE(u.NAME, IFF(m.USER_KEY = -1, 'UNKNOWN (no user id)',
                             'UNKNOWN (' || m.USER_KEY || ')')) AS USER_NAME,
        m.SOURCE,
        m.ROLE_NAME,
        m.MODEL_NAME,
        SUM(m.REQUESTS_USING) AS REQUESTS_USING,
        SUM(m.MAIN_REQUESTS) AS MAIN_REQUESTS,
        SUM(m.REQUEST_TOKEN_CREDITS) AS REQUEST_TOKEN_CREDITS,
        SUM(m.REQUEST_TOKENS) AS REQUEST_TOKENS,
        SUM(m.COCO_CREDITS) AS COCO_CREDITS,
        SUM(m.COCO_CREDITS_INPUT) AS COCO_CREDITS_INPUT,
        SUM(m.COCO_CREDITS_CACHE_READ) AS COCO_CREDITS_CACHE_READ,
        SUM(m.COCO_CREDITS_CACHE_WRITE) AS COCO_CREDITS_CACHE_WRITE,
        SUM(m.COCO_CREDITS_OUTPUT) AS COCO_CREDITS_OUTPUT,
        SUM(m.COCO_CREDITS_OTHER) AS COCO_CREDITS_OTHER,
        SUM(m.TOKENS_INPUT) AS TOKENS_INPUT,
        SUM(m.TOKENS_CACHE_READ) AS TOKENS_CACHE_READ,
        SUM(m.TOKENS_CACHE_WRITE) AS TOKENS_CACHE_WRITE,
        SUM(m.TOKENS_OUTPUT) AS TOKENS_OUTPUT,
        SUM(m.TOKENS_OTHER) AS TOKENS_OTHER,
        MIN(m.FIRST_TS) AS FIRST_TS,
        MAX(m.LAST_TS) AS LAST_TS
    FROM merged m
    LEFT JOIN users1 u ON u.USER_ID = m.USER_KEY
    GROUP BY 1, 2, 3, 4, 5
)
SELECT
    n.USAGE_DATE, n.USER_NAME, n.SOURCE, n.ROLE_NAME, n.MODEL_NAME,
    n.REQUESTS_USING, n.MAIN_REQUESTS, n.REQUEST_TOKEN_CREDITS, n.REQUEST_TOKENS,
    n.COCO_CREDITS, n.COCO_CREDITS_INPUT, n.COCO_CREDITS_CACHE_READ, n.COCO_CREDITS_CACHE_WRITE,
    n.COCO_CREDITS_OUTPUT, n.COCO_CREDITS_OTHER,
    n.TOKENS_INPUT, n.TOKENS_CACHE_READ, n.TOKENS_CACHE_WRITE, n.TOKENS_OUTPUT, n.TOKENS_OTHER,
    n.FIRST_TS, n.LAST_TS
FROM named n
WHERE {in_scope}
ORDER BY n.USAGE_DATE, n.USER_NAME, n.SOURCE, n.ROLE_NAME, n.MODEL_NAME
LIMIT 200000
"""


def quota_access_block_history(days: int, *, bounds: tuple | None = None) -> str:
    """Account-wide per-user AI-quota block history — who Snowflake blocked for
    hitting a per-user AI credit quota (SNOWFLAKE.CORE.QUOTA), and when.

    This is the ONE account-level, plain-SELECT read Snowflake exposes for native
    quotas; the per-quota config / limits / consumption are admin-scoped CALL methods
    on each quota object (no SQL enumeration, no read-only viewer path), so they are
    deliberately out of scope for a read-only console. The view's SQL-reference page
    404s; its columns, read from the owner's Snowsight preview (2026-09-29, v4.601.1),
    are ACTION_AT, QUOTA_ID, QUOTA_NAME, USER_ID, USER_NAME, CYCLE, ACTION,
    PER_USER_LIMIT, CREDITS and BLOCKED_UNTIL. ACTION_AT (when the block was recorded)
    windows and orders the read; there is NO CREATED_ON (the v4.543 reader windowed on
    it, so every read failed with an invalid identifier). SELECT * and bind client-side
    (logic/quotas.block_history) so a new column never breaks the read. The reader passes
    probe=True: the view is absent on accounts without the feature, an EXPECTED absence,
    not an error to log -- the panel still shows a failed read as unavailable, never as
    "no blocks". A probe read does not log a missing column either, so the canary
    ``cortex.quota_access_block_history`` (v4.603) is what catches the CREATED_ON class: it
    FAILs on an invalid identifier and is a GAP only when the view is absent. Honors the
    scope-bar 'Last month' bounds so the block window matches the tab's spend window.

    Review (v4.601.1): the read ALSO takes the last BLOCK_STATE_DAYS (32) days, whatever the
    window, and marks each row IN_WINDOW. The window's rows drive the event counts and the table;
    "currently blocked" is judged over every row, so a user blocked today still shows under 'Last
    month', and a MONTHLY block recorded before a short trailing window is still seen (a cycle is at
    most 31 days, so any later unblock of it is inside the read too)."""
    d = max(1, int(days))
    scope = (resolve_effective_window(d, "ACTION_AT", bounds=bounds)[1]
             if bounds is not None
             else f"ACTION_AT >= DATEADD('day', -{d}, CURRENT_TIMESTAMP())")
    return (
        f"SELECT *, ({scope}) AS IN_WINDOW\n"
        "FROM SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY\n"
        f"WHERE ({scope}) OR ACTION_AT >= DATEADD('day', -{BLOCK_STATE_DAYS}, CURRENT_TIMESTAMP())\n"
        "ORDER BY ACTION_AT DESC"
    )


# Days of block history read for the "currently blocked" state, whatever the page window (a quota cycle is at most
# a month, so a block still in force and any later unblock of it fall inside this span).
BLOCK_STATE_DAYS = 32
