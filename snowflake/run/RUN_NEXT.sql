-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (FIX PREP: object-cost loader broke on a Snowflake
--  ACCOUNT_USAGE schema change -- get the current view columns so the fix compiles)
--
--  ROOT CAUSE FOUND (from the prior RUN_NEXT results): SP_LOAD_OBJECT_COST fails
--  every night with
--      SQL compilation error: ... invalid identifier 'TABLE_NAME'
--  It is NOT a timeout (account 21600s / WH 1800s / task 3600000ms are all healthy)
--  and NOT an OVERWATCH code change (proc unchanged since V067). Snowflake changed
--  the columns of one or more ACCOUNT_USAGE views the loader reads, so its
--      ... || COALESCE(TABLE_NAME, 'UNKNOWN')
--  no longer compiles. The atomic load rolls back -> the 2026-09-09 fill is retained.
--
--  GOAL: list the CURRENT columns of the 5 "direct arm" views the proc reads, so the
--  fix targets the real column names (was TABLE_NAME renamed, or removed?). The proc
--  uses TABLE_NAME in [A],[B],[C]; TASK_NAME in [D]; PIPE_NAME in [E].
--  READ-ONLY. Run each; paste each column list back (especially [A],[B],[C]).
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;      -- reading the shared SNOWFLAKE.ACCOUNT_USAGE views
USE WAREHOUSE WH_ALFA_QUERY;      -- DESCRIBE needs no real compute; any running WH is fine

-- [A] The view the error points at (clustering arm, first TABLE_NAME reference).
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY;
--  >>> paste RESULT [A] (the "name" column is enough) <<<

-- [B] Materialized-view refresh arm (also uses TABLE_NAME).
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.MATERIALIZED_VIEW_REFRESH_HISTORY;
--  >>> paste RESULT [B] <<<

-- [C] Search-optimization arm (also uses TABLE_NAME).
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY;
--  >>> paste RESULT [C] <<<

-- [D] Serverless-task arm (uses TASK_NAME -- confirm it still exists).
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.SERVERLESS_TASK_HISTORY;
--  >>> paste RESULT [D] <<<

-- [E] Snowpipe arm (uses PIPE_NAME -- confirm it still exists).
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.PIPE_USAGE_HISTORY;
--  >>> paste RESULT [E] <<<

-- If DESCRIBE VIEW is blocked on the shared DB for any of the above, use this instead
-- and read the column headers off the result grid:
--   SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY LIMIT 1;
