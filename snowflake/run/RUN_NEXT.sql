-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  TIMEZONE DIAGNOSTIC (read-only). Gates the app-wide timestamp-consistency
--  fix: "when did X run" reads differently section to section because the
--  deployed SiS app CANNOT ALTER SESSION (owner's-rights no-op), so its clock =
--  the ACCOUNT default TIMEZONE, unless a warehouse it runs on overrides it.
--  These reads tell us which fix applies:
--    * account is NOT America/Chicago  -> ALTER ACCOUNT SET TIMEZONE (one lever
--      that moves the whole SiS clock) + pin the loader tasks + one mart reload.
--    * account IS already America/Chicago -> the flip is a no-op; the drift lives
--      only in NTZ marts frozen during an earlier non-Central load -> loader
--      re-derive + reload (no account change).
--  100% READ-ONLY: nothing is created/altered/dropped. Run as SNOW_ACCOUNTADMINS.
--  Paste back all three grids (esp. the value + level columns of STEP 1/2).
--
--  (NOTE for Claude/owner: this REPLACED the prior V146 apply + backfill script
--   on runbox. If V146 (the Cortex canonical repoint) has NOT been applied yet,
--   say so and it'll be re-staged -- the migration file is safe in the repo.)
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;

-- ---- STEP 1 (KEY): the ACCOUNT default TIMEZONE ---------------------------
-- Read the "value" AND "level" columns:
--   * level = ACCOUNT              -> it's explicitly set (to whatever value shows)
--   * level = <blank>             -> it's the Snowflake OUT-OF-BOX default,
--                                    which is America/Los_Angeles (NOT UTC)
-- The SiS app inherits this unless a warehouse below overrides it.
SHOW PARAMETERS LIKE 'TIMEZONE' IN ACCOUNT;

-- ---- STEP 2: WAREHOUSE-level overrides -----------------------------------
-- The app + the mart-loader tasks run on these warehouses. A warehouse-level
-- TIMEZONE (level = WAREHOUSE) BEATS the account default for anything running
-- there -- so if either shows a value, that's the app's / loader's real clock.
SHOW PARAMETERS LIKE 'TIMEZONE' IN WAREHOUSE WH_ALFA_QUERY;
SHOW PARAMETERS LIKE 'TIMEZONE' IN WAREHOUSE WH_ALFA_ADMIN;

-- ---- STEP 3: this worksheet's effective zone (reference) ------------------
-- Confirms the Snowsight session zone (we saw +0000 / UTC earlier). Not the SiS
-- session, but a useful cross-check against STEP 1/2.
SELECT CURRENT_TIMEZONE() AS SESSION_TZ, CURRENT_TIMESTAMP() AS NOW_WALLCLOCK;
