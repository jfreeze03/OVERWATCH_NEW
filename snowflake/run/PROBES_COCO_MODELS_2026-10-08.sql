-- =====================================================================================
--  PROBES_COCO_MODELS_2026-10-08.sql  --  CoCo (Cortex Code) usage by USER x MODEL: read-only checks before v4.612.0
--  Runbox file snowflake/run/PROBES_COCO_MODELS_2026-10-08.sql (separate from RUN_NEXT.sql, which holds the V174 + V175 apply).
--  WHY: the 2026-10-08 ask "track and drill down by user which models they select when using coco". OVERWATCH reads
--  only CORTEX_CODE_SNOWSIGHT_ + CORTEX_CODE_CLI_USAGE_HISTORY (cortex_sql.py:25-33, loader arm [9] V167:870-905), so
--  Cortex Code DESKTOP is never attributed to a user, and no reader keeps the MODEL key of CREDITS_GRANULAR.
--  v4.612.0 adds ONE live read of SNOWFLAKE_COCO_USAGE_HISTORY (cortex_sql.coco_model_usage_daily); C18 is its exact
--  text, aggregated, so its runtime, row count and reconciliation are measured before anyone reads the panel.
--  RUN AS SNOW_ACCOUNTADMINS with NO secondary roles (the app runs with its owner role only). The file sets both.
--  READ-ONLY: USE / ALTER SESSION SET TIMEZONE / SELECT only. No DML, no DDL, no CALL, no GRANT:
--  nothing in the account is created, changed or granted.
--  RUN ALL STOPS AT THE FIRST ERROR. An error IS that block's answer: copy its text, then highlight from the NEXT
--  block to the end of the file and run again. Paste back every grid ('no rows' is an answer), long grids as CSV,
--  and note the elapsed time Snowsight shows for C2 and C18.
--  COST: each block pays the Cortex Code secure-view expansion (about 20-25 s, flat in the window length; the
--  concept query's two scans took 47 s on X-Small). About 21 blocks, roughly 10-14 minutes in all.
--  RISKY LAST: C16 / C17 read CORTEX_CODE_DESKTOP_USAGE_HISTORY by name. An error there (002139, not authorized,
--  does not exist) is itself the answer: the per-interface Desktop view is not readable on its own.
--  CLOCK: rolling N days = N x 24 h back from now. LAST MONTH = the last COMPLETE Central calendar month (run in
--  October 2026 = September 2026). Day and month keys are CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE,
--  never a bare USAGE_TIME::DATE: USAGE_TIME is TIMESTAMP_TZ and ::DATE reads its stored offset (R2-052, V167).
--  C14 uses the bare form on purpose, to measure the difference.
--  USD columns = credits x SETTINGS AI_CREDIT_PRICE_USD (the AI rate, contract 2.20), never the 3.68 compute rate.
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;
USE SECONDARY ROLES NONE;
-- Warehouse: this worksheet's own (e.g. WH_ALFA_QUERY). Not WH_ALFA_ADMIN: that is OVERWATCH's task warehouse.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- C0 Session. Expect ROLE_NAME = SNOW_ACCOUNTADMINS, SECONDARY with no roles, NOW_SESSION ending -05:00 (CDT),
--    LAST_MONTH_FROM = 2026-09-01 and LAST_MONTH_TO_EXCL = 2026-10-01 when run in October 2026.
SELECT CURRENT_ROLE() AS ROLE_NAME,
       CURRENT_SECONDARY_ROLES() AS SECONDARY,
       CURRENT_WAREHOUSE() AS WH,
       CURRENT_TIMESTAMP() AS NOW_SESSION,
       DATEADD('month', -1, DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS LAST_MONTH_FROM,
       DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS LAST_MONTH_TO_EXCL;

-- C1 Columns of the unified view next to the three per-interface views, one row per column (the same
--    SNOWFLAKE.INFORMATION_SCHEMA.COLUMNS read as runbox F1, 2026-09-30).
-- DECIDES: UNIFIED_TYPE filled for the 12 documented columns (USER_ID, USER_NAME, USER_TAGS, REQUEST_ID,
--    PARENT_REQUEST_ID, USAGE_TIME, INTERFACE, TOKEN_CREDITS, TOKENS, TOKENS_GRANULAR, CREDITS_GRANULAR, METADATA)
--    = the view is visible to this role and has the documented shape (v4.612 reads USER_ID, USAGE_TIME, INTERFACE,
--    METADATA, TOKEN_CREDITS, TOKENS, CREDITS_GRANULAR, TOKENS_GRANULAR). INTERFACE is expected only in the unified
--    view. Any other SAME_EVERYWHERE = FALSE row is a shape difference C16 / C17 must respect. Empty grid = not visible.
WITH c AS (
    SELECT COLUMN_NAME,
           MAX(IFF(TABLE_NAME = 'SNOWFLAKE_COCO_USAGE_HISTORY', ORDINAL_POSITION, NULL)) AS UNIFIED_POSITION,
           MAX(IFF(TABLE_NAME = 'SNOWFLAKE_COCO_USAGE_HISTORY', DATA_TYPE, NULL)) AS UNIFIED_TYPE,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY', DATA_TYPE, NULL)) AS SNOWSIGHT_TYPE,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_CLI_USAGE_HISTORY', DATA_TYPE, NULL)) AS CLI_TYPE,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_DESKTOP_USAGE_HISTORY', DATA_TYPE, NULL)) AS DESKTOP_TYPE
    FROM SNOWFLAKE.INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'ACCOUNT_USAGE'
      AND TABLE_NAME IN ('SNOWFLAKE_COCO_USAGE_HISTORY', 'CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY',
                         'CORTEX_CODE_CLI_USAGE_HISTORY', 'CORTEX_CODE_DESKTOP_USAGE_HISTORY')
    GROUP BY COLUMN_NAME
)
SELECT COLUMN_NAME, UNIFIED_POSITION, UNIFIED_TYPE, SNOWSIGHT_TYPE, CLI_TYPE, DESKTOP_TYPE,
       (UNIFIED_TYPE IS NOT DISTINCT FROM SNOWSIGHT_TYPE
        AND UNIFIED_TYPE IS NOT DISTINCT FROM CLI_TYPE
        AND UNIFIED_TYPE IS NOT DISTINCT FROM DESKTOP_TYPE) AS SAME_EVERYWHERE
FROM c
ORDER BY COALESCE(UNIFIED_POSITION, 1000), COLUMN_NAME;

-- C2 Volume by INTERFACE (raw value, so a casing surprise shows), rolling 30 / 90 / 365 days.
-- DECIDES: desktop CREDITS_* > 0 = Desktop is used. Its USD_365D_AT_AI_RATE is spend OVERWATCH never attributes
--    to a user today. Compare CREDITS_90D for snowsight + cli with the 2026-09-30 baseline (2,165.20 credits).
--    FIRST_SEEN = how far back the unified view really reaches (the v4.612 panel can show no earlier day).
WITH rate AS (
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) AS AI_RATE
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
),
v AS (
    SELECT c.INTERFACE, c.USER_ID, c.USAGE_TIME,
           COALESCE(c.TOKEN_CREDITS, 0) AS CR, COALESCE(c.TOKENS, 0) AS TK
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
)
SELECT v.INTERFACE,
       COUNT_IF(v.USAGE_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS ROWS_30D,
       COUNT_IF(v.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())) AS ROWS_90D,
       COUNT(*) AS ROWS_365D,
       ROUND(SUM(IFF(v.USAGE_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP()), v.CR, 0)), 4) AS CREDITS_30D,
       ROUND(SUM(IFF(v.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP()), v.CR, 0)), 4) AS CREDITS_90D,
       ROUND(SUM(v.CR), 4) AS CREDITS_365D,
       ROUND(SUM(v.CR) * ANY_VALUE(r.AI_RATE), 2) AS USD_365D_AT_AI_RATE,
       ROUND(100 * SUM(v.CR) / NULLIF(SUM(SUM(v.CR)) OVER (), 0), 2) AS PCT_OF_365D_CREDITS,
       COUNT(DISTINCT IFF(v.USAGE_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP()), v.USER_ID, NULL)) AS USERS_30D,
       COUNT(DISTINCT v.USER_ID) AS USERS_365D,
       SUM(v.TK) AS TOKENS_365D,
       MIN(v.USAGE_TIME) AS FIRST_SEEN,
       MAX(v.USAGE_TIME) AS LAST_SEEN
FROM v
CROSS JOIN rate r
GROUP BY v.INTERFACE
ORDER BY CREDITS_365D DESC;

-- C3 What OVERWATCH is missing today: the unified view per source vs FACT_AI_USAGE_DAILY (the Cortex Code rows the
--    loader stores), last complete Central month and this month to yesterday.
-- DECIDES: the Desktop row's NOT_IN_OVERWATCH_* = Cortex Code spend no OVERWATCH per-user / per-company panel shows.
--    Snowsight / CLI rows should be about 0: a small drift there is load timing or day keying, not Desktop.
WITH clk AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_CT,
           DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS M_HI,
           DATEADD('month', -1, DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS M_LO
),
rate AS (
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) AS AI_RATE
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
),
v AS (
    SELECT CASE LOWER(c.INTERFACE) WHEN 'snowsight' THEN 'Snowsight' WHEN 'cli' THEN 'CLI'
                WHEN 'desktop' THEN 'Desktop' ELSE COALESCE(c.INTERFACE, '(null)') END AS SOURCE,
           IFF(CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= k.M_HI,
               'this month to yesterday', 'last month') AS PERIOD,
           SUM(COALESCE(c.TOKEN_CREDITS, 0)) AS VIEW_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN clk k
    WHERE c.USAGE_TIME >= DATEADD('day', -75, CURRENT_TIMESTAMP())
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= k.M_LO
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE < k.TODAY_CT
    GROUP BY 1, 2
),
f AS (
    SELECT x.SOURCE,
           IFF(x.DAY >= k.M_HI, 'this month to yesterday', 'last month') AS PERIOD,
           SUM(COALESCE(x.CREDITS, 0)) AS FACT_CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY x
    CROSS JOIN clk k
    WHERE x.SOURCE <> 'Functions' AND x.DAY >= k.M_LO AND x.DAY < k.TODAY_CT
    GROUP BY 1, 2
)
SELECT COALESCE(v.PERIOD, f.PERIOD) AS PERIOD,
       COALESCE(v.SOURCE, f.SOURCE) AS SOURCE,
       ROUND(v.VIEW_CREDITS, 4) AS COCO_VIEW_CREDITS,
       ROUND(f.FACT_CREDITS, 4) AS OVERWATCH_FACT_CREDITS,
       ROUND(COALESCE(v.VIEW_CREDITS, 0) - COALESCE(f.FACT_CREDITS, 0), 4) AS NOT_IN_OVERWATCH_CREDITS,
       ROUND((COALESCE(v.VIEW_CREDITS, 0) - COALESCE(f.FACT_CREDITS, 0)) * r.AI_RATE, 2) AS NOT_IN_OVERWATCH_USD_AT_AI_RATE
FROM v
FULL OUTER JOIN f ON f.SOURCE = v.SOURCE AND f.PERIOD = v.PERIOD
CROSS JOIN rate r
ORDER BY 1, 2;

-- C4 Shape of the two granular objects, 90 days: the object's own type and the type of each model's value.
-- DECIDES: MODEL_VALUE_TYPE = OBJECT on (nearly) every entry = the model -> {type: number} shape the repo already saw
--    in TOKENS_GRANULAR (cortex_sql.py:327-332) also holds for CREDITS_GRANULAR (v4.612 reads the four named leaves
--    of that object; a DECIMAL / DOUBLE / INTEGER value = a flat model -> number map, which v4.612 books under
--    COCO_CREDITS_OTHER). MODEL_VALUE_TYPE NULL = rows with a NULL or empty object: their credits
--    (TOKEN_CREDITS_ON_ROWS_WITHOUT_BREAKDOWN) are invisible to a FLATTEN without OUTER => TRUE, as in the concept query.
WITH b AS (
    SELECT LOWER(c.INTERFACE) AS INTERFACE, c.TOKEN_CREDITS, c.CREDITS_GRANULAR, c.TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
)
SELECT 'CREDITS_GRANULAR' AS COL, b.INTERFACE,
       TYPEOF(b.CREDITS_GRANULAR::VARIANT) AS OBJECT_TYPE,
       TYPEOF(m.VALUE) AS MODEL_VALUE_TYPE,
       COUNT(*) AS MODEL_ENTRIES,
       COUNT(DISTINCT m.SEQ) AS SOURCE_ROWS,
       ROUND(SUM(IFF(m.VALUE IS NULL, COALESCE(b.TOKEN_CREDITS, 0), 0)), 4) AS TOKEN_CREDITS_ON_ROWS_WITHOUT_BREAKDOWN
FROM b, LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR, OUTER => TRUE) m
GROUP BY 1, 2, 3, 4
UNION ALL
SELECT 'TOKENS_GRANULAR', b.INTERFACE,
       TYPEOF(b.TOKENS_GRANULAR::VARIANT),
       TYPEOF(m.VALUE),
       COUNT(*),
       COUNT(DISTINCT m.SEQ),
       NULL
FROM b, LATERAL FLATTEN(INPUT => b.TOKENS_GRANULAR, OUTER => TRUE) m
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 5 DESC;

-- C4c Do the two breakdowns travel together? 90 days, per INTERFACE: requests by whether CREDITS_GRANULAR and
--     TOKENS_GRANULAR each carry at least one model.
-- DECIDES: requests on (TRUE, TRUE) are the normal case. (FALSE, TRUE) = tokens without a credit breakdown: v4.612
--    lends their tokens to no model (their credits sit on '(no model breakdown)'). (TRUE, FALSE) = credits without
--    tokens: they raise that model's credits per 1M tokens; report their share of TOKEN_CREDITS.
SELECT LOWER(c.INTERFACE) AS INTERFACE,
       COALESCE(ARRAY_SIZE(OBJECT_KEYS(c.CREDITS_GRANULAR)), 0) > 0 AS HAS_CREDIT_BREAKDOWN,
       COALESCE(ARRAY_SIZE(OBJECT_KEYS(c.TOKENS_GRANULAR)), 0) > 0 AS HAS_TOKEN_BREAKDOWN,
       COUNT(*) AS REQUESTS,
       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 4) AS TOKEN_CREDITS,
       SUM(COALESCE(c.TOKENS, 0)) AS TOKENS
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
WHERE c.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3
ORDER BY 1, 2 DESC, 3 DESC;

-- C4b Models per request, 90 days (top 50 model sets).
-- DECIDES: requests carrying 2+ model keys = one request bills several models (e.g. a main model plus a helper the
--    product picks), so 'the model a user selected' is not the same as 'every model a request billed'. The v4.612
--    drill then shows billed models and labels them so, with the main model (most credits in the request) as the
--    selection proxy (C6 REQUESTS_AS_PRIMARY_MODEL).
WITH b AS (
    SELECT c.REQUEST_ID, c.USAGE_TIME, LOWER(c.INTERFACE) AS INTERFACE, c.CREDITS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
),
n AS (
    SELECT MAX(b.INTERFACE) AS INTERFACE,
           COUNT(m.KEY) AS MODELS_IN_REQUEST,
           LISTAGG(m.KEY::VARCHAR, '+') WITHIN GROUP (ORDER BY m.KEY::VARCHAR) AS MODEL_SET
    FROM b, LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR, OUTER => TRUE) m
    GROUP BY m.SEQ, b.REQUEST_ID, b.USAGE_TIME
)
SELECT n.INTERFACE, n.MODELS_IN_REQUEST, n.MODEL_SET, COUNT(*) AS REQUESTS
FROM n
GROUP BY 1, 2, 3
ORDER BY REQUESTS DESC
LIMIT 50;

-- C5 What sits inside each model's object, 90 days: every path under a model key, its JSON type and totals.
-- DECIDES: exactly input / cache_read_input / cache_write_input / output, all numeric = the four documented types
--    v4.612 reads by name. Any other path (a pre-computed 'total', a nested 'input.x', a new type) = credits v4.612
--    leaves on the '(not attributed to a model)' row (it never double counts a 'total' leaf, but a new type would
--    show there until the builder names it). LEAF_TYPE VARCHAR = numbers stored as strings (TRY_TO_DOUBLE still
--    reads them). MIN_VALUE < 0 = credit reversals exist.
WITH b AS (
    SELECT c.CREDITS_GRANULAR, c.TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
)
SELECT 'CREDITS_GRANULAR' AS COL, l.PATH AS PATH_IN_MODEL, TYPEOF(l.VALUE) AS LEAF_TYPE,
       COUNT(*) AS N, COUNT(DISTINCT m.KEY) AS MODELS,
       ROUND(SUM(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))), 6) AS NUMERIC_SUM,
       MIN(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))) AS MIN_VALUE,
       MAX(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))) AS MAX_VALUE
FROM b,
     LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR) m,
     LATERAL FLATTEN(INPUT => m.VALUE, RECURSIVE => TRUE) l
GROUP BY 1, 2, 3
UNION ALL
SELECT 'TOKENS_GRANULAR', l.PATH, TYPEOF(l.VALUE),
       COUNT(*), COUNT(DISTINCT m.KEY),
       ROUND(SUM(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))), 6),
       MIN(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))),
       MAX(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE)))
FROM b,
     LATERAL FLATTEN(INPUT => b.TOKENS_GRANULAR) m,
     LATERAL FLATTEN(INPUT => m.VALUE, RECURSIVE => TRUE) l
GROUP BY 1, 2, 3
ORDER BY 1, 4 DESC;

-- C6 Every distinct model key, 365 days and last Central month: robust credits (every numeric leaf) next to the
--    concept query's arithmetic on the same entries, case variants, interfaces, users, requests.
-- DECIDES: RAW_KEYS_FOR_THIS_NORMALIZED_KEY > 1 = keys differ only by case / spaces (normalize with LOWER(TRIM())).
--    REQUESTS_BOSS_FORMULA_DROPS / CREDITS_BOSS_FORMULA_DROPS > 0 = entries the concept query silently loses
--    (missing input or output). REQUESTS_AS_PRIMARY_MODEL = requests where this model carried the most credits.
WITH clk AS (
    SELECT DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS M_HI,
           DATEADD('month', -1, DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS M_LO
),
b AS (
    SELECT c.REQUEST_ID, c.USAGE_TIME, c.USER_ID, LOWER(c.INTERFACE) AS INTERFACE, c.CREDITS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
),
pm AS (
    SELECT m.SEQ AS RID, b.REQUEST_ID, b.USAGE_TIME, m.KEY::VARCHAR AS MODEL_KEY,
           MAX(b.INTERFACE) AS INTERFACE,
           MAX(b.USER_ID) AS USER_ID,
           CONVERT_TIMEZONE('America/Chicago', b.USAGE_TIME)::DATE AS DAY_CT,
           SUM(TRY_TO_DOUBLE(TO_VARCHAR(COALESCE(l.VALUE, m.VALUE)))) AS LEAF_CREDITS,
           MAX(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:input))
               + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:cache_read_input)), 0)
               + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:cache_write_input)), 0)
               + TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:output))) AS BOSS_FORMULA,
           MAX(IFF(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:input)) IS NULL, 1, 0)) AS NO_INPUT,
           MAX(IFF(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:output)) IS NULL, 1, 0)) AS NO_OUTPUT
    FROM b,
         LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR) m,
         LATERAL FLATTEN(INPUT => m.VALUE, RECURSIVE => TRUE, OUTER => TRUE) l
    GROUP BY m.SEQ, b.REQUEST_ID, b.USAGE_TIME, m.KEY
),
pm2 AS (
    SELECT pm.*,
           IFF(ROW_NUMBER() OVER (PARTITION BY pm.RID, pm.REQUEST_ID, pm.USAGE_TIME
                                  ORDER BY pm.LEAF_CREDITS DESC NULLS LAST, pm.MODEL_KEY) = 1, 1, 0) AS IS_PRIMARY
    FROM pm
)
SELECT p.MODEL_KEY,
       LOWER(TRIM(p.MODEL_KEY)) AS MODEL_NORMALIZED,
       COUNT(*) OVER (PARTITION BY LOWER(TRIM(p.MODEL_KEY))) AS RAW_KEYS_FOR_THIS_NORMALIZED_KEY,
       LISTAGG(DISTINCT p.INTERFACE, '+') WITHIN GROUP (ORDER BY p.INTERFACE) AS INTERFACES,
       COUNT(DISTINCT p.USER_ID) AS USERS_365D,
       COUNT(*) AS REQUESTS_365D,
       SUM(p.IS_PRIMARY) AS REQUESTS_AS_PRIMARY_MODEL,
       ROUND(SUM(p.LEAF_CREDITS), 4) AS CREDITS_365D,
       ROUND(SUM(IFF(p.DAY_CT >= k.M_LO AND p.DAY_CT < k.M_HI, p.LEAF_CREDITS, 0)), 4) AS CREDITS_LAST_MONTH,
       COUNT(DISTINCT IFF(p.DAY_CT >= k.M_LO AND p.DAY_CT < k.M_HI, p.USER_ID, NULL)) AS USERS_LAST_MONTH,
       ROUND(SUM(p.BOSS_FORMULA), 4) AS BOSS_FORMULA_CREDITS_365D,
       COUNT_IF(p.BOSS_FORMULA IS NULL AND COALESCE(p.LEAF_CREDITS, 0) <> 0) AS REQUESTS_BOSS_FORMULA_DROPS,
       ROUND(SUM(IFF(p.BOSS_FORMULA IS NULL, p.LEAF_CREDITS, 0)), 4) AS CREDITS_BOSS_FORMULA_DROPS,
       SUM(p.NO_INPUT) AS REQUESTS_MISSING_INPUT,
       SUM(p.NO_OUTPUT) AS REQUESTS_MISSING_OUTPUT,
       MIN(p.DAY_CT) AS FIRST_DAY,
       MAX(p.DAY_CT) AS LAST_DAY
FROM pm2 p
CROSS JOIN clk k
GROUP BY p.MODEL_KEY
ORDER BY CREDITS_365D DESC NULLS LAST;

-- C7 Reconciliation per INTERFACE, 90 days: each row's TOKEN_CREDITS vs the sum of its CREDITS_GRANULAR numeric
--    leaves, and TOKENS vs its TOKENS_GRANULAR leaves (one row per source row: FLATTEN SEQ + the row's keys).
-- DECIDES: DIFF about 0 and ROWS_DIFFER about 0 = the model split adds up to the billed request total, so the v4.612
--    '(not attributed to a model)' row stays tiny. ROWS_TOTAL_BUT_NO_LEAVES > 0 = requests with credits but no model
--    breakdown. A large DIFF_PCT = TOKEN_CREDITS and the leaves are different bases: do not mix them as the concept
--    query does (v4.612 keeps TOKEN_CREDITS as the spend and shows the gap as its own row).
WITH b AS (
    SELECT c.REQUEST_ID, c.USAGE_TIME, LOWER(c.INTERFACE) AS INTERFACE, c.TOKEN_CREDITS, c.TOKENS,
           c.CREDITS_GRANULAR, c.TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
),
cr AS (
    SELECT MAX(b.INTERFACE) AS INTERFACE, MAX(b.TOKEN_CREDITS) AS COL_TOTAL,
           SUM(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))) AS LEAF_SUM
    FROM b, LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR, RECURSIVE => TRUE, OUTER => TRUE) l
    GROUP BY l.SEQ, b.REQUEST_ID, b.USAGE_TIME
),
tk AS (
    SELECT MAX(b.INTERFACE) AS INTERFACE, MAX(b.TOKENS) AS COL_TOTAL,
           SUM(TRY_TO_DOUBLE(TO_VARCHAR(l.VALUE))) AS LEAF_SUM
    FROM b, LATERAL FLATTEN(INPUT => b.TOKENS_GRANULAR, RECURSIVE => TRUE, OUTER => TRUE) l
    GROUP BY l.SEQ, b.REQUEST_ID, b.USAGE_TIME
)
SELECT 'TOKEN_CREDITS vs CREDITS_GRANULAR leaves' AS CHECK_NAME, cr.INTERFACE,
       COUNT(*) AS ROWS_90D,
       ROUND(SUM(COALESCE(cr.COL_TOTAL, 0)), 6) AS COLUMN_TOTAL,
       ROUND(SUM(COALESCE(cr.LEAF_SUM, 0)), 6) AS LEAF_TOTAL,
       ROUND(SUM(COALESCE(cr.COL_TOTAL, 0)) - SUM(COALESCE(cr.LEAF_SUM, 0)), 6) AS DIFF,
       ROUND(100 * (SUM(COALESCE(cr.COL_TOTAL, 0)) - SUM(COALESCE(cr.LEAF_SUM, 0)))
             / NULLIF(SUM(COALESCE(cr.COL_TOTAL, 0)), 0), 4) AS DIFF_PCT,
       COUNT_IF(ABS(COALESCE(cr.COL_TOTAL, 0) - COALESCE(cr.LEAF_SUM, 0)) > 0.0001) AS ROWS_DIFFER,
       MAX(ABS(COALESCE(cr.COL_TOTAL, 0) - COALESCE(cr.LEAF_SUM, 0))) AS MAX_ROW_ABS_DIFF,
       COUNT_IF(cr.LEAF_SUM IS NULL AND COALESCE(cr.COL_TOTAL, 0) <> 0) AS ROWS_TOTAL_BUT_NO_LEAVES,
       COUNT_IF(cr.COL_TOTAL IS NULL AND COALESCE(cr.LEAF_SUM, 0) <> 0) AS ROWS_LEAVES_BUT_NULL_TOTAL
FROM cr
GROUP BY cr.INTERFACE
UNION ALL
SELECT 'TOKENS vs TOKENS_GRANULAR leaves (rows differ = by more than 0.5 token)', tk.INTERFACE,
       COUNT(*),
       ROUND(SUM(COALESCE(tk.COL_TOTAL, 0)), 0),
       ROUND(SUM(COALESCE(tk.LEAF_SUM, 0)), 0),
       ROUND(SUM(COALESCE(tk.COL_TOTAL, 0)) - SUM(COALESCE(tk.LEAF_SUM, 0)), 0),
       ROUND(100 * (SUM(COALESCE(tk.COL_TOTAL, 0)) - SUM(COALESCE(tk.LEAF_SUM, 0)))
             / NULLIF(SUM(COALESCE(tk.COL_TOTAL, 0)), 0), 4),
       COUNT_IF(ABS(COALESCE(tk.COL_TOTAL, 0) - COALESCE(tk.LEAF_SUM, 0)) > 0.5),
       MAX(ABS(COALESCE(tk.COL_TOTAL, 0) - COALESCE(tk.LEAF_SUM, 0))),
       COUNT_IF(tk.LEAF_SUM IS NULL AND COALESCE(tk.COL_TOTAL, 0) <> 0),
       COUNT_IF(tk.COL_TOTAL IS NULL AND COALESCE(tk.LEAF_SUM, 0) <> 0)
FROM tk
GROUP BY tk.INTERFACE
ORDER BY 1, 2;

-- C8 Keys, 365 days (one row): user identity coverage and REQUEST_ID uniqueness.
-- DECIDES: NULL_USER_NAME_ROWS > 0 = the concept query's top-15 can hold a NULL user that its join then drops.
--    USER_NAME_NE_USERS_NAME > 0 = the view's USER_NAME (documented as the login name) is not USERS.NAME, which the app
--    displays and company-scopes on: key on USER_ID (v4.612 does). USER_ID_NOT_IN_USERS / DROPPED_USER_* = honest-
--    UNKNOWN volume. ROWS_WHOSE_USER_ID_HAS_2PLUS_USERS_ROWS > 0 = USERS must be deduplicated before the join (v4.612
--    does; the existing cortex_code_user_daily does not). REQUEST_IDS_REPEATED / REQUEST_IDS_IN_2PLUS_INTERFACES > 0 =
--    duplicate rows inside the unified view.
WITH v AS (
    SELECT c.USER_ID, c.USER_NAME, c.REQUEST_ID, LOWER(c.INTERFACE) AS INTERFACE,
           COALESCE(c.TOKEN_CREDITS, 0) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
),
u AS (
    SELECT s.USER_ID, COUNT(*) AS U_ROWS, MAX(s.NAME) AS NAME, MAX(s.LOGIN_NAME) AS LOGIN_NAME,
           MAX(s.DELETED_ON) AS DELETED_ON
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS s
    GROUP BY s.USER_ID
),
r AS (
    SELECT v.REQUEST_ID, COUNT(*) AS N, COUNT(DISTINCT v.INTERFACE) AS IFACES, SUM(v.CR) AS CR
    FROM v
    WHERE v.REQUEST_ID IS NOT NULL
    GROUP BY v.REQUEST_ID
),
rs AS (
    SELECT COUNT_IF(r.N > 1) AS REQUEST_IDS_REPEATED,
           SUM(IFF(r.N > 1, r.N - 1, 0)) AS EXTRA_ROWS_ON_REPEATED_IDS,
           COUNT_IF(r.IFACES > 1) AS REQUEST_IDS_IN_2PLUS_INTERFACES,
           ROUND(SUM(IFF(r.IFACES > 1, r.CR, 0)), 4) AS CREDITS_ON_MULTI_INTERFACE_IDS
    FROM r
)
SELECT COUNT(*) AS ROWS_365D,
       COUNT(DISTINCT v.USER_ID) AS DISTINCT_USER_IDS,
       COUNT(DISTINCT v.USER_NAME) AS DISTINCT_USER_NAMES,
       COUNT_IF(v.USER_NAME IS NULL) AS NULL_USER_NAME_ROWS,
       ROUND(SUM(IFF(v.USER_NAME IS NULL, v.CR, 0)), 4) AS NULL_USER_NAME_CREDITS,
       COUNT_IF(v.USER_ID IS NULL) AS NULL_USER_ID_ROWS,
       COUNT_IF(v.USER_ID IS NOT NULL AND u.USER_ID IS NULL) AS USER_ID_NOT_IN_USERS_ROWS,
       ROUND(SUM(IFF(v.USER_ID IS NOT NULL AND u.USER_ID IS NULL, v.CR, 0)), 4) AS USER_ID_NOT_IN_USERS_CREDITS,
       COUNT_IF(u.U_ROWS > 1) AS ROWS_WHOSE_USER_ID_HAS_2PLUS_USERS_ROWS,
       COUNT_IF(u.DELETED_ON IS NOT NULL) AS DROPPED_USER_ROWS,
       ROUND(SUM(IFF(u.DELETED_ON IS NOT NULL, v.CR, 0)), 4) AS DROPPED_USER_CREDITS,
       COUNT_IF(v.USER_NAME = u.NAME) AS USER_NAME_EQ_USERS_NAME,
       COUNT_IF(v.USER_NAME = u.LOGIN_NAME) AS USER_NAME_EQ_LOGIN_NAME,
       COUNT_IF(u.USER_ID IS NOT NULL AND v.USER_NAME IS DISTINCT FROM u.NAME) AS USER_NAME_NE_USERS_NAME,
       COUNT_IF(v.REQUEST_ID IS NULL) AS NULL_REQUEST_ID_ROWS,
       ANY_VALUE(rs.REQUEST_IDS_REPEATED) AS REQUEST_IDS_REPEATED,
       ANY_VALUE(rs.EXTRA_ROWS_ON_REPEATED_IDS) AS EXTRA_ROWS_ON_REPEATED_IDS,
       ANY_VALUE(rs.REQUEST_IDS_IN_2PLUS_INTERFACES) AS REQUEST_IDS_IN_2PLUS_INTERFACES,
       ANY_VALUE(rs.CREDITS_ON_MULTI_INTERFACE_IDS) AS CREDITS_ON_MULTI_INTERFACE_IDS
FROM v
LEFT JOIN u ON u.USER_ID = v.USER_ID
CROSS JOIN rs;

-- C9 Renames and re-creates, 365 days: one USER_ID under several USER_NAMEs, one USER_NAME under several USER_IDs.
-- DECIDES: rows in the first group = a USER_NAME grain splits one person. Rows in the second = it merges two people.
--    Either way the drill keys on USER_ID. 'no rows' = the concept query's name grain happens to be safe today.
WITH v AS (
    SELECT c.USER_ID, c.USER_NAME, COALESCE(c.TOKEN_CREDITS, 0) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
)
SELECT 'one USER_ID, several USER_NAMEs (rename?)' AS ISSUE,
       TO_VARCHAR(v.USER_ID) AS KEY_VALUE,
       LISTAGG(DISTINCT COALESCE(v.USER_NAME, '(null)'), ', ')
           WITHIN GROUP (ORDER BY COALESCE(v.USER_NAME, '(null)')) AS VALUES_SEEN,
       COUNT(*) AS ROWS_365D,
       ROUND(SUM(v.CR), 4) AS CREDITS_365D
FROM v
GROUP BY v.USER_ID
HAVING COUNT(DISTINCT COALESCE(v.USER_NAME, '(null)')) > 1
UNION ALL
SELECT 'one USER_NAME, several USER_IDs (dropped + re-created?)',
       v.USER_NAME,
       LISTAGG(DISTINCT TO_VARCHAR(v.USER_ID), ', ') WITHIN GROUP (ORDER BY TO_VARCHAR(v.USER_ID)),
       COUNT(*),
       ROUND(SUM(v.CR), 4)
FROM v
WHERE v.USER_NAME IS NOT NULL
GROUP BY v.USER_NAME
HAVING COUNT(DISTINCT v.USER_ID) > 1
ORDER BY 1, 5 DESC;

-- C10 METADATA and PARENT_REQUEST_ID coverage per INTERFACE, 365 days.
-- DECIDES: ROLE_NAME_PCT / ROLE_NAME_FIRST_SEEN = how much of the v4.612 'Roles' drill reads '(not recorded)' (NULL
--    before the field existed, per docs) and from when. CHILD_ROW_PCT / CHILD_CREDIT_PCT = how much spend sits on rows
--    that have a parent (C11 and C13 decide whether those double count). SELF_PARENT_ROWS > 0 = PARENT_REQUEST_ID =
--    REQUEST_ID.
SELECT LOWER(c.INTERFACE) AS INTERFACE,
       COUNT(*) AS ROWS_365D,
       COUNT_IF(c.METADATA IS NOT NULL) AS METADATA_ROWS,
       COUNT_IF(c.METADATA:role_name::VARCHAR IS NOT NULL) AS ROLE_NAME_ROWS,
       ROUND(100 * COUNT_IF(c.METADATA:role_name::VARCHAR IS NOT NULL) / NULLIF(COUNT(*), 0), 2) AS ROLE_NAME_PCT,
       MIN(IFF(c.METADATA:role_name::VARCHAR IS NOT NULL, c.USAGE_TIME, NULL)) AS ROLE_NAME_FIRST_SEEN,
       COUNT_IF(c.USAGE_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
                AND c.METADATA:role_name::VARCHAR IS NULL) AS ROLE_NAME_MISSING_LAST_30D,
       COUNT(DISTINCT c.METADATA:role_name::VARCHAR) AS DISTINCT_ROLE_NAMES,
       LEFT(LISTAGG(DISTINCT c.METADATA:role_name::VARCHAR, ', ')
            WITHIN GROUP (ORDER BY c.METADATA:role_name::VARCHAR), 400) AS ROLE_NAMES,
       COUNT_IF(c.METADATA:inference_region::VARCHAR = 'global') AS REGION_GLOBAL_ROWS,
       COUNT_IF(c.METADATA:inference_region::VARCHAR = 'regional') AS REGION_REGIONAL_ROWS,
       COUNT_IF(c.METADATA:inference_region::VARCHAR IS NULL) AS REGION_NULL_ROWS,
       COUNT_IF(c.PARENT_REQUEST_ID IS NOT NULL) AS CHILD_ROWS,
       ROUND(100 * COUNT_IF(c.PARENT_REQUEST_ID IS NOT NULL) / NULLIF(COUNT(*), 0), 2) AS CHILD_ROW_PCT,
       ROUND(SUM(IFF(c.PARENT_REQUEST_ID IS NOT NULL, COALESCE(c.TOKEN_CREDITS, 0), 0)), 4) AS CHILD_ROW_CREDITS,
       ROUND(100 * SUM(IFF(c.PARENT_REQUEST_ID IS NOT NULL, COALESCE(c.TOKEN_CREDITS, 0), 0))
             / NULLIF(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 0), 2) AS CHILD_CREDIT_PCT,
       COUNT_IF(c.PARENT_REQUEST_ID = c.REQUEST_ID) AS SELF_PARENT_ROWS,
       COUNT_IF(ARRAY_SIZE(c.USER_TAGS) > 0) AS ROWS_WITH_USER_TAGS
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
WHERE c.USAGE_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY ROWS_365D DESC;

-- C11 Parent vs children, 90 days: for every PARENT_REQUEST_ID referenced, is the parent row present, and how do
--    its credits compare with the sum of its children?
-- DECIDES (a hint, C13 is decisive): PARENT_EQUALS_CHILD_SUM dominant = parent rows roll up their children (summing all
--    rows double counts). FOUND_PARENT_ZERO_CREDIT or a mix of ABOVE / BELOW = every row is its own billed request.
--    PARENT_ROW_NOT_FOUND = parents outside the 90-day edge, or parents that are not rows of this view at all.
WITH v AS (
    SELECT c.REQUEST_ID, c.PARENT_REQUEST_ID, LOWER(c.INTERFACE) AS INTERFACE, COALESCE(c.TOKEN_CREDITS, 0) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
),
kids AS (
    SELECT v.PARENT_REQUEST_ID AS PID, COUNT(*) AS N_CHILD, SUM(v.CR) AS CHILD_CR,
           COUNT(DISTINCT v.INTERFACE) AS CHILD_INTERFACES
    FROM v
    WHERE v.PARENT_REQUEST_ID IS NOT NULL
      AND v.PARENT_REQUEST_ID <> COALESCE(v.REQUEST_ID, '')
    GROUP BY v.PARENT_REQUEST_ID
),
par AS (
    SELECT v.REQUEST_ID AS PID, COUNT(*) AS PARENT_ROWS, SUM(v.CR) AS PARENT_CR,
           MAX(IFF(v.PARENT_REQUEST_ID IS NOT NULL, 1, 0)) AS PARENT_ALSO_A_CHILD
    FROM v
    WHERE v.REQUEST_ID IS NOT NULL
    GROUP BY v.REQUEST_ID
)
SELECT COUNT(*) AS PARENT_IDS_REFERENCED,
       COUNT(p.PID) AS PARENT_ROW_FOUND,
       COUNT(*) - COUNT(p.PID) AS PARENT_ROW_NOT_FOUND,
       SUM(k.N_CHILD) AS CHILD_ROWS,
       ROUND(SUM(k.CHILD_CR), 4) AS CHILD_CREDITS,
       ROUND(SUM(p.PARENT_CR), 4) AS FOUND_PARENT_CREDITS,
       COUNT_IF(p.PID IS NOT NULL AND p.PARENT_CR = 0) AS FOUND_PARENT_ZERO_CREDIT,
       COUNT_IF(p.PID IS NOT NULL AND ABS(p.PARENT_CR - k.CHILD_CR) <= 0.0001) AS PARENT_EQUALS_CHILD_SUM,
       COUNT_IF(p.PID IS NOT NULL AND p.PARENT_CR > k.CHILD_CR + 0.0001) AS PARENT_ABOVE_CHILD_SUM,
       COUNT_IF(p.PID IS NOT NULL AND p.PARENT_CR < k.CHILD_CR - 0.0001) AS PARENT_BELOW_CHILD_SUM,
       COUNT_IF(p.PARENT_ALSO_A_CHILD = 1) AS NESTED_PARENTS,
       COUNT_IF(p.PARENT_ROWS > 1) AS PARENTS_WITH_2PLUS_ROWS,
       COUNT_IF(k.CHILD_INTERFACES > 1) AS PARENTS_WITH_MIXED_CHILD_INTERFACES,
       MEDIAN(k.N_CHILD) AS MEDIAN_CHILDREN,
       MAX(k.N_CHILD) AS MAX_CHILDREN
FROM kids k
LEFT JOIN par p ON p.PID = k.PID;

-- C12 Where CoCo is BILLED: every METERING_DAILY_HISTORY line whose SERVICE_TYPE looks like Cortex Code / CoWork, per
--     UTC month (USAGE_DATE is a UTC day, formulas.py:664), today (UTC) excluded. Cheap.
-- DECIDES: one combined line vs separate Snowsight / CLI / Desktop lines. Every name matched here is in
--     common.AI_SERVICE_TOKENS (common.py:183), so BILLED totals already include Desktop at the AI rate. What is
--     missing is only the per-user / per-company split.
SELECT DATE_TRUNC('month', h.USAGE_DATE) AS MONTH_UTC,
       h.SERVICE_TYPE,
       ROUND(SUM(COALESCE(h.CREDITS_USED, 0)), 4) AS CREDITS_USED,
       ROUND(SUM(COALESCE(h.CREDITS_BILLED, 0)), 4) AS CREDITS_BILLED,
       COUNT(DISTINCT h.USAGE_DATE) AS DAYS_WITH_USAGE,
       MIN(h.USAGE_DATE) AS FIRST_DAY,
       MAX(h.USAGE_DATE) AS LAST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY h
WHERE h.USAGE_DATE >= DATEADD('month', -3, DATE_TRUNC('month', CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::DATE))
  AND h.USAGE_DATE < CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::DATE
  AND (h.SERVICE_TYPE ILIKE '%COCO%' OR h.SERVICE_TYPE ILIKE '%COWORK%' OR h.SERVICE_TYPE ILIKE '%CORTEX_CODE%')
GROUP BY 1, 2
ORDER BY 1, 2;

-- C13 THE DOUBLE-COUNT TEST: the usage view's credits vs metered CoCo credits, same UTC months (the view keyed by its
--     UTC day here, to match METERING_DAILY_HISTORY), today (UTC) excluded on both sides.
-- DECIDES: VIEW_TOTAL about METERED_BILLED = summing every row is right (no parent roll-up double count; v4.612 and
--     the existing AI users tab both sum every row). VIEW_TOTAL about 2x the child share above METERED while
--     VIEW_TOP_LEVEL_ONLY matches = parent rows include their children (then BOTH readers need the same filter).
--     METERED above VIEW_TOTAL = metering also carries something the view does not (e.g. CoWork on the same line).
WITH k AS (
    SELECT CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::DATE AS TODAY_UTC,
           DATEADD('month', -3, DATE_TRUNC('month', CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::DATE)) AS LO_UTC
),
m AS (
    SELECT DATE_TRUNC('month', h.USAGE_DATE) AS MONTH_UTC,
           LISTAGG(DISTINCT h.SERVICE_TYPE, ', ') WITHIN GROUP (ORDER BY h.SERVICE_TYPE) AS COCO_SERVICE_TYPES,
           SUM(COALESCE(h.CREDITS_USED, 0)) AS METERED_USED,
           SUM(COALESCE(h.CREDITS_BILLED, 0)) AS METERED_BILLED
    FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY h
    CROSS JOIN k
    WHERE h.USAGE_DATE >= k.LO_UTC AND h.USAGE_DATE < k.TODAY_UTC
      AND (h.SERVICE_TYPE ILIKE '%COCO%' OR h.SERVICE_TYPE ILIKE '%COWORK%' OR h.SERVICE_TYPE ILIKE '%CORTEX_CODE%')
    GROUP BY 1
),
v AS (
    SELECT DATE_TRUNC('month', CONVERT_TIMEZONE('UTC', c.USAGE_TIME)::DATE) AS MONTH_UTC,
           SUM(COALESCE(c.TOKEN_CREDITS, 0)) AS VIEW_TOTAL,
           SUM(IFF(LOWER(c.INTERFACE) = 'snowsight', COALESCE(c.TOKEN_CREDITS, 0), 0)) AS VIEW_SNOWSIGHT,
           SUM(IFF(LOWER(c.INTERFACE) = 'cli', COALESCE(c.TOKEN_CREDITS, 0), 0)) AS VIEW_CLI,
           SUM(IFF(LOWER(c.INTERFACE) = 'desktop', COALESCE(c.TOKEN_CREDITS, 0), 0)) AS VIEW_DESKTOP,
           SUM(IFF(c.PARENT_REQUEST_ID IS NULL, COALESCE(c.TOKEN_CREDITS, 0), 0)) AS VIEW_TOP_LEVEL_ONLY
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN k
    WHERE c.USAGE_TIME >= DATEADD('day', -130, CURRENT_TIMESTAMP())
      AND CONVERT_TIMEZONE('UTC', c.USAGE_TIME)::DATE >= k.LO_UTC
      AND CONVERT_TIMEZONE('UTC', c.USAGE_TIME)::DATE < k.TODAY_UTC
    GROUP BY 1
)
SELECT COALESCE(m.MONTH_UTC, v.MONTH_UTC) AS MONTH_UTC,
       m.COCO_SERVICE_TYPES,
       ROUND(m.METERED_USED, 4) AS METERED_USED,
       ROUND(m.METERED_BILLED, 4) AS METERED_BILLED,
       ROUND(v.VIEW_TOTAL, 4) AS VIEW_TOTAL,
       ROUND(v.VIEW_SNOWSIGHT, 4) AS VIEW_SNOWSIGHT,
       ROUND(v.VIEW_CLI, 4) AS VIEW_CLI,
       ROUND(v.VIEW_DESKTOP, 4) AS VIEW_DESKTOP,
       ROUND(v.VIEW_TOP_LEVEL_ONLY, 4) AS VIEW_TOP_LEVEL_ONLY,
       ROUND(COALESCE(v.VIEW_TOTAL, 0) - COALESCE(m.METERED_BILLED, 0), 4) AS VIEW_MINUS_BILLED,
       ROUND(100 * (COALESCE(v.VIEW_TOTAL, 0) - COALESCE(m.METERED_BILLED, 0)) / NULLIF(m.METERED_BILLED, 0), 2) AS VIEW_VS_BILLED_PCT
FROM m
FULL OUTER JOIN v ON v.MONTH_UTC = m.MONTH_UTC
ORDER BY 1;

-- C14 Month-boundary sensitivity of the concept query, last complete month: credits keyed by the Central day
--     (OVERWATCH), by the UTC day (a UTC Snowsight session), by the stored offset (a bare ::DATE, on purpose here), and
--     with the concept query's string bounds resolved in THIS session's zone (Central here, so it should equal the
--     first column).
-- DECIDES: how far a session-zone month moves a monthly total. The concept numbers used the boss's worksheet zone
--     (his SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION tells which).
WITH k AS (
    SELECT DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS M_HI,
           DATEADD('month', -1, DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS M_LO
),
v AS (
    SELECT c.USAGE_TIME, COALESCE(c.TOKEN_CREDITS, 0) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -75, CURRENT_TIMESTAMP())
)
SELECT k.M_LO AS MONTH_FROM,
       k.M_HI AS MONTH_TO_EXCL,
       ROUND(SUM(IFF(CONVERT_TIMEZONE('America/Chicago', v.USAGE_TIME)::DATE >= k.M_LO
                     AND CONVERT_TIMEZONE('America/Chicago', v.USAGE_TIME)::DATE < k.M_HI, v.CR, 0)), 4) AS CREDITS_CENTRAL_MONTH,
       ROUND(SUM(IFF(CONVERT_TIMEZONE('UTC', v.USAGE_TIME)::DATE >= k.M_LO
                     AND CONVERT_TIMEZONE('UTC', v.USAGE_TIME)::DATE < k.M_HI, v.CR, 0)), 4) AS CREDITS_UTC_MONTH,
       ROUND(SUM(IFF(v.USAGE_TIME::DATE >= k.M_LO AND v.USAGE_TIME::DATE < k.M_HI, v.CR, 0)), 4) AS CREDITS_STORED_OFFSET_MONTH,
       ROUND(SUM(IFF(v.USAGE_TIME >= TO_TIMESTAMP_TZ(TO_VARCHAR(k.M_LO))
                     AND v.USAGE_TIME < TO_TIMESTAMP_TZ(TO_VARCHAR(k.M_HI)), v.CR, 0)), 4) AS CREDITS_STRING_BOUNDS_THIS_SESSION,
       LISTAGG(DISTINCT TO_CHAR(v.USAGE_TIME, 'TZH:TZM'), ', ')
           WITHIN GROUP (ORDER BY TO_CHAR(v.USAGE_TIME, 'TZH:TZM')) AS STORED_OFFSETS_SEEN
FROM v
CROSS JOIN k
GROUP BY k.M_LO, k.M_HI;

-- C15 THE BOSS'S QUESTION, DONE CORRECTLY: last complete Central month, top 15 users by billed request credits
--     (TOKEN_CREDITS), every model each one was billed for, credits AND tokens by type. One scan of the unified view
--     (all three interfaces). Fixes vs the concept query: Central month by day key (not session-zone string bounds),
--     user = USER_ID (name shown from USERS, so a rename / re-create cannot split or merge a person), every numeric
--     leaf summed with no NULL arithmetic, no HAVING, a residual row for credits no model leaf explains (each user's
--     rows add back to USER_CREDITS), FLATTEN kept in its own CTEs (001072) and the credit and token sides joined
--     only after each is aggregated to (user, model) (no models x models fan-out), USD at the AI rate (display only).
--     BOSS_FORMULA_CREDITS = the concept query's arithmetic on the same rows, for a side-by-side.
--     If C11 / C13 show parent rows roll up their children, re-run with AND c.PARENT_REQUEST_ID IS NULL added to base.
WITH clk AS (
    SELECT DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS M_HI,
           DATEADD('month', -1, DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)) AS M_LO
),
rate AS (
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) AS AI_RATE
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
),
base AS (
    SELECT COALESCE(TO_VARCHAR(c.USER_ID), 'name:' || c.USER_NAME, '(no user)') AS USER_KEY,
           c.USER_ID, c.USER_NAME, LOWER(c.INTERFACE) AS INTERFACE,
           COALESCE(c.TOKEN_CREDITS, 0) AS TOKEN_CREDITS, c.CREDITS_GRANULAR, c.TOKENS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN clk k
    WHERE c.USAGE_TIME >= DATEADD('day', -75, CURRENT_TIMESTAMP())
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= k.M_LO
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE < k.M_HI
),
top_users AS (
    SELECT b.USER_KEY,
           MAX(b.USER_ID) AS USER_ID,
           MAX(b.USER_NAME) AS VIEW_USER_NAME,
           COUNT(*) AS REQUESTS,
           SUM(b.TOKEN_CREDITS) AS USER_CREDITS,
           LISTAGG(DISTINCT b.INTERFACE, '+') WITHIN GROUP (ORDER BY b.INTERFACE) AS INTERFACES,
           ROW_NUMBER() OVER (ORDER BY SUM(b.TOKEN_CREDITS) DESC, b.USER_KEY) AS USER_RANK
    FROM base b
    GROUP BY b.USER_KEY
    QUALIFY USER_RANK <= 15
),
cr_leaf AS (
    SELECT b.USER_KEY, b.INTERFACE, m.SEQ AS RID, m.KEY::VARCHAR AS MODEL_KEY,
           LOWER(COALESCE(l.KEY::VARCHAR, '(model total)')) AS LEAF,
           TRY_TO_DOUBLE(TO_VARCHAR(COALESCE(l.VALUE, m.VALUE))) AS V
    FROM base b,
         LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR) m,
         LATERAL FLATTEN(INPUT => m.VALUE, RECURSIVE => TRUE, OUTER => TRUE) l
    WHERE TRY_TO_DOUBLE(TO_VARCHAR(COALESCE(l.VALUE, m.VALUE))) IS NOT NULL
),
cr AS (
    SELECT x.USER_KEY, x.MODEL_KEY,
           COUNT(DISTINCT x.RID) AS REQUESTS_USING_MODEL,
           LISTAGG(DISTINCT x.INTERFACE, '+') WITHIN GROUP (ORDER BY x.INTERFACE) AS MODEL_INTERFACES,
           SUM(x.V) AS MODEL_CREDITS,
           SUM(IFF(x.LEAF = 'input', x.V, 0)) AS CR_INPUT,
           SUM(IFF(x.LEAF = 'cache_read_input', x.V, 0)) AS CR_CACHE_READ,
           SUM(IFF(x.LEAF = 'cache_write_input', x.V, 0)) AS CR_CACHE_WRITE,
           SUM(IFF(x.LEAF = 'output', x.V, 0)) AS CR_OUTPUT,
           SUM(IFF(x.LEAF IN ('input', 'cache_read_input', 'cache_write_input', 'output'), 0, x.V)) AS CR_OTHER
    FROM cr_leaf x
    GROUP BY x.USER_KEY, x.MODEL_KEY
),
tk_leaf AS (
    SELECT b.USER_KEY, m.KEY::VARCHAR AS MODEL_KEY,
           LOWER(COALESCE(l.KEY::VARCHAR, '(model total)')) AS LEAF,
           TRY_TO_DOUBLE(TO_VARCHAR(COALESCE(l.VALUE, m.VALUE))) AS V
    FROM base b,
         LATERAL FLATTEN(INPUT => b.TOKENS_GRANULAR) m,
         LATERAL FLATTEN(INPUT => m.VALUE, RECURSIVE => TRUE, OUTER => TRUE) l
    WHERE TRY_TO_DOUBLE(TO_VARCHAR(COALESCE(l.VALUE, m.VALUE))) IS NOT NULL
),
tk AS (
    SELECT y.USER_KEY, y.MODEL_KEY,
           SUM(y.V) AS TOKENS,
           SUM(IFF(y.LEAF = 'input', y.V, 0)) AS TK_INPUT,
           SUM(IFF(y.LEAF = 'cache_read_input', y.V, 0)) AS TK_CACHE_READ,
           SUM(IFF(y.LEAF = 'cache_write_input', y.V, 0)) AS TK_CACHE_WRITE,
           SUM(IFF(y.LEAF = 'output', y.V, 0)) AS TK_OUTPUT
    FROM tk_leaf y
    GROUP BY y.USER_KEY, y.MODEL_KEY
),
concept AS (
    SELECT b.USER_KEY, m.KEY::VARCHAR AS MODEL_KEY,
           SUM(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:input))
               + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:cache_read_input)), 0)
               + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:cache_write_input)), 0)
               + TRY_TO_DOUBLE(TO_VARCHAR(m.VALUE:output))) AS BOSS_FORMULA_CREDITS
    FROM base b,
         LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR) m
    GROUP BY 1, 2
),
by_model AS (
    SELECT COALESCE(cr.USER_KEY, tk.USER_KEY) AS USER_KEY,
           COALESCE(cr.MODEL_KEY, tk.MODEL_KEY) AS MODEL_KEY,
           cr.REQUESTS_USING_MODEL, cr.MODEL_INTERFACES, cr.MODEL_CREDITS,
           cr.CR_INPUT, cr.CR_CACHE_READ, cr.CR_CACHE_WRITE, cr.CR_OUTPUT, cr.CR_OTHER,
           tk.TOKENS, tk.TK_INPUT, tk.TK_CACHE_READ, tk.TK_CACHE_WRITE, tk.TK_OUTPUT
    FROM cr
    FULL OUTER JOIN tk ON tk.USER_KEY = cr.USER_KEY AND tk.MODEL_KEY = cr.MODEL_KEY
),
resid AS (
    SELECT t.USER_KEY,
           IFF(t.USER_CREDITS - COALESCE(SUM(bm.MODEL_CREDITS), 0) > 0,
               '(credits not broken down by model)', '(model leaves exceed TOKEN_CREDITS)') AS MODEL_KEY,
           t.USER_CREDITS - COALESCE(SUM(bm.MODEL_CREDITS), 0) AS MODEL_CREDITS
    FROM top_users t
    LEFT JOIN by_model bm ON bm.USER_KEY = t.USER_KEY
    GROUP BY t.USER_KEY, t.USER_CREDITS
    HAVING ABS(t.USER_CREDITS - COALESCE(SUM(bm.MODEL_CREDITS), 0)) > 0.0001
),
model_rows AS (
    SELECT bm.USER_KEY, bm.MODEL_KEY, bm.REQUESTS_USING_MODEL, bm.MODEL_INTERFACES, bm.MODEL_CREDITS,
           bm.CR_INPUT, bm.CR_CACHE_READ, bm.CR_CACHE_WRITE, bm.CR_OUTPUT, bm.CR_OTHER,
           bm.TOKENS, bm.TK_INPUT, bm.TK_CACHE_READ, bm.TK_CACHE_WRITE, bm.TK_OUTPUT
    FROM by_model bm
    UNION ALL
    SELECT r.USER_KEY, r.MODEL_KEY, NULL, NULL, r.MODEL_CREDITS,
           NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM resid r
),
users1 AS (
    SELECT s.USER_ID, s.NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS s
    QUALIFY ROW_NUMBER() OVER (PARTITION BY s.USER_ID ORDER BY s.CREATED_ON DESC NULLS LAST) = 1
)
SELECT t.USER_RANK,
       COALESCE(u.NAME, t.VIEW_USER_NAME, 'UNKNOWN (' || t.USER_KEY || ')') AS USER_NAME,
       t.USER_ID,
       t.INTERFACES AS USER_INTERFACES,
       t.REQUESTS AS USER_REQUESTS,
       ROUND(t.USER_CREDITS, 4) AS USER_CREDITS,
       ROUND(t.USER_CREDITS * r.AI_RATE, 2) AS USER_USD_AT_AI_RATE,
       x.MODEL_KEY,
       x.MODEL_INTERFACES,
       x.REQUESTS_USING_MODEL,
       ROUND(x.MODEL_CREDITS, 4) AS MODEL_CREDITS,
       ROUND(100 * x.MODEL_CREDITS / NULLIF(t.USER_CREDITS, 0), 2) AS PCT_OF_USER_CREDITS,
       ROUND(x.MODEL_CREDITS * r.AI_RATE, 2) AS MODEL_USD_AT_AI_RATE,
       ROUND(cp.BOSS_FORMULA_CREDITS, 4) AS BOSS_FORMULA_CREDITS,
       ROUND(x.MODEL_CREDITS - COALESCE(cp.BOSS_FORMULA_CREDITS, 0), 4) AS MISSED_BY_BOSS_FORMULA,
       ROUND(x.CR_INPUT, 4) AS CR_INPUT,
       ROUND(x.CR_CACHE_READ, 4) AS CR_CACHE_READ,
       ROUND(x.CR_CACHE_WRITE, 4) AS CR_CACHE_WRITE,
       ROUND(x.CR_OUTPUT, 4) AS CR_OUTPUT,
       ROUND(x.CR_OTHER, 4) AS CR_OTHER,
       ROUND(x.TOKENS, 0) AS TOKENS,
       ROUND(x.TK_INPUT, 0) AS TK_INPUT,
       ROUND(x.TK_CACHE_READ, 0) AS TK_CACHE_READ,
       ROUND(x.TK_CACHE_WRITE, 0) AS TK_CACHE_WRITE,
       ROUND(x.TK_OUTPUT, 0) AS TK_OUTPUT,
       -- the app's cache-hit formula (wave2.token_economics): cache_read / (cache_read + input)
       ROUND(100 * x.TK_CACHE_READ
             / NULLIF(COALESCE(x.TK_INPUT, 0) + COALESCE(x.TK_CACHE_READ, 0), 0), 1) AS CACHE_HIT_PCT
FROM top_users t
JOIN model_rows x ON x.USER_KEY = t.USER_KEY
LEFT JOIN concept cp ON cp.USER_KEY = x.USER_KEY AND cp.MODEL_KEY = x.MODEL_KEY
LEFT JOIN users1 u ON u.USER_ID = t.USER_ID
CROSS JOIN rate r
ORDER BY t.USER_RANK, x.MODEL_CREDITS DESC NULLS LAST, x.MODEL_KEY;

-- C18 THE v4.612.0 APP READ, aggregated: the exact CTE chain of cortex_sql.coco_model_usage_daily('ALL') (the panel's
--     ONE live read), with a one-row summary in place of its final SELECT. Note the elapsed time Snowsight shows.
-- DECIDES: ROWS_THE_APP_FETCHES well under 200,000 (the app's cap) and an elapsed time under the 180 s historical
--     tier. REQUESTS = the sum of C2 ROWS_365D and TOKEN_CREDITS_TOTAL = the sum of C2 CREDITS_365D (each request
--     counted once, on its main model). NOT_ATTRIBUTED_TO_A_MODEL about 0 = the model split adds up (C7); a large value
--     = the panel's '(not attributed to a model)' row will be large. MODEL_LEAF_TOKENS_TOTAL about TOKENS_TOTAL = the
--     token grain adds up. ROWS_WITHOUT_ROLE = rows whose Roles drill reads '(not recorded)'.
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
    WHERE C.USAGE_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
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
    WHERE ARRAY_SIZE(OBJECT_KEYS(B.CREDITS_GRANULAR)) > 0
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
SELECT COUNT(*) AS ROWS_THE_APP_FETCHES,
       COUNT(DISTINCT n.USER_NAME) AS USERS,
       COUNT(DISTINCT n.MODEL_NAME) AS MODEL_KEYS,
       COUNT(DISTINCT n.ROLE_NAME) AS ROLES,
       SUM(n.MAIN_REQUESTS) AS REQUESTS,
       SUM(n.REQUESTS_USING) AS REQUEST_MODEL_PAIRS,
       ROUND(SUM(n.REQUEST_TOKEN_CREDITS), 4) AS TOKEN_CREDITS_TOTAL,
       ROUND(SUM(n.COCO_CREDITS), 4) AS MODEL_LEAF_CREDITS_TOTAL,
       ROUND(SUM(n.REQUEST_TOKEN_CREDITS) - SUM(n.COCO_CREDITS), 4) AS NOT_ATTRIBUTED_TO_A_MODEL,
       ROUND(SUM(n.COCO_CREDITS_OTHER), 4) AS CREDITS_IN_NON_OBJECT_ENTRIES,
       ROUND(SUM(IFF(n.SOURCE = 'Desktop', n.REQUEST_TOKEN_CREDITS, 0)), 4) AS DESKTOP_TOKEN_CREDITS,
       ROUND(SUM(IFF(n.MODEL_NAME = '(no model breakdown)', n.REQUEST_TOKEN_CREDITS, 0)), 4) AS NO_BREAKDOWN_TOKEN_CREDITS,
       SUM(n.REQUEST_TOKENS) AS TOKENS_TOTAL,
       SUM(n.TOKENS_INPUT + n.TOKENS_CACHE_READ + n.TOKENS_CACHE_WRITE + n.TOKENS_OUTPUT + n.TOKENS_OTHER) AS MODEL_LEAF_TOKENS_TOTAL,
       COUNT_IF(n.ROLE_NAME = '(not recorded)') AS ROWS_WITHOUT_ROLE,
       MIN(n.USAGE_DATE) AS FIRST_DAY,
       MAX(n.USAGE_DATE) AS LAST_DAY
FROM named n;

-- C16 RISKY (reads CORTEX_CODE_DESKTOP_USAGE_HISTORY by name). The unified view vs the three per-interface views,
--     the last 30 COMPLETE Central days (a fixed window, so latency cannot make the two sides disagree).
-- DECIDES: every *_DIFF = 0 = SNOWFLAKE_COCO_USAGE_HISTORY is exactly the union (no double counting, nothing
--     extra): v4.612's Snowsight + CLI subtotal then equals the AI users tab, and the later Desktop fold (V176) may
--     read the unified view. An error on the desktop view = it is not readable on its own: delete the desktop branch,
--     re-run, and report the error.
WITH clk AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_CT
),
u AS (
    SELECT LOWER(c.INTERFACE) AS INTERFACE, COUNT(*) AS N, COUNT(DISTINCT c.REQUEST_ID) AS REQ,
           SUM(COALESCE(c.TOKEN_CREDITS, 0)) AS CR, SUM(COALESCE(c.TOKENS, 0)) AS TK
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN clk k
    WHERE c.USAGE_TIME >= DATEADD('day', -40, CURRENT_TIMESTAMP())
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= DATEADD('day', -30, k.TODAY_CT)
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE < k.TODAY_CT
    GROUP BY 1
),
p_raw AS (
    SELECT 'snowsight' AS INTERFACE, s.REQUEST_ID, s.USAGE_TIME, s.TOKEN_CREDITS, s.TOKENS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY s
    WHERE s.USAGE_TIME >= DATEADD('day', -40, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT 'cli', l.REQUEST_ID, l.USAGE_TIME, l.TOKEN_CREDITS, l.TOKENS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY l
    WHERE l.USAGE_TIME >= DATEADD('day', -40, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT 'desktop', d.REQUEST_ID, d.USAGE_TIME, d.TOKEN_CREDITS, d.TOKENS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_DESKTOP_USAGE_HISTORY d
    WHERE d.USAGE_TIME >= DATEADD('day', -40, CURRENT_TIMESTAMP())
),
p AS (
    SELECT r.INTERFACE, COUNT(*) AS N, COUNT(DISTINCT r.REQUEST_ID) AS REQ,
           SUM(COALESCE(r.TOKEN_CREDITS, 0)) AS CR, SUM(COALESCE(r.TOKENS, 0)) AS TK
    FROM p_raw r
    CROSS JOIN clk k
    WHERE CONVERT_TIMEZONE('America/Chicago', r.USAGE_TIME)::DATE >= DATEADD('day', -30, k.TODAY_CT)
      AND CONVERT_TIMEZONE('America/Chicago', r.USAGE_TIME)::DATE < k.TODAY_CT
    GROUP BY 1
)
SELECT COALESCE(u.INTERFACE, p.INTERFACE) AS INTERFACE,
       u.N AS UNIFIED_ROWS, p.N AS PER_VIEW_ROWS, COALESCE(u.N, 0) - COALESCE(p.N, 0) AS ROW_DIFF,
       u.REQ AS UNIFIED_REQUEST_IDS, p.REQ AS PER_VIEW_REQUEST_IDS,
       ROUND(u.CR, 6) AS UNIFIED_CREDITS, ROUND(p.CR, 6) AS PER_VIEW_CREDITS,
       ROUND(COALESCE(u.CR, 0) - COALESCE(p.CR, 0), 6) AS CREDIT_DIFF,
       COALESCE(u.TK, 0) - COALESCE(p.TK, 0) AS TOKEN_DIFF
FROM u
FULL OUTER JOIN p ON p.INTERFACE = u.INTERFACE
ORDER BY 1;

-- C17 RISKY, OPTIONAL (the heaviest block: four views, last 7 complete Central days). Request-level set difference:
--     rows in the unified view that no per-interface view has, and the reverse.
-- DECIDES: 'no rows' = row-for-row equality (EXCEPT is set-based, so duplicates are judged by C16's counts).
WITH clk AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_CT
),
u_raw AS (
    SELECT LOWER(c.INTERFACE) AS INTERFACE, c.REQUEST_ID, c.USER_ID, c.USAGE_TIME, c.TOKEN_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    WHERE c.USAGE_TIME >= DATEADD('day', -10, CURRENT_TIMESTAMP())
),
p_raw AS (
    SELECT 'snowsight' AS INTERFACE, s.REQUEST_ID, s.USER_ID, s.USAGE_TIME, s.TOKEN_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY s
    WHERE s.USAGE_TIME >= DATEADD('day', -10, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT 'cli', l.REQUEST_ID, l.USER_ID, l.USAGE_TIME, l.TOKEN_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY l
    WHERE l.USAGE_TIME >= DATEADD('day', -10, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT 'desktop', d.REQUEST_ID, d.USER_ID, d.USAGE_TIME, d.TOKEN_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_DESKTOP_USAGE_HISTORY d
    WHERE d.USAGE_TIME >= DATEADD('day', -10, CURRENT_TIMESTAMP())
),
u AS (
    SELECT x.INTERFACE, x.REQUEST_ID, x.USER_ID, x.USAGE_TIME, x.TOKEN_CREDITS
    FROM u_raw x
    CROSS JOIN clk k
    WHERE CONVERT_TIMEZONE('America/Chicago', x.USAGE_TIME)::DATE >= DATEADD('day', -7, k.TODAY_CT)
      AND CONVERT_TIMEZONE('America/Chicago', x.USAGE_TIME)::DATE < k.TODAY_CT
),
p AS (
    SELECT y.INTERFACE, y.REQUEST_ID, y.USER_ID, y.USAGE_TIME, y.TOKEN_CREDITS
    FROM p_raw y
    CROSS JOIN clk k
    WHERE CONVERT_TIMEZONE('America/Chicago', y.USAGE_TIME)::DATE >= DATEADD('day', -7, k.TODAY_CT)
      AND CONVERT_TIMEZONE('America/Chicago', y.USAGE_TIME)::DATE < k.TODAY_CT
)
SELECT 'in the unified view only' AS SIDE, a.INTERFACE, COUNT(*) AS ROWS_7D,
       ROUND(SUM(COALESCE(a.TOKEN_CREDITS, 0)), 6) AS CREDITS_7D
FROM (SELECT * FROM u EXCEPT SELECT * FROM p) a
GROUP BY 1, 2
UNION ALL
SELECT 'in a per-interface view only', z.INTERFACE, COUNT(*),
       ROUND(SUM(COALESCE(z.TOKEN_CREDITS, 0)), 6)
FROM (SELECT * FROM p EXCEPT SELECT * FROM u) z
GROUP BY 1, 2
ORDER BY 1, 2;

-- Z1 OWNER SWITCH: the same read as the app's future owner, with NO secondary roles. The concept query ran as
--    SNOW_SYSADMINS in Snowsight, where a user's default secondary roles can widen access, so it does not prove the
--    role alone can read this view. An owner's-rights app has no secondary roles. Expect numbers. An error = a grant
--    gap for the SNOW_SYSADMINS cutover (roles.sql:16 should already cover it via IMPORTED PRIVILEGES). Skip Z1 if
--    your user does not hold SNOW_SYSADMINS ("Requested role ... is not assigned" is then the answer).
USE ROLE SNOW_SYSADMINS;
USE SECONDARY ROLES NONE;
SELECT CURRENT_ROLE() AS ROLE_NAME,
       CURRENT_SECONDARY_ROLES() AS SECONDARY,
       COUNT(*) AS ROWS_7D,
       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 4) AS CREDITS_7D,
       COUNT_IF(LOWER(c.INTERFACE) = 'desktop') AS DESKTOP_ROWS_7D
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
WHERE c.USAGE_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP());
USE ROLE SNOW_ACCOUNTADMINS;
-- End. Optional: USE SECONDARY ROLES ALL restores the Snowsight default for this worksheet.
