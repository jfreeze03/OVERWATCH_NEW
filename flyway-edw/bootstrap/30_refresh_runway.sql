-- =====================================================================================================================
-- bootstrap/30_refresh_runway.sql -- refresh a test runway (SAN / SEA / PHX, or MGM) from production by zero-copy clone
-- =====================================================================================================================
-- WHAT    Replaces ALFA_EDW_<ID> with a zero-copy clone of ALFA_EDW_PRD and hands the clone to the runway:
--           PART 0  variables + guard: never PRD, never DEV, MGM only with allow_mgm = TRUE
--           PART 1  pre-checks (read-only): dangling policy references, hybrid / external tables, pipes that a clone
--                   drops or that would load PRD, PRD code naming ALFA_EDW_PRD (3-part names), objects the cloning
--                   role does not own, a PRD deploy in flight. Then SET prechecks_reviewed = TRUE.
--           PART 2  SYSADMIN          pause the outgoing runway's schedules and park it as ALFA_EDW_<ID>_PRE_REFRESH
--           PART 3  EDW_PRD_DEPLOYER  CREATE OR REPLACE DATABASE ALFA_EDW_<ID> CLONE ALFA_EDW_PRD
--           PART 4  EDW_PRD_DEPLOYER  every schema + object -> EDW_<ID>_DEPLOYER (schema-wide, COPY CURRENT
--                                     GRANTS); the database -> SYSADMIN
--           PART 5  SYSADMIN          database-level grants (they are never cloned): kit grants + the parked
--                                     runway's grants
--           PART 6  SECURITYADMIN     grants PRD functional roles (EDW_PRD_*) hold on the clone's objects are moved
--                                     to the runway's roles (EDW_<ID>_*)
--           PART 7  SYSADMIN          what arrived suspended, the REFRESHED row in FLYWAY.DEPLOY_LOG, verification
--           PART 8  next steps: re-deploy the project's own migrations, resume deliberately, drop the parked copy
-- WHEN    Between runway leases (GUIDE F2, RUNBOOKS R8), to adopt MGM / a test runway at PRD's state (GUIDE D6), or
--         when a test runway drifted beyond repair. Never on PRD (it is the source) or DEV (shared integration
--         database: reconcile it in place, GUIDE D6).
-- BEFORE  * Data owner + security sign-off (GUIDE A1): see DATA GOVERNANCE below.
--         * Jenkins: Reserve the lockable resource ALFA_EDW_<ID> for the whole refresh (no deploy may touch the
--           runway meanwhile), and ALFA_EDW_PRD around PART 3 only, so no PRD migrate is half-applied in the clone.
--         * Tell the runway's lease holder: everything in the runway that is not in PRD is gone after PART 3 (the
--           parked copy keeps it until PART 8).
-- WHO     A Snowflake admin holding SYSADMIN, SECURITYADMIN and (PART 1a only) ACCOUNTADMIN, in Snowsight. PARTs 3-4
--         run as EDW_PRD_DEPLOYER, which SYSADMIN holders inherit (bootstrap/10): using the PRD deploy role by hand
--         is break-glass (GUIDE B7) -- record this refresh in the change ticket.
-- WHY EDW_PRD_DEPLOYER CLONES
--   A clone contains only what the cloning role may see: SELECT for tables, USAGE for most objects, and OWNERSHIP for
--   tasks, pipes, streams and alerts. The PRD deploy role owns all of them (bootstrap/20 PART 3), so its clone is
--   complete; any other role silently loses objects. The role that clones owns the result, which is why PART 4 hands
--   the schemas to the runway's deploy role and the database to SYSADMIN (the kit's ownership model, bootstrap/20).
-- WHAT A CLONE DOES AND DOES NOT CARRY (docs/SHORTCOMINGS.md S20, S24)
--   * Grants on child objects come along (PRD functional roles included: PART 6); grants on the database do not
--     (PART 5). Schema-level future grants come along (PART 6 re-points them).
--   * Tasks, alerts and dynamic tables arrive SUSPENDED; auto-ingest pipes arrive STOPPED_CLONED, others paused;
--     pipes on internal stages and external tables are not cloned; hybrid tables are skipped (IGNORE HYBRID TABLES);
--     streams start at the clone point (unconsumed PRD changes are not readable).
--   * Definitions are copied verbatim: a view / procedure / task that names ALFA_EDW_PRD keeps reading or writing
--     PRD from the runway, and a pipe whose COPY target is schema-qualified loads the PRD table once resumed.
--   * FLYWAY comes along: the runway's history becomes PRD's, so the runway is at PRD's state and still baselined.
--     The project's own unmerged migrations are NOT in it: re-deploy them (PART 8).
-- DATA GOVERNANCE
--   After PART 3 the runway holds production data. Masking and row access policies stored inside PRD are cloned and
--   stay attached; policies stored outside PRD keep governing the clone (PART 1a lists both). Anyone with USAGE on the
--   runway database and a grant on an object reads PRD data subject to those policies: keep runway roles, and the
--   people in them, to what the data owner approved. The clone shares PRD's storage until rows change.
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables + guard (run before any other part)
-- =====================================================================================================================
SET target_runway = 'SAN';                  -- SAN | SEA | PHX; MGM only together with allow_mgm = TRUE
SET allow_mgm = FALSE;                      -- explicit switch: MGM is the PRD dress rehearsal (prod tier)
SET prechecks_reviewed = FALSE;             -- set TRUE only after reading both PART 1 reports
-- TRUE adds INCLUDE INTERNAL STAGES: named internal stages (and so their pipes) come along; files are copied only
-- for stages with a directory table, at data-transfer and compute cost. FALSE (default): they are left out.
-- Verify on first run that your account accepts the clause after IGNORE HYBRID TABLES.
SET include_internal_stages = FALSE;

-- Derived names. Do not edit.
SET target_db = 'ALFA_EDW_' || $target_runway;
SET target_role = 'EDW_' || $target_runway || '_DEPLOYER';
SET parked_db = $target_db || '_PRE_REFRESH';

USE ROLE SYSADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    mgm_ok BOOLEAN;
    tr VARCHAR;
    n_roles INTEGER;
    is_source EXCEPTION (-20301, 'PART 0: PRD is the source of a refresh, never its target.');
    is_dev EXCEPTION (-20302, 'PART 0: DEV is the shared integration database: reconcile it in place (GUIDE D6).');
    mgm_locked EXCEPTION (-20303, 'PART 0: refreshing MGM needs SET allow_mgm = TRUE (release managers informed).');
    bad_runway EXCEPTION (-20304, 'PART 0: target_runway must be SAN, SEA, PHX or MGM (upper case).');
    no_roles EXCEPTION (-20305, 'PART 0: EDW_PRD_DEPLOYER or the target deploy role is missing (bootstrap/10).');
BEGIN
    SELECT $target_runway, $allow_mgm, $target_role INTO :rw, :mgm_ok, :tr;
    IF (rw = 'PRD') THEN
        RAISE is_source;
    ELSEIF (rw = 'DEV') THEN
        RAISE is_dev;
    ELSEIF (rw = 'MGM' AND NOT COALESCE(mgm_ok, FALSE)) THEN
        RAISE mgm_locked;
    ELSEIF (rw IS NULL OR rw NOT IN ('SAN', 'SEA', 'PHX', 'MGM')) THEN
        RAISE bad_runway;
    END IF;
    SHOW ROLES LIKE 'EDW_%_DEPLOYER';
    SELECT COUNT_IF(t."name" IN ('EDW_PRD_DEPLOYER', :tr)) INTO :n_roles FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    IF (n_roles < 2) THEN
        RAISE no_roles;
    END IF;
    RETURN 'PART 0 OK: refresh ALFA_EDW_' || rw || ' from ALFA_EDW_PRD; runway role ' || tr
        || '; the current runway will be parked as ALFA_EDW_' || rw || '_PRE_REFRESH';
END;
$$;


-- =====================================================================================================================
-- PART 1 -- pre-checks (read-only). Read both reports, fix or accept every BLOCKER / HIGH line, then
--           SET prechecks_reviewed = TRUE;
-- =====================================================================================================================

-- ------------------------------------------------------------------------------------- 1a ACCOUNTADMIN (policies)
-- ACCOUNT_USAGE.POLICY_REFERENCES is the only account-wide list of where masking / row access / other policies are
-- attached; it is readable by ACCOUNTADMIN (or a role granted the SNOWFLAKE database's GOVERNANCE_VIEWER) and lags up
-- to about two hours. Why it matters: Snowflake refuses a CREATE OR REPLACE / DROP of a database or a clone that would
-- leave a policy reference dangling, so a policy INSIDE the runway that protects objects OUTSIDE it blocks dropping
-- the parked copy later (PART 8); PRD objects governed from outside PRD stay governed by that outside policy in the
-- runway (shared governance: changing it changes every runway).
USE ROLE ACCOUNTADMIN;

SELECT CASE
           WHEN r.POLICY_DB = $target_db THEN 'BLOCKS PART 8 DROP: a runway policy protects an object outside it'
           WHEN r.POLICY_DB = 'ALFA_EDW_PRD' THEN 'INFO: PRD policy protects an object outside PRD (clone gets a copy)'
           ELSE 'GOVERNANCE: PRD object protected by a policy stored outside PRD (the runway stays bound to it)'
       END AS FINDING,
       r.POLICY_DB || '.' || r.POLICY_SCHEMA || '.' || r.POLICY_NAME AS POLICY,
       r.POLICY_KIND,
       r.REF_DATABASE_NAME || '.' || r.REF_SCHEMA_NAME || '.' || r.REF_ENTITY_NAME AS PROTECTED_OBJECT,
       r.REF_ENTITY_DOMAIN,
       r.REF_COLUMN_NAME
  FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES AS r
 WHERE r.REF_DATABASE_NAME IS NOT NULL
   AND ((r.POLICY_DB = $target_db AND r.REF_DATABASE_NAME <> $target_db)
        OR (r.POLICY_DB = 'ALFA_EDW_PRD' AND r.REF_DATABASE_NAME <> 'ALFA_EDW_PRD')
        OR (r.REF_DATABASE_NAME = 'ALFA_EDW_PRD' AND r.POLICY_DB <> 'ALFA_EDW_PRD'))
 ORDER BY 1, 2, 4;

-- ------------------------------------------------------------------------------------------ 1b SYSADMIN (objects)
-- SYSADMIN inherits EDW_PRD_DEPLOYER (bootstrap/10), so it sees every PRD object and every definition the cloning
-- role sees. Each SHOW is read later through the query id saved right after it: run the whole block in order.
USE ROLE SYSADMIN;

SHOW SCHEMAS IN DATABASE ALFA_EDW_PRD;
SET qid_schemas = LAST_QUERY_ID();
SHOW TASKS IN DATABASE ALFA_EDW_PRD;
SET qid_tasks = LAST_QUERY_ID();
SHOW STREAMS IN DATABASE ALFA_EDW_PRD;
SET qid_streams = LAST_QUERY_ID();
SHOW PIPES IN DATABASE ALFA_EDW_PRD;
SET qid_pipes = LAST_QUERY_ID();
SHOW STAGES IN DATABASE ALFA_EDW_PRD;
SET qid_stages = LAST_QUERY_ID();
SHOW ALERTS IN DATABASE ALFA_EDW_PRD;
SET qid_alerts = LAST_QUERY_ID();
SHOW DYNAMIC TABLES IN DATABASE ALFA_EDW_PRD;
SET qid_dts = LAST_QUERY_ID();
SHOW EXTERNAL TABLES IN DATABASE ALFA_EDW_PRD;
SET qid_ext = LAST_QUERY_ID();
SHOW HYBRID TABLES IN DATABASE ALFA_EDW_PRD;
SET qid_hybrid = LAST_QUERY_ID();
SHOW DATABASE ROLES IN DATABASE ALFA_EDW_PRD;
SET qid_dbroles = LAST_QUERY_ID();
SHOW PARAMETERS LIKE 'EVENT_TABLE' IN DATABASE ALFA_EDW_PRD;
SET qid_evt = LAST_QUERY_ID();

-- The report: SEVERITY BLOCKER (do not proceed) > HIGH (fix first or accept in the ticket) > WARN (know it) > INFO.
-- Columns that differ between Snowflake releases are read through OBJECT_CONSTRUCT(*) so a missing one is NULL, not
-- an error.
WITH
pipe_defs AS (
    SELECT p."schema_name"::VARCHAR AS SCHEMA_NAME, p."name"::VARCHAR AS PIPE_NAME, p."owner"::VARCHAR AS OWNER_ROLE,
           UPPER(p."definition"::VARCHAR) AS DEF
      FROM TABLE(RESULT_SCAN($qid_pipes)) AS p
),
pipes AS (
    SELECT d.SCHEMA_NAME, d.PIPE_NAME, d.OWNER_ROLE, d.DEF,
           REGEXP_SUBSTR(d.DEF, 'COPY[[:space:]]+INTO[[:space:]]+([^[:space:](]+)', 1, 1, 'e', 1) AS COPY_TARGET,
           REGEXP_SUBSTR(d.DEF, '@([^[:space:]/;)]+)', 1, 1, 'e', 1) AS STAGE_REF
      FROM pipe_defs AS d
),
internal_stages AS (
    SELECT UPPER(s."name"::VARCHAR) AS STAGE_NAME
      FROM TABLE(RESULT_SCAN($qid_stages)) AS s
     WHERE UPPER(s."type"::VARCHAR) LIKE 'INTERNAL%'
),
owned AS (
    SELECT 'TASK' AS KIND, t."schema_name"::VARCHAR || '.' || t."name"::VARCHAR AS OBJECT_NAME,
           t."owner"::VARCHAR AS OWNER_ROLE
      FROM TABLE(RESULT_SCAN($qid_tasks)) AS t
    UNION ALL
    SELECT 'STREAM', s."schema_name"::VARCHAR || '.' || s."name"::VARCHAR, s."owner"::VARCHAR
      FROM TABLE(RESULT_SCAN($qid_streams)) AS s
    UNION ALL
    SELECT 'PIPE', p.SCHEMA_NAME || '.' || p.PIPE_NAME, p.OWNER_ROLE
      FROM pipes AS p
    UNION ALL
    SELECT 'ALERT', a."schema_name"::VARCHAR || '.' || a."name"::VARCHAR, a."owner"::VARCHAR
      FROM TABLE(RESULT_SCAN($qid_alerts)) AS a
),
code AS (
    SELECT 'VIEW' AS KIND, v.TABLE_SCHEMA || '.' || v.TABLE_NAME AS OBJECT_NAME, v.VIEW_DEFINITION AS DEF
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.VIEWS AS v
     WHERE v.TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
    UNION ALL
    SELECT 'FUNCTION', f.FUNCTION_SCHEMA || '.' || f.FUNCTION_NAME || f.ARGUMENT_SIGNATURE, f.FUNCTION_DEFINITION
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.FUNCTIONS AS f
    UNION ALL
    SELECT 'PROCEDURE', p.PROCEDURE_SCHEMA || '.' || p.PROCEDURE_NAME || p.ARGUMENT_SIGNATURE, p.PROCEDURE_DEFINITION
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.PROCEDURES AS p
    UNION ALL
    SELECT 'TASK', t."schema_name"::VARCHAR || '.' || t."name"::VARCHAR,
           t."definition"::VARCHAR || ' ' || COALESCE(GET(OBJECT_CONSTRUCT(*), 'condition')::VARCHAR, '')
      FROM TABLE(RESULT_SCAN($qid_tasks)) AS t
    UNION ALL
    SELECT 'DYNAMIC TABLE', d."schema_name"::VARCHAR || '.' || d."name"::VARCHAR,
           GET(OBJECT_CONSTRUCT(*), 'text')::VARCHAR
      FROM TABLE(RESULT_SCAN($qid_dts)) AS d
    UNION ALL
    SELECT 'ALERT', a."schema_name"::VARCHAR || '.' || a."name"::VARCHAR,
           COALESCE(GET(OBJECT_CONSTRUCT(*), 'condition')::VARCHAR, '') || ' '
           || COALESCE(GET(OBJECT_CONSTRUCT(*), 'action')::VARCHAR, '')
      FROM TABLE(RESULT_SCAN($qid_alerts)) AS a
    UNION ALL
    SELECT 'PIPE', p.SCHEMA_NAME || '.' || p.PIPE_NAME, p.DEF
      FROM pipes AS p
),
findings AS (
    SELECT 1 AS SEV_ORDER, 'BLOCKER' AS SEVERITY, 'PRD_DEPLOY_IN_FLIGHT' AS CHECK_NAME,
           'deploy ' || l.DEPLOY_ID AS OBJECT_NAME,
           'STARTED ' || TO_VARCHAR(MAX(l.EVENT_TS)) || ' with no SUCCEEDED / FAILED row: a PRD migrate may be '
           || 'running; wait for it (or RUNBOOKS R6 if the row is an orphan)' AS DETAIL
      FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_LOG AS l
     WHERE l.EVENT_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
     GROUP BY l.DEPLOY_ID
    HAVING COUNT_IF(l.EVENT = 'STARTED') > 0 AND COUNT_IF(l.EVENT IN ('SUCCEEDED', 'FAILED')) = 0
    UNION ALL
    SELECT 2, 'HIGH', 'CROSS_DB_REFERENCE', c.KIND || ' ' || c.OBJECT_NAME,
           'names ' || REGEXP_SUBSTR(UPPER(c.DEF), 'ALFA_EDW_[A-Z0-9_]+') || ': the runway copy keeps using that '
           || 'database, and afterMigrate__80_cross_env_guard fails every deploy to the runway until it is fixed'
      FROM code AS c
     WHERE REGEXP_INSTR(UPPER(c.DEF), 'ALFA_EDW_[A-Z0-9_]+"?[.]') > 0
    UNION ALL
    SELECT 2, 'HIGH', 'PIPE_QUALIFIED_TARGET', 'PIPE ' || p.SCHEMA_NAME || '.' || p.PIPE_NAME,
           'COPY INTO ' || p.COPY_TARGET || ' is qualified: the cloned pipe loads the PRD table once resumed. Keep '
           || 'it paused in the runway; recreate it with a bare table name (guard FG016)'
      FROM pipes AS p
     WHERE CONTAINS(p.COPY_TARGET, '.')
    UNION ALL
    SELECT 2, 'HIGH', 'NOT_OWNED_BY_PRD_DEPLOYER', o.KIND || ' ' || o.OBJECT_NAME,
           'owner ' || COALESCE(o.OWNER_ROLE, '?') || ': a clone includes tasks, streams, pipes and alerts only for '
           || 'their owner, so this one will be MISSING from the runway (adopt it: bootstrap/20 PART 3)'
      FROM owned AS o
     WHERE o.OWNER_ROLE IS DISTINCT FROM 'EDW_PRD_DEPLOYER'
    UNION ALL
    SELECT 3, 'WARN', 'SCHEMA_NOT_OWNED', 'SCHEMA ' || s."name"::VARCHAR,
           'owner ' || COALESCE(s."owner"::VARCHAR, '?') || ': objects EDW_PRD_DEPLOYER cannot see are left out'
      FROM TABLE(RESULT_SCAN($qid_schemas)) AS s
     WHERE s."name" <> 'INFORMATION_SCHEMA' AND s."owner" IS DISTINCT FROM 'EDW_PRD_DEPLOYER'
    UNION ALL
    SELECT 3, 'WARN', 'EXTERNAL_TABLE', 'EXTERNAL TABLE ' || e."schema_name"::VARCHAR || '.' || e."name"::VARCHAR,
           'never cloned; the runway history says it exists, so recreate it by hand (PART 8)'
      FROM TABLE(RESULT_SCAN($qid_ext)) AS e
    UNION ALL
    SELECT 3, 'WARN', 'HYBRID_TABLE', 'HYBRID TABLE ' || h."schema_name"::VARCHAR || '.' || h."name"::VARCHAR,
           'skipped by IGNORE HYBRID TABLES: it will not exist in the runway (PART 8)'
      FROM TABLE(RESULT_SCAN($qid_hybrid)) AS h
    UNION ALL
    SELECT 3, 'WARN', 'PIPE_INTERNAL_STAGE', 'PIPE ' || p.SCHEMA_NAME || '.' || p.PIPE_NAME,
           'reads internal stage @' || p.STAGE_REF || ': not cloned unless include_internal_stages = TRUE (PART 8)'
      FROM pipes AS p
     WHERE STARTSWITH(p.STAGE_REF, '%') OR STARTSWITH(p.STAGE_REF, '~')
        OR REPLACE(SPLIT_PART(p.STAGE_REF, '.', -1), '"', '') IN (SELECT i.STAGE_NAME FROM internal_stages AS i)
    UNION ALL
    SELECT 3, 'WARN', 'EVENT_TABLE_SET', 'DATABASE ALFA_EDW_PRD',
           'EVENT_TABLE = ' || e."value"::VARCHAR || ' at database level: a clone by a role other than ACCOUNTADMIN '
           || 'may fail or not copy it; re-set it on the runway if needed (verify on first run)'
      FROM TABLE(RESULT_SCAN($qid_evt)) AS e
     WHERE UPPER(e."level"::VARCHAR) = 'DATABASE' AND COALESCE(e."value"::VARCHAR, '') <> ''
    UNION ALL
    SELECT 3, 'WARN', 'DEFINITION_NOT_VISIBLE', c.KIND || ' x' || COUNT(*),
           'definitions SYSADMIN cannot read were NOT scanned for ALFA_EDW_ references'
      FROM code AS c
     WHERE c.DEF IS NULL AND c.KIND IN ('VIEW', 'FUNCTION', 'PROCEDURE')
     GROUP BY c.KIND
    UNION ALL
    SELECT 4, 'INFO', 'DATABASE_ROLE', 'DATABASE ROLE ' || r."name"::VARCHAR,
           'cloned with the database; check which account roles hold it in the runway after PART 6'
      FROM TABLE(RESULT_SCAN($qid_dbroles)) AS r
    UNION ALL
    SELECT 4, 'INFO', 'ARRIVES_SUSPENDED', 'TASKS / ALERTS / DYNAMIC TABLES / PIPES',
           (SELECT COUNT(*) FROM TABLE(RESULT_SCAN($qid_tasks))) || ' task(s), '
           || (SELECT COUNT(*) FROM TABLE(RESULT_SCAN($qid_alerts))) || ' alert(s), '
           || (SELECT COUNT(*) FROM TABLE(RESULT_SCAN($qid_dts))) || ' dynamic table(s), '
           || (SELECT COUNT(*) FROM pipes) || ' pipe(s) will arrive suspended / paused; nothing resumes on its own'
)
SELECT f.SEVERITY, f.CHECK_NAME, f.OBJECT_NAME, f.DETAIL
  FROM findings AS f
 ORDER BY f.SEV_ORDER, f.CHECK_NAME, f.OBJECT_NAME;

-- When every BLOCKER is gone and every HIGH line is fixed or accepted in the ticket:
-- SET prechecks_reviewed = TRUE;


-- =====================================================================================================================
-- PART 2 -- park the current runway (SYSADMIN, the database owner)
-- =====================================================================================================================
-- The outgoing runway is renamed, not dropped: it stays a complete fallback until PART 8, and its database-level
-- grants are the template PART 5 re-applies. Its tasks, alerts and dynamic tables are suspended and its pipes paused
-- first, so the parked copy cannot keep running (and spending) under its new name. Renaming also frees the name for
-- the clone: EDW_PRD_DEPLOYER could never replace a database SYSADMIN owns.
-- A parked copy from an earlier refresh must be dropped first (PART 8): one parked copy per runway, by design.
USE ROLE SYSADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    reviewed BOOLEAN;
    tgt VARCHAR;
    parked VARCHAR;
    n_tgt INTEGER;
    n_parked INTEGER;
    obj_list ARRAY;
    part_list ARRAY;
    n INTEGER;
    stmt VARCHAR;
    report VARCHAR DEFAULT '';
    not_reviewed EXCEPTION (-20311, 'PART 2: read the PART 1 reports, then SET prechecks_reviewed = TRUE.');
    parked_exists EXCEPTION (-20312, 'PART 2: <runway>_PRE_REFRESH exists from an earlier refresh: drop it (PART 8).');
BEGIN
    SELECT $prechecks_reviewed, $target_db, $parked_db INTO :reviewed, :tgt, :parked;
    IF (NOT COALESCE(reviewed, FALSE)) THEN
        RAISE not_reviewed;
    END IF;
    EXECUTE IMMEDIATE 'SHOW DATABASES LIKE ''' || :tgt || '%''';
    SELECT COUNT_IF(t."name" = :tgt), COUNT_IF(t."name" = :parked)
      INTO :n_tgt, :n_parked
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    IF (n_tgt = 0) THEN
        RETURN 'PART 2: ' || tgt || ' does not exist: nothing to park (first refresh). Continue with PART 3.';
    END IF;
    IF (n_parked > 0) THEN
        RAISE parked_exists;
    END IF;

    -- one statement per object; a failure is reported, the rest continue
    EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :tgt;
    SELECT COALESCE(ARRAY_AGG('ALTER TASK "' || REPLACE(t."database_name", '"', '""') || '"."'
               || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '" SUSPEND'),
               ARRAY_CONSTRUCT())
      INTO :obj_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE LOWER(t."state") = 'started';
    EXECUTE IMMEDIATE 'SHOW ALERTS IN DATABASE ' || :tgt;
    SELECT COALESCE(ARRAY_AGG('ALTER ALERT "' || REPLACE(t."database_name", '"', '""') || '"."'
               || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '" SUSPEND'),
               ARRAY_CONSTRUCT())
      INTO :part_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE LOWER(t."state") = 'started';
    obj_list := ARRAY_CAT(obj_list, part_list);
    EXECUTE IMMEDIATE 'SHOW DYNAMIC TABLES IN DATABASE ' || :tgt;
    SELECT COALESCE(ARRAY_AGG('ALTER DYNAMIC TABLE "' || REPLACE(t."database_name", '"', '""')
               || '"."' || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""')
               || '" SUSPEND'), ARRAY_CONSTRUCT())
      INTO :part_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    obj_list := ARRAY_CAT(obj_list, part_list);
    EXECUTE IMMEDIATE 'SHOW PIPES IN DATABASE ' || :tgt;
    SELECT COALESCE(ARRAY_AGG('ALTER PIPE "' || REPLACE(t."database_name", '"', '""') || '"."'
               || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""')
               || '" SET PIPE_EXECUTION_PAUSED = TRUE'), ARRAY_CONSTRUCT())
      INTO :part_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    obj_list := ARRAY_CAT(obj_list, part_list);

    n := ARRAY_SIZE(obj_list);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            stmt := GET(obj_list, i - 1)::VARCHAR;
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                report := report || '-- ok     ' || stmt || CHR(10);
            EXCEPTION
                WHEN OTHER THEN
                    report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;

    EXECUTE IMMEDIATE 'ALTER DATABASE ' || :tgt || ' RENAME TO ' || :parked;
    RETURN 'PART 2 OK: ' || tgt || ' parked as ' || parked || ' (' || n || ' schedule(s) paused first)' || CHR(10)
        || report;
END;
$$;


-- =====================================================================================================================
-- PART 3 -- the clone (EDW_PRD_DEPLOYER; reserve the Jenkins lock ALFA_EDW_PRD for this part)
-- =====================================================================================================================
-- CREATE OR REPLACE, not CREATE: it also re-runs cleanly if PART 4 failed after a first attempt (the half-handled
-- clone is still owned by EDW_PRD_DEPLOYER). It can never replace a runway SYSADMIN owns -- this role lacks the
-- privilege -- and the block re-checks the name. Cloning is metadata-only (no warehouse time); it needs the CREATE
-- DATABASE privilege bootstrap/10 PART 2c gives the PRD deploy role.
USE ROLE EDW_PRD_DEPLOYER;
USE WAREHOUSE WH_EDW_DEPLOY_PRD;

EXECUTE IMMEDIATE $$
DECLARE
    reviewed BOOLEAN;
    mgm_ok BOOLEAN;
    stages BOOLEAN;
    rw VARCHAR;
    tgt VARCHAR;
    stmt VARCHAR;
    taken_at VARCHAR;
    not_reviewed EXCEPTION (-20321, 'PART 3: read the PART 1 reports, then SET prechecks_reviewed = TRUE.');
    bad_target EXCEPTION (-20322, 'PART 3: target is not SAN, SEA, PHX or (with allow_mgm) MGM. Nothing was cloned.');
BEGIN
    SELECT $prechecks_reviewed, $allow_mgm, $include_internal_stages, $target_runway, $target_db
      INTO :reviewed, :mgm_ok, :stages, :rw, :tgt;
    IF (NOT COALESCE(reviewed, FALSE)) THEN
        RAISE not_reviewed;
    END IF;
    IF (NOT (rw IN ('SAN', 'SEA', 'PHX') OR (rw = 'MGM' AND COALESCE(mgm_ok, FALSE)))
        OR tgt IS DISTINCT FROM ('ALFA_EDW_' || rw)) THEN
        RAISE bad_target;
    END IF;
    stmt := 'CREATE OR REPLACE DATABASE ' || tgt || ' CLONE ALFA_EDW_PRD IGNORE HYBRID TABLES'
        || IFF(COALESCE(stages, FALSE), ' INCLUDE INTERNAL STAGES', '');
    EXECUTE IMMEDIATE :stmt;
    taken_at := TO_VARCHAR(CURRENT_TIMESTAMP(), 'YYYY-MM-DD HH24:MI:SS TZHTZM');
    EXECUTE IMMEDIATE 'COMMENT ON DATABASE ' || :tgt || ' IS ''ALFA EDW runway ' || :rw
        || ': zero-copy clone of ALFA_EDW_PRD taken ' || :taken_at || ' (bootstrap/30)''';
    RETURN 'PART 3 OK: ' || stmt || ' -- taken ' || taken_at || '. Unreserve ALFA_EDW_PRD in Jenkins.';
END;
$$;


-- =====================================================================================================================
-- PART 4 -- hand the clone over (EDW_PRD_DEPLOYER, the owner of everything in it)
-- =====================================================================================================================
-- Every schema and object goes to EDW_<ID>_DEPLOYER with COPY CURRENT GRANTS, schema-wide (GRANT OWNERSHIP ON ALL
-- <type> IN SCHEMA), which keeps task graphs linked -- never task by task. Objects first, then the schema, so this
-- role still owns the schema while it moves the contents. Then the database goes to SYSADMIN (bootstrap/20 model:
-- the deploy role never owns its database). A FAILED line names an object this role does not own (e.g. a policy a
-- governance role owns in PRD): decide its owner by hand.
USE ROLE EDW_PRD_DEPLOYER;
USE WAREHOUSE WH_EDW_DEPLOY_PRD;

EXECUTE IMMEDIATE $$
DECLARE
    tgt VARCHAR;
    trole VARCHAR;
    obj_types ARRAY DEFAULT ARRAY_CONSTRUCT('TAGS', 'MASKING POLICIES', 'ROW ACCESS POLICIES', 'SEQUENCES',
        'FILE FORMATS', 'STAGES', 'TABLES', 'EXTERNAL TABLES', 'EVENT TABLES', 'DYNAMIC TABLES', 'VIEWS',
        'MATERIALIZED VIEWS', 'FUNCTIONS', 'PROCEDURES', 'STREAMS', 'PIPES', 'SECRETS', 'ALERTS', 'TASKS');
    schema_list ARRAY;
    n INTEGER;
    m INTEGER;
    m_plus_schema INTEGER;
    fq VARCHAR;
    stmt VARCHAR;
    ok_cnt INTEGER DEFAULT 0;
    fail_cnt INTEGER DEFAULT 0;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $target_db, $target_role INTO :tgt, :trole;
    EXECUTE IMMEDIATE 'SHOW SCHEMAS IN DATABASE ' || :tgt;
    SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR) WITHIN GROUP (ORDER BY t."name"), ARRAY_CONSTRUCT())
      INTO :schema_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE t."name" <> 'INFORMATION_SCHEMA';
    n := ARRAY_SIZE(schema_list);
    m := ARRAY_SIZE(obj_types);
    m_plus_schema := m + 1;                          -- every object type, then the schema itself
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            fq := tgt || '."' || REPLACE(GET(schema_list, i - 1)::VARCHAR, '"', '""') || '"';
            FOR j IN 1 TO m_plus_schema DO
                IF (j <= m) THEN
                    stmt := 'GRANT OWNERSHIP ON ALL ' || GET(obj_types, j - 1)::VARCHAR || ' IN SCHEMA ' || fq
                        || ' TO ROLE ' || trole || ' COPY CURRENT GRANTS';
                ELSE
                    stmt := 'GRANT OWNERSHIP ON SCHEMA ' || fq || ' TO ROLE ' || trole || ' COPY CURRENT GRANTS';
                END IF;
                BEGIN
                    EXECUTE IMMEDIATE :stmt;
                    ok_cnt := ok_cnt + 1;
                EXCEPTION
                    WHEN OTHER THEN
                        fail_cnt := fail_cnt + 1;
                        report := report || '-- FAILED ' || stmt || ';  -- ' || SQLERRM || CHR(10);
                END;
            END FOR;
        END FOR;
    END IF;
    RETURN 'PART 4: ' || n || ' schema(s) handed to ' || trole || ': ' || ok_cnt || ' statement(s) ok, ' || fail_cnt
        || ' failed' || CHR(10) || report;
END;
$$;

-- The database itself to SYSADMIN (still as EDW_PRD_DEPLOYER, its owner until this statement).
GRANT OWNERSHIP ON DATABASE IDENTIFIER($target_db) TO ROLE SYSADMIN COPY CURRENT GRANTS;


-- =====================================================================================================================
-- PART 5 -- database-level grants (SYSADMIN, the database owner). Grants on a database are never cloned.
-- =====================================================================================================================
USE ROLE SYSADMIN;

-- The kit's own (bootstrap/20 PART 1).
GRANT USAGE, CREATE SCHEMA ON DATABASE IDENTIFIER($target_db) TO ROLE IDENTIFIER($target_role);
GRANT USAGE ON DATABASE IDENTIFIER($target_db) TO ROLE EDW_FLYWAY_AUDITOR;

-- Everything else the outgoing runway had at database level (USAGE for its functional roles, MONITOR, ...), copied
-- from the parked database. Grants to EDW_PRD_* roles are not copied: production roles get no path into a runway.
EXECUTE IMMEDIATE $$
DECLARE
    tgt VARCHAR;
    parked VARCHAR;
    n_parked INTEGER;
    grant_list ARRAY;
    n INTEGER;
    stmt VARCHAR;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $target_db, $parked_db INTO :tgt, :parked;
    EXECUTE IMMEDIATE 'SHOW DATABASES LIKE ''' || :parked || '''';
    SELECT COUNT_IF(t."name" = :parked) INTO :n_parked FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    IF (n_parked = 0) THEN
        RETURN 'PART 5: no parked runway (first refresh): only the kit grants were applied';
    END IF;
    EXECUTE IMMEDIATE 'SHOW GRANTS ON DATABASE ' || :parked;
    SELECT COALESCE(ARRAY_AGG('GRANT ' || t."privilege" || ' ON DATABASE ' || :tgt || ' TO ROLE "'
               || REPLACE(t."grantee_name", '"', '""') || '"'
               || IFF(LOWER(t."grant_option"::VARCHAR) = 'true', ' WITH GRANT OPTION', '')), ARRAY_CONSTRUCT())
      INTO :grant_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE t."granted_to" = 'ROLE'
       AND t."privilege" <> 'OWNERSHIP'
       AND NOT STARTSWITH(t."grantee_name", 'EDW_PRD_');
    n := ARRAY_SIZE(grant_list);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            stmt := GET(grant_list, i - 1)::VARCHAR;
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                report := report || '-- ok     ' || stmt || CHR(10);
            EXCEPTION
                WHEN OTHER THEN
                    report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;
    RETURN 'PART 5: ' || n || ' database grant(s) copied from ' || parked || CHR(10) || report;
END;
$$;


-- =====================================================================================================================
-- PART 6 -- PRD functional-role grants on the clone's objects -> the runway's roles (SECURITYADMIN)
-- =====================================================================================================================
-- Child-object grants come along with a clone, so after PART 3 every EDW_PRD_<x> role holds on ALFA_EDW_<ID> objects
-- what it holds in PRD (e.g. EDW_PRD_SALES_READ: SELECT on <runway>.SALES.ORDERS), plus PRD's schema-level future
-- grants. Those grants are inert while EDW_PRD_* roles lack USAGE on the runway database (PART 5 never gives it),
-- but they are wrong: this part revokes each one and grants the same privilege to EDW_<ID>_<x> when that role
-- exists (the kit's role_prefix convention, flyway.toml). Without a matching role the grant is only revoked; the
-- R__90 grants repeatables re-create the runway's grants at the next deploy anyway (their checksum includes the
-- placeholders, so they re-run on a runway whose history came from PRD). USAGE on functions / procedures is moved
-- schema-wide (ON ALL FUNCTIONS IN SCHEMA), because SHOW GRANTS names them with their signature.
-- SECURITYADMIN because MANAGE GRANTS is what lets one role revoke grants held by roles it does not own. Thousands of
-- grants take minutes: one statement each.
USE ROLE SECURITYADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    tgt VARCHAR;
    prefix VARCHAR;
    prd_roles ARRAY;
    tgt_roles ARRAY;
    grant_rows ARRAY;
    plan ARRAY DEFAULT ARRAY_CONSTRUCT();
    g VARIANT;
    r VARCHAR;
    mapped VARCHAR;
    has_mapped BOOLEAN;
    priv VARCHAR;
    obj_on VARCHAR;
    nm VARCHAR;
    gopt VARCHAR;
    sch VARCHAR;
    scope VARCHAR;
    plural VARCHAR;
    revoke_stmt VARCHAR;
    grant_stmt VARCHAR;
    stmt VARCHAR;
    n INTEGER;
    m INTEGER;
    ok_cnt INTEGER DEFAULT 0;
    fail_cnt INTEGER DEFAULT 0;
    unmapped INTEGER DEFAULT 0;
    notes VARCHAR DEFAULT '';
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $target_runway, $target_db INTO :rw, :tgt;
    prefix := tgt || '.';

    SHOW ROLES LIKE 'EDW_PRD_%';
    SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR) WITHIN GROUP (ORDER BY t."name"), ARRAY_CONSTRUCT())
      INTO :prd_roles
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE STARTSWITH(t."name", 'EDW_PRD_');
    EXECUTE IMMEDIATE 'SHOW ROLES LIKE ''EDW_' || :rw || '_%''';
    SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR), ARRAY_CONSTRUCT())
      INTO :tgt_roles
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE STARTSWITH(t."name", 'EDW_' || :rw || '_');

    n := ARRAY_SIZE(prd_roles);
    IF (n > 0) THEN
        FOR i IN 1 TO n DO
            r := GET(prd_roles, i - 1)::VARCHAR;
            mapped := 'EDW_' || rw || SUBSTR(r, 8);                  -- EDW_PRD_SALES_READ -> EDW_<ID>_SALES_READ
            has_mapped := ARRAY_CONTAINS(mapped::VARIANT, tgt_roles);

            -- grants on objects inside the runway (OWNERSHIP was moved in PART 4)
            BEGIN
                EXECUTE IMMEDIATE 'SHOW GRANTS TO ROLE "' || :r || '"';
                SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('p', t."privilege"::VARCHAR, 'on', t."granted_on"::VARCHAR,
                           'n', t."name"::VARCHAR, 'g', LOWER(t."grant_option"::VARCHAR))), ARRAY_CONSTRUCT())
                  INTO :grant_rows
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                 WHERE STARTSWITH(t."name", :prefix) AND t."privilege" <> 'OWNERSHIP';
                m := ARRAY_SIZE(grant_rows);
                IF (m > 0) THEN
                    FOR j IN 1 TO m DO
                        g := GET(grant_rows, j - 1);
                        priv := GET(g, 'p')::VARCHAR;
                        obj_on := GET(g, 'on')::VARCHAR;
                        nm := GET(g, 'n')::VARCHAR;
                        gopt := GET(g, 'g')::VARCHAR;
                        IF (obj_on = 'DATABASE_ROLE') THEN
                            revoke_stmt := 'REVOKE DATABASE ROLE ' || nm || ' FROM ROLE "' || r || '"';
                            grant_stmt := 'GRANT DATABASE ROLE ' || nm || ' TO ROLE "' || mapped || '"';
                        ELSEIF (obj_on IN ('FUNCTION', 'PROCEDURE')) THEN
                            sch := SPLIT_PART(nm, '.', 1) || '.' || SPLIT_PART(nm, '.', 2);
                            revoke_stmt := 'REVOKE ' || priv || ' ON ALL ' || obj_on || 'S IN SCHEMA ' || sch
                                || ' FROM ROLE "' || r || '"';
                            grant_stmt := 'GRANT ' || priv || ' ON ALL ' || obj_on || 'S IN SCHEMA ' || sch
                                || ' TO ROLE "' || mapped || '"';
                        ELSE
                            revoke_stmt := 'REVOKE ' || priv || ' ON ' || REPLACE(obj_on, '_', ' ') || ' ' || nm
                                || ' FROM ROLE "' || r || '"';
                            grant_stmt := 'GRANT ' || priv || ' ON ' || REPLACE(obj_on, '_', ' ') || ' ' || nm
                                || ' TO ROLE "' || mapped || '"' || IFF(gopt = 'true', ' WITH GRANT OPTION', '');
                        END IF;
                        IF (NOT ARRAY_CONTAINS(revoke_stmt::VARIANT, plan)) THEN
                            plan := ARRAY_APPEND(plan, revoke_stmt);
                            IF (has_mapped) THEN
                                plan := ARRAY_APPEND(plan, grant_stmt);
                            ELSE
                                unmapped := unmapped + 1;
                            END IF;
                        END IF;
                    END FOR;
                END IF;
            EXCEPTION
                WHEN OTHER THEN
                    notes := notes || '-- WARNING SHOW GRANTS TO ROLE ' || r || ' failed: ' || SQLERRM || CHR(10);
            END;

            -- schema-level future grants that came along with the clone (incl. FUTURE OWNERSHIP of EDW_PRD_DEPLOYER)
            BEGIN
                EXECUTE IMMEDIATE 'SHOW FUTURE GRANTS TO ROLE "' || :r || '"';
                SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('p', t."privilege"::VARCHAR, 'on', t."grant_on"::VARCHAR,
                           'n', t."name"::VARCHAR, 'g', LOWER(t."grant_option"::VARCHAR))), ARRAY_CONSTRUCT())
                  INTO :grant_rows
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                 WHERE STARTSWITH(t."name", :prefix);
                m := ARRAY_SIZE(grant_rows);
                IF (m > 0) THEN
                    FOR j IN 1 TO m DO
                        g := GET(grant_rows, j - 1);
                        priv := GET(g, 'p')::VARCHAR;
                        obj_on := REPLACE(GET(g, 'on')::VARCHAR, '_', ' ');
                        nm := GET(g, 'n')::VARCHAR;                      -- <db>.<schema>.<TABLE>
                        gopt := GET(g, 'g')::VARCHAR;
                        sch := REGEXP_REPLACE(nm, '[.]<[^>]*>$', '');
                        scope := IFF(CONTAINS(sch, '.'), 'SCHEMA ', 'DATABASE ');
                        plural := IFF(ENDSWITH(obj_on, 'Y'), LEFT(obj_on, LENGTH(obj_on) - 1) || 'IES', obj_on || 'S');
                        plan := ARRAY_APPEND(plan, 'REVOKE ' || priv || ' ON FUTURE ' || plural || ' IN ' || scope
                            || sch || ' FROM ROLE "' || r || '"');
                        IF (has_mapped) THEN
                            plan := ARRAY_APPEND(plan, 'GRANT ' || priv || ' ON FUTURE ' || plural || ' IN ' || scope
                                || sch || ' TO ROLE "' || mapped || '"'
                                || IFF(gopt = 'true' AND priv <> 'OWNERSHIP', ' WITH GRANT OPTION', ''));
                        ELSE
                            unmapped := unmapped + 1;
                        END IF;
                    END FOR;
                END IF;
            EXCEPTION
                WHEN OTHER THEN
                    notes := notes || '-- WARNING SHOW FUTURE GRANTS TO ROLE ' || r || ' failed: ' || SQLERRM
                        || CHR(10);
            END;
        END FOR;
    END IF;

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
    RETURN 'PART 6: ' || ARRAY_SIZE(prd_roles) || ' EDW_PRD_* role(s) checked; ' || ok_cnt || ' statement(s) ok, '
        || fail_cnt || ' failed; ' || unmapped || ' grant(s) had no EDW_' || rw || '_* counterpart (revoked only)'
        || CHR(10) || notes || report;
END;
$$;


-- =====================================================================================================================
-- PART 7 -- what arrived suspended, the audit row, verification (SYSADMIN)
-- =====================================================================================================================
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($target_db);

-- 7a  Scheduled objects in the runway. Nothing here resumes on its own, and nothing should be resumed blindly:
--     on SAN / SEA / PHX the deploys keep tasks suspended (tasks_enabled = "false"); on MGM resume roots
--     deliberately once PART 1's CROSS_DB_REFERENCE / PIPE_QUALIFIED_TARGET lines are fixed (PART 8).
SHOW TASKS IN DATABASE;
SET qid_rt = LAST_QUERY_ID();
SHOW ALERTS IN DATABASE;
SET qid_ra = LAST_QUERY_ID();
SHOW DYNAMIC TABLES IN DATABASE;
SET qid_rd = LAST_QUERY_ID();
SHOW PIPES IN DATABASE;
SET qid_rp = LAST_QUERY_ID();

SELECT 'TASK' AS KIND, t."schema_name"::VARCHAR || '.' || t."name"::VARCHAR AS OBJECT_NAME,
       t."state"::VARCHAR AS STATE, t."owner"::VARCHAR AS OWNER_ROLE,
       IFF(REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]', 'root', 'child') AS NOTE
  FROM TABLE(RESULT_SCAN($qid_rt)) AS t
UNION ALL
SELECT 'ALERT', a."schema_name"::VARCHAR || '.' || a."name"::VARCHAR, a."state"::VARCHAR, a."owner"::VARCHAR, ''
  FROM TABLE(RESULT_SCAN($qid_ra)) AS a
UNION ALL
SELECT 'DYNAMIC TABLE', d."schema_name"::VARCHAR || '.' || d."name"::VARCHAR,
       GET(OBJECT_CONSTRUCT(*), 'scheduling_state')::VARCHAR, d."owner"::VARCHAR, 'a resume starts a full refresh'
  FROM TABLE(RESULT_SCAN($qid_rd)) AS d
UNION ALL
SELECT 'PIPE', p."schema_name"::VARCHAR || '.' || p."name"::VARCHAR, 'see SYSTEM$PIPE_STATUS', p."owner"::VARCHAR,
       IFF(CONTAINS(REGEXP_SUBSTR(UPPER(p."definition"::VARCHAR), 'COPY[[:space:]]+INTO[[:space:]]+([^[:space:](]+)',
                                  1, 1, 'e', 1), '.'), 'QUALIFIED COPY TARGET: never resume, recreate', 'bare target')
  FROM TABLE(RESULT_SCAN($qid_rp)) AS p
ORDER BY 1, 2;

-- 7b  The audit row: the runway's DEPLOY_LOG now holds PRD's history; this row marks where the runway began.
INSERT INTO FLYWAY.DEPLOY_LOG (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
SELECT 'REFRESH-' || $target_runway || '-' || TO_CHAR(CURRENT_TIMESTAMP(), 'YYYYMMDDHH24MISS'),
       'REFRESHED',
       $target_runway,
       NULL,
       'bootstrap/30_refresh_runway.sql',
       CURRENT_USER(),
       CURRENT_USER(),
       CURRENT_TIMESTAMP(),
       'zero-copy clone of ALFA_EDW_PRD; earlier rows are PRD''s deploy history; previous runway parked as '
       || $parked_db || ' (if it existed)';

-- 7c  Verification (read-only). Every line starts with PASS / FAIL / CHECK / INFO.
EXECUTE IMMEDIATE $$
DECLARE
    tgt VARCHAR;
    trole VARCHAR;
    v VARCHAR;
    c1 INTEGER;
    c2 INTEGER;
    c3 INTEGER;
    prd_ver VARCHAR;
    tgt_ver VARCHAR;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $target_db, $target_role INTO :tgt, :trole;
    report := 'INFO   ' || tgt || ' refreshed from ALFA_EDW_PRD; runway role ' || trole || CHR(10);

    BEGIN
        EXECUTE IMMEDIATE 'SHOW GRANTS ON DATABASE ' || :tgt;
        SELECT COUNT_IF(t."privilege" = 'OWNERSHIP' AND t."grantee_name" = 'SYSADMIN'),
               COUNT_IF(t."privilege" IN ('USAGE', 'CREATE SCHEMA') AND t."grantee_name" = :trole),
               COUNT_IF(STARTSWITH(t."grantee_name", 'EDW_PRD_'))
          INTO :c1, :c2, :c3
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   database owned by SYSADMIN' || CHR(10)
            || IFF(c2 = 2, 'PASS', 'FAIL') || '   ' || trole || ': USAGE + CREATE SCHEMA on the database' || CHR(10)
            || IFF(c3 = 0, 'PASS', 'FAIL') || '   no EDW_PRD_* role has a grant on the runway database' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW GRANTS ON DATABASE: ' || SQLERRM || CHR(10);
    END;

    BEGIN
        EXECUTE IMMEDIATE 'SHOW SCHEMAS IN DATABASE ' || :tgt;
        SELECT COUNT_IF(t."owner" IS DISTINCT FROM :trole),
               LISTAGG(IFF(t."owner" IS DISTINCT FROM :trole, t."name" || ' (' || COALESCE(t."owner", '?') || ')',
                           NULL), ', ')
          INTO :c1, :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."name" <> 'INFORMATION_SCHEMA';
        report := report || IFF(c1 = 0, 'PASS   every schema owned by ' || trole,
                                'FAIL   ' || c1 || ' schema(s) not owned by ' || trole || ': ' || LEFT(v, 2000))
            || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW SCHEMAS: ' || SQLERRM || CHR(10);
    END;

    BEGIN
        EXECUTE IMMEDIATE 'SHOW TASKS IN DATABASE ' || :tgt;
        SELECT COUNT(*), COUNT_IF(t."owner" IS DISTINCT FROM :trole), COUNT_IF(LOWER(t."state") = 'started')
          INTO :c1, :c2, :c3
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c2 = 0 AND c3 = 0, 'PASS', 'FAIL') || '   tasks: ' || c1 || ' total, ' || c2
            || ' not owned by ' || trole || ', ' || c3 || ' started (want 0 and 0)' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW TASKS: ' || SQLERRM || CHR(10);
    END;

    -- table / view counts per type: the clone against PRD (hybrid and external tables are expected to differ)
    BEGIN
        EXECUTE IMMEDIATE 'SELECT LISTAGG(x.TABLE_TYPE || '' PRD '' || x.P || '' / runway '' || x.R, ''; '')'
            || ' WITHIN GROUP (ORDER BY x.TABLE_TYPE), COUNT_IF(x.P <> x.R)'
            || ' FROM (SELECT COALESCE(p.TABLE_TYPE, r.TABLE_TYPE) AS TABLE_TYPE, COALESCE(p.N, 0) AS P,'
            || ' COALESCE(r.N, 0) AS R'
            || ' FROM (SELECT TABLE_TYPE, COUNT(*) AS N FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.TABLES'
            || ' WHERE TABLE_SCHEMA <> ''INFORMATION_SCHEMA'' GROUP BY 1) AS p'
            || ' FULL OUTER JOIN (SELECT TABLE_TYPE, COUNT(*) AS N FROM ' || :tgt || '.INFORMATION_SCHEMA.TABLES'
            || ' WHERE TABLE_SCHEMA <> ''INFORMATION_SCHEMA'' GROUP BY 1) AS r ON r.TABLE_TYPE = p.TABLE_TYPE) AS x';
        SELECT $1, $2 INTO :v, :c1 FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
        report := report || IFF(c1 = 0, 'PASS', 'CHECK') || '   object counts: ' || COALESCE(v, '(none)') || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   object count comparison: ' || SQLERRM || CHR(10);
    END;

    -- the Flyway history came along: the runway's latest applied version equals PRD's
    BEGIN
        SELECT MAX_BY(h."version", h."installed_rank") INTO :prd_ver
          FROM ALFA_EDW_PRD.FLYWAY."flyway_schema_history" AS h
         WHERE h."success" AND h."version" IS NOT NULL;
        EXECUTE IMMEDIATE 'SELECT MAX_BY(h."version", h."installed_rank") FROM ' || :tgt
            || '.FLYWAY."flyway_schema_history" AS h WHERE h."success" AND h."version" IS NOT NULL';
        SELECT $1 INTO :tgt_ver FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
        report := report || IFF(prd_ver IS NOT DISTINCT FROM tgt_ver, 'PASS', 'FAIL') || '   Flyway history: runway at '
            || COALESCE(tgt_ver, '(none)') || ', PRD at ' || COALESCE(prd_ver, '(none)') || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   Flyway history comparison: ' || SQLERRM || CHR(10);
    END;

    RETURN report;
END;
$$;

SELECT s.INDEX AS LINE_NO, s.VALUE AS CHECK_RESULT
  FROM (SELECT $1 AS REPORT FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))) AS b,
       TABLE(SPLIT_TO_TABLE(b.REPORT, CHR(10))) AS s
 WHERE s.VALUE <> ''
 ORDER BY s.INDEX;


-- =====================================================================================================================
-- PART 8 -- next steps (process; the commands are written out, nothing below runs by itself)
-- =====================================================================================================================
-- 1. Re-deploy the project's own work. The runway's history is now PRD's: everything released is Success; the
--    project's unmerged versioned migrations are Pending (flyway.toml outOfOrder = true lets their older timestamps
--    apply), and every repeatable that uses a placeholder (edw_db, role_prefix, tasks_enabled, ...) is Outdated --
--    its checksum includes the placeholder values -- so it re-runs with the runway's values. That also re-creates
--    the R__90 grants for EDW_<ID>_* roles. Jenkins: EDW/deploy/<ID>/deploy with the project's branch bundle (or
--    the latest main bundle), then unreserve the lock ALFA_EDW_<ID>.
-- 2. The first deploy may report the cloned objects as drift (their LAST_DDL is the clone time, LAST_DDL_BY the admin
--    who cloned; verify on first run): review the list once and acknowledge it (ACK_DRIFT).
-- 3. PART 1 HIGH lines: fix 3-part ALFA_EDW_PRD references in the repository (a migration / repeatable change), not by
--    hand in the runway; until then afterMigrate__80_cross_env_guard fails the runway's deploys on purpose.
-- 4. Pipes with a qualified COPY target: never resume them in the runway (they would load PRD); recreate them with a
--    bare table name. A recreated pipe has no load history: do not ALTER PIPE ... REFRESH against a stage PRD shares.
-- 5. External tables, hybrid tables and pipes on internal stages: absent in the runway although the history says
--    they exist; re-create them by hand from their migration as EDW_<ID>_DEPLOYER (or accept their absence).
-- 6. Resuming (MGM, or a lease that needs schedules): as EDW_<ID>_DEPLOYER, root by root, after items 3-4:
--      SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('<ALFA_EDW_ID>.<SCHEMA>.<ROOT_TASK>');
--      ALTER DYNAMIC TABLE <SCHEMA>.<NAME> RESUME;     -- expect a full refresh on the runway's warehouse
--      ALTER ALERT <SCHEMA>.<NAME> RESUME;
--    Started tasks count against the account-wide limit on resumed tasks, shared by all six runways.
-- 7. Optional, to limit Time Travel storage on a test runway (SYSADMIN):
--      ALTER DATABASE IDENTIFIER($target_db) SET DATA_RETENTION_TIME_IN_DAYS = 1;
-- 8. Drop the parked copy once the runway works and the lease holder confirms nothing in it is needed (SYSADMIN).
--    A PART 1a "BLOCKS PART 8 DROP" line must be resolved first (move those objects to another policy), or the
--    drop fails with a dangling-policy error:
--      USE ROLE SYSADMIN;
--      SHOW DATABASES LIKE '%\\_PRE\\_REFRESH';
--      DROP DATABASE IF EXISTS IDENTIFIER($parked_db);
--    It stays recoverable with UNDROP DATABASE for the parked database's Time Travel retention.
