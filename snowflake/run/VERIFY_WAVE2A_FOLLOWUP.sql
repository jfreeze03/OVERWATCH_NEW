-- =====================================================================================
--  VERIFY_WAVE2A_FOLLOWUP.sql  --  read-only follow-up to the V151-V155 apply (2026-09-26)
--  Nothing here writes. Run top to bottom and paste back every grid (an empty grid is an answer).
--
--  (1) V152.1 re-check. The RUN_NEXT grid read SRCMAP_OK / QH_STAMP_OK = FALSE, but both fragments
--      are in the V152 procedure bodies verbatim, and the MART_TASK_NODE_DAILY view row (also V152)
--      did land. The likely cause is the CHECK, not the migration: GET_DDL returns a SQL procedure
--      body re-quoted as one string literal, so every ' inside the body comes back as ''. A fragment
--      that contains a quote then never matches. This grid asks both ways; expect the *_QUOTES_DOUBLED
--      pair TRUE (and DDL_AROUND_STAMP to show ''MART_CLOUD_SVC_DAILY - ...'' with doubled quotes).
--  (2) V152.2 -- run AFTER the next hourly run (about :10-:30 CT); empty before it is normal.
--  (3) The V154 / V155 / registered grids from RUN_NEXT that were not in the screenshots.
--  (4) Context only: what the 3,689 CRITICAL + 2,980 HIGH CHANGE RISK rows are (7-day window,
--      RISK_SCORE >= 70; all from before V151, which added 0 rows).
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- (1) V152.1 with both quote formats
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'),
                '(''MART_TASK_NODE_DAILY'', ''task_node'')') AS SRCMAP_AS_WRITTEN,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'),
                '(''''MART_TASK_NODE_DAILY'''', ''''task_node'''')') AS SRCMAP_QUOTES_DOUBLED,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(FLOAT)'),
                'SELECT ''MART_CLOUD_SVC_DAILY'', MAX(LOAD_TS)') AS QH_STAMP_AS_WRITTEN,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(FLOAT)'),
                'SELECT ''''MART_CLOUD_SVC_DAILY'''', MAX(LOAD_TS)') AS QH_STAMP_QUOTES_DOUBLED;
SELECT SUBSTR(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(FLOAT)'),
              POSITION('MART_CLOUD_SVC_DAILY' IN GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(FLOAT)')) - 20,
              90) AS DDL_AROUND_STAMP;

-- (2) V152.2 -- AFTER the next hourly run: STATUS 'loader' for MART_CLOUD_SVC_DAILY, 'task_node' for MART_TASK_NODE_DAILY
SELECT SOURCE_NAME, LAST_LOAD_TS, STATUS, GENERATION FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('MART_CLOUD_SVC_DAILY', 'MART_TASK_NODE_DAILY');

-- (3) remaining RUN_NEXT PART B grids (V154 tail, V155, registered)
SELECT STATUS, COUNT(*) AS N, COUNT(ACK_AT) AS ACKED, COUNT(MITIGATED_AT) AS MITIGATED, COUNT(MITIGATED_BY) AS MACHINE_MITIGATED
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS GROUP BY 1 ORDER BY 1;   -- M ~= probe C5 (minus any inside the 1h dwell or with a live successor)
SELECT LINKED_BY, AUTO_LINKED, COUNT(*) AS N FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
WHERE LINKED_AT >= DATEADD('hour', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
GROUP BY 1, 2 ORDER BY 1, 2;   -- attached members show LINKED_BY = SP_INCIDENT_AUTODECLARE
SELECT LOGGED_AT, ERROR_TYPE, LEFT(ERROR_MESSAGE, 200) AS MSG FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE ERROR_TYPE IN ('incident_attach_failed', 'incident_mitigate_failed')
  AND LOGGED_AT >= DATEADD('hour', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
ORDER BY 1 DESC;   -- 0 rows

-- ---- V155 checks -----------------------------------------------------
-- (V155.1) the collector's candidate cursor now skips statements carrying the SiS app tag (TRUE)
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(FLOAT)'),
                '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"') AS SIS_APP_TAG_EXCLUDED,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(FLOAT)'),
                'SCHEMA_NAME = qh.SCHEMA_NAME') AS V147_IDENTITY_STAMP_KEPT;
-- (V155.2) app statements the collector already landed. They are NOT removed (no backfill) and
-- age out inside the 30-day retention, so this should trend to 0; no new ones should appear.
SELECT COUNT(DISTINCT f.QUERY_ID) AS APP_QUERIES_STILL_IN_FACT, MAX(qh.START_TIME) AS NEWEST_APP_QUERY
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
  ON qh.QUERY_ID = f.QUERY_ID
 AND qh.START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP())
WHERE CONTAINS(COALESCE(qh.QUERY_TAG, ''), '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"');

-- (all) V151 through V155 are registered (5 rows).
SELECT VERSION, APPLIED_AT
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION BETWEEN 151 AND 155 ORDER BY VERSION;

-- (4) context: what fills CHANGE RISK today (read-only; top 25 kind / role groups)
SELECT SEVERITY, SPLIT_PART(TITLE, ':', 1) AS CHANGE_KIND, SPLIT_PART(DETAIL, ' via ', 2) AS ROLE_NAME,
       COUNT(*) AS N, MIN(DETECTED_AT) AS FIRST_SEEN, MAX(DETECTED_AT) AS LAST_SEEN
FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE
WHERE DOMAIN = 'CHANGE RISK'
GROUP BY ALL ORDER BY N DESC LIMIT 25;

ALTER SESSION UNSET TIMEZONE;
