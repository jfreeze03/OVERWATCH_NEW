-- =====================================================================================================================
-- bootstrap/20_runway_database.sql -- make one runway database ready for Flyway (and adopt an existing one)
-- =====================================================================================================================
-- WHAT    For ONE runway database ALFA_EDW_<ID> (set runway in PART 0):
--           PART 0  variables + guard
--           PART 1  SYSADMIN       the database exists and is owned by SYSADMIN (NOT by the deploy role);
--                                  USAGE + CREATE SCHEMA to EDW_<ID>_DEPLOYER; USAGE to EDW_FLYWAY_AUDITOR
--           PART 2  SYSADMIN, then the deploy role
--                                  schema FLYWAY owned by the deploy role, with the kit tables FLYWAY.DEPLOY_LOG and
--                                  FLYWAY.DEPLOY_TASK_STATE that the callbacks write; auditor USAGE on FLYWAY
--           PART 3  SYSADMIN       brownfield adoption: schema-wide ownership transfer of every existing schema and
--                                  object to the deploy role. DRY RUN by default (dry_run = TRUE prints the plan).
--           PART 4  workstation    flyway baseline (instructions)
--           PART 5  deploy role    post-baseline: auditor SELECT on FLYWAY."flyway_schema_history"
--           PART 6  SYSADMIN       verification (PASS / FAIL report)
-- WHO     A Snowflake admin in Snowsight, deliberately; never the pipeline. Run bootstrap/10 (PART 1 once, PART 2 for
--         this runway) first: this file uses the deploy role, its warehouse and the auditor role.
-- ORDER   New runway or first setup (GUIDE B3): PART 0, 1, 2, 6.  Adoption of an existing database (GUIDE D2): PART 0
--         + 3 with dry_run = TRUE, review, then in the freeze window dry_run = FALSE.  After `flyway baseline`
--         (GUIDE D5): PART 0 + 5 + 6. Every statement is idempotent; re-running a part is safe.
--         Keep a warehouse selected in the worksheet (the blocks read SHOW output with RESULT_SCAN).
--
-- WHY THE DATABASE STAYS WITH SYSADMIN
--   The deploy role owns everything INSIDE the database (schemas, tables, views, procedures, tasks, ...), so a
--   migration can create, alter, replace and grant whatever it manages. It does not own the database: a deploy (or a
--   stolen deploy key) cannot drop, replace, rename, clone over or re-grant ALFA_EDW_<ID> itself. Guard FG006X also
--   rejects DROP / CREATE OR REPLACE DATABASE in migrations; this makes it a privilege fact, not only a lint.
-- WHY ONE OWNER FOR EVERYTHING INSIDE
--   CREATE OR REPLACE and ALTER need OWNERSHIP of the existing object; with mixed owners a repeatable fails or flips
--   ownership. All tasks of one graph must share one owner, and ownership moved task by task severs the graph's
--   links; moving it schema-wide (GRANT OWNERSHIP ON ALL TASKS IN SCHEMA) keeps them -- PART 3 never moves one task.
--   Objects owned by the deploy role also EXECUTE as it (owner's-rights procedures, tasks, secure views, pipes):
--   grant EDW_<ID>_DEPLOYER whatever they read outside this database before re-enabling ETL (GUIDE D2 step 4).
-- GRANTS INSIDE THE RUNWAY
--   Defining FUTURE grants in a schema (the R__90 grants repeatables run GRANT ... ON FUTURE VIEWS IN SCHEMA ...)
--   needs OWNERSHIP of that schema or the account-wide MANAGE GRANTS privilege. After PART 3 the deploy role owns
--   every schema, so it needs no MANAGE GRANTS, and this kit does not grant it: MANAGE GRANTS would let a deploy
--   re-grant any object in the account. Keep future grants at ONE level (schema), never mixed with database-level
--   future grants: a schema-level future grant silently overrides database-level ones of the same object type.
-- TASKS
--   The deploy role needs EXECUTE TASK on the account (bootstrap/10 PART 2c) to resume the graphs it owns;
--   EXECUTE MANAGED TASK as well if the runway has serverless tasks (PART 3 counts them).
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables + guard (run before any other part)
-- =====================================================================================================================
SET runway = 'DEV';                         -- DEV | SAN | SEA | PHX | MGM | PRD

-- PART 3 only. TRUE = print the ownership-transfer plan and execute nothing. Set FALSE in the agreed freeze window.
SET dry_run = TRUE;

-- PART 3 only. TRUE moves masking / row access policies and tags as well (every object type, the kit's model).
-- Set FALSE when a separate governance role owns them on purpose (separation of duties): they then stay with it,
-- and migrations must not CREATE OR REPLACE them.
SET include_governance_objects = TRUE;

-- Derived names (runways.toml conventions). Do not edit.
SET runway_db = 'ALFA_EDW_' || $runway;
SET deploy_role = 'EDW_' || $runway || '_DEPLOYER';
SET deploy_wh = 'WH_EDW_DEPLOY_' || $runway;

USE ROLE SYSADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    r VARCHAR;
    n_role INTEGER;
    bad_runway EXCEPTION (-20201, 'PART 0: runway must be one of DEV, SAN, SEA, PHX, MGM, PRD (upper case).');
    no_role EXCEPTION (-20202, 'PART 0: the deploy role does not exist yet: run bootstrap/10 PART 2 for this runway.');
BEGIN
    SELECT $runway, $deploy_role INTO :rw, :r;
    IF (rw IS NULL OR rw NOT IN ('DEV', 'SAN', 'SEA', 'PHX', 'MGM', 'PRD')) THEN
        RAISE bad_runway;
    END IF;
    SHOW ROLES LIKE 'EDW_%_DEPLOYER';
    SELECT COUNT_IF(t."name" = :r) INTO :n_role FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    IF (n_role = 0) THEN
        RAISE no_role;
    END IF;
    RETURN 'PART 0 OK: database ALFA_EDW_' || rw || ', deploy role ' || r;
END;
$$;


-- =====================================================================================================================
-- PART 1 -- the database (SYSADMIN)
-- =====================================================================================================================
USE ROLE SYSADMIN;

-- No-op for the six existing databases. A brand-new runway is created empty here (or by clone: bootstrap/30).
CREATE DATABASE IF NOT EXISTS IDENTIFIER($runway_db)
    COMMENT = 'ALFA EDW runway. Owned by SYSADMIN; every schema and object inside by its EDW_<ID>_DEPLOYER.';

-- The database must be owned by SYSADMIN. If this raises, the current owner (or SECURITYADMIN) hands it over:
--   GRANT OWNERSHIP ON DATABASE <ALFA_EDW_ID> TO ROLE SYSADMIN COPY CURRENT GRANTS;
-- COPY CURRENT GRANTS keeps every existing grant on the database; nothing inside it changes owner.
EXECUTE IMMEDIATE $$
DECLARE
    db VARCHAR;
    owner_role VARCHAR;
    not_sysadmin EXCEPTION (-20211, 'PART 1: the runway database is not owned by SYSADMIN; see the comment above.');
BEGIN
    SELECT $runway_db INTO :db;
    EXECUTE IMMEDIATE 'SHOW DATABASES LIKE ''' || :db || '''';
    SELECT MAX(t."owner") INTO :owner_role FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t WHERE t."name" = :db;
    IF (owner_role IS DISTINCT FROM 'SYSADMIN') THEN
        RAISE not_sysadmin;
    END IF;
    RETURN 'PART 1 OK: ' || db || ' is owned by SYSADMIN';
END;
$$;

-- USAGE to resolve objects in the database; CREATE SCHEMA so migrations can add schemas (CREATE SCHEMA IF NOT
-- EXISTS <SCHEMA>). Nothing else at database level: the deploy role owns what it creates.
GRANT USAGE, CREATE SCHEMA ON DATABASE IDENTIFIER($runway_db) TO ROLE IDENTIFIER($deploy_role);
-- The read-only auditor (PR builds, promotion checks) reads only the Flyway history: USAGE here, more in PART 2 / 5.
GRANT USAGE ON DATABASE IDENTIFIER($runway_db) TO ROLE EDW_FLYWAY_AUDITOR;


-- =====================================================================================================================
-- PART 2 -- schema FLYWAY + the kit's tables (SYSADMIN creates the schema and hands it over; the deploy role
--           creates the tables, so it owns them and INSERT / SELECT come with ownership)
-- =====================================================================================================================
-- flyway.toml sets defaultSchema = "FLYWAY", schemas = ["FLYWAY"] and createSchemas = false: Flyway never creates
-- this schema; baseline / migrate fail if it is missing. Flyway keeps "flyway_schema_history" here and runs
-- USE SCHEMA FLYWAY before every migration. The kit tables make FLYWAY non-empty ON PURPOSE: Flyway's
-- "non-empty schema without a history table" check looks only at FLYWAY, so a migrate on a database that was never
-- baselined stops instead of replaying every migration against live objects. A brand-new empty runway is therefore
-- baselined too (PART 4) before its first deploy. runways.toml [drift] ignore_schemas lists FLYWAY, so the
-- callbacks' writes are not drift.
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($runway_db);

CREATE SCHEMA IF NOT EXISTS FLYWAY
    COMMENT = 'Flyway history (flyway_schema_history) + kit deploy log / task snapshot. Owned by EDW_<ID>_DEPLOYER.';
-- Also corrects an existing FLYWAY schema / tables created by another role (e.g. an earlier manual Flyway run).
GRANT OWNERSHIP ON SCHEMA FLYWAY TO ROLE IDENTIFIER($deploy_role) COPY CURRENT GRANTS;
GRANT OWNERSHIP ON ALL TABLES IN SCHEMA FLYWAY TO ROLE IDENTIFIER($deploy_role) COPY CURRENT GRANTS;

USE ROLE IDENTIFIER($deploy_role);
USE WAREHOUSE IDENTIFIER($deploy_wh);
USE DATABASE IDENTIFIER($runway_db);

-- One row per deploy event. Written by callbacks/beforeMigrate__10_deploy_log (STARTED), afterMigrate__60_task_restore
-- (TASKS_RESUMED / TASKS_SUSPENDED / FAILED), afterMigrate__80_cross_env_guard (FAILED), afterMigrate__99_deploy_log
-- (SUCCEEDED), afterMigrateError__99_deploy_log (FAILED), and bootstrap/30 (REFRESHED). Plain VARCHAR: Snowflake
-- stores only the bytes used, and a deploy must never fail because a build URL or a detail line is long.
-- EVENT_TS is TIMESTAMP_LTZ like Flyway's "installed_on": the callbacks select history rows installed at or after a
-- STARTED row's EVENT_TS.
CREATE TABLE IF NOT EXISTS FLYWAY.DEPLOY_LOG (
    DEPLOY_ID    VARCHAR       NOT NULL COMMENT 'Jenkins <ID>-<BUILD_NUMBER>; "manual" for a hand run',
    EVENT        VARCHAR       NOT NULL COMMENT 'STARTED|SUCCEEDED|FAILED|TASKS_RESUMED|TASKS_SUSPENDED|REFRESHED',
    ENV          VARCHAR                COMMENT 'runway id (flyway.toml placeholder env)',
    GIT_SHA      VARCHAR                COMMENT 'commit of the deployed bundle',
    BUILD_URL    VARCHAR                COMMENT 'Jenkins build URL',
    APPROVER     VARCHAR                COMMENT 'Jenkins approver (input step submitter) or "manual"',
    FLYWAY_USER  VARCHAR                COMMENT 'Snowflake user Flyway connected as (${flyway:user})',
    EVENT_TS     TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP() COMMENT 'when the event was written',
    DETAIL       VARCHAR                COMMENT 'free text: applied versions, failed migrations, task lists'
)
COMMENT = 'Flyway deploy audit trail for this runway (kit callbacks; bootstrap/20)';

-- Root tasks that were STARTED when a deploy began (callbacks/beforeMigrate__20_task_snapshot), so
-- afterMigrate__60_task_restore resumes exactly those, and afterMigrateError__99 lists the ones a failed run left
-- suspended. TASK_FQN is the fully quoted "DATABASE"."SCHEMA"."TASK" with embedded quotes doubled.
CREATE TABLE IF NOT EXISTS FLYWAY.DEPLOY_TASK_STATE (
    DEPLOY_ID    VARCHAR       NOT NULL COMMENT 'same DEPLOY_ID as FLYWAY.DEPLOY_LOG',
    TASK_FQN     VARCHAR       NOT NULL COMMENT '"DATABASE"."SCHEMA"."TASK"',
    STATE_BEFORE VARCHAR       NOT NULL COMMENT 'lower-case SHOW TASKS state at snapshot time (started)',
    CAPTURED_TS  TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP() COMMENT 'snapshot time'
)
COMMENT = 'Root tasks started before each deploy, for the post-deploy restore (kit callbacks; bootstrap/20)';

-- The auditor reads the history (PART 5, after baseline) and the deploy log (who deployed what, approved by whom)
-- without any deploy privilege. Owner-granted, so they survive as long as the objects do.
GRANT USAGE ON SCHEMA FLYWAY TO ROLE EDW_FLYWAY_AUDITOR;
GRANT SELECT ON TABLE FLYWAY.DEPLOY_LOG TO ROLE EDW_FLYWAY_AUDITOR;


-- =====================================================================================================================
-- PART 3 -- brownfield adoption: hand every existing schema and object to the deploy role (SYSADMIN)
-- =====================================================================================================================
-- Run AFTER the adoption plan (GUIDE D1: inventory, DDL-issuing processes, freeze window) and first with
-- dry_run = TRUE. The plan contains, in this order:
--   1. GRANT USAGE ON WAREHOUSE <wh> for every warehouse a task / dynamic table / alert in this database names:
--      after the transfer they run with the deploy role's privileges.
--   2. per schema (every one but INFORMATION_SCHEMA; FLYWAY, PUBLIC included): GRANT OWNERSHIP ON SCHEMA, then
--      GRANT OWNERSHIP ON ALL <type> IN SCHEMA for every object type below, all with COPY CURRENT GRANTS so every
--      existing grant (functional read/write roles) is kept. Schema-wide, so task graphs keep their links.
--   3. warnings: FUTURE OWNERSHIP grants (database or schema level) that hand NEW objects to another role -- revoke
--      or re-point them before the first deploy, or Flyway-created objects get the wrong owner; serverless tasks
--      (grant EXECUTE MANAGED TASK, bootstrap/10 PART 2c); the started tasks that the apply run re-checks.
-- With dry_run = FALSE it executes the plan one statement at a time (a failure is reported and the rest continue),
-- then resumes every task that was STARTED before and is suspended after (children first, then roots), so the
-- transfer changes no schedule even if your Snowflake release suspends tasks on ownership transfer (verify on first
-- run). Expect a few minutes for 50+ schemas.
-- Why SYSADMIN: it sees and (through the role hierarchy) owns every object whose owner role rolls up to SYSADMIN.
-- A FAILED line on ALL <type> means some object of that type is owned by a role outside that hierarchy: have
-- SECURITYADMIN grant that role to SYSADMIN (GRANT ROLE <owner> TO ROLE SYSADMIN, the standard hierarchy) and run
-- PART 3 again. The ACCOUNT_USAGE query after the block lists owners SYSADMIN may not see (ACCOUNTADMIN, ~2 h lag).
-- Pipes: an ownership transfer needs the pipe paused, or OPERATE + MONITOR on it with COPY CURRENT GRANTS (SYSADMIN
-- has both through the hierarchy). If ALL PIPES fails anyway: ALTER PIPE <pipe> SET PIPE_EXECUTION_PAUSED = TRUE,
-- re-run, then as the deploy role SELECT SYSTEM$PIPE_FORCE_RESUME('<db>.<schema>.<pipe>').
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($runway_db);

EXECUTE IMMEDIATE $$
DECLARE
    db VARCHAR;
    deployer VARCHAR;
    dry BOOLEAN;
    gov BOOLEAN;
    -- Data objects before the code that reads them; tasks and alerts last. Extend the list if your schemas hold
    -- other types (e.g. 'SEMANTIC VIEWS', 'NOTEBOOKS', 'STREAMLITS', 'ICEBERG TABLES'); a type Snowflake does not
    -- accept for ALL ... IN SCHEMA shows as a FAILED line, nothing else is affected.
    obj_types ARRAY DEFAULT ARRAY_CONSTRUCT('SEQUENCES', 'FILE FORMATS', 'STAGES', 'TABLES', 'EXTERNAL TABLES',
        'EVENT TABLES', 'DYNAMIC TABLES', 'VIEWS', 'MATERIALIZED VIEWS', 'FUNCTIONS', 'PROCEDURES', 'STREAMS',
        'PIPES', 'SECRETS', 'ALERTS', 'TASKS');
    gov_types ARRAY DEFAULT ARRAY_CONSTRUCT('TAGS', 'MASKING POLICIES', 'ROW ACCESS POLICIES');
    schema_list ARRAY;
    wh_list ARRAY DEFAULT ARRAY_CONSTRUCT();
    wh_part ARRAY;
    started_list ARRAY DEFAULT ARRAY_CONSTRUCT();
    resume_list ARRAY;
    plan ARRAY DEFAULT ARRAY_CONSTRUCT();
    notes VARCHAR DEFAULT '';
    report VARCHAR DEFAULT '';
    started_str VARCHAR;
    s VARCHAR;
    fq VARCHAR;
    stmt VARCHAR;
    v VARCHAR;
    n INTEGER;
    m INTEGER;
    not_owned INTEGER DEFAULT 0;
    serverless INTEGER DEFAULT 0;
    ok_cnt INTEGER DEFAULT 0;
    fail_cnt INTEGER DEFAULT 0;
BEGIN
    SELECT $runway_db, $deploy_role, $dry_run, $include_governance_objects INTO :db, :deployer, :dry, :gov;
    IF (gov) THEN
        obj_types := ARRAY_CAT(gov_types, obj_types);
    END IF;

    -- schemas, and how many are not yet owned by the deploy role
    EXECUTE IMMEDIATE 'SHOW SCHEMAS IN DATABASE ' || :db;
    SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR) WITHIN GROUP (ORDER BY t."name"), ARRAY_CONSTRUCT()),
           COUNT_IF(t."owner" IS DISTINCT FROM :deployer)
      INTO :schema_list, :not_owned
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE t."name" <> 'INFORMATION_SCHEMA';

    -- tasks: their warehouses, serverless ones, and the ones started now (fully quoted, like the callbacks)
    BEGIN
        EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(DISTINCT NULLIF(t."warehouse"::VARCHAR, '')), ARRAY_CONSTRUCT()),
               COUNT_IF(NULLIF(t."warehouse"::VARCHAR, '') IS NULL),
               COALESCE(ARRAY_AGG(IFF(LOWER(t."state") = 'started',
                   '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                   || '"."' || REPLACE(t."name", '"', '""') || '"', NULL)), ARRAY_CONSTRUCT())
          INTO :wh_list, :serverless, :started_list
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW TASKS failed (' || SQLERRM || '): task warehouses not planned'
                || CHR(10);
    END;
    BEGIN
        EXECUTE IMMEDIATE 'SHOW DYNAMIC TABLES IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(DISTINCT NULLIF(t."warehouse"::VARCHAR, '')), ARRAY_CONSTRUCT())
          INTO :wh_part
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        wh_list := ARRAY_DISTINCT(ARRAY_CAT(wh_list, wh_part));
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW DYNAMIC TABLES failed (' || SQLERRM || ')' || CHR(10);
    END;
    BEGIN
        EXECUTE IMMEDIATE 'SHOW ALERTS IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(DISTINCT NULLIF(t."warehouse"::VARCHAR, '')), ARRAY_CONSTRUCT())
          INTO :wh_part
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        wh_list := ARRAY_DISTINCT(ARRAY_CAT(wh_list, wh_part));
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW ALERTS failed (' || SQLERRM || ')' || CHR(10);
    END;

    -- database-level future OWNERSHIP grants to another role
    BEGIN
        EXECUTE IMMEDIATE 'SHOW FUTURE GRANTS IN DATABASE ' || :db;
        SELECT LISTAGG(t."grant_on" || ' -> ' || t."grantee_name", ', ')
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."privilege" = 'OWNERSHIP' AND t."grantee_name" IS DISTINCT FROM :deployer;
        IF (COALESCE(v, '') <> '') THEN
            notes := notes || '-- WARNING database-level FUTURE OWNERSHIP grants to another role: ' || v
                || ' (new objects would not belong to ' || deployer || ')' || CHR(10);
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW FUTURE GRANTS IN DATABASE failed (' || SQLERRM || ')' || CHR(10);
    END;

    -- 1. warehouses
    n := ARRAY_SIZE(wh_list);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            plan := ARRAY_APPEND(plan, 'GRANT USAGE ON WAREHOUSE "' || REPLACE(GET(wh_list, i - 1)::VARCHAR, '"', '""')
                || '" TO ROLE ' || deployer);
        END FOR;
    END IF;

    -- 2. schemas and their objects (+ schema-level future OWNERSHIP grants to another role)
    n := ARRAY_SIZE(schema_list);
    m := ARRAY_SIZE(obj_types);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            s := GET(schema_list, i - 1)::VARCHAR;
            fq := db || '."' || REPLACE(s, '"', '""') || '"';
            plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON SCHEMA ' || fq || ' TO ROLE ' || deployer
                || ' COPY CURRENT GRANTS');
            FOR j IN 1 TO m DO
                plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON ALL ' || GET(obj_types, j - 1)::VARCHAR
                    || ' IN SCHEMA ' || fq || ' TO ROLE ' || deployer || ' COPY CURRENT GRANTS');
            END FOR;
            BEGIN
                EXECUTE IMMEDIATE 'SHOW FUTURE GRANTS IN SCHEMA ' || :fq;
                SELECT LISTAGG(t."grant_on" || ' -> ' || t."grantee_name", ', ')
                  INTO :v
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                 WHERE t."privilege" = 'OWNERSHIP' AND t."grantee_name" IS DISTINCT FROM :deployer;
                IF (COALESCE(v, '') <> '') THEN
                    notes := notes || '-- WARNING ' || fq || ' has FUTURE OWNERSHIP grants to another role: ' || v
                        || CHR(10);
                END IF;
            EXCEPTION
                WHEN OTHER THEN
                    notes := notes || '-- WARNING SHOW FUTURE GRANTS IN SCHEMA ' || fq || ' failed (' || SQLERRM
                        || ')' || CHR(10);
            END;
        END FOR;
    END IF;

    IF (serverless > 0) THEN
        notes := notes || '-- NOTE ' || serverless || ' serverless task(s): grant EXECUTE MANAGED TASK to ' || deployer
            || ' (bootstrap/10 PART 2c) or they stop running after the transfer' || CHR(10);
    END IF;
    notes := notes || '-- NOTE ' || ARRAY_SIZE(started_list) || ' task(s) are started now; the apply run resumes any '
        || 'of them the transfer leaves suspended' || CHR(10);

    report := '-- database ' || db || ': ' || ARRAY_SIZE(schema_list) || ' schema(s), ' || not_owned
        || ' not owned by ' || deployer || '; ' || ARRAY_SIZE(plan) || ' statement(s) planned' || CHR(10) || notes;

    IF (dry) THEN
        report := '-- DRY RUN (dry_run = TRUE): nothing was executed. Review, then SET dry_run = FALSE; and run '
            || 'PART 3 again.' || CHR(10) || report;
        n := ARRAY_SIZE(plan);
        IF (n > 0) THEN
            FOR k IN 1 TO n DO
                report := report || GET(plan, k - 1)::VARCHAR || ';' || CHR(10);
            END FOR;
        END IF;
        RETURN report;
    END IF;

    -- apply
    n := ARRAY_SIZE(plan);
    IF (n > 0) THEN
        FOR k IN 1 TO n DO
            stmt := GET(plan, k - 1)::VARCHAR;
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                ok_cnt := ok_cnt + 1;
            EXCEPTION
                WHEN OTHER THEN
                    fail_cnt := fail_cnt + 1;
                    report := report || '-- FAILED ' || stmt || ';  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;

    -- tasks that were started before and are not started now: resume children first, then roots
    IF (ARRAY_SIZE(started_list) > 0) THEN
        started_str := '|' || ARRAY_TO_STRING(started_list, '|') || '|';
        EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(q.FQN) WITHIN GROUP (ORDER BY q.IS_ROOT, q.FQN), ARRAY_CONSTRUCT())
          INTO :resume_list
          FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                       || '"."' || REPLACE(t."name", '"', '""') || '"' AS FQN,
                       REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]' AS IS_ROOT,
                       LOWER(t."state") AS STATE
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS q
         WHERE q.STATE <> 'started'
           AND CONTAINS(:started_str, '|' || q.FQN || '|');
        n := ARRAY_SIZE(resume_list);
        IF (n > 0) THEN
            FOR k IN 1 TO n DO
                stmt := 'ALTER TASK ' || GET(resume_list, k - 1)::VARCHAR || ' RESUME';
                BEGIN
                    EXECUTE IMMEDIATE :stmt;
                    report := report || '-- resumed (was started before the transfer): ' || stmt || CHR(10);
                EXCEPTION
                    WHEN OTHER THEN
                        fail_cnt := fail_cnt + 1;
                        report := report || '-- FAILED ' || stmt || ';  -- ' || SQLERRM
                            || ' (EXECUTE TASK / warehouse USAGE on ' || deployer || '?)' || CHR(10);
                END;
            END FOR;
        END IF;
    END IF;

    RETURN '-- APPLIED: ' || ok_cnt || ' statement(s) succeeded, ' || fail_cnt || ' failed' || CHR(10) || report;
END;
$$;

-- The plan / result above, one line per row (run right after the block). A dry-run plan can also be copied from
-- here into a change ticket as-is.
SELECT s.INDEX AS LINE_NO, s.VALUE AS LINE
  FROM (SELECT $1 AS REPORT FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))) AS b,
       TABLE(SPLIT_TO_TABLE(b.REPORT, CHR(10))) AS s
 WHERE s.VALUE <> ''
 ORDER BY s.INDEX;

-- Optional, before the apply: every owner in this database, including objects SYSADMIN cannot see. ACCOUNT_USAGE is
-- readable by ACCOUNTADMIN (or a role granted the SNOWFLAKE database's IMPORTED PRIVILEGES / OBJECT_VIEWER) and lags
-- up to about two hours. Owners other than the deploy role after the apply = objects PART 3 could not move.
USE ROLE ACCOUNTADMIN;
SELECT 'SCHEMA' AS OBJECT_KIND, s.SCHEMA_OWNER AS OWNER_ROLE, COUNT(*) AS OBJECTS
  FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA AS s
 WHERE s.CATALOG_NAME = $runway_db AND s.DELETED IS NULL AND s.SCHEMA_NAME <> 'INFORMATION_SCHEMA'
 GROUP BY 1, 2
UNION ALL
SELECT t.TABLE_TYPE, t.TABLE_OWNER, COUNT(*)
  FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES AS t
 WHERE t.TABLE_CATALOG = $runway_db AND t.DELETED IS NULL
 GROUP BY 1, 2
UNION ALL
SELECT 'FUNCTION', f.FUNCTION_OWNER, COUNT(*)
  FROM SNOWFLAKE.ACCOUNT_USAGE.FUNCTIONS AS f
 WHERE f.FUNCTION_CATALOG = $runway_db AND f.DELETED IS NULL
 GROUP BY 1, 2
UNION ALL
SELECT 'PROCEDURE', p.PROCEDURE_OWNER, COUNT(*)
  FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES AS p
 WHERE p.PROCEDURE_CATALOG = $runway_db AND p.DELETED IS NULL
 GROUP BY 1, 2
ORDER BY 1, 2;


-- =====================================================================================================================
-- PART 4 -- flyway baseline (workstation; once per database; GUIDE D3 + D5)
-- =====================================================================================================================
-- Adopted databases are NOT replayed from migrations: `flyway baseline` writes one BASELINE row at the kit's
-- baseline_version (runways.toml [kit]); every migration at or below it counts as already delivered ("Below
-- Baseline"). Use the SAME version on all six databases and never change it afterwards. flyway.toml keeps
-- baselineOnMigrate = false, so this is always a deliberate step. For MGM / PRD it is break-glass (GUIDE B7:
-- ticket, reserved Jenkins lock ALFA_EDW_<ID>, time-boxed deploy-role grant).
--
--   cd flyway-edw
--   eval "$(python3 scripts/fw.py env --runway DEV --format shell)"; unset FLYWAY_TARGET
--   export SNOWFLAKE_ACCOUNT='<orgname>-<account_name>'
--   export FLYWAY_SF_USER='<your Snowflake user>'           # with a registered key pair and the deploy role
--   export FLYWAY_SF_KEY_FILE="$HOME/.snowflake/keys/<you>.p8"
--   read -rsp 'key passphrase: ' FLYWAY_SF_KEY_PWD && export FLYWAY_SF_KEY_PWD && echo
--   export FLYWAY_EMAIL='<you@company>'; read -rsp 'Redgate PAT: ' FLYWAY_TOKEN && export FLYWAY_TOKEN && echo
--   export JAVA_ARGS='-Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true'
--   flyway -environment=DEV info                     # expect: no schema history table, nothing applied
--   flyway -environment=DEV baseline -baselineVersion="$FLYWAY_BASELINE_VERSION" \
--          -baselineDescription="ALFA EDW adoption baseline"
--   flyway -environment=DEV info                     # expect: one Baseline row at the baseline version
--
-- `unset FLYWAY_TARGET` matters: fw.py exports it for the pipeline, and Flyway itself reads FLYWAY_TARGET as the
-- target VERSION. baseline runs no callbacks, so it needs no deploy placeholders. Then run PART 5.


-- =====================================================================================================================
-- PART 5 -- post-baseline: the auditor may read the history (deploy role, the table's owner)
-- =====================================================================================================================
-- FLYWAY."flyway_schema_history" exists only after PART 4 (Flyway creates it, as the deploy role). The name and its
-- columns are quoted lower-case. For one database the statement is simply:
--   GRANT SELECT ON TABLE <ALFA_EDW_ID>.FLYWAY."flyway_schema_history" TO ROLE EDW_FLYWAY_AUDITOR;
-- The block grants it only when the table exists (and the Enterprise snapshot history too, once `flyway snapshot`
-- has created it), so running this part early does no harm.
USE ROLE IDENTIFIER($deploy_role);
USE WAREHOUSE IDENTIFIER($deploy_wh);
USE DATABASE IDENTIFIER($runway_db);

EXECUTE IMMEDIATE $$
DECLARE
    n_hist INTEGER;
    n_snap INTEGER;
    report VARCHAR;
BEGIN
    SHOW TABLES LIKE 'flyway_%' IN SCHEMA FLYWAY;
    SELECT COUNT_IF(t."name" = 'flyway_schema_history'), COUNT_IF(t."name" = 'flyway_snapshot_history')
      INTO :n_hist, :n_snap
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    IF (n_hist = 0) THEN
        RETURN 'PART 5: FLYWAY."flyway_schema_history" does not exist yet: run flyway baseline (PART 4), then PART 5.';
    END IF;
    GRANT SELECT ON TABLE FLYWAY."flyway_schema_history" TO ROLE EDW_FLYWAY_AUDITOR;
    report := 'PART 5 OK: EDW_FLYWAY_AUDITOR can SELECT FLYWAY."flyway_schema_history"';
    IF (n_snap > 0) THEN
        GRANT SELECT ON TABLE FLYWAY."flyway_snapshot_history" TO ROLE EDW_FLYWAY_AUDITOR;
        report := report || ' and FLYWAY."flyway_snapshot_history"';
    END IF;
    RETURN report;
END;
$$;

-- What the baseline wrote (one BASELINE row at the kit baseline_version, success = TRUE):
SELECT h."installed_rank", h."version", h."description", h."type", h."installed_by", h."installed_on", h."success"
  FROM FLYWAY."flyway_schema_history" AS h
 ORDER BY h."installed_rank";


-- =====================================================================================================================
-- PART 6 -- verification (SYSADMIN; read-only). Every line starts with PASS / FAIL / CHECK / INFO.
-- =====================================================================================================================
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($runway_db);

EXECUTE IMMEDIATE $$
DECLARE
    db VARCHAR;
    deployer VARCHAR;
    v VARCHAR;
    c1 INTEGER;
    c2 INTEGER;
    c3 INTEGER;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $runway_db, $deploy_role INTO :db, :deployer;
    report := 'INFO   ' || db || ', deploy role ' || deployer || CHR(10);

    -- (1) the database: owner and grants
    BEGIN
        EXECUTE IMMEDIATE 'SHOW GRANTS ON DATABASE ' || :db;
        SELECT COUNT_IF(t."privilege" = 'OWNERSHIP' AND t."grantee_name" = 'SYSADMIN'),
               COUNT_IF(t."privilege" IN ('USAGE', 'CREATE SCHEMA') AND t."grantee_name" = :deployer),
               COUNT_IF(t."privilege" = 'USAGE' AND t."grantee_name" = 'EDW_FLYWAY_AUDITOR')
          INTO :c1, :c2, :c3
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   database owned by SYSADMIN' || CHR(10)
            || IFF(c2 = 2, 'PASS', 'FAIL') || '   ' || deployer || ': USAGE + CREATE SCHEMA on the database'
            || CHR(10) || IFF(c3 > 0, 'PASS', 'FAIL') || '   EDW_FLYWAY_AUDITOR: USAGE on the database' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW GRANTS ON DATABASE: ' || SQLERRM || CHR(10);
    END;

    -- (2) schema FLYWAY: owner and auditor USAGE
    BEGIN
        SHOW GRANTS ON SCHEMA FLYWAY;
        SELECT COUNT_IF(t."privilege" = 'OWNERSHIP' AND t."grantee_name" = :deployer),
               COUNT_IF(t."privilege" = 'USAGE' AND t."grantee_name" = 'EDW_FLYWAY_AUDITOR')
          INTO :c1, :c2
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   FLYWAY schema owned by ' || deployer || CHR(10)
            || IFF(c2 > 0, 'PASS', 'FAIL') || '   EDW_FLYWAY_AUDITOR: USAGE on FLYWAY' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   schema FLYWAY: ' || SQLERRM || ' (PART 2)' || CHR(10);
    END;

    -- (3) the kit tables: present, owned by the deploy role
    BEGIN
        SHOW TABLES IN SCHEMA FLYWAY;
        SELECT COUNT_IF(t."name" IN ('DEPLOY_LOG', 'DEPLOY_TASK_STATE') AND t."owner" = :deployer),
               COUNT_IF(t."name" = 'flyway_schema_history'),
               COUNT_IF(t."owner" IS DISTINCT FROM :deployer)
          INTO :c1, :c2, :c3
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 = 2, 'PASS', 'FAIL') || '   FLYWAY.DEPLOY_LOG + FLYWAY.DEPLOY_TASK_STATE owned by '
            || deployer || CHR(10)
            || IFF(c2 > 0, 'PASS   FLYWAY."flyway_schema_history" exists (baselined or deployed)',
                   'INFO   FLYWAY."flyway_schema_history" not there yet (flyway baseline: PART 4)') || CHR(10)
            || IFF(c3 = 0, 'PASS', 'FAIL') || '   every table in FLYWAY owned by ' || deployer || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW TABLES IN SCHEMA FLYWAY: ' || SQLERRM || CHR(10);
    END;

    -- (4) the columns the callbacks write (an older table created by hand could differ)
    BEGIN
        SELECT COUNT_IF(c.TABLE_NAME = 'DEPLOY_LOG' AND c.COLUMN_NAME IN ('DEPLOY_ID', 'EVENT', 'ENV', 'GIT_SHA',
                   'BUILD_URL', 'APPROVER', 'FLYWAY_USER', 'EVENT_TS', 'DETAIL')),
               COUNT_IF(c.TABLE_NAME = 'DEPLOY_TASK_STATE'
                        AND c.COLUMN_NAME IN ('DEPLOY_ID', 'TASK_FQN', 'STATE_BEFORE', 'CAPTURED_TS')),
               COUNT_IF(c.COLUMN_NAME IN ('EVENT_TS', 'CAPTURED_TS') AND c.DATA_TYPE = 'TIMESTAMP_LTZ')
          INTO :c1, :c2, :c3
          FROM INFORMATION_SCHEMA.COLUMNS AS c
         WHERE c.TABLE_SCHEMA = 'FLYWAY' AND c.TABLE_NAME IN ('DEPLOY_LOG', 'DEPLOY_TASK_STATE');
        report := report || IFF(c1 = 9 AND c2 = 4 AND c3 = 2, 'PASS', 'FAIL')
            || '   kit table columns (DEPLOY_LOG 9/' || c1 || ', DEPLOY_TASK_STATE 4/' || c2
            || ', TIMESTAMP_LTZ 2/' || c3 || ')' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   INFORMATION_SCHEMA.COLUMNS: ' || SQLERRM || CHR(10);
    END;

    -- (5) the history table is readable by the auditor (after PART 5)
    BEGIN
        SHOW GRANTS ON TABLE FLYWAY."flyway_schema_history";
        SELECT COUNT_IF(t."privilege" = 'SELECT' AND t."grantee_name" = 'EDW_FLYWAY_AUDITOR')
          INTO :c1
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   EDW_FLYWAY_AUDITOR: SELECT on the history table'
            || IFF(c1 > 0, '', ' (PART 5)') || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'INFO   no history table to check yet (PART 4, then PART 5)' || CHR(10);
    END;

    -- (6) ownership of every schema (adoption, PART 3)
    BEGIN
        EXECUTE IMMEDIATE 'SHOW SCHEMAS IN DATABASE ' || :db;
        SELECT COUNT_IF(t."owner" IS DISTINCT FROM :deployer),
               LISTAGG(IFF(t."owner" IS DISTINCT FROM :deployer, t."name" || ' (' || COALESCE(t."owner", '?') || ')',
                           NULL), ', ')
          INTO :c1, :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."name" <> 'INFORMATION_SCHEMA';
        report := report || IFF(c1 = 0, 'PASS   every schema owned by ' || deployer,
                                'CHECK  ' || c1 || ' schema(s) not owned by ' || deployer || ' (PART 3): '
                                || LEFT(v, 2000)) || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW SCHEMAS: ' || SQLERRM || CHR(10);
    END;

    -- (7) tasks: owner and state (one owner per graph; started counts against the account-wide limit)
    BEGIN
        EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :db;
        SELECT COUNT(*), COUNT_IF(t."owner" IS DISTINCT FROM :deployer), COUNT_IF(LOWER(t."state") = 'started')
          INTO :c1, :c2, :c3
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c2 = 0, 'PASS', 'CHECK') || '   tasks: ' || c1 || ' total, ' || c2
            || ' not owned by ' || deployer || ', ' || c3 || ' started' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW TASKS: ' || SQLERRM || CHR(10);
    END;

    RETURN report;
END;
$$;

SELECT s.INDEX AS LINE_NO, s.VALUE AS CHECK_RESULT
  FROM (SELECT $1 AS REPORT FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))) AS b,
       TABLE(SPLIT_TO_TABLE(b.REPORT, CHR(10))) AS s
 WHERE s.VALUE <> ''
 ORDER BY s.INDEX;

-- Next: the runway's Jenkins credentials (GUIDE B4); for MGM/SAN/SEA/PHX a refresh from PRD is
-- bootstrap/30_refresh_runway.sql; Time Travel recovery templates are bootstrap/40_point_in_time_recovery.sql.
