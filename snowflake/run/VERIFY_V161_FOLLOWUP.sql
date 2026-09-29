-- =====================================================================
--  OVERWATCH -- VERIFY_V161_FOLLOWUP.sql  (READ-ONLY)
--  The V161 PART B grid that was still pending: V161.13, the change-risk footprint of the V161 apply
--  (2026-09-28). Moved out of RUN_NEXT.sql when RUN_NEXT was reused for wave 4 (V162-V165). V161.1-.12 were
--  the apply-time grids and are done.
--  RUN_NEXT's copy read EVENT_TS >= now - 3h, so it only worked within ~3h of the apply (run early it showed only
--  the SUSPEND row). This copy anchors the window on V161's own SCHEMA_VERSION row instead (APPLIED_AT, Central
--  wall clock as that session stamped it; the in-flight wait and the DROPs ran in the minutes before it), so it
--  works any day. Paste every grid back; an empty grid is an answer ('no rows').
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;

-- (V161.13a) the footprint. Expect 3 DESTRUCTIVE / CRITICAL rows (the task, the proc, the schema) and MEDIUM ALTER
--            rows for the SUSPEND and the renames; a move that fell back to a DROP (likeliest: the permanent
--            OPERATOR_BACKUP_LOG) shows as one more CRITICAL row. Never delete these rows. The Security page shows
--            CHANGE RISK "Act" for 7 days from the apply.
SELECT f.CHANGE_KIND, f.RISK_LEVEL, COUNT(*) AS ROWS_IN_APPLY_WINDOW, MIN(LEFT(f.QUERY_PREVIEW, 90)) AS EXAMPLE
FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE f
CROSS JOIN (SELECT MAX(APPLIED_AT) AS APPLIED_AT
            FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
            WHERE VERSION = 161) a
WHERE CONVERT_TIMEZONE('America/Chicago', f.EVENT_TS)::TIMESTAMP_NTZ
      BETWEEN DATEADD('minute', -60, a.APPLIED_AT) AND DATEADD('minute', 15, a.APPLIED_AT)
  AND (CONTAINS(UPPER(f.QUERY_PREVIEW), 'OVERWATCH_BAK')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), 'TASK_BACKUP_OPERATOR')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), 'SP_BACKUP_OPERATOR_TABLES')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), 'OPERATOR_BACKUP_LOG')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), '_BAK_LAST'))
GROUP BY f.CHANGE_KIND, f.RISK_LEVEL
ORDER BY f.CHANGE_KIND, f.RISK_LEVEL;

-- (V161.13b) the same rows one by one (at most 60), for the record: who ran each, when (Central) and its score.
SELECT CONVERT_TIMEZONE('America/Chicago', f.EVENT_TS)::TIMESTAMP_NTZ AS EVENT_CT, f.USER_NAME, f.ROLE_NAME,
       f.QUERY_TYPE, f.CHANGE_KIND, f.RISK_SCORE, f.RISK_LEVEL, LEFT(f.QUERY_PREVIEW, 120) AS QUERY_PREVIEW
FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE f
CROSS JOIN (SELECT MAX(APPLIED_AT) AS APPLIED_AT
            FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
            WHERE VERSION = 161) a
WHERE CONVERT_TIMEZONE('America/Chicago', f.EVENT_TS)::TIMESTAMP_NTZ
      BETWEEN DATEADD('minute', -60, a.APPLIED_AT) AND DATEADD('minute', 15, a.APPLIED_AT)
  AND (CONTAINS(UPPER(f.QUERY_PREVIEW), 'OVERWATCH_BAK')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), 'TASK_BACKUP_OPERATOR')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), 'SP_BACKUP_OPERATOR_TABLES')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), 'OPERATOR_BACKUP_LOG')
       OR CONTAINS(UPPER(f.QUERY_PREVIEW), '_BAK_LAST'))
ORDER BY f.EVENT_TS
LIMIT 60;
