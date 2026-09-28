-- V161__retire_operator_backups.sql
--
-- Retire the scheduled operator-data backups (owner decision 2026-09-28). V158 (Next-Fifty #32) made
-- TASK_BACKUP_OPERATOR clone the 25 operator tables every day at 05:10 Central into dated TRANSIENT
-- generations in their own schema, DBA_MAINT_DB.OVERWATCH_BAK, next to V089's weekly <T>_BAK_LAST copies in
-- OVERWATCH, with a row-count ledger (OPERATOR_BACKUP_LOG), SETTINGS BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY
-- and the dead-man freshness row OPERATOR_BACKUP_DAILY. All of it goes. Recovery from here on is Snowflake
-- Time Travel (INSERT OVERWRITE ... AT / BEFORE, UNDROP) plus the manual <T>_BAK_<yyyymmdd> clones taken
-- before a risky change (teardown.sql B0, snowflake/rebuild/00_backup_operator_data.sql), which stay.
-- Retention: no migration sets DATA_RETENTION_TIME_IN_DAYS, so the account default applies; the TRANSIENT
-- operator tables (ALERT_EVENTS, ACTION_QUEUE, ...) keep at most 1 day of Time Travel and no Fail-safe.
--
-- Order (every drop existence-gated, so a re-run adds nothing):
-- * Preflight: DROP SCHEMA cascades, so V161 stops (-20611) if OVERWATCH_BAK holds a table or view that is not
--   a V158 generation, a moved <T>_BAK_LAST or OPERATOR_BACKUP_LOG. PREFLIGHT_V161.sql also lists non-table
--   objects (SHOW OBJECTS); read it before applying.
-- * TASK_BACKUP_OPERATOR is suspended, then V161 stops (-20612) while a run is in flight (a replay's V158 tail
--   starts one seconds earlier): its freshness MERGE could re-create the dead-man row after the DELETE below.
--   Wait a few minutes and re-run V161. Avoid applying between about 05:05 and 05:20 Central.
-- * The task and SP_BACKUP_OPERATOR_TABLES are dropped. OPERATOR_BACKUP_LOG and the 25 <T>_BAK_LAST copies
--   are moved into OVERWATCH_BAK (a DROP if the move fails) and the schema is dropped once, with every
--   generation in it. Names come only from the hard-coded 25 of V158; nothing is dropped by pattern, so the
--   manual <T>_BAK_<yyyymmdd> clones in OVERWATCH are never touched.
-- * SETTINGS BACKUP_KEEP_* and, last, SOURCE_FRESHNESS_STATE OPERATOR_BACKUP_DAILY are deleted (nothing
--   re-creates the row once the proc is gone), and any open OPS_PIPELINE_DEGRADED stale event for it is
--   closed as EXPECTED.
-- * V_SECURITY_EXCEPTION_QUEUE is re-derived from V158 without its backup-prune carve-out, which leaves V151's
--   view text. No prune has run before 2026-10-03 (BACKUP_KEEP_DAILY 7) / 2026-10-10 (the default 14), so
--   applied before then the carve-out never matched a row and the queue does not change.
--
-- Security footprint (disclosed; nothing emails or pages): applied as SNOW_ACCOUNTADMINS, the three DROPs
-- (task, proc, schema) land in FACT_SECURITY_CHANGE as DESTRUCTIVE / CRITICAL, so the Security page shows
-- CHANGE RISK "Act" for 7 days. The 26 RENAMEs score as ALTERs (MEDIUM), below the queue; a move that falls
-- back to a DROP adds one more CRITICAL row. Never delete these FACT_SECURITY_CHANGE rows.
--
-- Rolling back V161: re-run V015's TASK_BACKUP_OPERATOR block (lines 61-67; not the whole file, which would
-- re-create the retired MART_SPEND_ROLLUP_DT), then V158 in full. The dropped generations do not come back.
-- Owner applies in Snowsight after V160, as the role that applied V158 (it owns OVERWATCH_BAK and the task).
-- This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20161, 'V161 requires V160 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 160) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- 1. Preflight (read-only): DROP SCHEMA cascades, so stop before anything changes if OVERWATCH_BAK holds a
--    table or view that is not a V158 generation, a moved <T>_BAK_LAST, or OPERATOR_BACKUP_LOG.
EXECUTE IMMEDIATE
$$
DECLARE
    foreign_n NUMBER DEFAULT 0;
    foreign_objects EXCEPTION (-20611, 'V161 stopped before any change: DBA_MAINT_DB.OVERWATCH_BAK holds objects V158 did not create. Move them out (PREFLIGHT_V161.sql lists them), then re-run V161.');
BEGIN
    SELECT COUNT(*) INTO :foreign_n
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'OVERWATCH_BAK'
       AND NOT (TABLE_TYPE = 'BASE TABLE'
                AND (REGEXP_LIKE(TABLE_NAME, '(SETTINGS|COMPANY_SCOPE|ALERT_CONFIG|ALERT_EVENTS|ALERT_AUDIT|ACTION_QUEUE|SAVINGS_LEDGER|DEPARTMENT_MAP|ALERT_ROUTES|REMEDIATION_LOG|USER_PREFS|OBJECT_CHANGE_REGISTRY|WAREHOUSE_CHANGE_REGISTRY|WAREHOUSE_CONFIG_SNAPSHOT|PIPELINE_SLA_CONFIG|DAILY_DIGEST|DEPT_BUDGETS|INCIDENTS|INCIDENT_MEMBERS|ACTION_ACTIVITY|EVIDENCE_LINKS|ENTITY_CATALOG|USER_WATCHLIST|OPTIMIZATION_EXPERIMENTS|SLO_OBJECTIVES)_OWBAK_[DW][0-9]{8}')
                     OR TABLE_NAME IN ('OPERATOR_BACKUP_LOG', 'SETTINGS_BAK_LAST', 'COMPANY_SCOPE_BAK_LAST', 'ALERT_CONFIG_BAK_LAST',
                                       'ALERT_EVENTS_BAK_LAST', 'ALERT_AUDIT_BAK_LAST', 'ACTION_QUEUE_BAK_LAST', 'SAVINGS_LEDGER_BAK_LAST',
                                       'DEPARTMENT_MAP_BAK_LAST', 'ALERT_ROUTES_BAK_LAST', 'REMEDIATION_LOG_BAK_LAST', 'USER_PREFS_BAK_LAST',
                                       'OBJECT_CHANGE_REGISTRY_BAK_LAST', 'WAREHOUSE_CHANGE_REGISTRY_BAK_LAST', 'WAREHOUSE_CONFIG_SNAPSHOT_BAK_LAST', 'PIPELINE_SLA_CONFIG_BAK_LAST',
                                       'DAILY_DIGEST_BAK_LAST', 'DEPT_BUDGETS_BAK_LAST', 'INCIDENTS_BAK_LAST', 'INCIDENT_MEMBERS_BAK_LAST',
                                       'ACTION_ACTIVITY_BAK_LAST', 'EVIDENCE_LINKS_BAK_LAST', 'ENTITY_CATALOG_BAK_LAST', 'USER_WATCHLIST_BAK_LAST',
                                       'OPTIMIZATION_EXPERIMENTS_BAK_LAST', 'SLO_OBJECTIVES_BAK_LAST')));
    IF (foreign_n > 0) THEN
        RAISE foreign_objects;
    END IF;
END;
$$;

-- 2. Stop the 05:10 run (a standalone root: no AFTER dependents, V015:61-67).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;

-- 3. In-flight guard, AFTER the suspend (a started task always shows its next run as SCHEDULED in the future):
--    stop while a run executes or is due now, so its freshness MERGE cannot re-create the row deleted below.
--    A SCHEDULED row more than 30 minutes overdue is stale, not in flight, and never blocks.
EXECUTE IMMEDIATE
$$
DECLARE
    running NUMBER DEFAULT 0;
    backup_in_flight EXCEPTION (-20612, 'V161 stopped: a TASK_BACKUP_OPERATOR run is in flight. The task is now suspended; wait a few minutes for the run to finish, then re-run V161.');
BEGIN
    SELECT COUNT(*) INTO :running
      FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
               SCHEDULED_TIME_RANGE_START => DATEADD('hour', -6, CURRENT_TIMESTAMP()),
               TASK_NAME => 'TASK_BACKUP_OPERATOR'))
     WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
       AND (STATE = 'EXECUTING'
            OR (STATE = 'SCHEDULED'
                AND SCHEDULED_TIME <= CURRENT_TIMESTAMP()
                AND SCHEDULED_TIME >= DATEADD('minute', -30, CURRENT_TIMESTAMP())));
    IF (running > 0) THEN
        RAISE backup_in_flight;
    END IF;
END;
$$;

-- 4. Retire the objects. Existence-gated: a no-op DROP IF EXISTS still succeeds and is scored as a DROP, so a
--    re-run must issue none. The ledger and the 25 weekly copies move into OVERWATCH_BAK so ONE DROP SCHEMA
--    removes them with every generation (3 CRITICAL CHANGE RISK rows in all: task, proc, schema); a move that
--    fails falls back to a DROP of that one table.
EXECUTE IMMEDIATE
$$
DECLARE
    targets ARRAY DEFAULT [
        'OPERATOR_BACKUP_LOG', 'SETTINGS_BAK_LAST', 'COMPANY_SCOPE_BAK_LAST', 'ALERT_CONFIG_BAK_LAST',
        'ALERT_EVENTS_BAK_LAST', 'ALERT_AUDIT_BAK_LAST', 'ACTION_QUEUE_BAK_LAST', 'SAVINGS_LEDGER_BAK_LAST',
        'DEPARTMENT_MAP_BAK_LAST', 'ALERT_ROUTES_BAK_LAST', 'REMEDIATION_LOG_BAK_LAST', 'USER_PREFS_BAK_LAST',
        'OBJECT_CHANGE_REGISTRY_BAK_LAST', 'WAREHOUSE_CHANGE_REGISTRY_BAK_LAST', 'WAREHOUSE_CONFIG_SNAPSHOT_BAK_LAST', 'PIPELINE_SLA_CONFIG_BAK_LAST',
        'DAILY_DIGEST_BAK_LAST', 'DEPT_BUDGETS_BAK_LAST', 'INCIDENTS_BAK_LAST', 'INCIDENT_MEMBERS_BAK_LAST',
        'ACTION_ACTIVITY_BAK_LAST', 'EVIDENCE_LINKS_BAK_LAST', 'ENTITY_CATALOG_BAK_LAST', 'USER_WATCHLIST_BAK_LAST',
        'OPTIMIZATION_EXPERIMENTS_BAK_LAST', 'SLO_OBJECTIVES_BAK_LAST'
    ];
    found_names ARRAY;
    tname VARCHAR;
    n NUMBER DEFAULT 0;
    bak_schema BOOLEAN DEFAULT FALSE;
    i INT;
BEGIN
    SHOW TASKS LIKE 'TASK_BACKUP_OPERATOR' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_BACKUP_OPERATOR';
    IF (n > 0) THEN
        DROP TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;
    END IF;

    SELECT COUNT(*) INTO :n
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
     WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_BACKUP_OPERATOR_TABLES'
       AND ARGUMENT_SIGNATURE = '()';
    IF (n > 0) THEN
        DROP PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES();
    END IF;

    SELECT COUNT(*) > 0 INTO :bak_schema
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.SCHEMATA
     WHERE SCHEMA_NAME = 'OVERWATCH_BAK';

    -- ONE probe for the 26 names (exact names only: never a LIKE sweep, the manual clones share OVERWATCH).
    SELECT COALESCE(ARRAY_AGG(TABLE_NAME) WITHIN GROUP (ORDER BY TABLE_NAME), ARRAY_CONSTRUCT())
      INTO :found_names
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_TYPE = 'BASE TABLE'
       AND ARRAY_CONTAINS(TABLE_NAME::VARIANT, :targets);
    IF (ARRAY_SIZE(:found_names) > 0) THEN
        FOR i IN 0 TO ARRAY_SIZE(:found_names) - 1 DO
            tname := GET(:found_names, i)::VARCHAR;
            IF (bak_schema) THEN
                BEGIN
                    EXECUTE IMMEDIATE 'ALTER TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||
                                      ' RENAME TO DBA_MAINT_DB.OVERWATCH_BAK.' || :tname;
                EXCEPTION
                    WHEN OTHER THEN
                        EXECUTE IMMEDIATE 'DROP TABLE DBA_MAINT_DB.OVERWATCH.' || :tname;
                END;
            ELSE
                EXECUTE IMMEDIATE 'DROP TABLE DBA_MAINT_DB.OVERWATCH.' || :tname;
            END IF;
        END FOR;
    END IF;

    IF (bak_schema) THEN
        DROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK CASCADE;
    END IF;
END;
$$;

-- 5. The proc's SETTINGS (its only reader was the proc, V158:140-145).
DELETE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY');

-- 6. The dead-man freshness row, LAST: its only writer (the proc) is gone, so nothing re-creates it, and no
--    expected-source registry alerts on its absence (V043's retire idiom).
DELETE FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY';

-- 7. Close any open stale event for it (key shape V157 / V160: RULE|STALE|SOURCE|day); SNOOZED too, or the hourly
--    wake step would reopen an event no scan can close again (V157's retire idiom). ALERT_EVENTS history stays.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',
       RESOLVED_AT = CURRENT_TIMESTAMP()
 WHERE RULE_ID = 'OPS_PIPELINE_DEGRADED'
   AND STARTSWITH(DEDUPE_KEY, 'OPS_PIPELINE_DEGRADED|STALE|OPERATOR_BACKUP_DAILY|')
   AND STATUS IN ('OPEN', 'ACK', 'SNOOZED');

-- 8. The Security exception queue without V158's backup-prune carve-out (nothing is left for it to match).
-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V158; - the OVERWATCH_BAK backup-prune carve-out, leaving V151's view text, V161)
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
SELECT 161 AS VERSION,
       'Scheduled operator-data backups retired (owner decision 2026-09-28): recovery is Snowflake Time Travel plus the manual <T>_BAK_<yyyymmdd> clones taken before a risky change (teardown.sql B0, rebuild/00), which stay. Suspends then drops TASK_BACKUP_OPERATOR (stopping first if a run is in flight), drops SP_BACKUP_OPERATOR_TABLES, moves OPERATOR_BACKUP_LOG and the 25 weekly <T>_BAK_LAST copies into DBA_MAINT_DB.OVERWATCH_BAK and drops that schema once with every V158 generation (a preflight stops first if the schema holds anything else; every drop is existence-gated, names come only from V158''s 25). Deletes SETTINGS BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY and the OPERATOR_BACKUP_DAILY freshness row, closes any open OPS_PIPELINE_DEGRADED stale event for it as EXPECTED, and re-derives V_SECURITY_EXCEPTION_QUEUE from V158 without the backup-prune carve-out (V151''s view text). Footprint: 3 CRITICAL CHANGE RISK rows (task, proc, schema) for 7 days; the renames score MEDIUM. No task created, nothing runs at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 161);
