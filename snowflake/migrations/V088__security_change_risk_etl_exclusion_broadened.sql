-- V088__security_change_risk_etl_exclusion_broadened.sql
--
-- Broaden V080's Security "Change Risk" ETL exclusion. V080 excluded DESTRUCTIVE
-- (DROP/TRUNCATE) events from a FIXED list of 18 service roles, but on this
-- account the flood persisted (owner 2026-08-17) -- the roles driving it were not
-- in that list, and OVERWATCH's own DBA_MAINT_DB churn plus NULL-database drops
-- kept the CHANGE RISK domain score at 0/100.
--
-- Re-derives V_SECURITY_EXCEPTION_QUEUE (from the V075 base, same as V080) with a
-- BROADER CHANGE RISK exclusion: match the Terraform service-role convention
-- (TF_*, non-interactive automation) instead of a fixed list, and drop the app's
-- own DBA_MAINT_DB. Still DESTRUCTIVE-only and NULL-safe; GRANT/REVOKE/POLICY by
-- those roles and any human role's DROP still surface. Rows stay in
-- FACT_SECURITY_CHANGE for audit -- only the QUEUE and its domain score change.
--
-- View-only: no data reload, no new objects, no app version bump. Supersedes V080
-- (later CREATE OR REPLACE wins). Owner applies in Snowsight after V087. This file
-- never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20088, 'V088 requires V087 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 87) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (CHANGE RISK arm excludes TF_* / DBA_MAINT_DB DESTRUCTIVE, from V075)
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
      -- V088: V080's fixed 18-role list missed the roles driving the flood. The
      -- owner diagnostic (2026-08-17, Security change-risk noise panel) confirmed
      -- 94% of DESTRUCTIVE events are TF_* roles, 0% unattributed, ~0% on the app
      -- DB -- so match the Terraform service-role convention (TF_*, non-interactive
      -- automation) instead of a fixed list, and drop the app's own
      -- DBA_MAINT_DB.PUBLIC scratch churn. The DBA_MAINT_DB carve-out is narrowed
      -- to PUBLIC so a DROP/TRUNCATE of the OVERWATCH audit tables stays visible.
      -- Still DESTRUCTIVE-only and NULL-safe: GRANT/REVOKE/POLICY by these roles
      -- and any non-TF role's DROP still surface.
      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND (
          UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'
          OR (UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'
              AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC')))
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
SELECT 88 AS VERSION,
       'Security change-risk ETL exclusion broadened (owner diagnostic 2026-08-17 confirmed 94% of DESTRUCTIVE events are TF_* roles, 0% unattributed): V_SECURITY_EXCEPTION_QUEUE CHANGE RISK arm now excludes DESTRUCTIVE (DROP/TRUNCATE) events by any Terraform service role (TF_*, generalizing V080 fixed list) and on OVERWATCH own DBA_MAINT_DB.PUBLIC (narrowed to PUBLIC so audit-table drops stay visible), so the persistent routine-ETL flood stops zeroing the domain score. Still DESTRUCTIVE-only and NULL-safe; GRANT/REVOKE/POLICY and any non-TF role DROP still surface; rows stay in FACT_SECURITY_CHANGE for audit. View-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 88);
