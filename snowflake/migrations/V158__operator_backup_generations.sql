-- V158__operator_backup_generations.sql
--
-- Next-Fifty #32: operator-data backups rotate DAILY into dated generations instead of one weekly
-- *_BAK_LAST overwrite (a bad bulk edit noticed a day later could lose up to a week; after the next
-- Sunday the only backup already held the corruption).
--
-- * New TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK (owner-only). roles.sql grants FUTURE TABLES
--   only in OVERWATCH, so generations here add no grant/revoke churn to OVERWATCH (recent grant
--   changes, the RCA feed, unused-grant candidates, tag coverage) and survive a drop of OVERWATCH.
-- * SP_BACKUP_OPERATOR_TABLES re-derived from V089 (its current definer): every day at 05:10 Central
--   each of the 25 operator tables is cloned to an immutable TRANSIENT
--   OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd> (Central day). Sundays also clone <T>_OWBAK_W<yyyymmdd> from
--   that D generation and run the V089 <T>_BAK_LAST statement unchanged (still weekly). A table
--   missing on this install is a logged skip, not a failure.
-- * Backup-vs-source row counts go to the new OPERATOR_BACKUP_LOG (the proc trims it at 400 days).
-- * Prune: newest SETTINGS BACKUP_KEEP_DAILY (14) / BACKUP_KEEP_WEEKLY (8) generations per table and
--   kind (floors 7 / 4, ceilings 60 / 52). Only TRANSIENT base tables in OVERWATCH_BAK whose whole
--   name is one of the 25 + _OWBAK_[DW] + 8 digits, dated before today, re-checked before each DROP.
--   The manual <T>_BAK_<yyyymmdd> DR clones (teardown.sql B0, rebuild/00) use a different token.
-- * SOURCE_FRESHNESS_STATE 'OPERATOR_BACKUP_DAILY' (30h cadence by name) advances only on a run with
--   zero clone failures, so a failing or suspended backup goes stale and the dead-man paths fire.
-- * TASK_BACKUP_OPERATOR was created IF NOT EXISTS (V015), so its schedule moves in place
--   (SUSPEND / SET SCHEDULE / RESUME) from Sunday 05:40 to daily 05:10 Central.
-- * V_SECURITY_EXCEPTION_QUEUE re-derived from V151 (its current definer) with ONE carve-out: the
--   prune DROPs score DESTRUCTIVE 100 / CRITICAL, so a DROP run by the task (USER_NAME SYSTEM) whose
--   statement is exactly the generated OVERWATCH_BAK generation DROP leaves the CHANGE RISK queue.
--   The first prune happens 15 days after apply, so the carve-out is always in place first.
--
-- Restore = INSERT OVERWRITE as the table-owner role (RUNBOOK section 16): a TRANSIENT backup cannot
-- CLONE back into a permanent table, and a CLONE restore would re-apply the schema FUTURE grants.
-- The tail starts the first generation through the TASK (asynchronous; it runs as SYSTEM, inside the
-- carve-out, keyed on the Central day whatever the worksheet zone). Nothing is pruned on day 1.
-- Owner applies in Snowsight after V157. This file never runs from the app.
-- Rollback: V089:27-71 proc, ALTER TASK ... SET SCHEDULE = 'USING CRON 40 5 * * 0 America/Chicago',
-- and the V151 view.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20158, 'V158 requires V157 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 157) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Dedicated backup schema (decision O-12). TRANSIENT: generations need no Fail-safe. No grants: the
-- proc owner (the role applying this file) owns every generation and runs every restore.
CREATE TRANSIENT SCHEMA IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK COMMENT = 'OVERWATCH operator-data backup generations (V158). Owner-only; never dropped by teardown.';

-- Retention (decision O-13), Admin-editable within 7-60 / 4-52. WHEN NOT MATCHED only: an
-- operator's edited value is never overwritten.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('BACKUP_KEEP_DAILY', '14'),
        ('BACKUP_KEEP_WEEKLY', '8')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- Backup ledger (operator history: teardown lists it commented, never a live drop).
-- ACTION: CLONED / SKIPPED_MISSING / CLONE_FAILED / PRUNED / PRUNE_FAILED.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG (
    RUN_ID           VARCHAR(64)   NOT NULL,
    GENERATION       VARCHAR(16)   NOT NULL,
    SOURCE_TABLE     VARCHAR(256)  NOT NULL,
    BACKUP_TABLE     VARCHAR(256),
    ACTION           VARCHAR(20)   NOT NULL,
    ROW_COUNT        NUMBER(38,0),
    SOURCE_ROW_COUNT NUMBER(38,0),
    BYTES            NUMBER(38,0),
    DETAIL           VARCHAR(1000),
    LOGGED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- >>> derived:SP_BACKUP_OPERATOR_TABLES  (from V089; daily dated generations in OVERWATCH_BAK + keep-count prune + row-count log + freshness stamp, V158)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    tables ARRAY DEFAULT [
        'SETTINGS', 'COMPANY_SCOPE', 'ALERT_CONFIG', 'ALERT_EVENTS',
        'ALERT_AUDIT', 'ACTION_QUEUE', 'SAVINGS_LEDGER', 'DEPARTMENT_MAP',
        'ALERT_ROUTES', 'REMEDIATION_LOG', 'USER_PREFS',
        'OBJECT_CHANGE_REGISTRY', 'WAREHOUSE_CHANGE_REGISTRY',
        'WAREHOUSE_CONFIG_SNAPSHOT', 'PIPELINE_SLA_CONFIG', 'DAILY_DIGEST',
        'DEPT_BUDGETS', 'INCIDENTS', 'INCIDENT_MEMBERS', 'ACTION_ACTIVITY',
        'EVIDENCE_LINKS', 'ENTITY_CATALOG', 'USER_WATCHLIST',
        'OPTIMIZATION_EXPERIMENTS', 'SLO_OBJECTIVES'
    ];
    tname VARCHAR;
    emsg VARCHAR;
    done INT DEFAULT 0;
    i INT;
    -- V158: dated generations in DBA_MAINT_DB.OVERWATCH_BAK, keep-count prune, row-count log,
    -- freshness stamp.
    failed INT DEFAULT 0;          -- clone failures: they hold the freshness stamp
    missing INT DEFAULT 0;         -- source table absent on this install: a skip, never a failure
    pruned INT DEFAULT 0;
    prune_failed INT DEFAULT 0;
    present INT DEFAULT 0;
    keep_d FLOAT DEFAULT 14;
    keep_w FLOAT DEFAULT 8;
    day_ct DATE;
    gen_d VARCHAR;
    gen_w VARCHAR;
    is_sunday BOOLEAN DEFAULT FALSE;
    run_id VARCHAR;
    prune_re VARCHAR;
    pname VARCHAR;
    total_rows NUMBER(38,0) DEFAULT 0;
    fstatus VARCHAR;
    res RESULTSET;
BEGIN
    run_id := UUID_STRING();
    -- Retention from SETTINGS (Admin-editable; seeded 14 / 8 above). A missing or non-numeric value
    -- falls back to the default; the floors (7 daily / 4 weekly) mean no value can prune the history
    -- away, and the ceilings (60 / 52) bound storage.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_DAILY', VALUE, NULL))), 14),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_WEEKLY', VALUE, NULL))), 8)
      INTO :keep_d, :keep_w
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
    keep_d := LEAST(GREATEST(ROUND(keep_d), 7), 60);
    keep_w := LEAST(GREATEST(ROUND(keep_w), 4), 52);

    -- Generation key = the America/Chicago calendar day (TIMEZONE STANDARD), never the session zone.
    day_ct := CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE;
    gen_d := 'D' || TO_CHAR(day_ct, 'YYYYMMDD');
    gen_w := 'W' || TO_CHAR(day_ct, 'YYYYMMDD');
    is_sunday := (DAYOFWEEKISO(day_ct) = 7);
    -- The ONLY names the prune may ever touch: one of the 25 + _OWBAK_ + D/W + 8 digits. REGEXP_LIKE
    -- anchors the whole name, so a manual <T>_BAK_<yyyymmdd> DR clone can never match.
    prune_re := '(' || ARRAY_TO_STRING(:tables, '|') || ')_OWBAK_[DW][0-9]{8}';

    FOR i IN 0 TO ARRAY_SIZE(:tables) - 1 DO
        tname := GET(:tables, i)::VARCHAR;
        -- Source present on this install? Fails OPEN: if the metadata probe itself errors, the clone
        -- is attempted exactly as V089 did (a missing source then lands as clone_failed).
        present := 1;
        BEGIN
            SELECT COUNT(*) INTO :present
              FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = :tname
               AND TABLE_TYPE = 'BASE TABLE';
        EXCEPTION
            WHEN OTHER THEN
                present := 1;
        END;
        IF (present = 0) THEN
            missing := missing + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                (RUN_ID, GENERATION, SOURCE_TABLE, ACTION, DETAIL)
            SELECT :run_id, :gen_d, :tname, 'SKIPPED_MISSING', 'source table absent on this install';
        ELSE
            BEGIN
                -- V158: the daily generation, immutable once taken (IF NOT EXISTS), in the dedicated
                -- TRANSIENT schema OVERWATCH_BAK (no FUTURE grants there, so no grant churn).
                EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                  '_OWBAK_' || :gen_d || ' CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;
                IF (is_sunday) THEN
                    -- Sundays: the weekly generation, cloned from today's daily one inside OVERWATCH_BAK,
                    -- and the V089 *_BAK_LAST pointer in OVERWATCH, still weekly (statement unchanged).
                    EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                      '_OWBAK_' || :gen_w || ' CLONE DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                      '_OWBAK_' || :gen_d;
                    -- V089: TRANSIENT target -- a transient source (ALERT_EVENTS,
                    -- ACTION_QUEUE, ...) cannot clone into a PERMANENT table
                    -- ("Transient object cannot be cloned to a permanent object"),
                    -- which failed those backups every run. TRANSIENT works for both
                    -- transient and permanent sources and needs no Fail-safe.
                    EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||
                                      '_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;
                END IF;
                done := done + 1;
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    failed := failed + 1;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'BackupOperatorTables', 'clone_failed', LEFT(:emsg, 2000),
                           'table ' || :tname || ' generation ' || :gen_d || ' (OPERATOR_BACKUP_DAILY)', CURRENT_ROLE();
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                        (RUN_ID, GENERATION, SOURCE_TABLE, ACTION, DETAIL)
                    SELECT :run_id, :gen_d, :tname, 'CLONE_FAILED', LEFT(:emsg, 1000);
            END;
        END IF;
    END FOR;

    -- Row counts: INFORMATION_SCHEMA metadata only, no table scan. One CLONED row per generation
    -- taken today, backup vs source, so a restore can pick a generation on evidence. Isolated: a
    -- failure here never blocks the prune, the freshness stamp or the RETURN.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
        SELECT :run_id, :gen_d, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
        FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
        JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
          ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
         AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_d
        WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
          AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
        SELECT COALESCE(SUM(ROW_COUNT), 0) INTO :total_rows
          FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
         WHERE RUN_ID = :run_id AND ACTION = 'CLONED';
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_log_failed', LEFT(:emsg, 2000),
                   'row-count log ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): generations taken, counts not logged', CURRENT_ROLE();
    END;

    -- Prune: keep the newest keep_d daily / keep_w weekly generations per table and kind (rank-based,
    -- so a paused task never empties the history; today's generation is never a candidate). Only
    -- TRANSIENT base tables in DBA_MAINT_DB.OVERWATCH_BAK whose whole name matches prune_re, and the
    -- regex is re-checked right before each DROP. Isolated like the log above.
    BEGIN
        res := (
            SELECT g.TABLE_NAME
            FROM (
                SELECT TABLE_NAME,
                       LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16) AS BASE_NAME,
                       SUBSTR(TABLE_NAME, LENGTH(TABLE_NAME) - 8, 1) AS GEN_KIND,
                       TRY_TO_DATE(RIGHT(TABLE_NAME, 8), 'YYYYMMDD') AS GEN_DAY
                FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_CATALOG = 'DBA_MAINT_DB'
                  AND TABLE_SCHEMA = 'OVERWATCH_BAK'
                  AND TABLE_TYPE = 'BASE TABLE'
                  AND IS_TRANSIENT = 'YES'
                  AND REGEXP_LIKE(TABLE_NAME, :prune_re)
            ) g
            WHERE g.GEN_DAY IS NOT NULL
            QUALIFY g.GEN_DAY < :day_ct
                AND ROW_NUMBER() OVER (PARTITION BY g.BASE_NAME, g.GEN_KIND ORDER BY g.GEN_DAY DESC)
                    > IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)
            ORDER BY g.TABLE_NAME
        );
        LET c_prune CURSOR FOR res;
        FOR r IN c_prune DO
            pname := r.TABLE_NAME;
            IF (REGEXP_LIKE(pname, prune_re)) THEN   -- second check right before the DROP
                BEGIN
                    EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;
                    pruned := pruned + 1;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                        (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION)
                    SELECT :run_id, RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname, 'PRUNED';
                EXCEPTION
                    WHEN OTHER THEN
                        emsg := SQLERRM;
                        prune_failed := prune_failed + 1;
                        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                        SELECT 'BackupOperatorTables', 'backup_prune_failed', LEFT(:emsg, 2000),
                               'table ' || :pname || ' (OPERATOR_BACKUP_DAILY)', CURRENT_ROLE();
                        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, DETAIL)
                        SELECT :run_id, RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname,
                               'PRUNE_FAILED', LEFT(:emsg, 1000);
                END;
            END IF;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            prune_failed := prune_failed + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_prune_failed', LEFT(:emsg, 2000),
                   'prune scan ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): every generation kept this run', CURRENT_ROLE();
    END;

    -- The log trims itself (SP_PURGE_FACTS is untouched).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
     WHERE LOGGED_AT < DATEADD('day', -400, CURRENT_TIMESTAMP());

    -- Freshness (V068 idiom, Central-pinned). LAST_LOAD_TS advances only on a run with zero clone
    -- failures (V066 #11), so a failing or suspended backup goes stale and the dead-man paths fire.
    -- The name carries DAILY, so every name-based cadence rule judges it at 30h.
    fstatus := 'backup ' || gen_d || ': ' || done || ' cloned, ' || failed || ' failed, ' ||
               missing || ' missing, ' || pruned || ' pruned, ' || prune_failed || ' prune failed';
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'OPERATOR_BACKUP_DAILY' AS SOURCE_NAME,
               IFF(:failed = 0 AND :done > 0, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, NULL) AS RUN_TS,
               :total_rows AS ROW_COUNT,
               LEFT(:fstatus, 400) AS STATUS
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = COALESCE(s.RUN_TS, t.LAST_LOAD_TS),
        ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
        GENERATION = COALESCE(t.GENERATION, 0) + 1, STATUS = s.STATUS
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, SNAPSHOT_TS, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.RUN_TS, s.ROW_COUNT,
            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, 1, s.STATUS);

    IF (failed > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'BackupOperatorTables', 'backup_incomplete', LEFT(:fstatus, 2000),
               'OPERATOR_BACKUP_DAILY ' || :gen_d || ': freshness stamp held until a clean run; see OPERATOR_BACKUP_LOG',
               CURRENT_ROLE();
    END IF;

    RETURN 'cloned ' || :done || ' operator table(s) to OVERWATCH_BAK.*_OWBAK_' || :gen_d ||
           IFF(:is_sunday, ' (+ weekly ' || :gen_w || ' and *_BAK_LAST)', '') || '; ' || :failed || ' failed, ' ||
           :missing || ' missing, ' || :pruned || ' pruned, ' || :prune_failed || ' prune failed';
END;
$$;

-- Daily cadence (decision O-13): 05:10 Central rides the hourly chain's warm WH_ALFA_ADMIN
-- (TASK_LOAD_HOURLY :07) and runs ahead of the 06:30+ daily batch.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR
    SET SCHEDULE = 'USING CRON 10 5 * * * America/Chicago';
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR RESUME;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V151; + OVERWATCH_BAK backup-prune carve-out, V158)
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
      -- V158 (Next-Fifty #32): the backup-generation prune of OVERWATCH itself (task-run as SYSTEM,
      -- the exact generated DROP only). A human DROP of a backup or any other drop still surfaces.
      AND NOT (CHANGE_KIND = 'DESTRUCTIVE'
               AND UPPER(COALESCE(USER_NAME, '')) = 'SYSTEM'
               AND REGEXP_LIKE(COALESCE(QUERY_PREVIEW, ''),
                   'DROP TABLE IF EXISTS DBA_MAINT_DB[.]OVERWATCH_BAK[.][A-Z0-9_]+_OWBAK_[DW][0-9]{8}'))
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

-- First generation now, through the TASK: it runs as SYSTEM like every scheduled run (inside the
-- carve-out above), exercises the real task path, and keys the generation on the Central day
-- whatever the worksheet zone. Asynchronous: read TASK_HISTORY / OPERATOR_BACKUP_LOG in PART B.
EXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 158 AS VERSION,
       'Operator-data backups rotate daily (Next-Fifty #32): new TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK (owner-only, no FUTURE grants, so no grant churn in OVERWATCH). SP_BACKUP_OPERATOR_TABLES re-derived from V089 clones the 25 operator tables every day at 05:10 Central to immutable TRANSIENT OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd> generations (Sundays also _OWBAK_W, cloned from the D generation, and V089''s <T>_BAK_LAST statement, still weekly), logs backup vs source row counts to the new OPERATOR_BACKUP_LOG, prunes to SETTINGS BACKUP_KEEP_DAILY 14 / BACKUP_KEEP_WEEKLY 8 (floors 7/4, only TRANSIENT OVERWATCH_BAK tables whose whole name matches the generation pattern, dated before today) and stamps SOURCE_FRESHNESS_STATE OPERATOR_BACKUP_DAILY only on a run with zero clone failures. TASK_BACKUP_OPERATOR moved from Sunday 05:40 to daily 05:10 via SUSPEND/SET SCHEDULE/RESUME. V_SECURITY_EXCEPTION_QUEUE re-derived from V151 with one carve-out: the task''s own generation-prune DROP (USER_NAME SYSTEM, exact generated statement) leaves the CHANGE RISK queue. Restore = INSERT OVERWRITE as the table-owner role. Tail: EXECUTE TASK (first generation).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 158);
