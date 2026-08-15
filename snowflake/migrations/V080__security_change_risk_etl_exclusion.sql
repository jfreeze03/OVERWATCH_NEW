-- V080__security_change_risk_etl_exclusion.sql
--
-- Stop the Security "Change Risk" queue flooding on routine ETL truncate-and-
-- reload DDL. V075 flags every DROP/TRUNCATE as CRITICAL "DESTRUCTIVE", but on
-- this account that volume is automated ETL: a handful of service roles run
-- DROP TABLE IF EXISTS / TRUNCATE on EDW tables thousands of times a day
-- (confirmed from ACCOUNT_USAGE.QUERY_HISTORY, owner 2026-08-15), which buried
-- real signal and forced the domain score to 0/100.
--
-- Re-derives V_SECURITY_EXCEPTION_QUEUE (from V075) with ONE change: the CHANGE
-- RISK arm excludes DESTRUCTIVE events BY the known ETL/service roles. Scoped so
-- GRANT/REVOKE/POLICY by those roles, and DESTRUCTIVE by any OTHER (human) role,
-- are STILL surfaced; the rows remain in FACT_SECURITY_CHANGE for audit -- only
-- the exception QUEUE (and the domain score that reads it) stop counting them.
-- Adding/removing a role later is a one-line re-derivation of this view.
--
-- View-only: no data reload, no new objects, no app version bump. Fixes both the
-- queue display and the domain score at once (same view). Owner applies in
-- Snowsight after V079. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20080, 'V080 requires V079 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 79) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (CHANGE RISK arm excludes ETL/service-role DESTRUCTIVE, from V075)
CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE AS
WITH latest_posture AS (
    SELECT METRIC, VALUE, DAY
    FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
    QUALIFY DAY = MAX(DAY) OVER ()
), candidates AS (
    SELECT 'IDENTITY' AS DOMAIN, 'ALL' AS COMPANY,
           NULL::VARCHAR AS ACTOR_COMPANY, NULL::VARCHAR AS OBJECT_COMPANY,
           'ALERT' AS ENTITY_TYPE, METRIC AS ENTITY_KEY,
           IFF(METRIC = 'MFA_GAP_USERS', 'HIGH', 'MEDIUM') AS SEVERITY,
           CASE METRIC WHEN 'MFA_GAP_USERS' THEN 'Users with password activity and no MFA'
                       WHEN 'EXPIRED_CRED' THEN 'Expired credentials remain active'
                       ELSE 'Credentials expire within 10 days' END AS TITLE,
           VALUE || ' open exception(s)' AS DETAIL,
           VALUE AS IMPACT_COUNT, DAY::TIMESTAMP_NTZ AS DETECTED_AT, 1.0 AS CONFIDENCE
    FROM latest_posture
    WHERE METRIC IN ('MFA_GAP_USERS', 'EXPIRED_CRED', 'EXPIRING_CRED_10D') AND VALUE > 0
    UNION ALL
    SELECT 'PRIVILEGE', 'ALL', NULL, NULL, 'ALERT', METRIC,
           'HIGH', 'Recent break-glass grants', VALUE || ' grant(s) in 30 days',
           VALUE, DAY::TIMESTAMP_NTZ, 1.0
    FROM latest_posture
    WHERE METRIC = 'BREAKGLASS_GRANTS_30D' AND VALUE > 0
    UNION ALL
    SELECT 'TRUST CENTER', 'ALL', NULL, NULL, 'ALERT', SCANNER_ID,
           COALESCE(SEVERITY, 'MEDIUM'), SCANNER_NAME,
           CURRENT_COUNT || ' entity finding(s); ' || CHANGE_STATE,
           CURRENT_COUNT, SCANNED_AT::TIMESTAMP_NTZ,
           IFF(CHANGE_STATE IN ('NEW', 'REGRESSED'), 1.0, 0.9)
    FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_TRUST_DELTA
    WHERE CURRENT_COUNT > 0
    UNION ALL
    SELECT 'CHANGE RISK', COMPANY,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(COALESCE(USER_NAME, 'UNKNOWN')),
           IFF(DATABASE_NAME IS NULL, NULL,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)),
           IFF(DATABASE_NAME IS NULL, 'ALERT', 'OBJECT'),
           COALESCE(DATABASE_NAME || '.' || SCHEMA_NAME, QUERY_ID),
           RISK_LEVEL,
           CHANGE_KIND || ': ' || COALESCE(DATABASE_NAME || '.' || SCHEMA_NAME, QUERY_TYPE),
           COALESCE(USER_NAME, 'unknown') || ' via ' || COALESCE(ROLE_NAME, 'unknown'),
           1, EVENT_TS::TIMESTAMP_NTZ,
           RISK_SCORE / 100.0
    FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
    WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) AND RISK_SCORE >= 70
      -- V080: ETL/service-role truncate-and-reload is not a security event.
      -- DESTRUCTIVE by these roles is dropped from the queue, but their
      -- GRANT/REVOKE/POLICY changes and any other role's DROP still surface.
      -- COALESCE keeps it NULL-safe: an unattributed (NULL-role) DROP surfaces.
      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND COALESCE(ROLE_NAME, '') IN (
          'TF_SFR_PRD_GLUE', 'TF_SFR_MGM_GLUE', 'TF_SFR_SAN_GLUE',
          'TF_SFR_SEA_GLUE', 'TF_SFR_DEV_GLUE', 'TF_SFR_PHX_GLUE',
          'TF_SFR_PRD_INFORMATICA', 'TF_SFR_MGM_INFORMATICA', 'TF_SFR_SAN_INFORMATICA',
          'TF_SFR_SEA_INFORMATICA', 'TF_SFR_DEV_INFORMATICA', 'TF_SFR_PHX_INFORMATICA',
          'TF_O_PRD_ALFA_SYSADMIN', 'TF_O_MGM_ALFA_SYSADMIN', 'TF_O_SAN_ALFA_SYSADMIN',
          'TF_O_SEA_ALFA_SYSADMIN', 'TF_O_DEV_ALFA_SYSADMIN', 'TF_O_PHX_ALFA_SYSADMIN'))
), open_actions AS (
    SELECT SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY,
           MAX_BY(ACTION_ID, CREATED_AT) AS ACTION_ID,
           MAX_BY(OWNER, CREATED_AT) AS OWNER,
           MAX_BY(STATUS, CREATED_AT) AS ACTION_STATUS
    FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
    WHERE STATUS IN ('OPEN', 'IN_PROGRESS')
    GROUP BY 1, 2
)
SELECT c.DOMAIN, c.COMPANY, c.ACTOR_COMPANY, c.OBJECT_COMPANY,
       c.ENTITY_TYPE, c.ENTITY_KEY, c.SEVERITY, c.TITLE, c.DETAIL,
       c.IMPACT_COUNT,
       c.DETECTED_AT, c.CONFIDENCE, a.OWNER, a.ACTION_ID,
       COALESCE(a.ACTION_STATUS, 'UNTRACKED') AS STATUS
FROM candidates c
LEFT JOIN open_actions a
  ON a.SOURCE_ENTITY_TYPE = c.ENTITY_TYPE AND a.SOURCE_ENTITY_KEY = c.ENTITY_KEY;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 80 AS VERSION,
       'Security change-risk ETL exclusion: V_SECURITY_EXCEPTION_QUEUE CHANGE RISK arm excludes DESTRUCTIVE (DROP/TRUNCATE) events by the confirmed ETL/service roles (Glue/Informatica/transform + pipeline service accounts) so routine truncate-and-reload stops flooding the queue and zeroing the domain score. GRANT/REVOKE/POLICY by those roles and DESTRUCTIVE by any other role still surface; rows stay in FACT_SECURITY_CHANGE for audit. View-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 80);
