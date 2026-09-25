-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  APPLY V151, V152, V153, V154, V155 (in order) + PART B verify. FIVE pending migrations
--  (Next-Fifty wave 2a; app v4.593.0 = main 93b0b63 already reads all five).
--
--  BEFORE THIS FILE (once): if you have not run snowflake/run/PROBES_WAVE2.sql yet, run it
--  first -- it is the read-only BEFORE baseline, and the V151 / V154 grids below compare
--  against its A1 / A2 / C5 numbers. After V151-V154 apply, those previews no longer
--  show the "before" state.
--
--  Apply top-to-bottom as SNOW_ACCOUNTADMINS -- V151, V152, V153, V154, V155 -- then
--  paste back the PART B grids. Each guards on the prior (V151 needs V150 ... V155 needs
--  V154) and all are idempotent (CREATE OR REPLACE / ADD COLUMN IF NOT EXISTS /
--  SCHEMA_VERSION insert WHERE NOT EXISTS), so an already-applied one no-ops.
--
--  V151 (#8):  V_SECURITY_EXCEPTION_QUEUE re-created from V088 so a Terraform (TF_*)
--              DROP USER / ROLE / ... POLICY is no longer hidden as routine IaC churn.
--  V152 (#10c): MART_CLOUD_SVC_DAILY + MART_TASK_NODE_DAILY get loader-owned freshness
--              stamps (SP_LOAD_QH_EXTRACT from V149, SP_LOAD_MARTS_V27 from V146,
--              MART_SOURCE_FRESHNESS from V045). No tail CALL; the next hourly run stamps.
--  V153 (#11): SP_LEDGER_AUTOBOOK settles on the FULL 14-day window with a FLOAT rate
--              (from V145); SP_VERIFY_IDLE_SAVINGS (from V053) skips change-tied rows.
--              Tail CALL runs the autobook once -- it needs the TIMEZONE prelude below.
--  V154 (#12): INCIDENTS.MITIGATED_BY + SP_INCIDENT_AUTODECLARE (attach later criticals,
--              auto-mitigate once every member alert has cleared for an hour).
--  V155 (7c):  SP_LOAD_QUERY_OPERATOR_STATS (from V147) skips the app's own statements by
--              the tag Streamlit-in-Snowflake stamps on them. No tail CALL.
--
--  Requires V150 applied (V151's guard). V148-V150 confirmed applied 2026-09-24.
--  OPTIONAL, AFTER V153 ONLY: snowflake/run/RESETTLE_AUTOBOOK_14D.sql re-settles the
--  pre-V153 autobook rows (owner decision O-8). Grids 1/1b read-only; its write stays
--  commented until you decide. Never paste it into this file.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- the task clock: V153's tail CALL reads CURRENT_DATE()

-- =====================================================================
--  MIGRATION 1 of 5 -- APPLY V151 (idempotent; GUARDS on V150). Source: snowflake/migrations/V151__security_change_risk_identity_policy_drops.sql
-- =====================================================================
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

-- =====================================================================
--  MIGRATION 2 of 5 -- APPLY V152 (idempotent; GUARDS on V151). Source: snowflake/migrations/V152__pipeline_freshness_coverage.sql
-- =====================================================================
-- V152__pipeline_freshness_coverage.sql
--
-- Pipeline freshness coverage (Next-Fifty #10c). Every FACT_/MART_ table a loader MERGEs or
-- INSERTs into carries a loader-owned SOURCE_FRESHNESS_STATE stamp EXCEPT two, so a stalled load
-- of either was invisible to the health strip, the Control Room / Admin freshness boards and the
-- native NATIVE_ALERT_STALE_FACTS watcher:
--   * MART_CLOUD_SVC_DAILY -- filled by SP_LOAD_CLOUD_SVC_MART, which SP_LOAD_QH_EXTRACT CALLs
--     hourly; the V150 COST_CLOUD_SVC_ANOMALY baseline reads it.
--   * MART_TASK_NODE_DAILY -- filled by the [6b] arm of SP_LOAD_MARTS_V27('HOURLY'), which already
--     appends 'task_node' to :loaded on success, but that token was mapped nowhere.
--
-- The HOURLY stamp JOINs the MART_SOURCE_FRESHNESS view to a static srcmap VALUES list and gates on
-- ARRAY_CONTAINS(token, SPLIT(:loaded)): a token alone stamps nothing, and a view row alone stamps
-- nothing, so MART_TASK_NODE_DAILY gets BOTH. MART_CLOUD_SVC_DAILY rides SP_LOAD_QH_EXTRACT's existing
-- IF (ok) UNION ALL freshness MERGE (STATUS 'loader'): its MAX(LOAD_TS) advances only when the mart
-- MERGE succeeded (a failure logs cloud_svc_mart_failed with CONTEXT 'MART_CLOUD_SVC_DAILY - ...', which
-- Admin > Diagnose stale sources maps), so the stamp never reads a failed mart as fresh.
--
-- Three insertion-only re-derivations, each from its CURRENT definer; everything else byte-identical:
--   1. MART_SOURCE_FRESHNESS           from V045 -- + one MART_TASK_NODE_DAILY branch (the sibling
--                                         branches' exact clock idiom; no COPY GRANTS, as V041-V045);
--   2. SP_LOAD_QH_EXTRACT(FLOAT)       from V149 -- + one MART_CLOUD_SVC_DAILY arm in the stamp MERGE;
--   3. SP_LOAD_MARTS_V27(VARCHAR, FLOAT) from V146 -- + one HOURLY srcmap row.
-- Both new SOURCE_NAMEs contain DAILY, so the shared cadence name rule judges them at 30h although
-- both load hourly: lenient (a stall shows after 30h), never a false stale.
--
-- No task, rule or alert change and no tail CALL: the next hourly TASK_LOAD_HOURLY run stamps both
-- rows (STATUS 'loader' / 'task_node'). Idempotent; safe to re-run. Owner applies in Snowsight after
-- V151. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20152, 'V152 requires V151 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 151) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:MART_SOURCE_FRESHNESS  (from V045; + MART_TASK_NODE_DAILY row, V152)
CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS
SELECT 'FACT_QUERY_HOURLY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT,
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0 AS HOURS_SINCE_LOAD
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
UNION ALL
SELECT 'FACT_QUERY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
UNION ALL
SELECT 'FACT_WAREHOUSE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
UNION ALL
SELECT 'FACT_METERING_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
UNION ALL
SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
UNION ALL
SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
UNION ALL
SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
UNION ALL
SELECT 'MART_EXEC_BOARD', MAX(REFRESHED_AT), COUNT(*),
       DATEDIFF('minute', MAX(REFRESHED_AT), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
UNION ALL
SELECT 'MART_WAREHOUSE_EFFICIENCY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
UNION ALL
SELECT 'MART_QUERY_FAMILY_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
UNION ALL
SELECT 'FACT_QUERY_ROLE_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
UNION ALL
SELECT 'FACT_QUERY_SCHEMA_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
UNION ALL
SELECT 'MART_COST_ALLOCATION_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
UNION ALL
SELECT 'MART_TASK_GRAPH_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
UNION ALL
SELECT 'MART_SECURITY_POSTURE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
UNION ALL
SELECT 'MART_INCIDENT_TIMELINE', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
UNION ALL
SELECT 'FACT_AI_USAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
UNION ALL
SELECT 'MART_TAG_COVERAGE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
UNION ALL
SELECT 'MART_LOCK_WAIT_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY
UNION ALL
SELECT 'MART_PATTERN_COST_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
UNION ALL
SELECT 'OW_QH_EXTRACT', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
UNION ALL
SELECT 'FACT_COST_ALLOC_XDIM_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
UNION ALL
SELECT 'MART_OPS_DIAG_HOURLY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_OPS_DIAG_HOURLY
UNION ALL
SELECT 'FACT_PLATFORM_SCORE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY
UNION ALL
SELECT 'MART_TASK_NODE_DAILY', MAX(LOAD_TS), COUNT(*),
       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0
FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY;

-- >>> derived:SP_LOAD_QH_EXTRACT  (from V149; + MART_CLOUD_SVC_DAILY freshness stamp, V152)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo TIMESTAMP_NTZ;  -- reload lower bound
    d INT;
    emsg VARCHAR;
    ok BOOLEAN DEFAULT FALSE;  -- r22 #7: extract arm committed this cycle
BEGIN
    -- DAYS_BACK > 0 = explicit backfill window; 0 or NULL = watermark mode.
    -- The tasks pass 0 (never a bare NULL — no signature-resolution
    -- questions on any runtime).
    IF (COALESCE(DAYS_BACK, 0) > 0) THEN
        d := GREATEST(1, LEAST(DAYS_BACK, 400))::INT;
        lo := DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_NTZ;
    ELSE
        -- watermark - 45 min (ACCOUNT_USAGE lag overlap), first run 48h,
        -- catch-up clamped at the 3-day retention (wider gaps: backfill).
        SELECT GREATEST(
                   COALESCE(DATEADD('minute', -45, MAX(WM_TS)),
                            DATEADD('hour', -48, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ),
                   DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
          INTO :lo
        FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
        WHERE SOURCE = 'QH_EXTRACT';
    END IF;

    -- The one QUERY_HISTORY scan of the hourly cycle. Retention trim rides
    -- the same DELETE; an explicit backfill keeps its wider window until the
    -- next watermark-mode run trims back to 3 days. Both arms carry V017
    -- isolation (v4.36.1): a failed extract fill must not fail the task —
    -- the facts keep their last load and the freshness labels say so.
    -- r22 #7: the arm is one TRANSACTION — a failed INSERT rolls the DELETE
    -- back (no hole; consumers really do read the previous fill) and the
    -- watermark below only advances on COMMIT.
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
     WHERE START_TIME >= :lo
        OR START_TIME < LEAST(:lo, DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ);

    INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        (QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
         USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE, ERROR_MESSAGE,
         TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME, QUEUED_OVERLOAD_TIME,
         QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE, BYTES_SCANNED,
         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT,
         CREDITS_USED_CLOUD_SERVICES, SESSION_ID, IS_CLIENT_GENERATED_STATEMENT)
    SELECT QUERY_ID, START_TIME, WAREHOUSE_NAME, WAREHOUSE_SIZE, DATABASE_NAME, SCHEMA_NAME,
           USER_NAME, ROLE_NAME, QUERY_TYPE, EXECUTION_STATUS, ERROR_CODE::VARCHAR,
           LEFT(ERROR_MESSAGE, 200), TOTAL_ELAPSED_TIME, EXECUTION_TIME, COMPILATION_TIME,
           QUEUED_OVERLOAD_TIME, QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE,
           BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH,
           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0),
           SESSION_ID, IS_CLIENT_GENERATED_STATEMENT
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= :lo;
    COMMIT;
    ok := TRUE;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'extract_load_failed', :emsg, 'OW_QH_EXTRACT - consumers read the previous fill', CURRENT_ROLE();
    END;

    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
     WHERE HOUR_TS >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()));

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        (HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
         FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
    SELECT
        DATE_TRUNC('hour', START_TIME),
        WAREHOUSE_NAME,
        DATABASE_NAME,
        USER_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME),
        COUNT(*),
        SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)),
        SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000,
        APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95),
        SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000,
        SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3)
    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
    WHERE START_TIME >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))
    GROUP BY 1, 2, 3, 4, 5;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_HOURLY - extract unaffected', CURRENT_ROLE();
    END;

    -- r22 #1: the day-grain query fact — same dims as the hourly fact, 1/24th
    -- the rows, backfillable a full year (backfill_365.sql owns history; this
    -- arm keeps the trailing 3 days current). Company via the UDF on a plain
    -- column OUTSIDE the aggregation (V030 shape law). 'FAIL' matches the
    -- V002 hourly-fact convention.
    BEGIN
    BEGIN TRANSACTION;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY t
    USING (
        SELECT g.DAY, g.WAREHOUSE_NAME, g.DATABASE_NAME, g.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
               g.QUERY_COUNT, g.FAILED_COUNT, g.ELAPSED_SEC_SUM, g.QUEUED_SEC_SUM, g.SPILL_REMOTE_GB
        FROM (
            SELECT DATE(START_TIME) AS DAY,
                   COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                   COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                   COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                   COUNT(*) AS QUERY_COUNT,
                   SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT,
                   SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000 AS ELAPSED_SEC_SUM,
                   SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000 AS QUEUED_SEC_SUM,
                   SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_REMOTE_GB
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            -- Day-aligned (audit #6): only WHOLE days inside the 72h extract,
            -- so an aging day freezes COMPLETE, not at its last partial hour.
            WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())
            GROUP BY 1, 2, 3, 4
        ) g
    ) s
    ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
       AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
    WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERY_COUNT = s.QUERY_COUNT,
        FAILED_COUNT = s.FAILED_COUNT, ELAPSED_SEC_SUM = s.ELAPSED_SEC_SUM,
        QUEUED_SEC_SUM = s.QUEUED_SEC_SUM, SPILL_REMOTE_GB = s.SPILL_REMOTE_GB,
        LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
         FAILED_COUNT, ELAPSED_SEC_SUM, QUEUED_SEC_SUM, SPILL_REMOTE_GB)
    VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.COMPANY, s.QUERY_COUNT,
            s.FAILED_COUNT, s.ELAPSED_SEC_SUM, s.QUEUED_SEC_SUM, s.SPILL_REMOTE_GB);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_DAILY - extract unaffected', CURRENT_ROLE();
    END;

    -- V055: cloud-services breakdown mart, from the extract just filled.
    -- Isolated (V017): its failure must not break the extract or the
    -- watermark — consumers keep the previous mart fill.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ExtractLoader', 'cloud_svc_mart_failed', :emsg, 'MART_CLOUD_SVC_DAILY - extract unaffected', CURRENT_ROLE();
    END;

    -- R5: advance the watermark; R6: loader-owned freshness — ONLY when the
    -- extract arm committed (r22 #7: a failed cycle must re-cover its window).
    IF (ok) THEN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'QH_EXTRACT' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'OW_QH_EXTRACT' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
        UNION ALL
        SELECT 'FACT_QUERY_HOURLY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
        UNION ALL
        SELECT 'FACT_QUERY_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
        UNION ALL
        SELECT 'MART_CLOUD_SVC_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');
    END IF;

    RETURN 'qh extract + query facts loaded (extract committed: ' || :ok || ')';
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27  (from V146; + MART_TASK_NODE_DAILY in the HOURLY freshness srcmap, V152)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
    loaded VARCHAR DEFAULT '';
    d INT;
    ext_lo DATE;
    ext_lo_hour TIMESTAMP_LTZ;
    req_fail INT DEFAULT 0;   -- V066 #10: REQUIRED-arm (core fact/mart) failures this run
    opt_fail INT DEFAULT 0;   -- V066 #10: OPTIONAL-arm (tag-cov, task-node, AI/Cortex) failures
    bad_scope EXCEPTION (-20661,
        'SP_LOAD_MARTS_V27: SCOPE must be HOURLY or DAILY - refusing to run as a silent no-op load.');   -- V066 #37 VALIDATE SCOPE
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))::INT;

    -- V066 #37 VALIDATE SCOPE: an unrecognized SCOPE matched no arm and the terminal RETURN
    -- still claimed the marts loaded, so a typo'd scope silently loaded nothing. Fail loudly
    -- at the top instead (the outer BEGIN has no handler, so this RAISE aborts the proc).
    IF (UPPER(:SCOPE) NOT IN ('HOURLY', 'DAILY')) THEN
        RAISE bad_scope;
    END IF;

    IF (UPPER(:SCOPE) = 'HOURLY') THEN

        -- V062 B5/B10: clamp backfill lower bounds to the extract's first
        -- WHOLE day/hour so a wide :d actually loads :d days (not a silent 2),
        -- while normal ops (small :d) stay at the extract-bounded window.
        ext_lo := (SELECT COALESCE(
                       DATEADD('day', IFF(MIN(START_TIME) = DATE_TRUNC('day', MIN(START_TIME)), 0, 1), DATE(MIN(START_TIME))),
                       DATEADD('day', -:d, CURRENT_DATE()))
                   FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);
        ext_lo_hour := (SELECT COALESCE(
                       DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))),
                       DATEADD('day', -:d, CURRENT_DATE()))
                   FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);

        -- [1] warehouse efficiency ------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY t
            USING (
                WITH m AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           SUM(CREDITS_USED) AS CREDITS_TOTAL,
                           SUM(CREDITS_USED_COMPUTE) AS CREDITS_COMPUTE,
                           COUNT_IF(CREDITS_USED > 0) AS BILLED_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                ),
                q AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,
                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,
                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,
                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_NAME IS NOT NULL
                    GROUP BY 1, 2
                ),
                -- V103: ACTIVE_HOURS must count every clock hour a query was RUNNING, not just
                -- its START hour. The old COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) marked
                -- hours 11 and 12 of a 10:59->13:00 query IDLE, so IDLE_PCT (and every $ derived
                -- from it: the SUSPEND/DOWN sizing verdict, IDLE_MONTHLY_USD, the idle-$ KPI)
                -- overstated idle for any multi-hour query. Expand each query across the hours it
                -- SPANS (bounded to 25, matching insights_sql._active_hours_cte), attribute each
                -- spanned hour to its own DAY, and count distinct warehouse-day-hours.
                qh AS (
                    SELECT s.WAREHOUSE_NAME,
                           DATE(DATEADD('hour', g.SEQ, s.H0)) AS DAY,
                           DATEADD('hour', g.SEQ, s.H0) AS HOUR_TS
                    FROM (
                        SELECT WAREHOUSE_NAME,
                               DATE_TRUNC('hour', START_TIME) AS H0,
                               DATE_TRUNC('hour', COALESCE(END_TIME, START_TIME)) AS H1
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                        WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                          AND WAREHOUSE_NAME IS NOT NULL
                    ) s
                    JOIN (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 25))) g
                      ON DATEADD('hour', g.SEQ, s.H0) <= s.H1
                ),
                q_active AS (
                    SELECT WAREHOUSE_NAME, DAY, COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS
                    FROM qh
                    GROUP BY 1, 2
                ),
                m_idle AS (
                    -- V127: ACTUAL credits burned in warehouse-hours with NO active (span-
                    -- expanded) query -- mirrors the live twin insights_sql.idle_warehouse_analysis
                    -- (SUM(IFF(no active query hour, CREDITS_USED, 0))). Stored so the reader
                    -- eff_idle_analysis reads accurate idle spend instead of pro-rating the day's
                    -- total credits by the hour-count IDLE_PCT (which over-states idle for scale-out
                    -- warehouses, whose idle hours cost less than their active multi-cluster hours).
                    -- Join to DISTINCT active hours (like the live query_hours CTE) so a metering
                    -- slice is never fanned out by multiple queries sharing an hour.
                    SELECT DATE(mh.START_TIME) AS DAY, mh.WAREHOUSE_NAME,
                           SUM(IFF(a.HOUR_TS IS NULL, COALESCE(mh.CREDITS_USED, 0), 0)) AS IDLE_CREDITS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY mh
                    LEFT JOIN (SELECT DISTINCT WAREHOUSE_NAME, HOUR_TS FROM qh) a
                           ON a.WAREHOUSE_NAME = mh.WAREHOUSE_NAME
                          AND a.HOUR_TS = DATE_TRUNC('hour', mh.START_TIME)
                    WHERE mh.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND mh.WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                )
                SELECT COALESCE(m.DAY, q.DAY) AS DAY,
                       COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)) AS COMPANY,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0), 4) AS CREDITS_TOTAL,
                       ROUND(COALESCE(m.CREDITS_COMPUTE, 0), 4) AS CREDITS_COMPUTE,
                       COALESCE(q.QUERIES, 0) AS QUERIES,
                       COALESCE(q.FAILS, 0) AS FAILS,
                       ROUND(COALESCE(q.QUEUED_MIN, 0), 2) AS QUEUED_MIN,
                       ROUND(COALESCE(q.SPILL_GB, 0), 3) AS SPILL_GB,
                       ROUND(COALESCE(q.P95_S, 0), 1) AS P95_S,
                       ROUND(COALESCE(q.EXEC_HOURS, 0), 3) AS EXEC_HOURS,
                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,
                       COALESCE(qa.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,
                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(qa.ACTIVE_HOURS, 0), 0)
                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY,
                       ROUND(COALESCE(mi.IDLE_CREDITS, 0), 4) AS IDLE_CREDITS
                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME
                LEFT JOIN q_active qa ON qa.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)
                                     AND qa.DAY = COALESCE(m.DAY, q.DAY)
                LEFT JOIN m_idle mi ON mi.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)
                                   AND mi.DAY = COALESCE(m.DAY, q.DAY)
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET
                COMPANY = s.COMPANY, CREDITS_TOTAL = s.CREDITS_TOTAL,
                CREDITS_COMPUTE = s.CREDITS_COMPUTE, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_MIN = s.QUEUED_MIN, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S,
                EXEC_HOURS = s.EXEC_HOURS, BILLED_HOURS = s.BILLED_HOURS,
                ACTIVE_HOURS = s.ACTIVE_HOURS, IDLE_PCT = s.IDLE_PCT,
                CREDITS_PER_QUERY = s.CREDITS_PER_QUERY, IDLE_CREDITS = s.IDLE_CREDITS,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE, QUERIES, FAILS,
                 QUEUED_MIN, SPILL_GB, P95_S, EXEC_HOURS, BILLED_HOURS, ACTIVE_HOURS, IDLE_PCT, CREDITS_PER_QUERY, IDLE_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.COMPANY, s.CREDITS_TOTAL, s.CREDITS_COMPUTE, s.QUERIES, s.FAILS,
                    s.QUEUED_MIN, s.SPILL_GB, s.P95_S, s.EXEC_HOURS, s.BILLED_HOURS, s.ACTIVE_HOURS, s.IDLE_PCT, s.CREDITS_PER_QUERY, s.IDLE_CREDITS);
            loaded := loaded || 'wh_eff ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [2] query families (top 2000/day by exec time) --------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DAY,
                       QUERY_HASH,
                       COMPANY,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM (
                    -- V082: derive COMPANY per row FIRST (UDF outside the aggregation, the
                    -- V029 shape law), so the outer GROUP BY keys on a plain column and never
                    -- on the correlated-subquery UDF directly -- grouping BY that UDF is the
                    -- exact shape that logged mart_load_failed every hour after V027 (V029).
                    SELECT DATE(START_TIME) AS DAY,
                           QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY,
                           QUERY_TEXT, EXECUTION_STATUS, USER_NAME, WAREHOUSE_NAME,
                           DATABASE_NAME, SCHEMA_NAME, EXECUTION_TIME, TOTAL_ELAPSED_TIME,
                           COMPILATION_TIME, BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                      AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                )
                GROUP BY DAY, QUERY_HASH, COMPANY
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY, COMPANY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, COMPANY, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);
            loaded := loaded || 'qfam ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_QUERY_FAMILY_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [3] role-hour fact -------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.ROLE_NAME, g.WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.EXEC_SEC
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo_hour)
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.ROLE_NAME = s.ROLE_NAME AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (HOUR_TS, ROLE_NAME, WAREHOUSE_NAME, COMPANY, QUERIES, FAILS, EXEC_SEC)
            VALUES (s.HOUR_TS, s.ROLE_NAME, s.WAREHOUSE_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.EXEC_SEC);
            loaded := loaded || 'role_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_ROLE_HOURLY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [4] schema-hour fact -----------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.DATABASE_NAME, g.SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.QUEUED_SEC, g.SPILL_GB, g.P95_S
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                           COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                           ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo_hour)
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.DATABASE_NAME = s.DATABASE_NAME AND t.SCHEMA_NAME = s.SCHEMA_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_SEC = s.QUEUED_SEC, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (HOUR_TS, DATABASE_NAME, SCHEMA_NAME, COMPANY, QUERIES, FAILS, QUEUED_SEC, SPILL_GB, P95_S)
            VALUES (s.HOUR_TS, s.DATABASE_NAME, s.SCHEMA_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.QUEUED_SEC, s.SPILL_GB, s.P95_S);
            loaded := loaded || 'schema_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_SCHEMA_HOURLY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [4b] tag coverage by user, day grain (v4.14 tuning trio) --------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY t
            USING (
                SELECT g.DAY, g.USER_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(g.USER_NAME) AS COMPANY,
                       g.QUERIES, g.EXEC_SEC, g.UNTAGGED_EXEC_SEC
                FROM (
                    SELECT DATE(START_TIME) AS DAY,
                           COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                           COUNT(*) AS QUERIES,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC,
                           ROUND(SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL,
                                         COALESCE(EXECUTION_TIME, 0), 0)) / 1000, 1) AS UNTAGGED_EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                    GROUP BY 1, 2
                ) g
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES,
                EXEC_SEC = s.EXEC_SEC, UNTAGGED_EXEC_SEC = s.UNTAGGED_EXEC_SEC,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, COMPANY, QUERIES, EXEC_SEC, UNTAGGED_EXEC_SEC)
            VALUES (s.DAY, s.USER_NAME, s.COMPANY, s.QUERIES, s.EXEC_SEC, s.UNTAGGED_EXEC_SEC);
            loaded := loaded || 'tagcov ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TAG_COVERAGE_DAILY - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [5] cost allocation (exec-time share of each warehouse-hour) -------
        BEGIN
            CREATE OR REPLACE TEMPORARY TABLE _OW_ALLOC_BASE AS
            WITH wh AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       SUM(CREDITS_USED) AS HOUR_CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND WAREHOUSE_ID > 0
                GROUP BY 1, 2
            ),
            q AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       USER_NAME, COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       SUM(COALESCE(EXECUTION_TIME, 0)) AS EXEC_MS
                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND WAREHOUSE_NAME IS NOT NULL AND COALESCE(EXECUTION_TIME, 0) > 0
                GROUP BY 1, 2, 3, 4, 5, 6
            ),
            tot AS (
                SELECT HOUR_TS, WAREHOUSE_NAME, SUM(EXEC_MS) AS TOTAL_MS FROM q GROUP BY 1, 2
            )
            SELECT DATE(q.HOUR_TS) AS DAY, q.WAREHOUSE_NAME, q.USER_NAME, q.ROLE_NAME,
                   q.DATABASE_NAME, q.SCHEMA_NAME, q.EXEC_MS,
                   wh.HOUR_CREDITS * q.EXEC_MS / NULLIF(tot.TOTAL_MS, 0) AS ALLOC_CREDITS
            FROM q
            JOIN tot ON tot.HOUR_TS = q.HOUR_TS AND tot.WAREHOUSE_NAME = q.WAREHOUSE_NAME
            JOIN wh ON wh.HOUR_TS = q.HOUR_TS AND wh.WAREHOUSE_NAME = q.WAREHOUSE_NAME;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t
            USING (
                SELECT DAY, 'USER' AS DIMENSION, USER_NAME AS KEY_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'DATABASE', DATABASE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'SCHEMA', DATABASE_NAME || '.' || SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3, DATABASE_NAME
                UNION ALL
                SELECT DAY, 'ROLE', ROLE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
            ) s
            ON t.DAY = s.DAY AND t.DIMENSION = s.DIMENSION AND t.KEY_NAME = s.KEY_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, ALLOC_CREDITS = s.ALLOC_CREDITS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, DIMENSION, KEY_NAME, COMPANY, ALLOC_CREDITS, EXEC_SEC)
            VALUES (s.DAY, s.DIMENSION, s.KEY_NAME, s.COMPANY, s.ALLOC_CREDITS, s.EXEC_SEC);
            loaded := loaded || 'alloc ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_COST_ALLOCATION_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [5b] cross-dim allocation fact (V041 R2): persist _OW_ALLOC_BASE at
        -- DAY x WAREHOUSE x DATABASE x USER before it collapses to single-dim.
        -- NO schema grain (cardinality; schema stays live-filtered). Same
        -- expressions as [5], so the day-sums reconcile by construction.
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY t
            USING (
                SELECT DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
                FROM _OW_ALLOC_BASE
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
               AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET EXEC_SEC = s.EXEC_SEC,
                ALLOC_CREDITS = s.ALLOC_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, EXEC_SEC, ALLOC_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.EXEC_SEC, s.ALLOC_CREDITS);
            loaded := loaded || 'alloc_xdim ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_COST_ALLOC_XDIM_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [6] task graphs -----------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY t
            USING (
                WITH attempts AS (
                    -- V126: keep EVERY attempt (do NOT collapse to the terminal attempt before
                    -- the credit join) and tag the terminal one. TASK_RUNS / FAILED_TASKS still
                    -- count scheduled tasks via TERMINAL_RN = 1 (a task auto-retried to success
                    -- is not a graph-run failure), but WH_CREDITS now SUMs the compute of EVERY
                    -- attempt -- each retry really billed compute. This mirrors the live twin
                    -- graph_sql.graph_daily_costs exactly, so the same task-graph panel's cost no
                    -- longer flips with mart warmth. V102's terminal-only credit join dropped a
                    -- failed-retry attempt's compute (documented as accepted, but it disagreed
                    -- with the live path and the "every task run" panel caption).
                    SELECT COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
                           h.NAME, h.DATABASE_NAME, h.SCHEMA_NAME,
                           h.QUERY_START_TIME, h.COMPLETED_TIME, h.STATE,
                           COALESCE(a.CREDITS, 0) AS CREDITS,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID), h.NAME, h.SCHEDULED_TIME
                               ORDER BY h.COMPLETED_TIME DESC NULLS LAST) AS TERMINAL_RN
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    LEFT JOIN (
                        SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
                              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                              WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                                AND STATE IN ('SUCCEEDED', 'FAILED')
                          )
                        GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)
                    ) a ON a.ROOT_ID = h.QUERY_ID
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND h.STATE IN ('SUCCEEDED', 'FAILED')
                ),
                runs AS (
                    SELECT RUN_KEY,
                           MIN_BY(NAME, QUERY_START_TIME) AS PIPELINE,
                           MIN_BY(DATABASE_NAME, QUERY_START_TIME) AS DATABASE_NAME,
                           MIN_BY(SCHEMA_NAME, QUERY_START_TIME) AS SCHEMA_NAME,
                           DATE(MIN(QUERY_START_TIME)) AS DAY,
                           COUNT_IF(TERMINAL_RN = 1) AS TASK_RUNS,
                           COUNT_IF(TERMINAL_RN = 1 AND STATE = 'FAILED') AS FAILED_TASKS,
                           DATEDIFF('second', MIN(QUERY_START_TIME), MAX(COMPLETED_TIME)) AS WALL_SEC,
                           SUM(CREDITS) AS CREDITS
                    FROM attempts
                    GROUP BY RUN_KEY
                )
                SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME,
                       COUNT(*) AS GRAPH_RUNS,
                       COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES,
                       SUM(TASK_RUNS) AS TASK_RUNS,
                       ROUND(AVG(WALL_SEC), 1) AS AVG_WALL_SEC,
                       ROUND(APPROX_PERCENTILE(WALL_SEC, 0.95), 1) AS P95_WALL_SEC,
                       ROUND(SUM(CREDITS), 4) AS WH_CREDITS
                FROM runs GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.PIPELINE = s.PIPELINE
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET GRAPH_RUNS = s.GRAPH_RUNS,
                RUNS_WITH_FAILURES = s.RUNS_WITH_FAILURES, TASK_RUNS = s.TASK_RUNS,
                AVG_WALL_SEC = s.AVG_WALL_SEC, P95_WALL_SEC = s.P95_WALL_SEC,
                WH_CREDITS = s.WH_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES,
                 TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS)
            VALUES (s.DAY, s.PIPELINE, s.DATABASE_NAME, s.SCHEMA_NAME, s.GRAPH_RUNS,
                    s.RUNS_WITH_FAILURES, s.TASK_RUNS, s.AVG_WALL_SEC, s.P95_WALL_SEC, s.WH_CREDITS);
            loaded := loaded || 'graphs ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [6b] per-node task timing (queue + exec delay) -> MART_TASK_NODE_DAILY
        -- Observability for the deferred reconcile-scheduling work: the
        -- SCHEDULED_TIME->QUERY_START_TIME dispatch delay (which the pipeline-grain
        -- arm [6] discards) quantifies the 06:40/06:45 XSMALL contention. Own
        -- guarded arm; touches no existing statement; one TASK_HISTORY scan at the
        -- same -:d window; MERGE on (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME).
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY t
            USING (
                SELECT DATE(QUERY_START_TIME) AS DAY,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       NAME AS TASK_NAME,
                       COUNT(*) AS RUNS,
                       COUNT_IF(STATE = 'FAILED') AS FAILED,
                       ROUND(AVG(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS AVG_QUEUE_SEC,
                       ROUND(APPROX_PERCENTILE(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0), 0.95) / 1000, 2) AS P95_QUEUE_SEC,
                       ROUND(MAX(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS MAX_QUEUE_SEC,
                       ROUND(AVG(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS AVG_EXEC_SEC,
                       ROUND(APPROX_PERCENTILE(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME), 0.95) / 1000, 2) AS P95_EXEC_SEC,
                       ROUND(MAX(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS MAX_EXEC_SEC,
                       MIN(QUERY_START_TIME) AS FIRST_START,
                       MAX(COMPLETED_TIME) AS LAST_COMPLETED
                FROM (
                    -- V102: collapse task auto-retries to the terminal attempt so RUNS /
                    -- FAILED and the queue/exec percentiles count scheduled runs, not
                    -- attempts, mirroring the live ops_sql.task_runs / task_recent_states.
                    SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                           QUERY_START_TIME, COMPLETED_TIME, STATE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                    WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND STATE IN ('SUCCEEDED', 'FAILED')
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                               ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
                ) th
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.TASK_NAME = s.TASK_NAME
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET
                RUNS = s.RUNS, FAILED = s.FAILED,
                AVG_QUEUE_SEC = s.AVG_QUEUE_SEC, P95_QUEUE_SEC = s.P95_QUEUE_SEC, MAX_QUEUE_SEC = s.MAX_QUEUE_SEC,
                AVG_EXEC_SEC = s.AVG_EXEC_SEC, P95_EXEC_SEC = s.P95_EXEC_SEC, MAX_EXEC_SEC = s.MAX_EXEC_SEC,
                FIRST_START = s.FIRST_START, LAST_COMPLETED = s.LAST_COMPLETED, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, RUNS, FAILED,
                 AVG_QUEUE_SEC, P95_QUEUE_SEC, MAX_QUEUE_SEC,
                 AVG_EXEC_SEC, P95_EXEC_SEC, MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED)
            VALUES (s.DAY, s.DATABASE_NAME, s.SCHEMA_NAME, s.TASK_NAME, s.RUNS, s.FAILED,
                    s.AVG_QUEUE_SEC, s.P95_QUEUE_SEC, s.MAX_QUEUE_SEC,
                    s.AVG_EXEC_SEC, s.P95_EXEC_SEC, s.MAX_EXEC_SEC, s.FIRST_START, s.LAST_COMPLETED);
            loaded := loaded || 'task_node ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_NODE_DAILY - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            -- V066 #3: wrap the DELETE+INSERT in ONE transaction. Under AUTOCOMMIT the DELETE
            -- committed immediately, so a later failure in the 4-way UNION INSERT (a transient
            -- ACCOUNT_USAGE read / COMPANY_FOR_DATABASE UDF error) left the trailing 48h BLANK
            -- until the next hourly rebuild -- an incident timeline empty mid-incident. ROLLBACK
            -- on error restores the prior rows (the B34 FACT_TASK_DAILY wrap pattern).
            BEGIN TRANSACTION;
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());

            INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
                (EVENT_TS, KIND, COMPANY, SEVERITY, TITLE, REF_ID)
            SELECT RAISED_AT, 'ALERT', COMPANY, SEVERITY, LEFT(TITLE, 300), EVENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
            UNION ALL
            SELECT COMPLETED_TIME, 'TASK_FAIL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE COMPLETED_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'
            UNION ALL
            SELECT START_TIME, 'DDL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'INFO', LEFT(QUERY_TYPE || ' by ' || USER_NAME || ' (' || COALESCE(ROLE_NAME, '?') || ')', 300), QUERY_ID
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                                 'DROP', 'RENAME', 'CREATE_VIEW', 'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
            UNION ALL
            SELECT CHANGE_SEEN_AT, 'WH_CHANGE', COMPANY, 'INFO',
                   LEFT(WAREHOUSE_NAME || ' ' || SETTING || ' ' || COALESCE(OLD_VALUE, '?') || '->' || COALESCE(NEW_VALUE, '?'), 300),
                   CHANGE_ID
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            COMMIT;
            loaded := loaded || 'timeline ';
        EXCEPTION
            WHEN OTHER THEN
                ROLLBACK;   -- V066 #3: undo the 48h DELETE if the rebuild INSERT failed
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        -- V066 #11 FRESHNESS ADVANCES ON FAILURE: stamp ONLY the sources whose arm actually
        -- loaded this run. This MERGE used to advance GENERATION and write the successful-arm
        -- list as STATUS across the whole STATIC group, so a source whose arm just failed
        -- still looked freshly loaded. Each arm appends its token to :loaded only on its
        -- success path, so gate the source set on token membership (ARRAY_CONTAINS over
        -- SPLIT(:loaded)); a failed source is left untouched -- its prior generation/snapshot
        -- stand, correctly reading as not-loaded-this-run -- and STATUS now carries that
        -- source's own outcome.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,
                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f
            JOIN (
                SELECT SOURCE_NAME, TOKEN FROM VALUES
                    ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'wh_eff'),
                    ('MART_QUERY_FAMILY_DAILY', 'qfam'),
                    ('FACT_QUERY_ROLE_HOURLY', 'role_hr'),
                    ('FACT_QUERY_SCHEMA_HOURLY', 'schema_hr'),
                    ('MART_TAG_COVERAGE_DAILY', 'tagcov'),
                    ('MART_COST_ALLOCATION_DAILY', 'alloc'),
                    ('FACT_COST_ALLOC_XDIM_DAILY', 'alloc_xdim'),
                    ('MART_TASK_GRAPH_DAILY', 'graphs'),
                    ('MART_TASK_NODE_DAILY', 'task_node'),
                    ('MART_INCIDENT_TIMELINE', 'timeline')
                    AS srcmap(SOURCE_NAME, TOKEN)
            ) m ON m.SOURCE_NAME = f.SOURCE_NAME
            WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))
            GROUP BY f.SOURCE_NAME
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = s.STATUS
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);

    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN

        -- [7] security posture ------------------------------------------------
        BEGIN
            -- V041 R11 (guarded, v4.36.1): SHOW -> RESULT_SCAN once daily
            -- (V024 precedent), so Security stops paying a SHOW + parse per
            -- render. The nested handler means a SHOW failure can never take
            -- the CORE posture metrics down with it — the monitor arms below
            -- emit no rows that day instead (HAVING; never a lying zero).
            BEGIN
                SHOW WAREHOUSES LIMIT 500;
                CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR AS
                SELECT "name"::VARCHAR AS WAREHOUSE_NAME,
                       COALESCE("resource_monitor"::VARCHAR, 'null') AS RESOURCE_MONITOR,
                       TRY_TO_NUMBER("auto_suspend"::VARCHAR) AS AUTO_SUSPEND
                FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR (
                        WAREHOUSE_NAME VARCHAR, RESOURCE_MONITOR VARCHAR, AUTO_SUSPEND NUMBER);
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'MartLoader', 'monitor_counts_skipped', :emsg, 'SHOW WAREHOUSES unavailable - core posture unaffected', CURRENT_ROLE();
            END;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
            USING (
                -- A4: CREDENTIALS scanned ONCE; both metrics via COUNT_IF + UNPIVOT (was two scans).
                SELECT CURRENT_DATE() AS DAY, cu.METRIC AS METRIC, 'ALL' AS COMPANY, cu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(EXPIRATION_DATE IS NOT NULL
                                    AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())) AS "EXPIRING_CRED_10D",
                           COUNT_IF(EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS "EXPIRED_CRED"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                ) c
                UNPIVOT (VALUE FOR METRIC IN ("EXPIRING_CRED_10D", "EXPIRED_CRED")) cu
                UNION ALL
                SELECT CURRENT_DATE(), 'ADMIN_STMTS_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                  AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                UNION ALL
                -- A4: GRANTS_TO_USERS scanned ONCE; both grant metrics via COUNT_IF + UNPIVOT (was two
                -- scans). The outer WHERE is a superset of the rows either metric needs (created >= -30d
                -- covers the -24h change window; deleted >= -24h keeps revoked-in-24h rows), and each
                -- COUNT_IF re-applies its exact original predicate, so both counts are unchanged.
                SELECT CURRENT_DATE() AS DAY, gu.METRIC AS METRIC, 'ALL' AS COMPANY, gu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                                    OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) AS "GRANT_CHANGES_24H",
                           COUNT_IF(DELETED_ON IS NULL
                                    AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                                    AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS "BREAKGLASS_GRANTS_30D"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                    WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
                       OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                ) g
                UNPIVOT (VALUE FOR METRIC IN ("GRANT_CHANGES_24H", "BREAKGLASS_GRANTS_30D")) gu
                UNION ALL
                -- V041 R9: unused-role posture from the role-hour fact, not a
                -- 90d QUERY_HISTORY anti-join. Coverage-gated: HAVING emits NO
                -- row (never a lying zero) until the fact spans the window.
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY q
                      WHERE q.HOUR_TS >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )
                HAVING (SELECT MIN(HOUR_TS) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY)
                       <= DATEADD('day', -89, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'MFA_GAP_USERS', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
                WHERE U.DELETED_ON IS NULL AND COALESCE(U.DISABLED, FALSE) = FALSE
                  AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
                  AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                              WHERE L.USER_NAME = U.NAME
                                AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                                AND L.PASSWORD_LOGINS > 0)
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_MONITOR', 'ALL',
                       COUNT_IF(LOWER(TRIM(RESOURCE_MONITOR)) IN ('null', '', 'none'))
                FROM _OW_WH_MONITOR
                HAVING COUNT(*) > 0
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_AUTOSUSPEND', 'ALL',
                       COUNT_IF(COALESCE(AUTO_SUSPEND, 0) <= 0)
                FROM _OW_WH_MONITOR
                HAVING COUNT(*) > 0
            ) s
            ON t.DAY = s.DAY AND t.METRIC = s.METRIC AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET VALUE = s.VALUE, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, METRIC, COMPANY, VALUE)
            VALUES (s.DAY, s.METRIC, s.COMPANY, s.VALUE);
            loaded := loaded || 'posture ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_SECURITY_POSTURE_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [9] AI usage (Cortex Code views bill this account; Functions guarded)
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                SELECT c.USAGE_TIME::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       ANY_VALUE(u.EMAIL) AS EMAIL,
                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact
                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ
                       -- (live 2026-08-13: "expecting TIMESTAMP_NTZ(9) but got
                       -- TIMESTAMP_TZ(9) for column FIRST_TS" killed this arm on
                       -- every run, starving the AI coverage gate).
                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(c.TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM (
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    UNION ALL
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI'
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                ) c
                LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS u ON u.USER_ID = c.USER_ID
                GROUP BY 1, 2, 3
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, EMAIL = s.EMAIL, FIRST_TS = s.FIRST_TS,
                LAST_TS = s.LAST_TS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, USER_NAME, SOURCE, MODEL_NAME, EMAIL, FIRST_TS, LAST_TS, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.EMAIL, s.FIRST_TS, s.LAST_TS,
                    s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_code ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (code views) - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                -- V146: repointed off the FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY onto the canonical
                -- CORTEX_AI_FUNCTIONS_USAGE_HISTORY. Not a drop-in: TOKEN_CREDITS -> CREDITS; START_TIME
                -- is TIMESTAMP_LTZ (was NTZ) so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ (same TZ->NTZ MERGE
                -- guard as the ai_code arm, V078); and there is NO scalar TOKENS column -- token counts
                -- live in the METRICS ARRAY as {"key":{"metric":"input"|"output","unit":"tokens"},"value":N},
                -- so LATERAL FLATTEN sums value where unit='tokens'. CREDITS + REQUESTS are deduped to
                -- once per source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for
                -- empty METRICS, still counted once) so the FLATTEN fan-out cannot multiply them.
                SELECT f.START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(NULLIF(f.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(f.START_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(f.START_TIME)::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(CASE WHEN COALESCE(m.INDEX, 0) = 0 THEN 1 END) AS REQUESTS,
                       SUM(CASE WHEN m.VALUE:key:unit::STRING = 'tokens'
                                THEN m.VALUE:value::NUMBER ELSE 0 END) AS TOKENS,
                       ROUND(SUM(CASE WHEN COALESCE(m.INDEX, 0) = 0
                                      THEN COALESCE(f.CREDITS, 0) ELSE 0 END), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY f,
                     LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m
                WHERE f.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, EMAIL = s.EMAIL, FIRST_TS = s.FIRST_TS,
                LAST_TS = s.LAST_TS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, USER_NAME, SOURCE, MODEL_NAME, EMAIL, FIRST_TS, LAST_TS, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.EMAIL, s.FIRST_TS, s.LAST_TS,
                    s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_functions ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        -- V066 #11 FRESHNESS ADVANCES ON FAILURE (DAILY scope): same token-gated stamp.
        -- Only posture / AI sources whose arm loaded advance; FACT_AI_USAGE_DAILY collapses
        -- its two arms (ai_code, ai_functions) to one row via GROUP BY so the MERGE matches
        -- its target exactly once.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,
                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f
            JOIN (
                SELECT SOURCE_NAME, TOKEN FROM VALUES
                    ('MART_SECURITY_POSTURE_DAILY', 'posture'),
                    ('FACT_AI_USAGE_DAILY', 'ai_code'),
                    ('FACT_AI_USAGE_DAILY', 'ai_functions')
                    AS srcmap(SOURCE_NAME, TOKEN)
            ) m ON m.SOURCE_NAME = f.SOURCE_NAME
            -- V066 #23 AI FRESHNESS PARTIAL: FACT_AI_USAGE_DAILY has TWO independent arms
            -- (ai_code + ai_functions) mapped to the ONE physical source. The #11 per-token
            -- WHERE ARRAY_CONTAINS stamped the whole source fresh as soon as a SINGLE arm's
            -- token reached :loaded, so a half-loaded AI source read green. Gate the whole
            -- group: stamp a source only when EVERY one of its tokens loaded (both AI arms,
            -- or the lone posture arm). A partial AI load leaves the prior stamp standing, so
            -- the source reads as not-loaded-this-run (same treatment #11 gives a failed arm).
            GROUP BY f.SOURCE_NAME
            HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = s.STATUS
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);

    END IF;

    -- V066 #10 FALSE SUCCESS: the terminal RETURN used to always claim the marts loaded,
    -- even when an arm's EXCEPTION handler swallowed a failure and continued. Return a
    -- machine-readable verdict from the REQUIRED / OPTIONAL failure counters instead.
    IF (req_fail = 0) THEN
        RETURN 'MARTS OK (' || :SCOPE || ', ' || :d || 'd): ' || :loaded
               || IFF(:opt_fail > 0, '[' || :opt_fail || ' optional failed]', '');
    END IF;
    RETURN 'MARTS WITH ERRORS: ' || :req_fail || ' required, ' || :opt_fail || ' optional ('
           || :SCOPE || ', ' || :d || 'd): ' || :loaded;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 152 AS VERSION,
       'Pipeline freshness coverage (Next-Fifty #10c): the two FACT_/MART_ load targets that carried no loader-owned SOURCE_FRESHNESS_STATE stamp now get one. MART_CLOUD_SVC_DAILY (the V150 COST_CLOUD_SVC_ANOMALY baseline) is stamped by SP_LOAD_QH_EXTRACT (re-derived from V149) as one more arm of its IF (ok) UNION ALL freshness MERGE -- MAX(LOAD_TS) advances only when the SP_LOAD_CLOUD_SVC_MART MERGE succeeded. MART_TASK_NODE_DAILY needs both halves of the token-gated HOURLY stamp: a view row (MART_SOURCE_FRESHNESS re-created from V045 + one branch) and a srcmap row ((''MART_TASK_NODE_DAILY'', ''task_node'') in SP_LOAD_MARTS_V27, re-derived from V146) for the token its [6b] arm already appends on success. Insertions only; each object is otherwise byte-identical to its base. Both names contain DAILY, so the shared name rule judges them at 30h although both load hourly (lenient, never a false stale). No task, rule or alert change; no tail CALL -- the next hourly run stamps both rows.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 152);

-- =====================================================================
--  MIGRATION 3 of 5 -- APPLY V153 (idempotent; GUARDS on V152; tail CALL). Source: snowflake/migrations/V153__ledger_autobook_full_window_settle.sql
-- =====================================================================
-- V153__ledger_autobook_full_window_settle.sql -- settle autobooked savings on the FULL 14-day window.
--
--   Next-Fifty #11 (+ the #5 adopt follow-up). SP_WAREHOUSE_CHANGE_SCAN (V109) refreshes a change's
--   AFTER_* stats and VERDICT daily while CURRENT_DATE() <= TRACKING_UNTIL (detection day + 14), and a
--   row leaves PENDING as soon as AFTER_DAYS >= 3 AND AFTER_QUERIES >= 20. SP_LEDGER_AUTOBOOK (V145)
--   settled any non-PENDING row, so VERIFIED_USD froze on ~3 days of after-data and was never
--   re-measured; NO_BASELINE rows settled the SAME day they were booked (no after-rate yet, COALESCEd
--   to 0 = the whole baseline booked as saved). The credit rate was also read into a NUMBER (scale 0)
--   via TRY_TO_NUMBER, so the seeded '3.68' priced as 4 (+8.7% on every autobook VERIFIED_USD).
--
--   SP_LEDGER_AUTOBOOK is re-derived from V145, its current definition (V145 already carries V118's
--   LBA-1 dedup and V038's book/settle core -- preserved byte-for-byte). Deltas, each locked by the
--   normalize-and-compare test tests/migrations/test_v153_ledger_autobook_full_window_settle.py:
--     1. rate FLOAT via TRY_TO_DOUBLE (every other proc's idiom).
--     2. ADOPT: an app-booked manual ESTIMATED row for the same change (the Next-Fifty #5 twin rule,
--        same ON/WHERE lines as mart_sql._ledger_twin_select) gets SOURCE_CHANGE_ID stamped instead of
--        the scan booking a second row for the change. One-to-one; unbooked saving-direction only.
--     3. Settle gate: CURRENT_DATE() > TRACKING_UNTIL AND AFTER_CREDITS_PER_DAY IS NOT NULL AND VERDICT
--        IN (IMPROVED, NEUTRAL, REGRESSED, NO_BASELINE, INSUFFICIENT_AFTER). NO_BASELINE /
--        INSUFFICIENT_AFTER mean fewer than 20 queries in a window, not missing credits -- they settle
--        on the measured credits too (a low-query warehouse is the usual auto-suspend target).
--     4. Settle NOTE: full-window wording + per-day query-volume ratio, VOLUME_CONFOUNDED outside
--        0.7-1.3x (dollars NOT adjusted), PERF_REGRESSED on a REGRESSED verdict that still saved, and
--        PERF_UNJUDGED on NO_BASELINE / INSUFFICIENT_AFTER.
--     5. Close-out: a closed-window NO_BASELINE / INSUFFICIENT_AFTER change with nothing metered after
--        it (AFTER_CREDITS_PER_DAY NULL) closes REJECTED with no dollars.
--   No MIN_CLUSTERS arm: that lever stays registry-only (Next-Fifty #38 is wave 3).
--   Timing: a change now settles 15-16 days after it is made (the morning after its 14-day window
--   closes), not ~3; "verified this quarter" / the active run-rate move accordingly.
--   SP_VERIFY_IDLE_SAVINGS is re-derived from V053 to skip rows tied to a detected change.
--   Forward-only: already-settled rows are never rewritten here (re-settling them is a separate owner
--   opt-in, snowflake/resettle_autobook_14d.sql). No schema change, no task change, teardown
--   unchanged. Apply AFTER V152. Idempotent.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20153, 'V153 requires V152 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 152) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LEDGER_AUTOBOOK  (from V145; full-window settle gate, volume note, adopt, FLOAT rate, close-out; V153)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rate FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :rate
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- V153 ADOPT (Next-Fifty #5 follow-up): an app-booked manual ESTIMATED row for the SAME change is
    -- stamped with the change's SOURCE_CHANGE_ID instead of the INSERT below booking a second row. Same
    -- match as the app twin rule (mart_sql._ledger_twin_select): same warehouse, same lever (RESIZE ==
    -- SIZE), the scan saw it from 1h before to 3 days after the row was booked. One-to-one only (first
    -- change per row AND first row per change), only unbooked saving-direction changes; anything else
    -- falls through to the INSERT + the app twin rule exactly as before. The adopted row keeps its app
    -- ESTIMATED_USD and settles below like any autobook row.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET SOURCE_CHANGE_ID = a.CHANGE_ID,
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | adopted by the daily change scan (change '
                        || a.CHANGE_ID || '): settles on its 14-day measured window.', 2000)
      FROM (SELECT m.ITEM_ID, r.CHANGE_ID
              FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER m
              JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
                ON UPPER(r.WAREHOUSE_NAME) = UPPER(TRIM(m.TARGET_OBJECT))
               AND r.SETTING = IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE', UPPER(TRIM(m.FINDING_TYPE)))
               AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ >= DATEADD('hour', -1, m.CREATED_AT)
               AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ < DATEADD('day', 3, m.CREATED_AT)
             WHERE m.SOURCE_CHANGE_ID IS NULL
               AND m.STATE = 'ESTIMATED'
               AND UPPER(TRIM(m.FINDING_TYPE)) IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')
               AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER b
                               WHERE b.SOURCE_CHANGE_ID = r.CHANGE_ID)
               AND (
            (r.SETTING = 'AUTO_SUSPEND'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'MAX_CLUSTERS'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'SCALING_POLICY'
             AND UPPER(COALESCE(r.NEW_VALUE, '')) = 'ECONOMY'
             AND UPPER(COALESCE(r.OLD_VALUE, '')) = 'STANDARD')
         OR (r.SETTING = 'SIZE'
             AND CASE UPPER(REPLACE(COALESCE(r.NEW_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 99 END
               < CASE UPPER(REPLACE(COALESCE(r.OLD_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 0 END)
               )
            QUALIFY ROW_NUMBER() OVER (PARTITION BY m.ITEM_ID ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1
                AND ROW_NUMBER() OVER (PARTITION BY r.CHANGE_ID ORDER BY m.CREATED_AT, m.ITEM_ID) = 1) a
     WHERE l.ITEM_ID = a.ITEM_ID
       AND l.SOURCE_CHANGE_ID IS NULL;

    -- Book detected cost-lever changes as ESTIMATED $0. V145: also stamp FINDING_TYPE from the
    -- source SETTING (SIZE -> RESIZE) so the lever is on the row, not just in the free-text NOTE.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, SOURCE_CHANGE_ID, FINDING_TYPE)
    SELECT 'Detected ' || r.SETTING || ' change on ' || r.WAREHOUSE_NAME || ': '
               || COALESCE(r.OLD_VALUE, '?') || ' -> ' || COALESCE(r.NEW_VALUE, '?'),
           'ESTIMATED',
           0,
           'SELECT * FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY WHERE CHANGE_ID = ''' || r.CHANGE_ID || '''',
           'Auto-booked from the daily warehouse-change scan; the 14-day measured verdict settles it.',
           r.CHANGE_ID,
           CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
    WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
                      WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID)
      AND (
            (r.SETTING = 'AUTO_SUSPEND'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'MAX_CLUSTERS'
             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))
         OR (r.SETTING = 'SCALING_POLICY'
             AND UPPER(COALESCE(r.NEW_VALUE, '')) = 'ECONOMY'
             AND UPPER(COALESCE(r.OLD_VALUE, '')) = 'STANDARD')
         OR (r.SETTING = 'SIZE'
             AND CASE UPPER(REPLACE(COALESCE(r.NEW_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 99 END
               < CASE UPPER(REPLACE(COALESCE(r.OLD_VALUE, ''), '-', ''))
                     WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3
                     WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5
                     WHEN '2XLARGE' THEN 6 WHEN 'XXLARGE' THEN 6
                     WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 0 END)
      );

    -- Settle forward-only. LBA-1: rank co-occurring levers within one measured
    -- window; the primary (RN=1) carries the full warehouse saving, the rest settle
    -- VERIFIED at $0 so the physical saving is booked exactly once.
    -- V153: settle ONLY once the change's 14-day after-window has CLOSED (CURRENT_DATE() >
    -- TRACKING_UNTIL is the scan's own close-out predicate: it stops refreshing AFTER_* and VERDICT
    -- then) and only when credits were metered after the change; the V145 gate settled on ~3 days
    -- of after-data. NO_BASELINE / INSUFFICIENT_AFTER (fewer than 20 queries in a window) settle on
    -- CREDITS too, marked PERF_UNJUDGED. The NOTE carries the per-day query-volume ratio (after vs the
    -- 14-day baseline), VOLUME_CONFOUNDED outside 0.7-1.3x. Dollars are never adjusted for volume.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),
           VERIFIED_USD = CASE
                              WHEN s.WH_SAVED_MONTHLY_USD < 5 THEN NULL
                              WHEN s.RN = 1 THEN ROUND(s.WH_SAVED_MONTHLY_USD, 2)
                              ELSE 0
                          END,
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured on the full window: '
                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' credits/day (14d baseline) -> '
                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))
                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))
                        || 'd after (' || s.VERDICT || '); floor $5/mo.'
                        || ' | volume ' || COALESCE(TO_VARCHAR(ROUND(s.VOL_RATIO, 2)), '?')
                        || 'x baseline queries/day'
                        || IFF(s.VOL_RATIO < 0.7 OR s.VOL_RATIO > 1.3,
                               ' - VOLUME_CONFOUNDED: the workload itself moved, so part of the credit delta may not be the lever (dollars not adjusted).',
                               '.')
                        || IFF(s.VERDICT = 'REGRESSED' AND s.WH_SAVED_MONTHLY_USD >= 5,
                               ' | PERF_REGRESSED: credits fell but latency, queueing or failures got worse (WH_CHANGE_REGRESSION).', '')
                        || IFF(s.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER'),
                               ' | PERF_UNJUDGED: fewer than 20 queries in a window, so latency/queue/failure axes were not judged.', '')
                        || IFF(s.WH_SAVED_MONTHLY_USD >= 5 AND s.RN > 1,
                               ' | LBA-1 co-attributed: warehouse saving booked once on change '
                               || s.PRIMARY_CHANGE_ID || '.', ''), 2000)
      FROM (SELECT r.CHANGE_ID, r.VERDICT, r.AFTER_DAYS,
                   r.BASELINE_CREDITS_PER_DAY AS BASE,
                   r.AFTER_CREDITS_PER_DAY AS AFT,
                   -- V153: per-day query volume after the change vs the 14-day baseline
                   -- (BASELINE_QUERIES is a 14-day total, AFTER_QUERIES a total over AFTER_DAYS).
                   (r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))
                       / NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS RN,
                   FIRST_VALUE(r.CHANGE_ID) OVER (
                       PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY,
                                    r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS
                       ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) AS PRIMARY_CHANGE_ID,
                   (COALESCE(r.BASELINE_CREDITS_PER_DAY, 0) - COALESCE(r.AFTER_CREDITS_PER_DAY, 0))
                       * :rate * 30 AS WH_SAVED_MONTHLY_USD
              FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
             WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')
               AND CURRENT_DATE() > r.TRACKING_UNTIL
               AND r.AFTER_CREDITS_PER_DAY IS NOT NULL
               -- Rank ONLY changes that Step-1 actually booked a ledger row for (the
               -- saving-direction levers). The registry also holds non-saving changes
               -- (SIZE up, AUTO_SUSPEND up) with the SAME measured-window signature but
               -- NO ledger row; if one of those won RN=1 it would carry the full saving
               -- into a row that doesn't exist while the genuine saving lever settled at
               -- $0 -- booking a real saving as ZERO. Restricting the population to booked
               -- levers guarantees the RN=1 primary always has a row to receive the USD.
               AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2
                            WHERE l2.SOURCE_CHANGE_ID = r.CHANGE_ID)) s
     WHERE l.SOURCE_CHANGE_ID = s.CHANGE_ID
       AND l.STATE = 'ESTIMATED';

    -- V153 close-out: a closed-window NO_BASELINE / INSUFFICIENT_AFTER change with NO metered credits
    -- after it (AFTER_CREDITS_PER_DAY NULL: the warehouse never ran after the change) has nothing to
    -- measure, so it closes REJECTED with no dollars. The V145 gate settled these through the credit
    -- formula with the NULL after-rate COALESCEd to 0, i.e. booked the WHOLE baseline as VERIFIED.
    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
       SET STATE = 'REJECTED',
           VERIFIED_AT = CURRENT_TIMESTAMP(),
           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',
           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | not measurable (' || r.VERDICT
                        || '): no metered credits after the change, so no saving is booked.', 2000)
      FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
     WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID
       AND l.STATE = 'ESTIMATED'
       AND r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')
       AND CURRENT_DATE() > r.TRACKING_UNTIL
       AND r.AFTER_CREDITS_PER_DAY IS NULL;

    RETURN 'OK';
END;
$$;

-- >>> derived:SP_VERIFY_IDLE_SAVINGS  (from V053; skip rows tied to a detected change; V153)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_IDLE_SAVINGS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_VERIFICATION_RUNS
        (ITEM_ID, WAREHOUSE_NAME, BASELINE_EST_USD, MEASURED_IDLE_USD_30D, PROPOSED_VERIFIED_USD)
    WITH items AS (
        SELECT ITEM_ID,
               COALESCE(NULLIF(TARGET_OBJECT, ''),
                        TRIM(REPLACE(DESCRIPTION, 'Auto-suspend tune: ', ''))) AS WAREHOUSE_NAME,
               ESTIMATED_USD
        FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        WHERE STATE = 'ESTIMATED'
          AND (FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %')
          -- V153: a row tied to a detected change (autobook or adopted) settles on the change
          -- scan's measured window -- an idle-spend proposal for it is noise, or a second verify.
          AND SOURCE_CHANGE_ID IS NULL
    ),
    query_hours AS (
        SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= DATEADD('day', -30, CURRENT_DATE())
          AND WAREHOUSE_NAME IS NOT NULL
    ),
    idle_now AS (
        SELECT M.WAREHOUSE_NAME,
               SUM(IFF(Q.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0)) * :credit_price AS IDLE_USD_30D
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
        LEFT JOIN query_hours Q
               ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
              AND Q.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
        WHERE M.START_TIME >= DATEADD('day', -30, CURRENT_DATE())
        GROUP BY M.WAREHOUSE_NAME
    )
    SELECT i.ITEM_ID, i.WAREHOUSE_NAME, i.ESTIMATED_USD,
           ROUND(COALESCE(n.IDLE_USD_30D, 0), 2),
           ROUND(GREATEST(0, i.ESTIMATED_USD - COALESCE(n.IDLE_USD_30D, 0)), 2)
    FROM items i
    LEFT JOIN idle_now n ON UPPER(n.WAREHOUSE_NAME) = UPPER(i.WAREHOUSE_NAME);

    RETURN 'savings verification run complete';
END;
$$;

-- Run once at apply (the same work TASK_LEDGER_AUTOBOOK does after the 06:40 America/Chicago change
-- scan). This is NOT a no-op: it can ADOPT a manual ESTIMATED row booked in the last 3 days whose change
-- the scan has already seen but not yet booked. On a FIRST apply nothing is settle-eligible yet (the V145
-- gate already settled every non-PENDING row, and a closed window is never PENDING); on a rebuild
-- replay it simply settles whatever windows have closed, with this file's logic. Run it under the
-- RUN_NEXT prelude ALTER SESSION SET TIMEZONE = 'America/Chicago' so CURRENT_DATE() and ADOPT's
-- LTZ -> NTZ cast use the task clock, not a UTC worksheet's. If it errors, SCHEMA_VERSION 153 is not
-- written; ADOPT stamps may already be committed (harmless under V145) -- roll back by re-running the
-- V145 and V053 CREATE OR REPLACE blocks.
CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 153 AS VERSION,
       'SP_LEDGER_AUTOBOOK settles on the FULL 14-day window (Next-Fifty #11): re-derived from V145 (V118 LBA-1 dedup + V038 core preserved byte-for-byte) with settle gate CURRENT_DATE() > TRACKING_UNTIL AND AFTER_CREDITS_PER_DAY IS NOT NULL over the 5 closed verdicts, so a change settles 15-16 days after it is made instead of ~3; NO_BASELINE / INSUFFICIENT_AFTER settle on metered credits with a PERF_UNJUDGED note and close REJECTED only when nothing was metered after the change (V145 booked the whole baseline for them); the settle note carries the per-day query-volume ratio with a VOLUME_CONFOUNDED marker outside 0.7-1.3x (dollars not adjusted) and PERF_REGRESSED; the credit rate is FLOAT via TRY_TO_DOUBLE (TRY_TO_NUMBER rounded 3.68 to 4, +8.7%); an app-booked manual row for the same change is ADOPTED (SOURCE_CHANGE_ID stamped, Next-Fifty #5 twin rule) instead of double-booked. No MIN_CLUSTERS arm. SP_VERIFY_IDLE_SAVINGS re-derived from V053 to skip change-tied rows. Forward-only (historical rows untouched; re-settle is an owner opt-in); no schema or task change; teardown unchanged.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 153);

-- =====================================================================
--  MIGRATION 4 of 5 -- APPLY V154 (idempotent; GUARDS on V153). Source: snowflake/migrations/V154__incident_attach_automitigate.sql
-- =====================================================================
-- V154__incident_attach_automitigate.sql
--
-- Incident loop (Next-Fifty #12b). Two gaps in SP_INCIDENT_AUTODECLARE:
--   * A later unlinked OPEN/ACK CRITICAL whose family ALREADY has an OPEN/MITIGATED incident in
--     the same company is dropped by the family-already-open guard (V099) and was never linked
--     anywhere, so incident timelines, RCA and member counts silently missed it. The new [attach]
--     arm links it to that incident. Its candidate set is exactly the guard's witness set, so every
--     such critical is either declared or attached. It never creates an incident or moves a STATUS.
--   * Nothing ever wrote STATUS = 'MITIGATED'. The new [auto-mitigate] sweep moves an OPEN incident
--     forward-only to MITIGATED once EVERY ALERT member has been RESOLVED for >= 1h (a member whose
--     event row is gone counts as live; a SUPERSEDED / SNOOZE_SUPPRESSED member blocks while a
--     same-rule same-company successor is still OPEN/ACK/SNOOZED). MITIGATED_AT = the last member
--     resolve, not the sweep clock. Never RESOLVED: closing stays human so the root cause is
--     captured; the app lists these as 'ready to close'. OWNER / ACK_AT are never written here.
--
-- Re-derives SP_INCIDENT_AUTODECLARE from V099 (its current definition; V098 re-link guard + V099
-- company scope kept), byte-identical except three deltas: DECLARE +attached/mitigated/emsg, the two
-- exception-isolated blocks inserted after the member INSERT, and a RETURN that reports the attach +
-- mitigate counts. Adds INCIDENTS.MITIGATED_BY VARCHAR(200) (machine provenance: the sweep stamps
-- 'SP_INCIDENT_AUTODECLARE'; NULL = a human Mark mitigated). ADD COLUMN IF NOT EXISTS is idempotent.
--
-- TOGGLE: both new arms sit AFTER the INCIDENT_AUTO_DECLARE_CRITICAL check (V099), so when
-- auto-declare is switched off in Settings the proc returns 'auto-declare off' before them -- no
-- attach and no auto-mitigate either. The setting defaults to TRUE when absent (probe C3 reads it).
--
-- FIRST RUN: OPEN incidents whose member alerts have all long since resolved move to MITIGATED on
-- the first run (MITIGATED_AT = their historical last resolve), so the 90d time-to-mitigate median
-- includes them until they age out. TASK_INCIDENT_AUTODECLARE runs AFTER TASK_LOAD_HOURLY, beside the
-- alert-scan chain, so attach / mitigate see the previous hour's alert state (MITIGATED_AT uses the
-- true resolve time, so the metric is unaffected).
--
-- No task change, no backfill and no CALL in this file: the owner runs one CALL in the RUN_NEXT
-- PART B verify grid (the same work the hourly task does; it raises no alert and sends no email).
-- Apply AFTER V153. Rollback: re-run V099's SP_INCIDENT_AUTODECLARE definition (MITIGATED_BY may stay).
-- This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20154, 'V154 requires V153 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 153) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Machine provenance for the [auto-mitigate] sweep (owner decision O-5). NULL = a human moved it.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.INCIDENTS ADD COLUMN IF NOT EXISTS MITIGATED_BY VARCHAR(200);

-- >>> derived:SP_INCIDENT_AUTODECLARE  (from V099; + [attach] arm + [auto-mitigate] sweep, Next-Fifty #12b)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    enabled VARCHAR;
    made INT DEFAULT 0;
    attached INT DEFAULT 0;
    mitigated INT DEFAULT 0;
    emsg VARCHAR;
BEGIN
    SELECT COALESCE(MAX(VALUE), 'TRUE') INTO :enabled
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'INCIDENT_AUTO_DECLARE_CRITICAL';
    IF (UPPER(:enabled) <> 'TRUE') THEN
        RETURN 'auto-declare off';
    END IF;

    CREATE OR REPLACE TEMPORARY TABLE _OW_AUTODECL AS
    WITH crit AS (
        SELECT e.EVENT_ID, e.COMPANY, e.SEVERITY, e.TITLE, e.RAISED_AT,
               SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE UPPER(e.SEVERITY) = 'CRITICAL'
          AND e.STATUS IN ('OPEN', 'ACK')
          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
    )
    SELECT UUID_STRING() AS INCIDENT_ID, FAMILY, COMPANY,
           MAX_BY(TITLE, RAISED_AT) AS TITLE,
           MIN(RAISED_AT) AS FIRST_TS
    FROM crit c
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT'
          AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND i.COMPANY = c.COMPANY
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY
    )
    GROUP BY FAMILY, COMPANY;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, STARTED_AT,
         ROOT_CAUSE_KIND, DECLARED_BY)
    SELECT INCIDENT_ID, LEFT('Auto: ' || TITLE, 300), 'CRITICAL', 'OPEN', COMPANY,
           CURRENT_TIMESTAMP(), FIRST_TS, 'UNKNOWN', 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
    SELECT d.INCIDENT_ID, 'ALERT', e.EVENT_ID, e.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL d
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
      ON e.COMPANY = d.COMPANY
     AND UPPER(e.SEVERITY) = 'CRITICAL'
     AND e.STATUS IN ('OPEN', 'ACK')
     AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
     AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY
     AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                     WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);

    -- [attach] V154 (Next-Fifty #12b): a later unlinked OPEN/ACK CRITICAL whose family ALREADY has an
    -- OPEN/MITIGATED incident in the SAME company is skipped by the family-already-open guard above
    -- (V099) and, before V154, was never linked anywhere -- timelines, RCA and member counts silently
    -- missed it. Link it to that incident instead. The candidate set is EXACTLY the guard's witness
    -- set (same company, an ALERT member of the same family, OPEN/MITIGATED), so every such critical
    -- is either declared above or attached here. One target per event: an incident already holding
    -- a member of the SAME entity (DEDUPE_KEY field 2) wins, then the newest DETECTED_AT, then
    -- INCIDENT_ID (deterministic). Never creates an incident and never changes a STATUS -- a re-fire
    -- on a MITIGATED incident shows as a live member, so it simply stops being 'ready to close'.
    -- Isolated: a failure logs and leaves the declare above intact.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
            (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
        SELECT t.INCIDENT_ID, 'ALERT', t.EVENT_ID, t.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
        FROM (
            SELECT e.EVENT_ID, e.RAISED_AT, i.INCIDENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i
              ON i.COMPANY = e.COMPANY
             AND i.STATUS IN ('OPEN', 'MITIGATED')
            JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
              ON m.INCIDENT_ID = i.INCIDENT_ID
             AND m.MEMBER_KIND = 'ALERT'
            JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a
              ON a.EVENT_ID = m.REF_ID
             AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1)
                 = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1)
            WHERE UPPER(e.SEVERITY) = 'CRITICAL'
              AND e.STATUS IN ('OPEN', 'ACK')
              AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                              WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID)
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.EVENT_ID
                ORDER BY IFF(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)
                             = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2), 0, 1),
                         i.DETECTED_AT DESC, i.INCIDENT_ID) = 1
        ) t;
        attached := SQLROWCOUNT;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'IncidentAutodeclare', 'incident_attach_failed', :emsg,
                   'V154 attach arm - the declare above is unaffected', CURRENT_ROLE();
    END;

    -- [auto-mitigate] V154 (Next-Fifty #12b): an OPEN incident whose EVERY ALERT member is RESOLVED
    -- (a member whose event row is gone counts as NOT resolved) moves forward-only to MITIGATED --
    -- never to RESOLVED: closing stays human so the root cause is captured (the app surfaces these
    -- as 'ready to close'). A member closed as SUPERSEDED / SNOOZE_SUPPRESSED is a machine hand-off
    -- to a successor, so it blocks while a same-rule same-company event raised at/after it is still
    -- OPEN/ACK/SNOOZED. >=1h dwell on the LAST resolve (anti-flap). MITIGATED_AT = when the last
    -- member resolved (not this sweep's clock), so time-to-mitigate is not inflated by the dwell or the
    -- hourly cadence -- and for a machine hand-off member, when its same-rule successor resolved (the
    -- condition really ended then; review fix: the member's own machine-close time understated MTTM and
    -- let the dwell pass on stale timestamps). MITIGATED_BY names the machine (a human Mark mitigated
    -- leaves it NULL). OWNER / ACK_AT are never touched here (MTTA stays a human number). Isolated like
    -- [attach].
    BEGIN
        CREATE OR REPLACE TEMPORARY TABLE _OW_INC_MITIGATE AS
        WITH live AS (
            SELECT RULE_ID, COMPANY, MAX(RAISED_AT) AS LAST_LIVE_AT
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED')
            GROUP BY RULE_ID, COMPANY
        )
        SELECT i.INCIDENT_ID,
               GREATEST(MAX(e.RESOLVED_AT), COALESCE(MAX(s.RESOLVED_AT), MAX(e.RESOLVED_AT)),
                        MAX(i.DETECTED_AT)) AS MITIGATED_TS
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
          ON m.INCIDENT_ID = i.INCIDENT_ID
         AND m.MEMBER_KIND = 'ALERT'
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          ON e.EVENT_ID = m.REF_ID
        LEFT JOIN live l
          ON l.RULE_ID = e.RULE_ID
         AND l.COMPANY = e.COMPANY
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
          ON COALESCE(e.RESOLUTION_KIND, '') IN ('SUPERSEDED', 'SNOOZE_SUPPRESSED')
         AND s.RULE_ID = e.RULE_ID
         AND s.COMPANY = e.COMPANY
         AND s.RAISED_AT >= e.RAISED_AT
         AND s.STATUS = 'RESOLVED'
        WHERE i.STATUS = 'OPEN'
        GROUP BY i.INCIDENT_ID
        HAVING COUNT_IF(e.EVENT_ID IS NULL OR e.STATUS <> 'RESOLVED') = 0
           AND COUNT_IF(COALESCE(e.RESOLUTION_KIND, '') IN ('SUPERSEDED', 'SNOOZE_SUPPRESSED')
                        AND l.LAST_LIVE_AT >= e.RAISED_AT) = 0
           AND GREATEST(MAX(e.RESOLVED_AT), COALESCE(MAX(s.RESOLVED_AT), MAX(e.RESOLVED_AT)))
               <= DATEADD('hour', -1, CURRENT_TIMESTAMP());

        UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENTS i
           SET STATUS = 'MITIGATED',
               MITIGATED_AT = r.MITIGATED_TS,
               MITIGATED_BY = 'SP_INCIDENT_AUTODECLARE',
               UPDATED_AT = CURRENT_TIMESTAMP()
          FROM _OW_INC_MITIGATE r
         WHERE i.INCIDENT_ID = r.INCIDENT_ID
           AND i.STATUS = 'OPEN';
        mitigated := SQLROWCOUNT;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'IncidentAutodeclare', 'incident_mitigate_failed', :emsg,
                   'V154 auto-mitigate sweep - declare/attach above unaffected', CURRENT_ROLE();
    END;

    SELECT COUNT(*) INTO :made FROM _OW_AUTODECL;
    RETURN 'auto-declared ' || :made || ' incident(s); attached ' || :attached || ' later critical(s); auto-mitigated ' || :mitigated || ' incident(s)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 154 AS VERSION,
       'Incident loop (Next-Fifty #12b): SP_INCIDENT_AUTODECLARE re-derived from V099 (V098 re-link guard + V099 company scope kept) with an [attach] arm linking a later unlinked OPEN/ACK CRITICAL to the OPEN/MITIGATED incident its family''s already-open guard matched, and an [auto-mitigate] sweep moving an OPEN incident to MITIGATED (never RESOLVED -- closing stays human) once every ALERT member has been RESOLVED for >= 1h (MITIGATED_AT = the last member resolve; a SUPERSEDED / SNOOZE_SUPPRESSED member blocks while its successor is live). Adds INCIDENTS.MITIGATED_BY (machine provenance; NULL = a human). Both arms are exception-isolated and sit after the INCIDENT_AUTO_DECLARE_CRITICAL toggle (off = neither runs); no task change, no backfill, no CALL at apply.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 154);

-- =====================================================================
--  MIGRATION 5 of 5 -- APPLY V155 (idempotent; GUARDS on V154). Source: snowflake/migrations/V155__operator_stats_sis_app_tag.sql
-- =====================================================================
-- V155__operator_stats_sis_app_tag.sql
--
-- Keep OVERWATCH's own statements out of the QOIE Slice 2 operator-stats collector.
-- Streamlit-in-Snowflake stamps every statement the app runs with its own QUERY_TAG
-- ({"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP", ...}) and overrides any tag the app sends
-- (owner diagnostic 2026-09-24). Since app v4.591.0, Operations query triage drops those statements;
-- SP_LOAD_QUERY_OPERATOR_STATS's candidate cursor (documented as triage's exact filter set) still
-- matched only the 'OVERWATCH%' prefix and '%OVERWATCH_APP%' text, which a SiS-stamped statement never
-- carries - so a heavy app read could land on the Operator boards while triage hid it.
--
-- Re-derives SP_LOAD_QUERY_OPERATOR_STATS from V147 (its current definition), byte-identical except ONE
-- added cursor predicate (test_v155 proves it). No schema change, no backfill (already collected app rows
-- age out inside the 30-day retention), no task change, no tail procedure run. Apply AFTER V154.
-- Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20155, 'V155 requires V154 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 154) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_QUERY_OPERATOR_STATS (from V147; + Streamlit-in-Snowflake app-tag exclusion in the candidate cursor, V155)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QUERY_OPERATOR_STATS(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    keep INT;
    landed INT DEFAULT 0;      -- operator ROWS actually inserted (SQLROWCOUNT sum)
    collected INT DEFAULT 0;   -- candidate queries that landed >= 1 operator row
    skipped INT DEFAULT 0;     -- candidates that errored OR returned empty (aged/utility)
    emsg VARCHAR;              -- last per-id SQLERRM sample (distinguishes a SQL bug from a skip)
    ins VARCHAR;
    -- Candidate query_ids: the recent (2-day, inside the 14-day operator-stats window)
    -- expensive queries, using query_optimization_triage's EXACT filter set (self-noise
    -- dropped, a genuine-inefficiency gate) so the two surfaces agree. INCREMENTAL: skip
    -- any query already collected. Capped at 250/run (one table-function call per row).
    c_qids CURSOR FOR
        SELECT qh.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
        WHERE qh.START_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP())
          AND qh.EXECUTION_STATUS = 'SUCCESS'
          AND qh.QUERY_TYPE <> 'CALL'
          AND UPPER(COALESCE(qh.QUERY_TEXT, '')) NOT LIKE 'EXECUTE STREAMLIT%'
          AND UPPER(COALESCE(qh.QUERY_TEXT, '')) NOT LIKE '%OVERWATCH_APP%'
          AND COALESCE(qh.QUERY_TAG, '') NOT LIKE 'OVERWATCH%'
          -- V155: Streamlit-in-Snowflake stamps the app's own statements with this tag (it overrides the
          -- app's), so match it too - parity with ops_sql.query_optimization_triage (common.not_app_self_sql).
          AND NOT CONTAINS(COALESCE(qh.QUERY_TAG, ''), '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"')
          AND (COALESCE(qh.BYTES_SPILLED_TO_REMOTE_STORAGE, 0) > 0
               OR COALESCE(qh.BYTES_SCANNED, 0) > 50 * POWER(1024, 3))
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
              WHERE f.QUERY_ID = qh.QUERY_ID)
        ORDER BY COALESCE(qh.BYTES_SPILLED_TO_REMOTE_STORAGE, 0) DESC,
                 qh.BYTES_SCANNED DESC
        LIMIT 250;
BEGIN
    -- DAYS_BACK bounds how many days of collected operator stats to RETAIN (default 30);
    -- the collection window itself is a fixed 2 days (inside the function's 14-day reach).
    keep := GREATEST(COALESCE(:DAYS_BACK, 30), 1)::INT;

    -- Land the raw operator rows per query_id. The query_id is a Snowflake UUID from
    -- ACCOUNT_USAGE (safe to embed); the table-function argument cannot be bound, so build
    -- the INSERT with EXECUTE IMMEDIATE. QUERY_* enrichment columns are filled set-based
    -- below. PARENT_OPERATORS is an ARRAY (BCR-1175) - store the first parent, NULL at root.
    FOR q IN c_qids DO
        BEGIN
            ins := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY '
                || '(QUERY_ID, STEP_ID, OPERATOR_ID, PARENT_OPERATOR_ID, OPERATOR_TYPE, '
                || 'INPUT_ROWS, OUTPUT_ROWS, ROW_MULTIPLE, REMOTE_SPILL_GB, LOCAL_SPILL_GB, '
                || 'GB_SCANNED, PARTITIONS_SCANNED, PARTITIONS_TOTAL, SCAN_PCT, OP_TIME_PCT) '
                || 'SELECT ' || '''' || q.QUERY_ID || '''' || ', STEP_ID, OPERATOR_ID, '
                || 'PARENT_OPERATORS[0]::NUMBER, OPERATOR_TYPE, '
                || 'OPERATOR_STATISTICS:input_rows::NUMBER, '
                || 'OPERATOR_STATISTICS:output_rows::NUMBER, '
                || 'ROUND(OPERATOR_STATISTICS:output_rows::FLOAT '
                || '/ NULLIF(OPERATOR_STATISTICS:input_rows::FLOAT, 0), 3), '
                || 'ROUND(OPERATOR_STATISTICS:spilling:bytes_spilled_remote_storage::FLOAT '
                || '/ POWER(1024, 3), 3), '
                || 'ROUND(OPERATOR_STATISTICS:spilling:bytes_spilled_local_storage::FLOAT '
                || '/ POWER(1024, 3), 3), '
                || 'ROUND(OPERATOR_STATISTICS:io:bytes_scanned::FLOAT / POWER(1024, 3), 3), '
                || 'OPERATOR_STATISTICS:pruning:partitions_scanned::NUMBER, '
                || 'OPERATOR_STATISTICS:pruning:partitions_total::NUMBER, '
                || 'ROUND(OPERATOR_STATISTICS:pruning:partitions_scanned::FLOAT '
                || '/ NULLIF(OPERATOR_STATISTICS:pruning:partitions_total::FLOAT, 0) * 100, 2), '
                -- V144: overall_percentage is a 0-1 fraction (owner probe) -> * 100 for a 0-100 _PCT.
                || 'ROUND(EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT * 100, 2) '
                || 'FROM TABLE(GET_QUERY_OPERATOR_STATS(' || '''' || q.QUERY_ID || '''' || '))';
            EXECUTE IMMEDIATE :ins;
            -- SQLROWCOUNT = operator rows the table function returned for this id. It can
            -- legitimately be 0 WITHOUT raising (an aged/utility id returns empty), so count
            -- ROWS LANDED, not INSERT successes — otherwise an all-empty run reports false
            -- success and the all-empty alert below never fires.
            landed := landed + SQLROWCOUNT;
            IF (SQLROWCOUNT > 0) THEN
                collected := collected + 1;
            ELSE
                skipped := skipped + 1;
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                -- An aged (>14d), non-existent, utility (no profile), or unauthorized
                -- query_id: skip it (expected), do not abort the run. Keep the last SQLERRM
                -- so a SYSTEMATIC dynamic-SQL bug (every id raises) is distinguishable from
                -- an expected skip when the all-empty alert fires (no CI executes this SQL).
                emsg := SQLERRM;
                skipped := skipped + 1;
        END;
    END FOR;

    -- Enrich the just-landed rows (QUERY_DAY IS NULL) from QUERY_HISTORY: the fingerprint
    -- hash to join Slice-1, the warehouse/company scope axis, and the query's elapsed time
    -- (so the app can turn OP_TIME_PCT into per-operator seconds). Set-based, no injection.
    UPDATE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
    SET QUERY_DAY = TO_DATE(qh.START_TIME),
        QUERY_PARAMETERIZED_HASH = qh.QUERY_PARAMETERIZED_HASH,
        WAREHOUSE_NAME = qh.WAREHOUSE_NAME,
        WAREHOUSE_SIZE = qh.WAREHOUSE_SIZE,
        COMPANY = DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qh.WAREHOUSE_NAME),
        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3),
        -- V147: identity grain so the Operator profile honors the User/Database/Schema scope
        -- filters (same QUERY_HISTORY columns the query-level _query_scope filters on).
        USER_NAME = qh.USER_NAME,
        DATABASE_NAME = qh.DATABASE_NAME,
        SCHEMA_NAME = qh.SCHEMA_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
    WHERE f.QUERY_ID = qh.QUERY_ID
      AND f.QUERY_DAY IS NULL
      -- -5d (candidate window is only -2d): if a prior run aborted between INSERT and this
      -- enrich, the NOT-EXISTS gate blocks re-collection, so a following run's QUERY_DAY-IS-
      -- NULL retry is the only self-heal path; the wider window gives it real grace to catch up.
      AND qh.START_TIME >= DATEADD('day', -5, CURRENT_TIMESTAMP());

    -- Retain `keep` days of collected operator stats (LOAD_TS is always set).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY
    WHERE LOAD_TS < DATEADD('day', -:keep, CURRENT_TIMESTAMP());

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_QUERY_OPERATOR_STATS_DAILY' AS SOURCE_NAME,
               MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET
        t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT,
        t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    -- 0 rows landed while candidates existed is a real signal: log ONE summary line, not
    -- one per skipped id (the false-error-noise lesson). emsg present => a per-id error
    -- (likely a systematic dynamic-SQL bug); emsg NULL => all-empty returns (privilege gap
    -- on the fleet warehouses for the owning role, or nothing inside the 14-day window).
    IF (landed = 0 AND skipped > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OperatorStatsCollector', 'operator_stats_all_empty',
               'GET_QUERY_OPERATOR_STATS landed 0 operator rows for ' || :skipped || ' candidate queries'
               || COALESCE(' | last per-id error: ' || :emsg, ' | no per-id error raised (all empty returns)'),
               'if an error is shown: likely a dynamic-SQL defect; if all empty: check OPERATE/MONITOR '
               || 'on the fleet warehouses for the owning role, or the 14-day operator-stats window',
               CURRENT_ROLE();
    END IF;

    RETURN 'OK landed=' || :landed || ' queries=' || :collected || ' skipped=' || :skipped;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 155 AS VERSION,
       'Operator-stats collector ignores the app''s own statements: SP_LOAD_QUERY_OPERATOR_STATS re-derived from V147 (byte-identical except one candidate-cursor predicate) to also exclude statements carrying the QUERY_TAG Streamlit-in-Snowflake stamps on every app statement (StreamlitName = DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP). SiS overrides the app''s own OVERWATCH tag, so the cursor''s OVERWATCH-prefix and OVERWATCH_APP-text filters never matched app traffic; Operations query triage already drops it (app v4.591.0), so the two surfaces agree again. No schema change, no backfill: already collected app rows age out inside the 30-day retention.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 155);

-- =====================================================================
--  PART B -- VERIFY (read-only except the one V154 CALL, which is the hourly task's own work).
--  Paste every grid back; an empty grid is an answer ('no rows').
-- =====================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- ---- V151 checks -----------------------------------------------------
-- ===================== PART B — V151 verify (run after MIGRATION 1 of 5) =====================
-- (V151.1) the new keep list is live in the view DDL (both TRUE)
SELECT CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'DROP MASKING POLICY %') AS V151_KEEP_LIST,
       CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'DROP BACKUP POLICY %') AS V151_BACKUP_OPENER;
-- (V151.2) read the view: a bad predicate errors only at read time, never at CREATE VIEW
SELECT DOMAIN, SEVERITY, COUNT(*) AS N
FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE
GROUP BY 1, 2 ORDER BY 1, 2;
-- expect: CHANGE RISK rows = probe A1 (CHANGE_RISK_ROWS_V088) + probe A2 (V151_KEEPS = TRUE) EVENTS_7D, all new rows CRITICAL.
-- 1 new CRITICAL row puts CHANGE RISK at Watch (75); 2 put it at Act (50) and turn the Security verdict bad.
-- (V151.3) every TF_* DESTRUCTIVE row now queued is an identity / policy drop.
-- Scoped to TITLE 'DESTRUCTIVE:%' because TF_* GRANT/REVOKE (PRIVILEGE) and ALTER/CREATE_USER rows were already in the queue before V151.
-- The QUERY_TYPE / PREVIEW_80 columns (joined back to the fact row) should all read DROP_USER / DROP_ROLE / DROP ... POLICY.
SELECT q.TITLE, q.DETAIL, q.SEVERITY, q.DETECTED_AT, f.QUERY_TYPE, LEFT(f.QUERY_PREVIEW, 80) AS PREVIEW_80
FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE q
LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE f
  ON f.EVENT_TS::TIMESTAMP_NTZ = q.DETECTED_AT
 AND f.CHANGE_KIND = 'DESTRUCTIVE'
 AND COALESCE(f.USER_NAME, 'unknown') || ' via ' || COALESCE(f.ROLE_NAME, 'unknown') = q.DETAIL
WHERE q.DOMAIN = 'CHANGE RISK'
  AND q.TITLE LIKE 'DESTRUCTIVE:%'
  AND q.DETAIL ILIKE '% via TF~_%' ESCAPE '~'
ORDER BY q.DETECTED_AT DESC;
SELECT VERSION, APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 151;

-- ---- V152 checks -----------------------------------------------------
-- (V152.1) both loader edits are live, and the view row reads (predicate errors surface only at read time)
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), '(''MART_TASK_NODE_DAILY'', ''task_node'')') AS SRCMAP_OK,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(FLOAT)'), 'SELECT ''MART_CLOUD_SVC_DAILY''') AS QH_STAMP_OK;   -- both TRUE
SELECT SOURCE_NAME, LAST_LOAD_TS, HOURS_SINCE_LOAD FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS WHERE SOURCE_NAME = 'MART_TASK_NODE_DAILY';   -- 1 row, HOURS_SINCE_LOAD about < 1-2h
-- (V152.2) AFTER the next hourly run (about :10-:30 CT), NOT at apply time:
SELECT SOURCE_NAME, LAST_LOAD_TS, STATUS, GENERATION FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('MART_CLOUD_SVC_DAILY', 'MART_TASK_NODE_DAILY');   -- STATUS 'loader' for MART_CLOUD_SVC_DAILY, 'task_node' for MART_TASK_NODE_DAILY

-- ---- V153 checks -----------------------------------------------------
-- (V153.1) The V153 tail CALL above must have returned 'OK'. Proc shape (expect every column TRUE):
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()'), 'AND CURRENT_DATE() > r.TRACKING_UNTIL') AS FULL_WINDOW_GATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()'), 'AND r.AFTER_CREDITS_PER_DAY IS NOT NULL') AS SETTLES_ON_METERED_CREDITS,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()'), 'AND r.AFTER_CREDITS_PER_DAY IS NULL;') AS CLOSEOUT_ONLY_UNMETERED,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()'), 'rate FLOAT') AS FLOAT_RATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()'), 'adopted by the daily change scan') AS ADOPT_ARM,
       NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()'), 'MIN_CLUSTERS') AS NO_MIN_ARM,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_VERIFY_IDLE_SAVINGS()'), 'AND SOURCE_CHANGE_ID IS NULL') AS VERIFIER_SKIPS_AUTO;
-- the rate the autobook uses now vs the pre-V153 rounded one (sizes the +8.7% history)
SELECT VALUE AS CREDIT_PRICE_SETTING, TRY_TO_NUMBER(VALUE) AS PRE_V153_RATE, TRY_TO_DOUBLE(VALUE) AS V153_RATE
FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD';
-- what is waiting to settle, and when (a row settles the morning AFTER its TRACKING_UNTIL)
SELECT r.VERDICT, COUNT(*) AS WAITING, MIN(r.TRACKING_UNTIL) AS FIRST_SETTLES_AFTER, MAX(r.TRACKING_UNTIL) AS LAST_SETTLES_AFTER,
       COUNT_IF(r.AFTER_CREDITS_PER_DAY IS NULL) AS NOTHING_METERED_YET
FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r ON r.CHANGE_ID = l.SOURCE_CHANGE_ID
WHERE l.STATE = 'ESTIMATED' GROUP BY 1 ORDER BY 1;
-- manual rows the apply-time CALL adopted (0 is normal)
SELECT COUNT(*) AS ADOPTED FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER WHERE NOTES LIKE '%adopted by the daily change scan%';
-- V153 settles so far: expect 0 at apply; the count rises from FIRST_SETTLES_AFTER + 1 day
SELECT COUNT_IF(NOTES LIKE '%measured on the full window%') AS FULL_WINDOW_SETTLES,
       COUNT_IF(NOTES LIKE '%not measurable (%') AS CLOSED_UNMETERED
FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER;

-- ---- V154 checks -----------------------------------------------------
-- (V154.1) column + proc landed (V098 guard / V099 scope kept)
SELECT COUNT(*) AS MITIGATED_BY_COL FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'INCIDENTS' AND COLUMN_NAME = 'MITIGATED_BY';   -- 1
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), '[attach] V154') AS ATTACH_ARM,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), '[auto-mitigate] V154') AS MITIGATE_SWEEP,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), 'AND i.COMPANY = c.COMPANY') AS V099_SCOPE_KEPT;   -- all TRUE
SELECT KEY, VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'INCIDENT_AUTO_DECLARE_CRITICAL';   -- no row or TRUE = both new arms run
-- run the new arms once (same work as the hourly task; no alerts, no email)
CALL DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE();   -- 'auto-declared X incident(s); attached A later critical(s); auto-mitigated M incident(s)' ('auto-declare off' = toggle off)
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

ALTER SESSION UNSET TIMEZONE;
