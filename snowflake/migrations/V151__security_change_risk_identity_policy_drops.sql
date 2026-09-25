-- V151__security_change_risk_identity_policy_drops.sql
--
-- Stop hiding Terraform-role DROP USER / DROP ROLE / DROP POLICY from the Security
-- exception queue (Next-Fifty rank 8). V088 excluded EVERY CHANGE_KIND=DESTRUCTIVE
-- row by a TF_* role (or on DBA_MAINT_DB.PUBLIC) to stop the truncate-and-reload
-- flood -- but SP_LOAD_SECURITY_FACTS (V105) files DROP_USER / DROP_ROLE / DROP
-- POLICY under DESTRUCTIVE (its DROP test precedes its USER / POLICY tests), so a
-- TF_* role deleting a user, a role or a masking / row-access / network policy never
-- reached the queue or the CHANGE RISK domain score, contrary to the V088 comment.
--
-- Re-derives V_SECURITY_EXCEPTION_QUEUE from V088 (its current definition; V088
-- itself re-derived from V075 and superseded V080), byte-identical except the
-- CHANGE RISK exclusion block: a row whose QUERY_TYPE names a USER / ROLE / POLICY
-- object, or whose whitespace-normalized statement opens with DROP USER, DROP ROLE,
-- DROP DATABASE ROLE, DROP APPLICATION ROLE or DROP (kind) POLICY (17 keyword-
-- anchored openers), is no longer excluded. TF_* table / schema drops, TRUNCATEs
-- and the DBA_MAINT_DB.PUBLIC churn stay excluded, NULL-safe as before. There is
-- deliberately no bare POLICY substring test (a TF_* TRUNCATE of an insurance
-- FACT_POLICY table stays excluded) and no DATABASE_NAME IS NOT NULL gate
-- (DATABASE_NAME is the session database, and NULL-database drops were flood).
--
-- Posture impact (intentional): every surfaced row is CRITICAL (DROP base score 90)
-- and costs the CHANGE RISK domain 25 points, so 1 surfaced row in 7 days reads
-- Watch (75) and 2 read Act (50), which turns the Security page verdict bad.
--
-- Rows were never deleted from FACT_SECURITY_CHANGE, so the fix is retroactive over
-- the 7-day queue window. View-only: no data reload, no new objects, no tail
-- procedure run. Owner applies in Snowsight after V150. This file never runs from
-- the app. Rollback: re-run the V088 view statement (V088:34-108).

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20151, 'V151 requires V150 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 150) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V088; CHANGE RISK exclusion keeps TF_* identity / policy drops)
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
      -- V088 excluded DESTRUCTIVE (DROP/TRUNCATE) rows by Terraform service roles
      -- (TF_*) and on the app scratch schema DBA_MAINT_DB.PUBLIC -- the owner
      -- diagnostic (2026-08-17) put 94% of the flood on TF_* roles.
      -- V151: that exclusion also hid every TF_* DROP USER / DROP ROLE / DROP POLICY,
      -- because the loader files DROP_USER and DROP_ROLE under DESTRUCTIVE (its DROP
      -- test runs before its USER test). Identity and governance deletions are not
      -- truncate-and-reload noise, so a row is now kept whenever its QUERY_TYPE names
      -- a USER / ROLE / POLICY object or its statement opens DROP USER, DROP ROLE,
      -- DROP DATABASE ROLE, DROP APPLICATION ROLE or DROP (kind) POLICY. The preview
      -- test is keyword-anchored, never a bare POLICY substring, so a TF_* TRUNCATE of
      -- an insurance FACT_POLICY table stays excluded. Table/schema drops by TF_*
      -- roles and the DBA_MAINT_DB.PUBLIC churn stay excluded, NULL-safe as before.
      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND (
          UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'
          OR (UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'
              AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC'))
          AND NOT (COALESCE(QUERY_TYPE, '') ILIKE ANY ('%USER%', '%ROLE%', '%POLICY%')
              OR LTRIM(REGEXP_REPLACE(UPPER(COALESCE(QUERY_PREVIEW, '')), '[[:space:]]+', ' '))
                 LIKE ANY ('DROP USER %', 'DROP ROLE %', 'DROP DATABASE ROLE %',
                           'DROP APPLICATION ROLE %', 'DROP MASKING POLICY %',
                           'DROP ROW ACCESS POLICY %', 'DROP NETWORK POLICY %',
                           'DROP PASSWORD POLICY %', 'DROP SESSION POLICY %',
                           'DROP AUTHENTICATION POLICY %', 'DROP AGGREGATION POLICY %',
                           'DROP PROJECTION POLICY %', 'DROP JOIN POLICY %',
                           'DROP PACKAGES POLICY %', 'DROP PRIVACY POLICY %',
                           'DROP STORAGE LIFECYCLE POLICY %', 'DROP BACKUP POLICY %')))
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
SELECT 151 AS VERSION,
       'Security change-risk identity/policy drops un-hidden (Next-Fifty rank 8): V_SECURITY_EXCEPTION_QUEUE re-derived from V088 (byte-identical except the CHANGE RISK exclusion) so the TF_* / DBA_MAINT_DB.PUBLIC DESTRUCTIVE exclusion no longer swallows DROP USER / DROP ROLE / DROP DATABASE ROLE / DROP APPLICATION ROLE / DROP (kind) POLICY -- a row is kept when QUERY_TYPE names USER/ROLE/POLICY or the whitespace-normalized statement opens with one of those DROP keywords (keyword-anchored, so a TF_* TRUNCATE of a FACT_POLICY table stays excluded; no DATABASE_NAME IS NOT NULL gate, since NULL-database drops were part of the flood). The V105 classifier files DROP_USER/DROP_ROLE as DESTRUCTIVE (DROP tested before USER), which is why V088 hid them. Rows were always kept in FACT_SECURITY_CHANGE, so the 7-day queue window corrects immediately. View-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 151);
