-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  PROBE: pin the CORTEX_AI_FUNCTIONS_USAGE_HISTORY schema before repointing
--  the AI-cost reads onto it (the canonical Cortex AI-functions usage view).
--
--  WHY: we're moving OVERWATCH's AI-cost reads off the FROZEN
--  ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY (the mart loader) and off
--  CORTEX_AISQL_USAGE_HISTORY (cortex_model_costs) onto the canonical
--  CORTEX_AI_FUNCTIONS_USAGE_HISTORY. That view is NOT a drop-in: it exposes
--  CREDITS (not TOKEN_CREDITS) and has NO scalar TOKENS column -- token counts
--  live inside a METRICS ARRAY whose element structure the docs don't fully
--  specify. This probe pins the exact columns + the METRICS shape so the
--  token-sum and the credits-per-1M-token math get written correctly, not guessed.
--
--  Run as SNOW_ACCOUNTADMINS (the app's OWNING role -- same context the app runs
--  in). 100% READ-ONLY: nothing is created, altered or dropped.
--  Paste back: STEP 1 (grid), a few rows of STEP 2, all of STEP 3, and STEP 4.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_ADMIN;

-- ---- STEP 1: the exact column list + types --------------------------------
-- Confirm three things from the grid:
--   * CREDITS         -> exact name + it's a NUMBER (vs TOKEN_CREDITS)
--   * START_TIME      -> TIMESTAMP_LTZ vs TIMESTAMP_NTZ  (tells me whether the
--                        loader's FIRST_TS/LAST_TS need a ::TIMESTAMP_NTZ cast)
--   * METRICS         -> it's an ARRAY
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY;

-- ---- STEP 2: a few raw rows (sanity: is there data in-window?) -------------
-- If 0 rows, note it -- data only exists on/after 2026-01-05. If MODEL_NAME is
-- blank for some functions that's expected (empty when not applicable).
SELECT START_TIME, FUNCTION_NAME, MODEL_NAME, CREDITS, METRICS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
WHERE START_TIME >= DATEADD('day', -60, CURRENT_TIMESTAMP())
  AND METRICS IS NOT NULL
LIMIT 5;

-- ---- STEP 3 (KEY): the METRICS array element structure ---------------------
-- FLATTEN exposes each element's exact JSON. I need the KEY NAMES that carry the
-- token count -- e.g. {"name":"tokens","value":N} vs
-- {"metric":{"type":"tokens","unit":"token"},"value":N} vs something else.
-- Paste the whole METRIC_ELEMENT column (the JSON is what matters).
SELECT f.value AS METRIC_ELEMENT
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY,
     LATERAL FLATTEN(input => METRICS) f
WHERE START_TIME >= DATEADD('day', -60, CURRENT_TIMESTAMP())
LIMIT 25;

-- ---- STEP 4: the DISTINCT set of element key-layouts -----------------------
-- OBJECT_KEYS lists each element's top-level keys, so I definitively know the
-- layout (and whether inputs/outputs are separate metric rows). Paste the grid.
SELECT
    ARRAY_TO_STRING(OBJECT_KEYS(f.value), ', ') AS ELEMENT_KEYS,
    COUNT(*)                                    AS N
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY,
     LATERAL FLATTEN(input => METRICS) f
WHERE START_TIME >= DATEADD('day', -60, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY N DESC;
