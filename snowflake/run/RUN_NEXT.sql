-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (DIAGNOSE: native Snowflake budget panel is empty)
--
--  After the PART B grants (BUDGET_VIEWER + IMPORTED PRIVILEGES) + a redeploy, the
--  Cost Intelligence > Contract & Forecast "Native Snowflake budget" panel still reads
--  empty. This isolates WHY: a privilege gap (the application role isn't active in the
--  app's owner's-rights context), a config gap (no account budget configured in Snowsight),
--  or the table function genuinely returning nothing.
--
--  Run each STEP as SNOW_ACCOUNTADMINS (the app's OWNING role -- same context the app runs
--  in). READ-ONLY: nothing is changed. Paste STEP 2's output back (and STEP 1 if short).
-- =====================================================================

-- ---- STEP 1: are the grants actually on the app's role? --------------------
-- Look in the output for:  APPLICATION ROLE  SNOWFLAKE.BUDGET_VIEWER
--                    and:  IMPORTED PRIVILEGES  on DATABASE SNOWFLAKE
USE ROLE SNOW_ACCOUNTADMINS;
SHOW GRANTS TO ROLE SNOW_ACCOUNTADMINS;

-- ---- STEP 2 (KEY): the app's EXACT read, run directly as the owning role ----
-- Three possible outcomes -- tell me which:
--   (a) an ERROR (e.g. "... does not exist or not authorized")  -> paste the full error
--   (b) "0 rows"                                                 -> budget not configured
--   (c) rows come back                                          -> works for the role but not the app
USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_ADMIN;
SELECT SERVICE_TYPE, SUM(CREDITS_USED) AS CREDITS
FROM TABLE(SNOWFLAKE.LOCAL.ACCOUNT_ROOT_BUDGET!GET_SERVICE_TYPE_USAGE_V2('2026-09', '2026-09'))
GROUP BY SERVICE_TYPE
ORDER BY CREDITS DESC;

-- ---- STEP 3: is a spending limit even set on the account budget? ------------
-- NULL or an error => no account budget is configured. Set one in Snowsight >
-- Admin > Cost Management > Budgets (the account budget), then the panel populates.
USE ROLE SNOW_ACCOUNTADMINS;
CALL SNOWFLAKE.LOCAL.ACCOUNT_ROOT_BUDGET!GET_SPENDING_LIMIT();
