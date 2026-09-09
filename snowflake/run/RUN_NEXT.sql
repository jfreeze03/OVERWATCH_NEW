-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (DIAGNOSTIC: "Reference-data gaps" banner)
--
--  Settled so far:
--    - SNOW_ACCOUNTADMINS reads the staging + XLAT tables and the MINUS
--      returns a real gap (HOEvaluateFarmExposure_alfa_alfa). Confirmed.
--    - The app owns owner's-rights AS SNOW_ACCOUNTADMINS, so it executes
--      as that same role. Access / grants are NOT the cause. Ruled out.
--
--  What is left: the panel does NOT run one check -- it builds a UNION ALL
--  of EVERY row in the ETL_REF_GAP_CHECKS setting. A row whose staging
--  table does not exist (typo, wrong schema, dropped) is NOT skipped, so
--  the WHOLE scan throws "does not exist or not authorized" and the panel
--  shows the setup/grant banner -- even though pc_uwissuetype works alone.
--
--  These two reads show exactly what the deployed app is configured with.
--  Run All as SNOW_ACCOUNTADMINS; paste BOTH RESULT blocks back into chat.
--
--  NOTE: this replaces the earlier V125-V127 migration handoff. That SQL is
--  preserved in runbox git history (commit 8a5f6f6) and every migration
--  still lives in snowflake/migrations/ -- the owner-apply backlog is
--  untouched; this file is only borrowing RUN_NEXT.sql for a diagnostic.
-- =====================================================================

-- 1) Is the ref-gap seed (V128) applied?  If MAX(VERSION) < 128, nothing
--    seeded the config -- so the values in (2) were entered by hand in
--    Admin, and a hand-typed staging table is the prime suspect.
-- RESULT:
SELECT MAX(VERSION) AS SCHEMA_VERSION
  FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;

-- 2) The EXACT config the deployed app reads. In ETL_REF_GAP_CHECKS, each
--    '|' row is one check the scan UNIONs together, format:
--        [*]<name> | <staging_fqn> | <staging_code_col>
--    If there is MORE than the one pinned '*pc_uwissuetype.code' row, the
--    extra row's staging table is the suspect -- verify it exists and is
--    spelled exactly right. (Your proven-good row is:
--     *pc_uwissuetype.code | ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE | CODE_STG)
-- RESULT:
SELECT KEY, VALUE
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY IN ('ETL_REF_GAP_XLAT', 'ETL_REF_GAP_CHECKS')
 ORDER BY KEY;
