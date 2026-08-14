-- OVERWATCH Streamlit warehouse-runtime reset
-- Owner-run worksheet: execute as the role that owns OVERWATCH_APP.
--
-- ARTIFACT_REPOSITORIES is a container-runtime dependency setting. Snowflake
-- keeps it on the Streamlit object even when snowflake.yml omits it, and the
-- App settings dialog cannot switch to warehouse runtime until it is removed.
-- This is app-object configuration only: no OVERWATCH tables or data change.

ALTER STREAMLIT IF EXISTS DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP
  UNSET ARTIFACT_REPOSITORIES;

ALTER STREAMLIT IF EXISTS DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP SET
  RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME',
  QUERY_WAREHOUSE = WH_ALFA_ADMIN;

-- Verify in Snowsight: App settings -> Execution should show
-- "Run on warehouse (legacy)" and WH_ALFA_ADMIN.
