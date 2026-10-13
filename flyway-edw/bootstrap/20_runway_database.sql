-- =====================================================================================================================
-- bootstrap/20_runway_database.sql -- make one runway database ready for Flyway (and adopt an existing one)
-- =====================================================================================================================
-- WHAT    For ONE runway database ALFA_EDW_<ID> (set runway in PART 0):
--           PART 0  variables + guard
--           PART 1  SYSADMIN       the database exists and is owned by SYSADMIN (NOT by the deploy role);
--                                  USAGE + CREATE SCHEMA to EDW_<ID>_DEPLOYER; USAGE to EDW_FLYWAY_AUDITOR and to the
--                                  runway's access roles EDW_<ID>_<SCHEMA>_READ / _WRITE (bootstrap/10 PART 2e)
--           PART 2  SYSADMIN, then the deploy role
--                                  schema FLYWAY (managed access) owned by the deploy role, with the kit tables
--                                  FLYWAY.DEPLOY_LOG and FLYWAY.DEPLOY_TASK_STATE that the callbacks write; auditor
--                                  USAGE on FLYWAY. Re-run it after a kit upgrade: it adds new kit columns.
--           PART 3  SYSADMIN       brownfield adoption: schema-wide ownership transfer of every existing schema and
--                                  object to the deploy role, then MANAGED ACCESS on every schema. DRY RUN by default
--                                  (dry_run = TRUE prints the plan).
--           PART 4  workstation    flyway baseline (instructions)
--           PART 5  deploy role    post-baseline: auditor SELECT on FLYWAY."flyway_schema_history"
--           PART 6  SYSADMIN       verification (PASS / FAIL report)
-- WHO     A Snowflake admin in Snowsight, deliberately; never the pipeline. Run bootstrap/10 (PART 1 once, PART 2 for
--         this runway) first: this file uses the deploy role, its warehouse, the auditor role and the access roles.
-- ORDER   New runway or first setup (GUIDE B3): PART 0, 1, 2, 6.  Adoption of an existing database (GUIDE D2): PART 0
--         + 1 + 2, then PART 3 with dry_run = TRUE, review, then in the freeze window dry_run = FALSE.  After `flyway
--         baseline` (GUIDE D5): PART 0 + 5 + 6. Kit upgrade: PART 0 + 2 + 6 on every runway before the first deploy
--         of the new kit (the callbacks need the current FLYWAY.DEPLOY_TASK_STATE columns).
--         Re-running PARTs 0, 1, 2, 5, 6 is safe (idempotent). PART 3 is safe to re-run but not free: Snowflake
--         SUSPENDS every task of a schema whose tasks are transferred (even to the role that already owns them), so
--         PART 3 transfers tasks only in schemas that still hold a task not owned by the deploy role, records the
--         started tasks in FLYWAY.DEPLOY_TASK_STATE first, and resumes them after the transfer.
--         Keep a warehouse selected in the worksheet (the blocks read SHOW output with RESULT_SCAN).
--
-- WHY THE DATABASE STAYS WITH SYSADMIN
--   The deploy role owns everything INSIDE the database (schemas, tables, views, procedures, tasks, ...), so a
--   migration can create, alter, replace and grant whatever it manages. It does not own the database: a deploy (or a
--   stolen deploy key) cannot drop, replace, rename, clone over or re-grant ALFA_EDW_<ID> itself. Guard FG006 also
--   rejects DROP / CREATE OR REPLACE DATABASE in migrations; this makes it a privilege fact, not only a lint.
-- WHY ONE OWNER FOR EVERYTHING INSIDE
--   CREATE OR REPLACE and ALTER need OWNERSHIP of the existing object; with mixed owners a repeatable fails or flips
--   ownership. All tasks of one graph must share one owner, and ownership moved task by task severs the graph's
--   links; moving it schema-wide (GRANT OWNERSHIP ON ALL TASKS IN SCHEMA) keeps them -- PART 3 never moves one task.
--   Objects owned by the deploy role also EXECUTE as it (owner's-rights procedures, tasks, secure views, pipes):
--   grant EDW_<ID>_DEPLOYER whatever they read outside this database before re-enabling ETL (GUIDE D2 step 4).
-- GRANTS INSIDE THE RUNWAY: MANAGED ACCESS SCHEMAS
--   Owning a STANDARD schema is NOT enough to define FUTURE grants in it (the R__90 grants repeatables run GRANT ...
--   ON FUTURE VIEWS IN SCHEMA ...): in a standard schema that needs the account-wide MANAGE GRANTS privilege. In a
--   MANAGED ACCESS schema the schema OWNER can define them. So every EDW schema is a managed access schema owned by
--   the deploy role: new schemas are created WITH MANAGED ACCESS by their V migration (guard FG011 warns otherwise),
--   FLYWAY by PART 2, and PART 3 switches every adopted schema with ALTER SCHEMA ... ENABLE MANAGED ACCESS after
--   the ownership transfer. The kit never grants MANAGE GRANTS to a deploy role: it would let a deploy re-grant any
--   object in the account. In a managed access schema only the schema owner (or MANAGE GRANTS) grants on its
--   objects, so a functional role that owned objects before adoption can no longer re-grant them: access goes
--   through the schema's grants file. Keep future grants at ONE level (schema), never mixed with database-level
--   future grants: a schema-level future grant silently overrides database-level ones of the same object type.
-- TASKS AND ALERTS
--   The deploy role needs EXECUTE TASK on the account (bootstrap/10 PART 2c) to resume the graphs it owns;
--   EXECUTE MANAGED TASK as well if the runway has serverless tasks (PART 3 counts them). Alerts run with their
--   owner's privileges: EXECUTE ALERT on the account (bootstrap/10 PART 2c grants it on MGM / PRD), EXECUTE MANAGED
--   ALERT for serverless alerts. PART 3 refuses to apply while the database holds alerts and the grant is missing.
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables + guard (run before any other part)
-- =====================================================================================================================
SET runway = 'DEV';                         -- DEV | SAN | SEA | PHX | MGM | PRD

-- PART 3 only. TRUE = print the ownership-transfer plan and execute nothing. Set FALSE in the agreed freeze window.
SET dry_run = TRUE;

-- PART 3 only. FALSE (default): masking / row access policies and tags stay with their current owner (normally a
-- governance role: separation of duties); migrations must then not CREATE OR REPLACE them. TRUE moves them to the
-- deploy role as well -- only when no separate governance role owns them on purpose: the deploy role (and every
-- SYSADMIN holder, who inherits it) could then rewrite policy bodies and unmask data. Secrets are never moved: their
-- values cannot live in git, so they stay with their owner, who grants USAGE on them to the deploy role where an
-- owner's-rights procedure or external access integration of the runway uses them.
SET include_governance_objects = FALSE;

-- PART 3 only. A role that OWNED schemas or objects before adoption (an ETL or load role) loses its implicit owner
-- rights the moment ownership moves: COPY CURRENT GRANTS copies the grants the objects have to OTHER roles, not the
-- owner's own rights. TRUE (default) plans explicit working grants for each previous owner after the transfer: USAGE
-- on the schema; SELECT, INSERT, UPDATE, DELETE, TRUNCATE on all tables; SELECT on all views and streams; USAGE on
-- all sequences, file formats, functions and procedures. Stages (READ / WRITE / USAGE), dynamic and external tables
-- and task operation (OPERATE / MONITOR) are not covered: grant those by hand from the dry-run list. Objects Flyway
-- creates later get their grants from the layer-90 grants files, not from here. FALSE plans none (you re-grant).
SET keep_previous_owner_access = TRUE;

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

-- The database must be owned by SYSADMIN. If the block below raises, its message names the current owner. Then, in
-- this order:
--   1. The current owner (or SECURITYADMIN) hands the database over:
--        GRANT OWNERSHIP ON DATABASE <ALFA_EDW_ID> TO ROLE SYSADMIN COPY CURRENT GRANTS;
--      COPY CURRENT GRANTS keeps the grants the database has to OTHER roles, and nothing inside it changes owner.
--      It does NOT keep the previous owner's own rights: its USAGE, MONITOR and CREATE SCHEMA came from OWNERSHIP.
--      Every object inside needs database USAGE, so a functional role (ETL, dbt, admin) that owned the database
--      loses ALL of its access to the runway at that moment.
--   2. Immediately afterwards -- unless the previous owner inherits SYSADMIN (ACCOUNTADMIN does) -- SYSADMIN gives
--      it back what it used:
--        GRANT USAGE, MONITOR ON DATABASE <ALFA_EDW_ID> TO ROLE <previous owner>;
--      Add CREATE SCHEMA only if that role legitimately creates schemas (after adoption, migrations do: GUIDE D1).
--   3. Run PART 1 again.
EXECUTE IMMEDIATE $$
DECLARE
    db VARCHAR;
    owner_role VARCHAR;
    msg VARCHAR;
BEGIN
    SELECT $runway_db INTO :db;
    EXECUTE IMMEDIATE 'SHOW DATABASES LIKE ''' || :db || '''';
    SELECT MAX(t."owner") INTO :owner_role FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t WHERE t."name" = :db;
    IF (owner_role IS DISTINCT FROM 'SYSADMIN') THEN
        -- a declared exception's message must be a literal: raise from a one-line block to name the owner
        msg := 'PART 1: ' || db || ' is owned by ' || COALESCE(owner_role, '(a role SYSADMIN cannot see)')
            || ', not SYSADMIN. Hand it over, then re-grant USAGE / MONITOR to that role (steps 1-3 above): '
            || 'its implicit owner rights do not survive the transfer.';
        EXECUTE IMMEDIATE 'DECLARE not_sysadmin EXCEPTION (-20211, ''' || REPLACE(msg, '''', '''''')
            || '''); BEGIN RAISE not_sysadmin; END;';
    END IF;
    RETURN 'PART 1 OK: ' || db || ' is owned by SYSADMIN';
END;
$$;

-- USAGE to resolve objects in the database; CREATE SCHEMA so migrations can add schemas (CREATE SCHEMA IF NOT
-- EXISTS <SCHEMA> WITH MANAGED ACCESS). Nothing else at database level: the deploy role owns what it creates.
GRANT USAGE, CREATE SCHEMA ON DATABASE IDENTIFIER($runway_db) TO ROLE IDENTIFIER($deploy_role);
-- The read-only auditor (PR builds, promotion checks) reads only the Flyway history: USAGE here, more in PART 2 / 5.
GRANT USAGE ON DATABASE IDENTIFIER($runway_db) TO ROLE EDW_FLYWAY_AUDITOR;

-- The runway's access roles (bootstrap/10 PART 2e: EDW_<ID>_<SCHEMA>_READ / _WRITE) get USAGE on the database: a
-- database-level grant, so it lives here, never in the layer-90 grants files (guard FG007). Every existing role of
-- that form is granted; re-run this block after bootstrap/10 PART 2e adds roles for a new schema.
EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    db VARCHAR;
    role_list ARRAY;
    n INTEGER;
    r VARCHAR;
    report VARCHAR DEFAULT '';
    bad_vars EXCEPTION (-20212, 'PART 1: runway and runway_db disagree: re-run all of PART 0.');
BEGIN
    SELECT $runway, $runway_db INTO :rw, :db;
    IF (db IS DISTINCT FROM ('ALFA_EDW_' || rw)) THEN
        RAISE bad_vars;
    END IF;
    EXECUTE IMMEDIATE 'SHOW ROLES LIKE ''EDW_' || :rw || '_%''';
    SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR) WITHIN GROUP (ORDER BY t."name"), ARRAY_CONSTRUCT())
      INTO :role_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE REGEXP_LIKE(t."name", 'EDW_' || :rw || '_[A-Z][A-Z0-9_]*_(READ|WRITE)');
    n := ARRAY_SIZE(role_list);
    IF (n = 0) THEN
        RETURN 'PART 1: no access role EDW_' || rw || '_<SCHEMA>_READ / _WRITE exists yet (bootstrap/10 PART 2e); '
            || 'create them before the first layer-90 grants file deploys';
    END IF;
    FOR i IN 1 TO n DO
        r := GET(role_list, i - 1)::VARCHAR;
        BEGIN
            EXECUTE IMMEDIATE 'GRANT USAGE ON DATABASE ' || :db || ' TO ROLE ' || :r;
            report := report || 'ok      USAGE on ' || db || ' to ' || r || CHR(10);
        EXCEPTION
            WHEN OTHER THEN
                report := report || 'FAILED  USAGE on ' || db || ' to ' || r || ': ' || SQLERRM || CHR(10);
        END;
    END FOR;
    RETURN 'PART 1: ' || n || ' access role(s)' || CHR(10) || report;
END;
$$;


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
-- callbacks' writes are not drift. FLYWAY is a managed access schema like every other (an existing FLYWAY schema is
-- switched by PART 3, or by hand: ALTER SCHEMA <ALFA_EDW_ID>.FLYWAY ENABLE MANAGED ACCESS).
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($runway_db);

CREATE SCHEMA IF NOT EXISTS FLYWAY
    WITH MANAGED ACCESS
    COMMENT = 'Flyway history (flyway_schema_history) + kit deploy log / task snapshot. Owned by EDW_<ID>_DEPLOYER.';
-- Also corrects an existing FLYWAY schema / tables created by another role (e.g. an earlier manual Flyway run).
GRANT OWNERSHIP ON SCHEMA FLYWAY TO ROLE IDENTIFIER($deploy_role) COPY CURRENT GRANTS;
GRANT OWNERSHIP ON ALL TABLES IN SCHEMA FLYWAY TO ROLE IDENTIFIER($deploy_role) COPY CURRENT GRANTS;

USE ROLE IDENTIFIER($deploy_role);
USE WAREHOUSE IDENTIFIER($deploy_wh);
USE DATABASE IDENTIFIER($runway_db);

-- One row per deploy event. Written by callbacks/beforeMigrate__10_deploy_log (STARTED),
-- afterMigrate__50_cross_env_guard (FAILED), afterMigrate__60_task_restore (TASKS_RESUMED / TASKS_SUSPENDED / FAILED),
-- afterMigrate__99_deploy_log (SUCCEEDED), afterMigrateError__99_deploy_log (FAILED), and bootstrap/30 (REFRESHED).
-- Plain VARCHAR: Snowflake stores only the bytes used, and a deploy must never fail because a build URL or a detail
-- line is long. EVENT_TS is TIMESTAMP_LTZ like Flyway's "installed_on": the callbacks select history rows installed
-- at or after a STARTED row's EVENT_TS.
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

-- Task graphs that were STARTED when a deploy began, member by member (callbacks/beforeMigrate__20_task_snapshot),
-- so afterMigrate__60_task_restore resumes exactly what was running (roots AND members), and afterMigrateError__99
-- lists what a failed run left suspended. TASK_FQN / ROOT_FQN are fully quoted "DATABASE"."SCHEMA"."TASK" with
-- embedded quotes doubled; a root's row has TASK_FQN = ROOT_FQN. bootstrap/20 PART 3 (DEPLOY_ID ADOPT-...),
-- bootstrap/30 PART 2 (REFRESH-...) and bootstrap/40 (RESTORE-...) record what they suspend here too.
CREATE TABLE IF NOT EXISTS FLYWAY.DEPLOY_TASK_STATE (
    DEPLOY_ID    VARCHAR       NOT NULL COMMENT 'same DEPLOY_ID as FLYWAY.DEPLOY_LOG; ADOPT-/REFRESH-/RESTORE- by hand',
    TASK_FQN     VARCHAR       NOT NULL COMMENT '"DATABASE"."SCHEMA"."TASK" (bootstrap/40: also alerts, pipes, DTs)',
    STATE_BEFORE VARCHAR       NOT NULL COMMENT 'lower-case state at snapshot time: started | suspended',
    CAPTURED_TS  TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP() COMMENT 'snapshot time',
    ROOT_FQN     VARCHAR                COMMENT 'root of the task graph the task belongs to; = TASK_FQN for a root'
)
COMMENT = 'Task graphs (roots and members) started before each deploy, for the post-deploy restore (kit callbacks)';

-- Kit upgrade of an existing table (re-runnable): the member-level snapshot added ROOT_FQN. Until this has run on a
-- runway, beforeMigrate__20 stops every deploy there before anything is applied.
ALTER TABLE FLYWAY.DEPLOY_TASK_STATE ADD COLUMN IF NOT EXISTS ROOT_FQN VARCHAR;
COMMENT ON COLUMN FLYWAY.DEPLOY_TASK_STATE.ROOT_FQN IS
    'root of the task graph the task belongs to; = TASK_FQN for a root';
COMMENT ON COLUMN FLYWAY.DEPLOY_TASK_STATE.STATE_BEFORE IS
    'lower-case state at snapshot time: started | suspended (bootstrap/40 RESTORE- rows: <kind>:<state>)';

-- The auditor reads the history (PART 5, after baseline) and the deploy log (who deployed what, approved by whom)
-- without any deploy privilege. Owner-granted, so they survive as long as the objects do.
GRANT USAGE ON SCHEMA FLYWAY TO ROLE EDW_FLYWAY_AUDITOR;
GRANT SELECT ON TABLE FLYWAY.DEPLOY_LOG TO ROLE EDW_FLYWAY_AUDITOR;


-- =====================================================================================================================
-- PART 3 -- brownfield adoption: hand every existing schema and object to the deploy role (SYSADMIN)
-- =====================================================================================================================
-- Run AFTER the adoption plan (GUIDE D1: inventory, DDL-issuing processes, freeze window) and PART 2, first with
-- dry_run = TRUE. The report lists, before the plan: every started task and every started alert (fully quoted), every
-- pipe that moves with its current execution state, each previous owner role and the schemas it owned (in), and
-- the warnings. The plan contains, in this order:
--   1. GRANT USAGE ON WAREHOUSE <wh> for every warehouse a task / dynamic table / alert in this database names:
--      after the transfer they run with the deploy role's privileges.
--   2. per schema (every one but INFORMATION_SCHEMA; FLYWAY, PUBLIC included):
--      a. ALTER PIPE ... SET PIPE_EXECUTION_PAUSED = TRUE for each pipe not owned by the deploy role (a pipe's
--         ownership moves only while it is paused);
--      b. GRANT OWNERSHIP ON SCHEMA, then GRANT OWNERSHIP ON ALL <type> IN SCHEMA for every object type below, all
--         with COPY CURRENT GRANTS so every grant to OTHER roles is kept. ALL ALERTS / ALL TASKS only in schemas that
--         hold an alert / task not owned by the deploy role (a task transfer suspends the schema's tasks). Schema-
--         wide, so task graphs keep their links;
--      c. GRANT OWNERSHIP ON PIPE ... one pipe at a time (Snowflake refuses bulk ownership grants on pipes), after
--         the pipe's tables and stages moved; then SELECT SYSTEM$PIPE_FORCE_RESUME('<pipe>') for each pipe that was
--         RUNNING (the one-argument form overrides the ownership-transfer check; verify on DEV first);
--      d. with keep_previous_owner_access = TRUE: working grants for each previous owner role (PART 0 lists them);
--      e. ALTER SCHEMA ... ENABLE MANAGED ACCESS when the schema is not managed access yet (verify on DEV first that
--         SYSADMIN may run it; if not, the FAILED line is run by SECURITYADMIN, which holds MANAGE GRANTS).
--   3. warnings: FUTURE OWNERSHIP grants (database or schema level) that hand NEW objects to another role -- revoke
--      or re-point them before the first deploy, or Flyway-created objects get the wrong owner; serverless tasks
--      (grant EXECUTE MANAGED TASK, bootstrap/10 PART 2c); alerts (EXECUTE ALERT / EXECUTE MANAGED ALERT).
-- With dry_run = FALSE it first re-checks (and refuses to start, changing nothing) when runway / runway_db /
-- deploy_role disagree, SHOW TASKS failed, or the database holds alerts but the deploy role lacks EXECUTE ALERT (and
-- EXECUTE MANAGED ALERT for serverless ones). It then records every started task in FLYWAY.DEPLOY_TASK_STATE
-- (DEPLOY_ID 'ADOPT-<timestamp>', printed in the report), executes the plan one statement at a time (a failure is
-- reported and the rest continue), and resumes every task that was started before and is not started after
-- (children, then finalizers, then roots) and every alert that was started before. A transfer of a schema's tasks
-- ALWAYS suspends all of them, so this is not optional. If the resume phase itself fails, the report still lists
-- what was started; the ADOPT- rows hold the task list (RUNBOOKS R9). Expect a few minutes for 50+ schemas.
-- Why SYSADMIN: it sees and (through the role hierarchy) owns every object whose owner role rolls up to SYSADMIN.
-- A FAILED line on ALL <type> means some object of that type is owned by a role outside that hierarchy: have
-- SECURITYADMIN grant that role to SYSADMIN (GRANT ROLE <owner> TO ROLE SYSADMIN, the standard hierarchy) and run
-- PART 3 again -- never for a governance / policy-admin or secrets-owning role (that would give every SYSADMIN holder
-- its powers): keep include_governance_objects = FALSE and leave those objects with their owner. The ACCOUNT_USAGE
-- query after the block lists owners SYSADMIN may not see (ACCOUNTADMIN, ~2 h lag).
-- Previous owners: a role that owned schemas or objects keeps no implicit rights after the transfer (the grants
-- under d. replace the usual ones; see keep_previous_owner_access in PART 0). An owner that is a DATABASE role gets
-- a FAILED line under d.: grant it by hand with GRANT ... TO DATABASE ROLE.
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($runway_db);

EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    db VARCHAR;
    deployer VARCHAR;
    dry BOOLEAN;
    gov BOOLEAN;
    keep_old BOOLEAN;
    bad_vars EXCEPTION (-20231,
        'PART 3: runway, runway_db, deploy_role and the session database disagree: re-run PART 0. Nothing done.');
    no_task_list EXCEPTION (-20232,
        'PART 3: SHOW TASKS failed, so the started tasks are unknown (see the dry run). Nothing was done.');
    no_alert_grant EXCEPTION (-20233,
        'PART 3: alerts exist; grant the deploy role EXECUTE (MANAGED) ALERT first (bootstrap/10 2c). Nothing done.');
    -- Data objects before the code that reads them; alerts, tasks and pipes are handled separately. Extend the list
    -- if your schemas hold other types (e.g. 'SEMANTIC VIEWS', 'NOTEBOOKS', 'STREAMLITS', 'ICEBERG TABLES'); a type
    -- Snowflake does not accept for ALL ... IN SCHEMA shows as a FAILED line, nothing else is affected. Never add
    -- 'PIPES' (bulk ownership grants on pipes are refused) or 'SECRETS' (they stay with their owner, PART 0).
    obj_types ARRAY DEFAULT ARRAY_CONSTRUCT('SEQUENCES', 'FILE FORMATS', 'STAGES', 'TABLES', 'EXTERNAL TABLES',
        'EVENT TABLES', 'DYNAMIC TABLES', 'VIEWS', 'MATERIALIZED VIEWS', 'FUNCTIONS', 'PROCEDURES', 'STREAMS');
    gov_types ARRAY DEFAULT ARRAY_CONSTRUCT('TAGS', 'MASKING POLICIES', 'ROW ACCESS POLICIES');
    -- what keep_previous_owner_access grants each previous owner, per schema (<fq> and <role> are filled in)
    old_owner_grants ARRAY DEFAULT ARRAY_CONSTRUCT(
        'GRANT USAGE ON SCHEMA <fq> TO ROLE <role>',
        'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA <fq> TO ROLE <role>',
        'GRANT SELECT ON ALL VIEWS IN SCHEMA <fq> TO ROLE <role>',
        'GRANT SELECT ON ALL STREAMS IN SCHEMA <fq> TO ROLE <role>',
        'GRANT USAGE ON ALL SEQUENCES IN SCHEMA <fq> TO ROLE <role>',
        'GRANT USAGE ON ALL FILE FORMATS IN SCHEMA <fq> TO ROLE <role>',
        'GRANT USAGE ON ALL FUNCTIONS IN SCHEMA <fq> TO ROLE <role>',
        'GRANT USAGE ON ALL PROCEDURES IN SCHEMA <fq> TO ROLE <role>');
    sep VARCHAR DEFAULT CHR(31);                           -- separates schema and owner in owner_pairs
    schema_list ARRAY;
    managed_str VARCHAR DEFAULT '|';                       -- |schema| already managed access
    task_schemas VARCHAR DEFAULT '|';                      -- |schema| holding a task not owned by the deploy role
    alert_schemas VARCHAR DEFAULT '|';                     -- |schema| holding an alert not owned by the deploy role
    owner_pairs ARRAY DEFAULT ARRAY_CONSTRUCT();           -- 'schema' || sep || 'previous owner role'
    pair_part ARRAY;
    pipes ARRAY DEFAULT ARRAY_CONSTRUCT();                 -- {s, fqn, state} of pipes not owned by the deploy role
    wh_list ARRAY DEFAULT ARRAY_CONSTRUCT();
    wh_part ARRAY;
    started_list ARRAY DEFAULT ARRAY_CONSTRUCT();
    started_alerts ARRAY DEFAULT ARRAY_CONSTRUCT();
    resume_list ARRAY;
    plan ARRAY DEFAULT ARRAY_CONSTRUCT();
    roles_obj OBJECT DEFAULT OBJECT_CONSTRUCT();
    role_keys ARRAY;
    pipe_item VARIANT;
    pipes_st ARRAY DEFAULT ARRAY_CONSTRUCT();
    notes VARCHAR DEFAULT '';
    lists VARCHAR DEFAULT '';
    report VARCHAR DEFAULT '';
    started_str VARCHAR;
    adopt_id VARCHAR;
    s VARCHAR;
    fq VARCHAR;
    pfq VARCHAR;
    o VARCHAR;
    stmt VARCHAR;
    v VARCHAR;
    n INTEGER;
    m INTEGER;
    not_owned INTEGER DEFAULT 0;
    serverless INTEGER DEFAULT 0;
    n_alerts INTEGER DEFAULT 0;
    serverless_alerts INTEGER DEFAULT 0;
    n_grant INTEGER;
    n_mgrant INTEGER;
    tasks_ok BOOLEAN DEFAULT TRUE;
    ok_cnt INTEGER DEFAULT 0;
    fail_cnt INTEGER DEFAULT 0;
BEGIN
    SELECT $runway, $runway_db, $deploy_role, $dry_run, $include_governance_objects, $keep_previous_owner_access
      INTO :rw, :db, :deployer, :dry, :gov, :keep_old;
    -- a partly re-run PART 0 must never hand one runway's objects to another runway's deploy role
    IF (rw IS NULL OR rw NOT IN ('DEV', 'SAN', 'SEA', 'PHX', 'MGM', 'PRD')
        OR db IS DISTINCT FROM ('ALFA_EDW_' || rw) OR deployer IS DISTINCT FROM ('EDW_' || rw || '_DEPLOYER')
        OR CURRENT_DATABASE() IS DISTINCT FROM db) THEN
        RAISE bad_vars;
    END IF;
    IF (gov) THEN
        obj_types := ARRAY_CAT(gov_types, obj_types);
    END IF;

    -- schemas: names, how many are not yet owned by the deploy role, managed access, and previous owners
    EXECUTE IMMEDIATE 'SHOW SCHEMAS IN DATABASE ' || :db;
    SELECT COALESCE(ARRAY_AGG(x.NAME) WITHIN GROUP (ORDER BY x.NAME), ARRAY_CONSTRUCT()),
           COUNT_IF(x.OWNER IS DISTINCT FROM :deployer),
           '|' || COALESCE(LISTAGG(IFF(CONTAINS(x.OPTIONS, 'MANAGED ACCESS'), x.NAME, NULL), '|'), '') || '|',
           COALESCE(ARRAY_AGG(DISTINCT IFF(COALESCE(x.OWNER, '') IN ('', :deployer, 'SYSADMIN', 'ACCOUNTADMIN'), NULL,
                                           x.NAME || :sep || x.OWNER)), ARRAY_CONSTRUCT())
      INTO :schema_list, :not_owned, :managed_str, :owner_pairs
      FROM (SELECT t."name"::VARCHAR AS NAME, t."owner"::VARCHAR AS OWNER,
                   UPPER(COALESCE(GET(OBJECT_CONSTRUCT(*), 'options')::VARCHAR, '')) AS OPTIONS
              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
             WHERE t."name" <> 'INFORMATION_SCHEMA') AS x;

    -- owners of tables and views (SHOW OBJECTS)
    BEGIN
        EXECUTE IMMEDIATE 'SHOW OBJECTS IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(DISTINCT IFF(COALESCE(t."owner"::VARCHAR, '') IN ('', :deployer, 'SYSADMIN',
                                                   'ACCOUNTADMIN') OR t."schema_name" = 'INFORMATION_SCHEMA', NULL,
                                               t."schema_name"::VARCHAR || :sep || t."owner"::VARCHAR)),
                        ARRAY_CONSTRUCT())
          INTO :pair_part
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        owner_pairs := ARRAY_DISTINCT(ARRAY_CAT(owner_pairs, pair_part));
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW OBJECTS failed (' || SQLERRM || '): table / view owners not listed'
                || CHR(10);
    END;

    -- tasks: warehouses, serverless ones, the started ones (fully quoted, like the callbacks), owners, and the
    -- schemas whose tasks must move
    BEGIN
        EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(DISTINCT NULLIF(t."warehouse"::VARCHAR, '')), ARRAY_CONSTRUCT()),
               COUNT_IF(NULLIF(t."warehouse"::VARCHAR, '') IS NULL),
               COALESCE(ARRAY_AGG(IFF(LOWER(t."state") = 'started',
                   '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                   || '"."' || REPLACE(t."name", '"', '""') || '"', NULL)), ARRAY_CONSTRUCT()),
               '|' || COALESCE(LISTAGG(DISTINCT IFF(t."owner" IS DISTINCT FROM :deployer, t."schema_name", NULL),
                                       '|'), '') || '|',
               COALESCE(ARRAY_AGG(DISTINCT IFF(COALESCE(t."owner"::VARCHAR, '') IN ('', :deployer, 'SYSADMIN',
                                                   'ACCOUNTADMIN'), NULL,
                                               t."schema_name"::VARCHAR || :sep || t."owner"::VARCHAR)),
                        ARRAY_CONSTRUCT())
          INTO :wh_list, :serverless, :started_list, :task_schemas, :pair_part
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        owner_pairs := ARRAY_DISTINCT(ARRAY_CAT(owner_pairs, pair_part));
    EXCEPTION
        WHEN OTHER THEN
            tasks_ok := FALSE;
            notes := notes || '-- WARNING SHOW TASKS failed (' || SQLERRM || '): the apply run refuses to start'
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
    -- alerts: warehouses, how many (serverless ones), the started ones, owners, schemas whose alerts must move
    BEGIN
        EXECUTE IMMEDIATE 'SHOW ALERTS IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(DISTINCT NULLIF(t."warehouse"::VARCHAR, '')), ARRAY_CONSTRUCT()),
               COUNT(*),
               COUNT_IF(NULLIF(t."warehouse"::VARCHAR, '') IS NULL),
               COALESCE(ARRAY_AGG(IFF(LOWER(t."state") = 'started',
                   '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                   || '"."' || REPLACE(t."name", '"', '""') || '"', NULL)), ARRAY_CONSTRUCT()),
               '|' || COALESCE(LISTAGG(DISTINCT IFF(t."owner" IS DISTINCT FROM :deployer, t."schema_name", NULL),
                                       '|'), '') || '|',
               COALESCE(ARRAY_AGG(DISTINCT IFF(COALESCE(t."owner"::VARCHAR, '') IN ('', :deployer, 'SYSADMIN',
                                                   'ACCOUNTADMIN'), NULL,
                                               t."schema_name"::VARCHAR || :sep || t."owner"::VARCHAR)),
                        ARRAY_CONSTRUCT())
          INTO :wh_part, :n_alerts, :serverless_alerts, :started_alerts, :alert_schemas, :pair_part
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        wh_list := ARRAY_DISTINCT(ARRAY_CAT(wh_list, wh_part));
        owner_pairs := ARRAY_DISTINCT(ARRAY_CAT(owner_pairs, pair_part));
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW ALERTS failed (' || SQLERRM || ')' || CHR(10);
    END;
    -- pipes not owned by the deploy role (moved one by one), with their owners
    BEGIN
        EXECUTE IMMEDIATE 'SHOW PIPES IN DATABASE ' || :db;
        SELECT COALESCE(ARRAY_AGG(IFF(t."owner" IS DISTINCT FROM :deployer,
                   OBJECT_CONSTRUCT('s', t."schema_name"::VARCHAR,
                                    'fqn', :db || '."' || REPLACE(t."schema_name", '"', '""') || '"."'
                                           || REPLACE(t."name", '"', '""') || '"'), NULL)), ARRAY_CONSTRUCT()),
               COALESCE(ARRAY_AGG(DISTINCT IFF(COALESCE(t."owner"::VARCHAR, '') IN ('', :deployer, 'SYSADMIN',
                                                   'ACCOUNTADMIN'), NULL,
                                               t."schema_name"::VARCHAR || :sep || t."owner"::VARCHAR)),
                        ARRAY_CONSTRUCT())
          INTO :pipes, :pair_part
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        owner_pairs := ARRAY_DISTINCT(ARRAY_CAT(owner_pairs, pair_part));
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '-- WARNING SHOW PIPES failed (' || SQLERRM || '): pipes not planned' || CHR(10);
    END;
    -- each moving pipe's execution state now (RUNNING ones are force-resumed after the transfer)
    n := ARRAY_SIZE(pipes);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            pipe_item := GET(pipes, i - 1);
            pfq := GET(pipe_item, 'fqn')::VARCHAR;
            BEGIN
                EXECUTE IMMEDIATE 'SELECT PARSE_JSON(SYSTEM$PIPE_STATUS(''' || REPLACE(:pfq, '''', '''''')
                    || ''')):executionState::VARCHAR';
                SELECT $1 INTO :v FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
            EXCEPTION
                WHEN OTHER THEN
                    v := 'UNKNOWN (' || SQLERRM || ')';
            END;
            pipes_st := ARRAY_APPEND(pipes_st, OBJECT_INSERT(pipe_item::OBJECT, 'state', COALESCE(v, 'UNKNOWN'),
                                                             TRUE));
            lists := lists || '-- pipe ' || pfq || ': ' || COALESCE(v, 'UNKNOWN')
                || IFF(v = 'RUNNING', ' (paused for the transfer, force-resumed after it)',
                       ' (paused for the transfer; stays paused)') || CHR(10);
        END FOR;
        pipes := pipes_st;
    END IF;

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

    -- previous owner roles and the schemas they owned (in): they lose their implicit owner rights at the transfer
    n := ARRAY_SIZE(owner_pairs);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            v := GET(owner_pairs, i - 1)::VARCHAR;
            o := SPLIT_PART(v, sep, 2);
            roles_obj := OBJECT_INSERT(roles_obj, o,
                COALESCE(GET(roles_obj, o)::VARCHAR || ', ', '') || SPLIT_PART(v, sep, 1), TRUE);
        END FOR;
        role_keys := OBJECT_KEYS(roles_obj);
        FOR i IN 1 TO ARRAY_SIZE(role_keys) DO
            o := GET(role_keys, i - 1)::VARCHAR;
            notes := notes || '-- WARNING previous owner ' || o || ' owned schema(s) / objects in: '
                || GET(roles_obj, o)::VARCHAR || '. It loses its implicit owner rights at the transfer; '
                || IFF(keep_old, 'working grants are planned for it below', 'nothing is planned for it '
                       || '(keep_previous_owner_access = FALSE): re-grant what its jobs need') || CHR(10);
        END FOR;
    END IF;

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
            -- a. pause the pipes that move
            IF (ARRAY_SIZE(pipes) > 0) THEN
                FOR k IN 1 TO ARRAY_SIZE(pipes) DO
                    IF (GET(GET(pipes, k - 1), 's')::VARCHAR = s) THEN
                        plan := ARRAY_APPEND(plan, 'ALTER PIPE ' || GET(GET(pipes, k - 1), 'fqn')::VARCHAR
                            || ' SET PIPE_EXECUTION_PAUSED = TRUE');
                    END IF;
                END FOR;
            END IF;
            -- b. the schema, then every object type schema-wide; alerts and tasks only where one is not owned yet
            plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON SCHEMA ' || fq || ' TO ROLE ' || deployer
                || ' COPY CURRENT GRANTS');
            FOR j IN 1 TO m DO
                plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON ALL ' || GET(obj_types, j - 1)::VARCHAR
                    || ' IN SCHEMA ' || fq || ' TO ROLE ' || deployer || ' COPY CURRENT GRANTS');
            END FOR;
            IF (CONTAINS(alert_schemas, '|' || s || '|')) THEN
                plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON ALL ALERTS IN SCHEMA ' || fq || ' TO ROLE ' || deployer
                    || ' COPY CURRENT GRANTS');
            END IF;
            IF (CONTAINS(task_schemas, '|' || s || '|')) THEN
                plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON ALL TASKS IN SCHEMA ' || fq || ' TO ROLE ' || deployer
                    || ' COPY CURRENT GRANTS');
            END IF;
            -- c. pipes one by one, after their tables and stages; force-resume the ones that were running
            IF (ARRAY_SIZE(pipes) > 0) THEN
                FOR k IN 1 TO ARRAY_SIZE(pipes) DO
                    pipe_item := GET(pipes, k - 1);
                    IF (GET(pipe_item, 's')::VARCHAR = s) THEN
                        pfq := GET(pipe_item, 'fqn')::VARCHAR;
                        plan := ARRAY_APPEND(plan, 'GRANT OWNERSHIP ON PIPE ' || pfq || ' TO ROLE ' || deployer
                            || ' COPY CURRENT GRANTS');
                        IF (GET(pipe_item, 'state')::VARCHAR = 'RUNNING') THEN
                            plan := ARRAY_APPEND(plan, 'SELECT SYSTEM$PIPE_FORCE_RESUME('''
                                || REPLACE(pfq, '''', '''''') || ''')');
                        END IF;
                    END IF;
                END FOR;
            END IF;
            -- d. working grants for the previous owners of this schema or of objects in it
            IF (keep_old AND ARRAY_SIZE(owner_pairs) > 0) THEN
                FOR k IN 1 TO ARRAY_SIZE(owner_pairs) DO
                    v := GET(owner_pairs, k - 1)::VARCHAR;
                    IF (SPLIT_PART(v, sep, 1) = s) THEN
                        o := '"' || REPLACE(SPLIT_PART(v, sep, 2), '"', '""') || '"';
                        FOR j IN 1 TO ARRAY_SIZE(old_owner_grants) DO
                            plan := ARRAY_APPEND(plan, REPLACE(REPLACE(GET(old_owner_grants, j - 1)::VARCHAR,
                                                                       '<fq>', fq), '<role>', o));
                        END FOR;
                    END IF;
                END FOR;
            END IF;
            -- e. managed access, so the deploy role (schema owner) can define the layer-90 FUTURE grants
            IF (NOT CONTAINS(managed_str, '|' || s || '|')) THEN
                plan := ARRAY_APPEND(plan, 'ALTER SCHEMA ' || fq || ' ENABLE MANAGED ACCESS');
            END IF;
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
    IF (n_alerts > 0) THEN
        notes := notes || '-- NOTE ' || n_alerts || ' alert(s), ' || serverless_alerts || ' serverless: grant EXECUTE '
            || 'ALERT' || IFF(serverless_alerts > 0, ' and EXECUTE MANAGED ALERT', '') || ' to ' || deployer
            || ' (bootstrap/10 PART 2c) first: the apply run refuses to start without it' || CHR(10);
    END IF;
    lists := '-- ' || ARRAY_SIZE(started_list) || ' started task(s) (suspended by the transfer of their schema''s '
        || 'tasks, resumed by the apply run): ' || IFF(ARRAY_SIZE(started_list) = 0, 'none',
                                                       ARRAY_TO_STRING(started_list, ', ')) || CHR(10)
        || '-- ' || ARRAY_SIZE(started_alerts) || ' started alert(s) (resumed by the apply run if the transfer '
        || 'suspends them): ' || IFF(ARRAY_SIZE(started_alerts) = 0, 'none', ARRAY_TO_STRING(started_alerts, ', '))
        || CHR(10) || lists;

    report := '-- database ' || db || ': ' || ARRAY_SIZE(schema_list) || ' schema(s), ' || not_owned
        || ' not owned by ' || deployer || '; ' || ARRAY_SIZE(plan) || ' statement(s) planned' || CHR(10)
        || lists || notes;

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

    -- apply: refuse to start (nothing changed yet) when the task list is unknown or an alert grant is missing
    IF (NOT tasks_ok) THEN
        RAISE no_task_list;
    END IF;
    IF (n_alerts > 0) THEN
        EXECUTE IMMEDIATE 'SHOW GRANTS TO ROLE ' || :deployer;
        SELECT COUNT_IF(t."privilege" = 'EXECUTE ALERT' AND t."granted_on" = 'ACCOUNT'),
               COUNT_IF(t."privilege" = 'EXECUTE MANAGED ALERT' AND t."granted_on" = 'ACCOUNT')
          INTO :n_grant, :n_mgrant
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        IF (n_grant = 0 OR (serverless_alerts > 0 AND n_mgrant = 0)) THEN
            RAISE no_alert_grant;
        END IF;
    END IF;

    -- record the started tasks before the first transfer (it suspends them): the list must survive a failed run
    adopt_id := 'ADOPT-' || TO_CHAR(CURRENT_TIMESTAMP(), 'YYYYMMDDHH24MISS');
    BEGIN
        EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :db;
        INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS, ROOT_FQN)
        SELECT :adopt_id, x.FQN, 'started', CURRENT_TIMESTAMP(), IFF(x.IS_ROOT, x.FQN, NULL)
          FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                       || '"."' || REPLACE(t."name", '"', '""') || '"' AS FQN,
                       REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]' AS IS_ROOT,
                       LOWER(t."state") AS STATE
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS x
         WHERE x.STATE = 'started';
        SELECT COALESCE(ARRAY_AGG(s.TASK_FQN) WITHIN GROUP (ORDER BY s.TASK_FQN), ARRAY_CONSTRUCT())
          INTO :started_list
          FROM FLYWAY.DEPLOY_TASK_STATE AS s
         WHERE s.DEPLOY_ID = :adopt_id;
    EXCEPTION
        WHEN OTHER THEN
            RETURN '-- NOT APPLIED: the started tasks could not be recorded in FLYWAY.DEPLOY_TASK_STATE ('
                || SQLERRM || '). Run PART 2 (it creates / upgrades the table), then PART 3 again.' || CHR(10)
                || report;
    END;
    report := '-- started tasks recorded as DEPLOY_ID ' || adopt_id || ' in FLYWAY.DEPLOY_TASK_STATE' || CHR(10)
        || report;

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

    -- tasks started before and not started now: children first, then finalizers, then roots (a child or a
    -- finalizer can be resumed only while its root is suspended). Any error here still returns the lists above.
    BEGIN
        IF (ARRAY_SIZE(started_list) > 0) THEN
            started_str := '|' || ARRAY_TO_STRING(started_list, '|') || '|';
            EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :db;
            SELECT COALESCE(ARRAY_AGG(q.FQN) WITHIN GROUP (ORDER BY q.RANK_ORDER, q.FQN), ARRAY_CONSTRUCT())
              INTO :resume_list
              FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."'
                           || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""')
                           || '"' AS FQN,
                           IFF(REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') <> '[]', 0,
                               IFF(NULLIF(GET(TRY_PARSE_JSON(GET(OBJECT_CONSTRUCT(*), 'task_relations')::VARCHAR),
                                              'FinalizedRootTask')::VARCHAR, '') IS NULL, 2, 1)) AS RANK_ORDER,
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
        -- alerts started before and not started now
        IF (ARRAY_SIZE(started_alerts) > 0) THEN
            started_str := '|' || ARRAY_TO_STRING(started_alerts, '|') || '|';
            EXECUTE IMMEDIATE 'SHOW ALERTS IN DATABASE ' || :db;
            SELECT COALESCE(ARRAY_AGG(q.FQN) WITHIN GROUP (ORDER BY q.FQN), ARRAY_CONSTRUCT())
              INTO :resume_list
              FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."'
                           || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""')
                           || '"' AS FQN,
                           LOWER(t."state") AS STATE
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS q
             WHERE q.STATE <> 'started'
               AND CONTAINS(:started_str, '|' || q.FQN || '|');
            n := ARRAY_SIZE(resume_list);
            IF (n > 0) THEN
                FOR k IN 1 TO n DO
                    stmt := 'ALTER ALERT ' || GET(resume_list, k - 1)::VARCHAR || ' RESUME';
                    BEGIN
                        EXECUTE IMMEDIATE :stmt;
                        report := report || '-- resumed (was started before the transfer): ' || stmt || CHR(10);
                    EXCEPTION
                        WHEN OTHER THEN
                            fail_cnt := fail_cnt + 1;
                            report := report || '-- FAILED ' || stmt || ';  -- ' || SQLERRM
                                || ' (EXECUTE ALERT / warehouse USAGE on ' || deployer || '?)' || CHR(10);
                    END;
                END FOR;
            END IF;
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            RETURN '-- APPLIED with the RESUME PHASE FAILED (' || SQLERRM || '): resume the started tasks / alerts '
                || 'listed below by hand (RUNBOOKS R9; tasks also under DEPLOY_ID ' || adopt_id || ')' || CHR(10)
                || '-- ' || ok_cnt || ' statement(s) succeeded, ' || fail_cnt || ' failed' || CHR(10) || report;
    END;

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
--   eval "$(python3 scripts/fw.py env --runway DEV --format shell)"
--   export SNOWFLAKE_ACCOUNT='<orgname>-<account_name>'
--   export FLYWAY_SF_USER='<your Snowflake user>'           # with a registered key pair and the deploy role
--   export FLYWAY_SF_KEY_FILE="$HOME/.snowflake/keys/<you>.p8"
--   read -rsp 'key passphrase: ' FLYWAY_SF_KEY_PWD && export FLYWAY_SF_KEY_PWD && echo
--   export FLYWAY_EMAIL='<you@company>'; read -rsp 'Redgate PAT: ' FLYWAY_TOKEN && export FLYWAY_TOKEN && echo
--   export JAVA_ARGS='-Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true'
--   flyway -environment=DEV info                     # expect: no schema history table, nothing applied
--   test -n "$EDW_BASELINE_VERSION" || { echo 'EDW_BASELINE_VERSION unset: run the fw.py env line'; exit 1; }
--   flyway -environment=DEV baseline -baselineVersion="$EDW_BASELINE_VERSION" \
--          -baselineDescription="ALFA EDW adoption baseline"
--   flyway -environment=DEV info                     # expect: one Baseline row at the baseline version
--
-- fw.py exports the kit's settings as EDW_* (EDW_BASELINE_VERSION = runways.toml [kit] baseline_version, EDW_TARGET,
-- ...) and never a FLYWAY_<SETTING> name. Export no FLYWAY_* variable of your own either: Flyway reads FLYWAY_TARGET,
-- FLYWAY_BASELINE_VERSION, FLYWAY_ENVIRONMENT, ... from the environment as its OWN settings and would silently
-- retarget the run (FLYWAY_SF_* / FLYWAY_RH_DB are not Flyway settings: flyway.toml reads them through ${env.*}).
-- Never drop -baselineVersion: Flyway's default baseline version is 1, below every kit migration, so all of them
-- would count as pending on the adopted database. baseline runs no callbacks, so it needs no deploy placeholders.
-- Then run PART 5.


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
    rw VARCHAR;
    db VARCHAR;
    deployer VARCHAR;
    v VARCHAR;
    c1 INTEGER;
    c2 INTEGER;
    c3 INTEGER;
    c4 INTEGER;
    v2 VARCHAR;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $runway, $runway_db, $deploy_role INTO :rw, :db, :deployer;
    report := 'INFO   ' || db || ', deploy role ' || deployer || CHR(10);

    -- (1) the database: owner and grants
    BEGIN
        EXECUTE IMMEDIATE 'SHOW GRANTS ON DATABASE ' || :db;
        SELECT COUNT_IF(t."privilege" = 'OWNERSHIP' AND t."grantee_name" = 'SYSADMIN'),
               COUNT_IF(t."privilege" IN ('USAGE', 'CREATE SCHEMA') AND t."grantee_name" = :deployer),
               COUNT_IF(t."privilege" = 'USAGE' AND t."grantee_name" = 'EDW_FLYWAY_AUDITOR'),
               COUNT_IF(t."privilege" = 'USAGE'
                        AND REGEXP_LIKE(t."grantee_name", 'EDW_' || :rw || '_[A-Z][A-Z0-9_]*_(READ|WRITE)'))
          INTO :c1, :c2, :c3, :c4
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   database owned by SYSADMIN' || CHR(10)
            || IFF(c2 = 2, 'PASS', 'FAIL') || '   ' || deployer || ': USAGE + CREATE SCHEMA on the database'
            || CHR(10) || IFF(c3 > 0, 'PASS', 'FAIL') || '   EDW_FLYWAY_AUDITOR: USAGE on the database' || CHR(10)
            || IFF(c4 > 0, 'INFO', 'CHECK') || '   ' || c4 || ' access role(s) EDW_' || rw
            || '_<SCHEMA>_READ / _WRITE with USAGE on the database (layer-90 grantees: bootstrap/10 PART 2e, PART 1)'
            || CHR(10);
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
                        AND c.COLUMN_NAME IN ('DEPLOY_ID', 'TASK_FQN', 'STATE_BEFORE', 'CAPTURED_TS', 'ROOT_FQN')),
               COUNT_IF(c.COLUMN_NAME IN ('EVENT_TS', 'CAPTURED_TS') AND c.DATA_TYPE = 'TIMESTAMP_LTZ')
          INTO :c1, :c2, :c3
          FROM INFORMATION_SCHEMA.COLUMNS AS c
         WHERE c.TABLE_SCHEMA = 'FLYWAY' AND c.TABLE_NAME IN ('DEPLOY_LOG', 'DEPLOY_TASK_STATE');
        report := report || IFF(c1 = 9 AND c2 = 5 AND c3 = 2, 'PASS', 'FAIL')
            || '   kit table columns (DEPLOY_LOG 9/' || c1 || ', DEPLOY_TASK_STATE 5/' || c2
            || ', TIMESTAMP_LTZ 2/' || c3 || ')' || IFF(c2 = 5, '', ' -- re-run PART 2 (kit upgrade)') || CHR(10);
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

    -- (6) every schema: owned by the deploy role (adoption, PART 3) and MANAGED ACCESS (the layer-90 FUTURE grants
    --     fail in a standard schema)
    BEGIN
        EXECUTE IMMEDIATE 'SHOW SCHEMAS IN DATABASE ' || :db;
        SELECT COUNT_IF(x.OWNER IS DISTINCT FROM :deployer),
               LISTAGG(IFF(x.OWNER IS DISTINCT FROM :deployer, x.NAME || ' (' || COALESCE(x.OWNER, '?') || ')',
                           NULL), ', '),
               COUNT_IF(NOT CONTAINS(x.OPTIONS, 'MANAGED ACCESS')),
               LISTAGG(IFF(CONTAINS(x.OPTIONS, 'MANAGED ACCESS'), NULL, x.NAME), ', ')
          INTO :c1, :v, :c2, :v2
          FROM (SELECT t."name"::VARCHAR AS NAME, t."owner"::VARCHAR AS OWNER,
                       UPPER(COALESCE(GET(OBJECT_CONSTRUCT(*), 'options')::VARCHAR, '')) AS OPTIONS
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                 WHERE t."name" <> 'INFORMATION_SCHEMA') AS x;
        report := report || IFF(c1 = 0, 'PASS   every schema owned by ' || deployer,
                                'CHECK  ' || c1 || ' schema(s) not owned by ' || deployer || ' (PART 3): '
                                || LEFT(v, 2000)) || CHR(10)
            || IFF(c2 = 0, 'PASS   every schema is MANAGED ACCESS',
                   'FAIL   ' || c2 || ' schema(s) not MANAGED ACCESS (PART 3; layer-90 FUTURE grants fail there): '
                   || LEFT(v2, 2000)) || CHR(10);
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
