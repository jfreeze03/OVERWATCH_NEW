-- 03_roles.sql — BYTE-IDENTICAL copy of snowflake/roles.sql (locked by
-- tests/test_rebuild_bundle.py); numbered for the rebuild order.

-- roles.sql — OVERWATCH access. Owner decision 2026-10-05 (supersedes 2026-07-13's
-- "SNOW_ACCOUNTADMINS + SNOW_SYSADMINS, period"): four roles can OPEN the app --
-- SNOW_ACCOUNTADMINS, SNOW_SYSADMINS, SNOW_PRI_GFR_PRD_ALFA_DSA (admin) and
-- SNOW_PRI_GFR_PRD_ALFA_DTI (read-only view). The app (4.610+) decides admin vs
-- view in-app; DSA/DTI get USAGE on the database, schema and app ONLY (no table,
-- view, warehouse or IMPORTED grants). The two SNOW_* roles keep their worksheet
-- grants below. The old OVERWATCH_MONITOR / OVERWATCH_OPERATOR layer is retired.
-- Run as SNOW_ACCOUNTADMINS (IMPORTED PRIVILEGES needs ACCOUNTADMIN-tier).

-- Retire the old layer if it exists (idempotent).
DROP ROLE IF EXISTS OVERWATCH_OPERATOR;
DROP ROLE IF EXISTS OVERWATCH_MONITOR;

-- ACCOUNT_USAGE access (live fallbacks + ad-hoc reads).
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SNOW_ACCOUNTADMINS;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SNOW_SYSADMINS;

-- App objects. DBA_MAINT_DB.OVERWATCH is shared with the previous app's
-- objects, so ALL/FUTURE grants cover those too (intentional, owner decision).
GRANT USAGE ON DATABASE DBA_MAINT_DB TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON DATABASE DBA_MAINT_DB TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_ACCOUNTADMINS;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS;
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_ACCOUNTADMINS;
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS;
GRANT SELECT ON ALL VIEWS IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_ACCOUNTADMINS;
GRANT SELECT ON ALL VIEWS IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_ACCOUNTADMINS;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(VARCHAR) TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(VARCHAR) TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(VARCHAR) TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(VARCHAR) TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(VARCHAR) TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(VARCHAR) TO ROLE SNOW_SYSADMINS;

-- Compute: the Streamlit app and loader/task work share WH_ALFA_ADMIN.
GRANT USAGE ON WAREHOUSE WH_ALFA_ADMIN TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON WAREHOUSE WH_ALFA_ADMIN TO ROLE SNOW_SYSADMINS;

-- Audit tables stay append-only even for the two admin roles (r27 #6 —
-- restores the r26 regression). Admins can technically re-grant; this
-- blocks accidents, not adversaries, same as the old model documented.
REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT     FROM ROLE SNOW_ACCOUNTADMINS;
REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT     FROM ROLE SNOW_SYSADMINS;
REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG FROM ROLE SNOW_ACCOUNTADMINS;
REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG FROM ROLE SNOW_SYSADMINS;

-- Validate the seal (Wave 3 #5). The named REVOKEs above only protect the
-- audit tables AS THEY EXIST RIGHT NOW. A restore (CREATE OR REPLACE TABLE ...
-- CLONE, or UNDROP) re-materializes the table and REAPPLIES the schema's FUTURE
-- grants — which include UPDATE/DELETE (lines 22-23) — silently reopening the
-- audit trail. Snowflake future grants are schema-wide (there is NO per-table
-- future grant), and operator tables in this schema legitimately need
-- UPDATE/DELETE, so a blanket REVOKE ... ON FUTURE TABLES would break them and
-- is the wrong tool. Instead, FAIL LOUDLY if any audit table carries an
-- UPDATE or DELETE grant to any role, so a restore can not silently reopen them.
-- Remediate by re-running the REVOKE statements above.
EXECUTE IMMEDIATE $$
DECLARE
  audit_writable EXCEPTION (-20021,
    'Append-only violation: ALERT_AUDIT and/or REMEDIATION_LOG carries an UPDATE or DELETE grant (a restore reapplied the schema FUTURE grant). Re-run the REVOKE UPDATE, DELETE statements above to reseal the audit trail.');
  writable INTEGER DEFAULT 0;
  c INTEGER;
BEGIN
  SHOW GRANTS ON TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT;
  SELECT COUNT(*) INTO :c FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
    WHERE "privilege" IN ('UPDATE', 'DELETE');
  writable := writable + c;
  SHOW GRANTS ON TABLE DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG;
  SELECT COUNT(*) INTO :c FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
    WHERE "privilege" IN ('UPDATE', 'DELETE');
  writable := writable + c;
  IF (writable > 0) THEN
    RAISE audit_writable;
  END IF;
  RETURN 'Audit tables sealed — no UPDATE/DELETE grants on ALERT_AUDIT or REMEDIATION_LOG.';
END;
$$;

-- ---------------------------------------------------------------------------
-- Streamlit USAGE re-grant (Wave 3 #4). The manual deploy path in DEPLOYMENT.md
-- (§3) uses CREATE OR REPLACE STREAMLIT with NO COPY GRANTS: it drops and
-- recreates the app object and DESTROYS every grant on it, so USAGE is lost on
-- every redeploy and any non-owning role gets "does not exist or not
-- authorized" until this runs. Run this block AFTER each CREATE OR REPLACE
-- STREAMLIT (or add COPY GRANTS to that statement). DATABASE + SCHEMA USAGE are
-- restated so the block is self-contained after an app/schema rebuild. After a
-- deploy, running this block (through the proof below) is all that is needed.
GRANT USAGE ON DATABASE  DBA_MAINT_DB                        TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON DATABASE  DBA_MAINT_DB                        TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON DATABASE  DBA_MAINT_DB                        TO ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
GRANT USAGE ON DATABASE  DBA_MAINT_DB                        TO ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;
GRANT USAGE ON SCHEMA    DBA_MAINT_DB.OVERWATCH              TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON SCHEMA    DBA_MAINT_DB.OVERWATCH              TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON SCHEMA    DBA_MAINT_DB.OVERWATCH              TO ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
GRANT USAGE ON SCHEMA    DBA_MAINT_DB.OVERWATCH              TO ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;
GRANT USAGE ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP TO ROLE SNOW_ACCOUNTADMINS;
GRANT USAGE ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP TO ROLE SNOW_SYSADMINS;
GRANT USAGE ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP TO ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
GRANT USAGE ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP TO ROLE SNOW_PRI_GFR_PRD_ALFA_DTI;

-- r27 #7 + Wave 3 #4 (four roles since 2026-10-05): prove the app object's USAGE
-- set — HARD FAIL. RAISEs if the app has any USAGE grantee other than the four
-- access roles (a user, a database or application role, or any other role), or
-- if it is missing USAGE for any of the four. The app relies on this exact set:
-- every signed-in viewer who is not an admin gets the read-only view. Replace the
-- app name if yours differs.
EXECUTE IMMEDIATE $$
DECLARE
  unexpected_grantee EXCEPTION (-20011,
    'Streamlit OVERWATCH_APP has a USAGE grantee outside {SNOW_ACCOUNTADMINS, SNOW_SYSADMINS, SNOW_PRI_GFR_PRD_ALFA_DSA, SNOW_PRI_GFR_PRD_ALFA_DTI} — REVOKE it.');
  missing_grantee EXCEPTION (-20012,
    'Streamlit OVERWATCH_APP is missing USAGE for an access role (a CREATE OR REPLACE STREAMLIT dropped its grants) — re-run the GRANT USAGE ON STREAMLIT block above.');
  bad INTEGER;
  present INTEGER;
BEGIN
  SHOW GRANTS ON STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP;
  SELECT
    COUNT_IF("privilege" = 'USAGE'
             AND NOT ("granted_to" = 'ROLE'
                      AND "grantee_name" IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS',
                                             'SNOW_PRI_GFR_PRD_ALFA_DSA', 'SNOW_PRI_GFR_PRD_ALFA_DTI'))),
    COUNT(DISTINCT CASE WHEN "privilege" = 'USAGE' AND "granted_to" = 'ROLE'
             AND "grantee_name" IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS',
                                    'SNOW_PRI_GFR_PRD_ALFA_DSA', 'SNOW_PRI_GFR_PRD_ALFA_DTI')
             THEN "grantee_name" END)
    INTO :bad, :present
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
  IF (bad > 0) THEN
    RAISE unexpected_grantee;
  END IF;
  IF (present < 4) THEN
    RAISE missing_grantee;
  END IF;
  RETURN 'Streamlit grants OK — the four access roles are present, no unexpected grantee.';
END;
$$;

SELECT 'roles applied' AS STATUS;
