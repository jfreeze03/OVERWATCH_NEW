-- =====================================================================================================================
-- bootstrap/40_point_in_time_recovery.sql -- put a table or schema of one runway back to before a deploy
-- =====================================================================================================================
-- WHAT    Templates for Time Travel recovery after a deploy damaged data or structure (RUNBOOKS R3, GUIDE F6):
--           PART 0  variables + guard
--           PART 1  session: the runway's deploy role, warehouse, database; Time Travel retention check
--           PART 2  find the point: the deploy's STARTED row in FLYWAY.DEPLOY_LOG (written by
--                   beforeMigrate__10_deploy_log before the first migration ran) and what that deploy applied
--           PART 3  inspect: row counts now vs at the point
--           ---- Run All stops before the templates on purpose: run ONE template, statement by statement ----
--           A  table:    clone it AT the point side by side, compare, SWAP WITH
--           B  schema:   clone it AT the point side by side, compare, SWAP WITH, re-grant the schema's own grants
--           C  dropped or replaced table (CREATE OR REPLACE TABLE, DROP TABLE): UNDROP
--           D  replaced view / function / procedure: no Time Travel for definitions -- previous repeatable version
--              from git (or the previous CREATE statement from QUERY_HISTORY)
--           PART 8  Flyway afterwards: history, repair, fix-forward (comments)
--           PART 9  cleanup of the side-by-side copies (commented out)
-- WHO     A DBA with the runway's deploy role. On MGM / PRD that is break-glass (GUIDE B7): change ticket, Jenkins
--         lock ALFA_EDW_<ID> RESERVED so no deploy runs during the recovery, time-boxed role grant. Why the deploy
--         role: everything it creates here (the side-by-side copy that becomes the live object after a swap) must be
--         owned by EDW_<ID>_DEPLOYER, or the next migration that alters it fails (bootstrap/20 ownership model).
-- LIMITS  * Time Travel reaches back DATA_RETENTION_TIME_IN_DAYS (default 1 day; up to 90 on Enterprise edition
--           and higher). Transient tables keep at most 1 day. PART 1 shows the setting; PART 2 checks the point.
--           Recommended for MGM / PRD (SYSADMIN, a cost decision): at least the release-verification window, e.g.
--             ALTER DATABASE <ALFA_EDW_ID> SET DATA_RETENTION_TIME_IN_DAYS = 7;
--         * A clone AT a timestamp brings tasks / alerts / dynamic tables back SUSPENDED, streams start at the
--           clone point, pipes on internal stages and external tables are not cloned, and a schema clone with
--           hybrid tables needs IGNORE HYBRID TABLES and an explicitly cast AT TIMESTAMP (used below).
--         * Snowflake DDL autocommits: there is no transaction to roll back; these templates are the rollback.
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables + guard
-- =====================================================================================================================
SET runway = 'PRD';                         -- DEV | SAN | SEA | PHX | MGM | PRD
-- The deploy to undo: Jenkins sets DEPLOY_ID = '<ID>-<BUILD_NUMBER>' (build page, or the PART 2 list). The guard
-- refuses the unedited value.
SET deploy_id = 'PRD-<BUILD_NUMBER>';
SET object_schema = 'SALES';                -- schema of the damaged object (templates A, B, C, D)
SET object_name = 'ORDERS';                 -- table (A, C) or view / function / procedure (D)
SET restore_tag = TO_CHAR(CURRENT_DATE(), 'YYYYMMDD');

-- Derived names. Do not edit.
SET runway_db = 'ALFA_EDW_' || $runway;
SET deploy_role = 'EDW_' || $runway || '_DEPLOYER';
SET deploy_wh = 'WH_EDW_DEPLOY_' || $runway;
SET schema_fqn = $runway_db || '.' || $object_schema;
SET table_fqn = $schema_fqn || '.' || $object_name;
SET restore_table_fqn = $table_fqn || '_RESTORE_' || $restore_tag;
SET damaged_table_fqn = $table_fqn || '_DAMAGED_' || $restore_tag;
SET restore_schema_fqn = $schema_fqn || '_RESTORE_' || $restore_tag;

EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    did VARCHAR;
    sch VARCHAR;
    obj VARCHAR;
    bad_runway EXCEPTION (-20401, 'PART 0: runway must be one of DEV, SAN, SEA, PHX, MGM, PRD (upper case).');
    bad_deploy EXCEPTION (-20402, 'PART 0: set deploy_id to the real <ID>-<BUILD_NUMBER> (PART 2 lists them).');
    bad_name EXCEPTION (-20403, 'PART 0: object_schema / object_name must be plain upper-case names.');
    flyway_schema EXCEPTION (-20404, 'PART 0: never restore schema FLYWAY: it holds the history (PART 8).');
BEGIN
    SELECT $runway, $deploy_id, $object_schema, $object_name INTO :rw, :did, :sch, :obj;
    IF (rw IS NULL OR rw NOT IN ('DEV', 'SAN', 'SEA', 'PHX', 'MGM', 'PRD')) THEN
        RAISE bad_runway;
    END IF;
    IF (did IS NULL OR CONTAINS(did, '<') OR did = '') THEN
        RAISE bad_deploy;
    END IF;
    IF (NOT (REGEXP_LIKE(sch, '[A-Z][A-Z0-9_]*') AND REGEXP_LIKE(obj, '[A-Z][A-Z0-9_$]*'))) THEN
        RAISE bad_name;
    END IF;
    IF (sch = 'FLYWAY') THEN
        RAISE flyway_schema;
    END IF;
    RETURN 'PART 0 OK: ALFA_EDW_' || rw || '.' || sch || '.' || obj || ', deploy ' || did;
END;
$$;


-- =====================================================================================================================
-- PART 1 -- session + retention (the runway's deploy role)
-- =====================================================================================================================
USE ROLE IDENTIFIER($deploy_role);
USE WAREHOUSE IDENTIFIER($deploy_wh);
USE DATABASE IDENTIFIER($runway_db);
USE SCHEMA IDENTIFIER($schema_fqn);

SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN DATABASE;
SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN SCHEMA;


-- =====================================================================================================================
-- PART 2 -- find the point in time
-- =====================================================================================================================
-- Recent deploys of this runway (pick DEPLOY_ID if you do not have it):
SELECT l.DEPLOY_ID,
       MIN(IFF(l.EVENT = 'STARTED', l.EVENT_TS, NULL)) AS STARTED_AT,
       MAX(l.EVENT_TS) AS LAST_EVENT_AT,
       LISTAGG(DISTINCT l.EVENT, ', ') AS EVENTS,
       MAX(l.GIT_SHA) AS GIT_SHA,
       MAX(l.APPROVER) AS APPROVER
  FROM FLYWAY.DEPLOY_LOG AS l
 GROUP BY l.DEPLOY_ID
 ORDER BY LAST_EVENT_AT DESC
 LIMIT 20;

-- The chosen deploy, event by event (DETAIL of SUCCEEDED / FAILED lists the versions it applied or failed):
SELECT l.EVENT, l.EVENT_TS, l.ENV, l.GIT_SHA, l.BUILD_URL, l.APPROVER, l.FLYWAY_USER, l.DETAIL
  FROM FLYWAY.DEPLOY_LOG AS l
 WHERE l.DEPLOY_ID = $deploy_id
 ORDER BY l.EVENT_TS;

-- The point: one second before the latest STARTED row of that deploy (beforeMigrate__10 writes it before the first
-- migration runs, so nothing of this deploy is visible at that time).
SET recover_ts = (SELECT DATEADD('second', -1, MAX(l.EVENT_TS))
                    FROM FLYWAY.DEPLOY_LOG AS l
                   WHERE l.DEPLOY_ID = $deploy_id AND l.EVENT = 'STARTED');
-- Alternative: the UTC timestamp the deploy job printed in its "Record Time Travel point" stage, e.g.
--   SET recover_ts = '2026-10-13 14:05:00 +0000'::TIMESTAMP_TZ::TIMESTAMP_LTZ;

-- Is the point usable for the object? (Time Travel retention of the table; it may be gone: template C.)
EXECUTE IMMEDIATE $$
DECLARE
    ts TIMESTAMP_LTZ;
    tbl VARCHAR;
    days INTEGER;
    age_h INTEGER;
    no_point EXCEPTION (-20411, 'PART 2: no STARTED row for deploy_id in FLYWAY.DEPLOY_LOG; pick one from the list.');
BEGIN
    SELECT $recover_ts, $table_fqn INTO :ts, :tbl;
    IF (ts IS NULL) THEN
        RAISE no_point;
    END IF;
    BEGIN
        EXECUTE IMMEDIATE 'SHOW PARAMETERS LIKE ''DATA_RETENTION_TIME_IN_DAYS'' IN TABLE ' || :tbl;
        SELECT MAX(TRY_TO_NUMBER(t."value"::VARCHAR)) INTO :days FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    EXCEPTION
        WHEN OTHER THEN
            days := NULL;
    END;
    age_h := DATEDIFF('hour', ts, CURRENT_TIMESTAMP());
    RETURN 'recover to ' || TO_VARCHAR(ts) || ' (' || age_h || ' h ago); ' || tbl || ' retention: '
        || COALESCE(TO_VARCHAR(days) || ' day(s) -> '
                    || IFF(age_h < days * 24, 'inside Time Travel', 'OUTSIDE Time Travel: Fail-safe is Snowflake '
                           || 'Support only'), 'unknown (table dropped or replaced? template C)');
END;
$$;

-- What that deploy applied (Flyway history rows at or after the point; "success" = FALSE marks a failed one):
SELECT h."installed_rank", h."version", h."description", h."type", h."script", h."installed_on",
       h."execution_time", h."success"
  FROM FLYWAY."flyway_schema_history" AS h
 WHERE h."installed_on" >= $recover_ts
 ORDER BY h."installed_rank";


-- =====================================================================================================================
-- PART 3 -- inspect (read-only)
-- =====================================================================================================================
SELECT 'now' AS TABLE_VERSION, COUNT(*) AS ROW_COUNT
  FROM IDENTIFIER($table_fqn)
UNION ALL
SELECT 'at ' || TO_VARCHAR($recover_ts), COUNT(*)
  FROM IDENTIFIER($table_fqn) AT(TIMESTAMP => $recover_ts::TIMESTAMP_LTZ);

-- Run All stops here: the templates below change the runway. Run the one you need, one statement at a time.
EXECUTE IMMEDIATE $$
DECLARE
    stop_here EXCEPTION (-20499, 'Run All stops here on purpose: run template A, B, C or D statement by statement.');
BEGIN
    RAISE stop_here;
END;
$$;


-- =====================================================================================================================
-- TEMPLATE A -- one table: clone it as it was, compare, swap
-- =====================================================================================================================
-- A1  The copy, side by side (zero-copy; COPY GRANTS keeps the table's explicit grants on the copy, so they are
--     still there after the swap). Fails if the table did not exist at the point -- created by this deploy -- or was
--     replaced since (CREATE OR REPLACE TABLE): use template C then.
CREATE TABLE IDENTIFIER($restore_table_fqn)
    CLONE IDENTIFIER($table_fqn) AT(TIMESTAMP => $recover_ts::TIMESTAMP_LTZ)
    COPY GRANTS;

-- A2  Compare before you swap. The difference queries need the same columns on both sides: if the deploy added or
--     dropped columns, name the common columns instead of *.
SELECT (SELECT COUNT(*) FROM IDENTIFIER($restore_table_fqn)) AS ROWS_IN_COPY,
       (SELECT COUNT(*) FROM IDENTIFIER($table_fqn)) AS ROWS_NOW;
SELECT * FROM (SELECT * FROM IDENTIFIER($restore_table_fqn) MINUS SELECT * FROM IDENTIFIER($table_fqn)) LIMIT 100;
SELECT * FROM (SELECT * FROM IDENTIFIER($table_fqn) MINUS SELECT * FROM IDENTIFIER($restore_table_fqn)) LIMIT 100;

-- A3  The swap: one atomic rename of both tables. Afterwards the live name holds the recovered data and
--     <table>_RESTORE_<date> holds the damaged version (evidence; PART 9). Views and procedures that name the table
--     use the recovered one at once. Check afterwards (verify on first run): SHOW GRANTS ON TABLE for both names;
--     streams on the table (recreate them on the live table if they now follow the evidence copy); dynamic tables
--     or materialized views over it (a refresh / recompute follows).
ALTER TABLE IDENTIFIER($table_fqn) SWAP WITH IDENTIFIER($restore_table_fqn);


-- =====================================================================================================================
-- TEMPLATE B -- a whole schema (never FLYWAY): clone it as it was, compare, swap, re-grant
-- =====================================================================================================================
-- B1  The copy (needs CREATE SCHEMA on the database, which the deploy role has). Child objects keep their grants;
--     the schema's own grants are not cloned (B4). Tasks / alerts / dynamic tables arrive SUSPENDED.
CREATE SCHEMA IDENTIFIER($restore_schema_fqn)
    CLONE IDENTIFIER($schema_fqn) AT(TIMESTAMP => $recover_ts::TIMESTAMP_LTZ)
    IGNORE HYBRID TABLES;

-- B2  Compare object lists and row counts (INFORMATION_SCHEMA of this database, real time):
SELECT COALESCE(a.TABLE_NAME, b.TABLE_NAME) AS TABLE_NAME, a.ROW_COUNT AS ROWS_IN_COPY, b.ROW_COUNT AS ROWS_NOW,
       a.TABLE_TYPE AS TYPE_IN_COPY, b.TABLE_TYPE AS TYPE_NOW
  FROM (SELECT t.TABLE_NAME, t.ROW_COUNT, t.TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES AS t
         WHERE t.TABLE_SCHEMA = $object_schema || '_RESTORE_' || $restore_tag) AS a
  FULL OUTER JOIN (SELECT t.TABLE_NAME, t.ROW_COUNT, t.TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES AS t
                    WHERE t.TABLE_SCHEMA = $object_schema) AS b
    ON a.TABLE_NAME = b.TABLE_NAME
 ORDER BY 1;

-- B3  The swap (atomic). The live name now holds the recovered schema; <schema>_RESTORE_<date> the damaged one.
ALTER SCHEMA IDENTIFIER($schema_fqn) SWAP WITH IDENTIFIER($restore_schema_fqn);

-- B4  The schema-level grants (USAGE, CREATE ... for functional roles) stayed on the damaged copy: re-apply them to
--     the live schema. Schema-level FUTURE grants: compare SHOW FUTURE GRANTS IN SCHEMA on both names (verify on first
--     run; the R__90 grants repeatable restores them at the next deploy anyway).
EXECUTE IMMEDIATE $$
DECLARE
    live VARCHAR;
    damaged VARCHAR;
    grant_list ARRAY;
    n INTEGER;
    stmt VARCHAR;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $schema_fqn, $restore_schema_fqn INTO :live, :damaged;
    EXECUTE IMMEDIATE 'SHOW GRANTS ON SCHEMA ' || :damaged;
    SELECT COALESCE(ARRAY_AGG('GRANT ' || t."privilege" || ' ON SCHEMA ' || :live || ' TO ROLE "'
               || REPLACE(t."grantee_name", '"', '""') || '"'
               || IFF(LOWER(t."grant_option"::VARCHAR) = 'true', ' WITH GRANT OPTION', '')), ARRAY_CONSTRUCT())
      INTO :grant_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE t."granted_to" = 'ROLE' AND t."privilege" <> 'OWNERSHIP';
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
    RETURN 'B4: ' || n || ' schema grant(s) re-applied to ' || live || CHR(10) || report;
END;
$$;

-- B5  Tasks in the recovered schema are suspended. On MGM / PRD resume the roots that should run (as the deploy
--     role; children follow):
SHOW TASKS IN SCHEMA;
SELECT t."name", t."state", t."predecessors", t."schedule"
  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
 ORDER BY 1;
--   SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('<ALFA_EDW_ID>.<SCHEMA>.<ROOT_TASK>');


-- =====================================================================================================================
-- TEMPLATE C -- a dropped or replaced table: UNDROP
-- =====================================================================================================================
-- CREATE OR REPLACE TABLE and DROP TABLE leave the previous table in Time Travel as a DROPPED version (guard FG006
-- blocks both in migrations without a waiver; this is for when one got through). UNDROP restores the most recently
-- dropped version under its name, and fails while a table of that name exists: move the current one aside first.
-- C1  Versions of the name (dropped_on is set for dropped / replaced ones):
SHOW TABLES HISTORY IN SCHEMA;
SELECT t."name", t."created_on", t."dropped_on", t."rows", t."owner"
  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
 WHERE t."name" = $object_name
 ORDER BY t."dropped_on" DESC NULLS FIRST;

-- C2  Move the current table (if any) aside, then UNDROP. Repeat the pair to reach an older dropped version.
ALTER TABLE IDENTIFIER($table_fqn) RENAME TO IDENTIFIER($damaged_table_fqn);
UNDROP TABLE IDENTIFIER($table_fqn);
-- Streams on the replaced table went stale with the replace: recreate them (CREATE OR REPLACE STREAM ... COPY
-- GRANTS) after the UNDROP. A dropped schema is the same pattern: UNDROP SCHEMA <ALFA_EDW_ID>.<SCHEMA>;


-- =====================================================================================================================
-- TEMPLATE D -- a replaced view / function / procedure: no Time Travel for definitions
-- =====================================================================================================================
-- Views, functions and procedures are repeatables (R__ files). The REAL fix is in git: revert the change in a pull
-- request and deploy it through Jenkins -- the reverted file has a new checksum, so Flyway re-runs it on every
-- runway it reaches (the repeatable then matches the repository again).
--   git log --oneline -- repeatable/<SCHEMA>/R__30_<SCHEMA>__<object>.sql
--   git show <good_commit>:repeatable/<SCHEMA>/R__30_<SCHEMA>__<object>.sql
-- Emergency bridge until that deploy: run the good version by hand as the deploy role, after replacing each
-- ${name} placeholder with this runway's value from flyway.toml [environments.<ID>.flyway.placeholders] (that
-- environment's table replaces the root [flyway.placeholders] one). The next deploy of an UNCHANGED repository does
-- not touch it (same checksum as the bad version already recorded), and the drift check reports it: the git revert
-- is still required.
-- D1  Keep the current (bad) definition as evidence:
SELECT GET_DDL('VIEW', $object_schema || '.' || $object_name) AS CURRENT_DDL;
--     For a function or procedure GET_DDL needs the argument types: GET_DDL('PROCEDURE', 'SALES.SP_X(VARCHAR)').
-- D2  The previous CREATE statement exactly as Flyway ran it (placeholders already replaced), from QUERY_HISTORY.
--     ACCOUNT_USAGE is readable by ACCOUNTADMIN (or a role granted SNOWFLAKE's IMPORTED PRIVILEGES / GOVERNANCE_VIEWER)
--     and lags up to ~45 minutes; it keeps a year.
USE ROLE ACCOUNTADMIN;
SELECT q.START_TIME, q.USER_NAME, q.ROLE_NAME, q.QUERY_ID, q.QUERY_TEXT
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY AS q
 WHERE q.START_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
   AND q.START_TIME < $recover_ts
   AND q.DATABASE_NAME = $runway_db
   AND q.EXECUTION_STATUS = 'SUCCESS'
   AND UPPER(q.QUERY_TEXT) LIKE 'CREATE OR %'
   AND CONTAINS(UPPER(q.QUERY_TEXT), UPPER($object_schema || '.' || $object_name))
 ORDER BY q.START_TIME DESC
 LIMIT 5;
USE ROLE IDENTIFIER($deploy_role);
-- D3  Run the chosen statement as the deploy role (copy it from D2 or from the substituted git file). Keep COPY
--     GRANTS in it (guard FG011): without it the replaced object loses its explicit grants.


-- =====================================================================================================================
-- PART 8 -- Flyway afterwards (workstation; GUIDE B7 for a manual Flyway session, RUNBOOKS R1 / R3)
-- =====================================================================================================================
--   eval "$(python3 scripts/fw.py env --runway <ID> --format shell)"; unset FLYWAY_TARGET   # + B7 exports
--   flyway -environment=<ID> info
-- * Data restored (A, B, C on data only): the history is still true; nothing to do in Flyway. Record the restore in
--   the change ticket. The next deploy's drift check lists the swapped / undropped objects (DDL by you): review and
--   acknowledge (ACK_DRIFT).
-- * Structure restored to before a migration: the history says that version is applied, the database says it is
--   not. Never edit FLYWAY."flyway_schema_history" by hand. Fix forward: a NEW versioned migration that brings the
--   object to the intended state idempotently (ADD COLUMN IF NOT EXISTS, CREATE OR ALTER ...), or that records the
--   decision to abandon the change. `flyway undo` is no help here: it runs the U script against the database, which
--   you have already put back.
-- * A failed deploy (a history row with "success" = FALSE):
--     SELECT h."installed_rank", h."version", h."description", h."script", h."installed_on"
--       FROM FLYWAY."flyway_schema_history" AS h WHERE NOT h."success";
--   clean up what the failed script left behind (Snowflake DDL autocommits, so it is partly applied), then
--     flyway -environment=<ID> repair
--   which removes failed rows, realigns checksums / descriptions / types with the files, and marks applied-but-
--   missing migrations as deleted. Run it from the SAME bundle that was deployed: on a test runway that carries
--   another project's migrations, a repair from a bundle without them marks them deleted (prefer a refresh,
--   bootstrap/30). Then fix forward with a new migration and deploy it through Jenkins.
-- * Task graphs the failed deploy left suspended: FLYWAY.DEPLOY_TASK_STATE holds the roots that were started before
--   it (afterMigrateError__99 lists them in the FAILED row); resume them once the fix is in (RUNBOOKS R9).
-- * Unreserve the Jenkins lock ALFA_EDW_<ID>; revoke any break-glass grant.


-- =====================================================================================================================
-- PART 9 -- cleanup of the side-by-side copies, after sign-off (commented out on purpose; deploy role)
-- =====================================================================================================================
-- After template A the _RESTORE_ table holds the DAMAGED version; after B the _RESTORE_ schema does; after C the
-- _DAMAGED_ table holds the version you moved aside. Each stays recoverable with UNDROP for its retention period.
--   DROP TABLE IF EXISTS IDENTIFIER($restore_table_fqn);
--   DROP SCHEMA IF EXISTS IDENTIFIER($restore_schema_fqn);
--   DROP TABLE IF EXISTS IDENTIFIER($damaged_table_fqn);
