-- =====================================================================================================================
-- bootstrap/40_point_in_time_recovery.sql -- put a table or schema of one runway back to before a deploy
-- =====================================================================================================================
-- WHAT    Templates for Time Travel recovery after a deploy damaged data or structure (RUNBOOKS R3, GUIDE F6):
--           PART 0  variables + guard (runway, deploy_id and confirm_template have no usable default on purpose)
--           PART 1  session: the runway's deploy role, warehouse, database; Time Travel retention check
--           PART 2  find the point: the deploy's STARTED row in FLYWAY.DEPLOY_LOG (written by
--                   beforeMigrate__10_deploy_log before the first migration ran) and what that deploy applied
--           PART 3  inspect: row counts now vs at the point
--           ---- Run All stops before the templates on purpose: run ONE template, statement by statement ----
--           Q0 stop the schema's writers (templates A and B): its started root tasks and alerts, running dynamic
--              tables and loading pipes, recorded in FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID RESTORE-<deploy_id>)
--           A  table:    clone it AT the point side by side, compare, SWAP WITH
--           B  schema:   clone it AT the point side by side, compare (incl. what a schema clone cannot carry),
--                        SWAP WITH, re-grant the schema's own grants
--           Q9 restart exactly what Q0 stopped (after A3 or B4)
--           C  dropped or replaced table (CREATE OR REPLACE TABLE, DROP TABLE): UNDROP
--           D  replaced view / function / procedure: no Time Travel for definitions -- previous repeatable version
--              from git (or the previous CREATE statement from QUERY_HISTORY)
--           PART 8  Flyway afterwards: history, repair, fix-forward (comments)
--           PART 9  cleanup of the side-by-side copies (commented out)
-- SAFETY  Every destructive statement (A1, A3, B1, B3, C2, PART 9) is COMMENTED OUT: remove the leading "-- " of
--         ONE statement, run it, check, move on. The blocks that stop / restart schedules (Q0, Q9) and re-grant (B4)
--         run only with SET confirm_template = '<A|B>:<deploy_id>' (PART 0). So a top-to-bottom run -- Snowsight
--         "Run All", or a client that continues after errors (SnowSQL -f defaults to exit_on_error=false) -- changes
--         nothing: PART 0 refuses the unedited runway, and every later part needs a deliberate step.
-- WHO     A DBA with the runway's deploy role. On MGM / PRD that is break-glass (GUIDE B7): change ticket, Jenkins
--         lock ALFA_EDW_<ID> RESERVED so no deploy runs during the recovery, time-boxed role grant. Why the deploy
--         role: everything it creates here (the side-by-side copy that becomes the live object after a swap) must be
--         owned by EDW_<ID>_DEPLOYER, or the next migration that alters it fails (bootstrap/20 ownership model). The
--         Jenkins lock stops deploys only: the runway's own schedules keep writing until Q0 stops them.
-- LIMITS  * Time Travel reaches back DATA_RETENTION_TIME_IN_DAYS (default 1 day; up to 90 on Enterprise edition
--           and higher). Transient tables keep at most 1 day. PART 1 shows the setting; PART 2 checks the point.
--           Recommended for MGM / PRD (SYSADMIN, a cost decision): at least the release-verification window, e.g.
--             ALTER DATABASE <ALFA_EDW_ID> SET DATA_RETENTION_TIME_IN_DAYS = 7;
--         * A swap DISCARDS EVERY WRITE MADE AFTER THE POINT, legitimate ETL rows included: they leave with the
--           evidence copy (_RESTORE_). The data owner decides which rows from that copy are re-inserted (A2's MINUS,
--           on the business key) or whether the ETL re-runs for that window. Q0 stops new writes from then on.
--         * A clone AT a timestamp brings tasks / alerts / dynamic tables back SUSPENDED, streams start at the
--           clone point, named internal stages (with their files), pipes on them, external tables and hybrid tables
--           are not carried by a schema clone (hybrid tables need IGNORE HYBRID TABLES and an explicitly cast AT
--           TIMESTAMP, used below). Template B therefore checks for them (B2b) before the swap.
--         * Snowflake DDL autocommits: there is no transaction to roll back; these templates are the rollback.
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables + guard
-- =====================================================================================================================
SET runway = '<ID>';                        -- DEV | SAN | SEA | PHX | MGM | PRD  (the guard refuses '<ID>')
-- The deploy to undo: Jenkins sets DEPLOY_ID = '<ID>-<BUILD_NUMBER>' (build page, or the PART 2 list); "manual" for a
-- hand run. The guard refuses the unedited value and a deploy id of another runway.
SET deploy_id = '<ID>-<BUILD_NUMBER>';
SET object_schema = 'SALES';                -- schema of the damaged object (templates A, B, C, D)
SET object_name = 'ORDERS';                 -- table (A, C) or view / function / procedure (D)
SET restore_tag = TO_CHAR(CURRENT_DATE(), 'YYYYMMDD');
-- The template you are running, as '<A|B>:' followed by deploy_id, e.g. 'A:PRD-1234'. Q0, Q9 and B4 refuse to run
-- otherwise. Set it only when you start a template.
SET confirm_template = 'none';

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
    other_runway EXCEPTION (-20405, 'PART 0: deploy_id belongs to another runway: it must start with <runway>- .');
BEGIN
    SELECT $runway, $deploy_id, $object_schema, $object_name INTO :rw, :did, :sch, :obj;
    IF (rw IS NULL OR rw NOT IN ('DEV', 'SAN', 'SEA', 'PHX', 'MGM', 'PRD')) THEN
        RAISE bad_runway;
    END IF;
    IF (did IS NULL OR CONTAINS(did, '<') OR did = '') THEN
        RAISE bad_deploy;
    END IF;
    IF (NOT (STARTSWITH(did, rw || '-') OR did = 'manual')) THEN
        RAISE other_runway;
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
-- Q0 -- stop the schema's writers (templates A and B; deploy role). First: SET confirm_template = 'A:<deploy_id>'
--       (or 'B:<deploy_id>').
-- =====================================================================================================================
-- Rows written between the point and the swap leave with the evidence copy, and rows written after A2 / B2 are not
-- even in the comparison. So before cloning, everything in the object's schema that runs on a schedule stops:
-- started ROOT tasks (their graphs stop with them; finalizers follow their root), started alerts, running dynamic
-- tables, and every pipe (paused). Each one is first recorded in FLYWAY.DEPLOY_TASK_STATE under DEPLOY_ID
-- RESTORE-<deploy_id> (tasks with STATE_BEFORE 'started', the others as '<kind>:<state>'), so Q9 restarts exactly
-- these and nothing an operator had stopped. The report also lists tasks and pipes in OTHER schemas whose definition
-- names the table (A) or the schema (B): CHECK lines -- suspend those by hand if they write it (a task that only
-- CALLs a procedure is not found this way: check the ETL inventory). Re-running Q0 is harmless.
EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    did VARCHAR;
    sch VARCHAR;
    obj VARCHAR;
    confirm VARCHAR;
    db VARCHAR;
    sfq VARCHAR;
    restore_id VARCHAR;
    needle VARCHAR;
    roots ARRAY;
    alerts ARRAY;
    dts ARRAY;
    pipes ARRAY;
    fqn VARCHAR;
    v VARCHAR;
    stmt VARCHAR;
    report VARCHAR DEFAULT '';
    not_confirmed EXCEPTION (-20421,
        'Q0: SET confirm_template = ''A:<deploy_id>'' or ''B:<deploy_id>'' (PART 0), in the runway database.');
BEGIN
    SELECT $runway, $deploy_id, $object_schema, $object_name, $confirm_template, $runway_db, $schema_fqn
      INTO :rw, :did, :sch, :obj, :confirm, :db, :sfq;
    IF ((confirm IS DISTINCT FROM 'A:' || did AND confirm IS DISTINCT FROM 'B:' || did)
        OR db IS DISTINCT FROM 'ALFA_EDW_' || rw OR CURRENT_DATABASE() IS DISTINCT FROM db) THEN
        RAISE not_confirmed;
    END IF;
    restore_id := 'RESTORE-' || did;
    needle := UPPER(sch || '.' || IFF(LEFT(confirm, 1) = 'A', obj, ''));

    EXECUTE IMMEDIATE 'SHOW TASKS IN SCHEMA ' || :sfq;
    SELECT COALESCE(ARRAY_AGG(x.FQN), ARRAY_CONSTRUCT()) INTO :roots
      FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                   || '"."' || REPLACE(t."name", '"', '""') || '"' AS FQN,
                   LOWER(t."state") AS STATE,
                   REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]' AS IS_ROOT,
                   NULLIF(GET(TRY_PARSE_JSON(GET(OBJECT_CONSTRUCT(*), 'task_relations')::VARCHAR),
                              'FinalizedRootTask')::VARCHAR, '') AS FIN_OF
              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS x
     WHERE x.STATE = 'started' AND x.IS_ROOT AND x.FIN_OF IS NULL;
    EXECUTE IMMEDIATE 'SHOW ALERTS IN SCHEMA ' || :sfq;
    SELECT COALESCE(ARRAY_AGG('"' || REPLACE(t."database_name", '"', '""') || '"."'
               || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '"'),
               ARRAY_CONSTRUCT())
      INTO :alerts
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE LOWER(t."state") = 'started';
    EXECUTE IMMEDIATE 'SHOW DYNAMIC TABLES IN SCHEMA ' || :sfq;
    SELECT COALESCE(ARRAY_AGG('"' || REPLACE(x.DB, '"', '""') || '"."' || REPLACE(x.SCH, '"', '""') || '"."'
               || REPLACE(x.NAME, '"', '""') || '"'), ARRAY_CONSTRUCT())
      INTO :dts
      FROM (SELECT t."database_name"::VARCHAR AS DB, t."schema_name"::VARCHAR AS SCH, t."name"::VARCHAR AS NAME,
                   UPPER(COALESCE(GET(OBJECT_CONSTRUCT(*), 'scheduling_state')::VARCHAR, 'RUNNING')) AS SCHED
              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS x
     WHERE NOT CONTAINS(x.SCHED, 'SUSPENDED');
    EXECUTE IMMEDIATE 'SHOW PIPES IN SCHEMA ' || :sfq;
    SELECT COALESCE(ARRAY_AGG('"' || REPLACE(t."database_name", '"', '""') || '"."'
               || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '"'),
               ARRAY_CONSTRUCT())
      INTO :pipes
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;

    -- record first, then stop: an INSERT that fails stops the block before that object is touched
    IF (ARRAY_SIZE(roots) > 0) THEN
        FOR i IN 1 TO ARRAY_SIZE(roots) DO
            fqn := GET(roots, i - 1)::VARCHAR;
            INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS, ROOT_FQN)
            VALUES (:restore_id, :fqn, 'started', CURRENT_TIMESTAMP(), :fqn);
            stmt := 'ALTER TASK ' || fqn || ' SUSPEND';
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                report := report || '-- ok     ' || stmt || CHR(10);
            EXCEPTION
                WHEN OTHER THEN
                    report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;
    IF (ARRAY_SIZE(alerts) > 0) THEN
        FOR i IN 1 TO ARRAY_SIZE(alerts) DO
            fqn := GET(alerts, i - 1)::VARCHAR;
            INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS, ROOT_FQN)
            VALUES (:restore_id, :fqn, 'alert:started', CURRENT_TIMESTAMP(), NULL);
            stmt := 'ALTER ALERT ' || fqn || ' SUSPEND';
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                report := report || '-- ok     ' || stmt || CHR(10);
            EXCEPTION
                WHEN OTHER THEN
                    report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;
    IF (ARRAY_SIZE(dts) > 0) THEN
        FOR i IN 1 TO ARRAY_SIZE(dts) DO
            fqn := GET(dts, i - 1)::VARCHAR;
            INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS, ROOT_FQN)
            VALUES (:restore_id, :fqn, 'dynamic table:running', CURRENT_TIMESTAMP(), NULL);
            stmt := 'ALTER DYNAMIC TABLE ' || fqn || ' SUSPEND';
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                report := report || '-- ok     ' || stmt || CHR(10);
            EXCEPTION
                WHEN OTHER THEN
                    report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;
    IF (ARRAY_SIZE(pipes) > 0) THEN
        FOR i IN 1 TO ARRAY_SIZE(pipes) DO
            fqn := GET(pipes, i - 1)::VARCHAR;
            BEGIN
                EXECUTE IMMEDIATE 'SELECT PARSE_JSON(SYSTEM$PIPE_STATUS(''' || REPLACE(:fqn, '''', '''''')
                    || ''')):executionState::VARCHAR';
                SELECT $1 INTO :v FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
            EXCEPTION
                WHEN OTHER THEN
                    v := 'UNKNOWN';
            END;
            IF (v = 'RUNNING') THEN
                INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS, ROOT_FQN)
                VALUES (:restore_id, :fqn, 'pipe:running', CURRENT_TIMESTAMP(), NULL);
            END IF;
            stmt := 'ALTER PIPE ' || fqn || ' SET PIPE_EXECUTION_PAUSED = TRUE';
            BEGIN
                EXECUTE IMMEDIATE :stmt;
                report := report || '-- ok     ' || stmt || ' (was ' || COALESCE(v, 'UNKNOWN') || ')' || CHR(10);
            EXCEPTION
                WHEN OTHER THEN
                    report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
            END;
        END FOR;
    END IF;

    -- writers elsewhere in the database that name the table / schema (reported, not touched)
    BEGIN
        SHOW TASKS IN DATABASE;
        SELECT LISTAGG(t."schema_name" || '.' || t."name" || ' (' || LOWER(t."state") || ')', ', ')
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."schema_name" <> :sch AND CONTAINS(UPPER(COALESCE(t."definition"::VARCHAR, '')), :needle);
        IF (COALESCE(v, '') <> '') THEN
            report := report || 'CHECK  tasks in other schemas naming ' || needle || ': ' || v
                || ' -- suspend their roots by hand if they write it' || CHR(10);
        END IF;
        SHOW PIPES IN DATABASE;
        SELECT LISTAGG(t."schema_name" || '.' || t."name", ', ')
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."schema_name" <> :sch AND CONTAINS(UPPER(COALESCE(t."definition"::VARCHAR, '')), :needle);
        IF (COALESCE(v, '') <> '') THEN
            report := report || 'CHECK  pipes in other schemas naming ' || needle || ': ' || v
                || ' -- pause them by hand if they load it' || CHR(10);
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'CHECK  database-wide writer scan failed: ' || SQLERRM || CHR(10);
    END;

    RETURN 'Q0: schedules of ' || sfq || ' stopped and recorded as DEPLOY_ID ' || restore_id || ' ('
        || ARRAY_SIZE(roots) || ' root task(s), ' || ARRAY_SIZE(alerts) || ' alert(s), ' || ARRAY_SIZE(dts)
        || ' dynamic table(s), ' || ARRAY_SIZE(pipes) || ' pipe(s) paused). Q9 restarts them after the swap.'
        || CHR(10) || report;
END;
$$;


-- =====================================================================================================================
-- TEMPLATE A -- one table: clone it as it was, compare, swap   (Q0 first; Q9 after A3)
-- =====================================================================================================================
-- A1  The copy, side by side (zero-copy; COPY GRANTS keeps the table's explicit grants on the copy, so they are
--     still there after the swap). Fails if the table did not exist at the point -- created by this deploy -- or was
--     replaced since (CREATE OR REPLACE TABLE): use template C then. Remove the "-- " of the three lines to run it:
-- CREATE TABLE IDENTIFIER($restore_table_fqn)
--     CLONE IDENTIFIER($table_fqn) AT(TIMESTAMP => $recover_ts::TIMESTAMP_LTZ)
--     COPY GRANTS;

-- A2  Compare before you swap. The difference queries need the same columns on both sides: if the deploy added or
--     dropped columns, name the common columns instead of *. The second MINUS lists the rows the swap will DISCARD:
--     everything written after the point, legitimate ETL rows included. Keep that list (key columns) for the data
--     owner: those rows are re-inserted from the evidence copy after A3, or the ETL re-runs for that window.
SELECT (SELECT COUNT(*) FROM IDENTIFIER($restore_table_fqn)) AS ROWS_IN_COPY,
       (SELECT COUNT(*) FROM IDENTIFIER($table_fqn)) AS ROWS_NOW;
SELECT * FROM (SELECT * FROM IDENTIFIER($restore_table_fqn) MINUS SELECT * FROM IDENTIFIER($table_fqn)) LIMIT 100;
SELECT * FROM (SELECT * FROM IDENTIFIER($table_fqn) MINUS SELECT * FROM IDENTIFIER($restore_table_fqn)) LIMIT 100;

-- A3  The swap: one atomic rename of both tables. Afterwards the live name holds the recovered data and
--     <table>_RESTORE_<date> holds the damaged version AND every row written after the point (evidence; PART 9).
--     Views and procedures that name the table use the recovered one at once. Check afterwards (verify on first
--     run): SHOW GRANTS ON TABLE for both names; streams on the table (recreate them on the live table if they now
--     follow the evidence copy); dynamic tables or materialized views over it (a refresh / recompute follows).
--     Then re-insert the rows the data owner wants back (A2), and run Q9. Remove the "-- " to run it:
-- ALTER TABLE IDENTIFIER($table_fqn) SWAP WITH IDENTIFIER($restore_table_fqn);


-- =====================================================================================================================
-- TEMPLATE B -- a whole schema (never FLYWAY): clone it as it was, compare, swap, re-grant   (Q0 first; Q9 after B4)
-- =====================================================================================================================
-- B1  The copy (needs CREATE SCHEMA on the database, which the deploy role has). Child objects keep their grants;
--     the schema's own grants are not cloned (B4). Tasks / alerts / dynamic tables arrive SUSPENDED. Remove the
--     "-- " of the three lines to run it:
-- CREATE SCHEMA IDENTIFIER($restore_schema_fqn)
--     CLONE IDENTIFIER($schema_fqn) AT(TIMESTAMP => $recover_ts::TIMESTAMP_LTZ)
--     IGNORE HYBRID TABLES;

-- B2  Compare object lists and row counts (INFORMATION_SCHEMA of this database, real time):
SELECT COALESCE(a.TABLE_NAME, b.TABLE_NAME) AS TABLE_NAME, a.ROW_COUNT AS ROWS_IN_COPY, b.ROW_COUNT AS ROWS_NOW,
       a.TABLE_TYPE AS TYPE_IN_COPY, b.TABLE_TYPE AS TYPE_NOW
  FROM (SELECT t.TABLE_NAME, t.ROW_COUNT, t.TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES AS t
         WHERE t.TABLE_SCHEMA = $object_schema || '_RESTORE_' || $restore_tag) AS a
  FULL OUTER JOIN (SELECT t.TABLE_NAME, t.ROW_COUNT, t.TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES AS t
                    WHERE t.TABLE_SCHEMA = $object_schema) AS b
    ON a.TABLE_NAME = b.TABLE_NAME
 ORDER BY 1;

-- B2b What the copy LACKS (read-only). A schema clone does not carry named internal stages (nor their files), pipes
--     on them, external tables or hybrid tables. After B3 they would sit only in the evidence schema, live loads
--     would stop, and PART 9 would drop the only copy. A STOP line means: do not swap the schema; restore the damaged
--     tables one by one with template A instead (and drop the copy, PART 9).
EXECUTE IMMEDIATE $$
DECLARE
    live VARCHAR;
    copy_fqn VARCHAR;
    kinds ARRAY DEFAULT ARRAY_CONSTRUCT('STAGES', 'PIPES', 'EXTERNAL TABLES', 'HYBRID TABLES');
    kind VARCHAR;
    copy_names VARCHAR;
    missing VARCHAR;
    stop_lines VARCHAR DEFAULT '';
    notes VARCHAR DEFAULT '';
BEGIN
    SELECT $schema_fqn, $restore_schema_fqn INTO :live, :copy_fqn;
    FOR i IN 1 TO ARRAY_SIZE(kinds) DO
        kind := GET(kinds, i - 1)::VARCHAR;
        BEGIN
            EXECUTE IMMEDIATE 'SHOW ' || :kind || ' IN SCHEMA ' || :copy_fqn;
            SELECT '|' || COALESCE(LISTAGG(t."name", '|'), '') || '|' INTO :copy_names
              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
            EXECUTE IMMEDIATE 'SHOW ' || :kind || ' IN SCHEMA ' || :live;
            SELECT LISTAGG(IFF(CONTAINS(:copy_names, '|' || t."name" || '|'), NULL, t."name"), ', ')
              INTO :missing
              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
            IF (COALESCE(missing, '') <> '') THEN
                stop_lines := stop_lines || 'STOP   ' || LOWER(kind) || ' in ' || live || ' but not in the copy: '
                    || missing || CHR(10);
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                notes := notes || 'CHECK  SHOW ' || kind || ' failed: ' || SQLERRM || ' (compare by hand)' || CHR(10);
        END;
    END FOR;
    IF (stop_lines = '') THEN
        RETURN 'OK     the copy carries every stage, pipe, external and hybrid table of ' || live || CHR(10) || notes;
    END IF;
    RETURN stop_lines || notes || 'Do not run B3: restore table by table with template A.';
END;
$$;

-- B3  The swap (atomic). The live name now holds the recovered schema; <schema>_RESTORE_<date> the damaged one, with
--     every row written after the point and the objects Q0 stopped. Only after B2b said OK. Remove the "-- " to run:
-- ALTER SCHEMA IDENTIFIER($schema_fqn) SWAP WITH IDENTIFIER($restore_schema_fqn);

-- B4  The schema-level grants (USAGE, CREATE ... for functional roles and DATABASE roles) stayed on the damaged copy:
--     re-apply them to the live schema (needs confirm_template = 'B:<deploy_id>'). Schema-level FUTURE grants:
--     compare SHOW FUTURE GRANTS IN SCHEMA on both names (verify on first run; the R__90 grants repeatable restores
--     them at the next deploy anyway).
EXECUTE IMMEDIATE $$
DECLARE
    did VARCHAR;
    confirm VARCHAR;
    live VARCHAR;
    damaged VARCHAR;
    grant_list ARRAY;
    n INTEGER;
    stmt VARCHAR;
    report VARCHAR DEFAULT '';
    not_confirmed EXCEPTION (-20431, 'B4: SET confirm_template = ''B:<deploy_id>'' (PART 0) after B3.');
BEGIN
    SELECT $deploy_id, $confirm_template, $schema_fqn, $restore_schema_fqn INTO :did, :confirm, :live, :damaged;
    IF (confirm IS DISTINCT FROM 'B:' || did) THEN
        RAISE not_confirmed;
    END IF;
    EXECUTE IMMEDIATE 'SHOW GRANTS ON SCHEMA ' || :damaged;
    -- account roles quoted; a database role's grantee_name is already qualified <db>.<role>
    SELECT COALESCE(ARRAY_AGG('GRANT ' || t."privilege" || ' ON SCHEMA ' || :live
               || IFF(t."granted_to" = 'DATABASE_ROLE', ' TO DATABASE ROLE ' || t."grantee_name",
                      ' TO ROLE "' || REPLACE(t."grantee_name", '"', '""') || '"')
               || IFF(LOWER(t."grant_option"::VARCHAR) = 'true', ' WITH GRANT OPTION', '')), ARRAY_CONSTRUCT())
      INTO :grant_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE t."granted_to" IN ('ROLE', 'DATABASE_ROLE') AND t."privilege" <> 'OWNERSHIP';
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


-- =====================================================================================================================
-- Q9 -- restart what Q0 stopped (after A3, or after B4; same confirm_template as Q0)
-- =====================================================================================================================
-- Resumes exactly the objects Q0 recorded under DEPLOY_ID RESTORE-<deploy_id>, by name -- after a schema swap that
-- name now holds the recovered (cloned) object, which arrived suspended: root tasks with SYSTEM$TASK_DEPENDENTS_ENABLE
-- (root and every dependent; a child an operator had suspended is resumed too: suspend it again if needed), alerts and
-- dynamic tables with RESUME (a dynamic table then refreshes in full), pipes un-paused. A recovered pipe that shows
-- STOPPED_CLONED in SYSTEM$PIPE_STATUS needs SELECT SYSTEM$PIPE_FORCE_RESUME('<pipe>') (verify on first run); never
-- ALTER PIPE ... REFRESH it (it would reload files already loaded). After template B it also confirms that nothing
-- runs in the evidence schema: no started task or alert there; its pipes stay paused. Re-running Q9 is harmless.
EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    did VARCHAR;
    confirm VARCHAR;
    db VARCHAR;
    rsfq VARCHAR;
    restore_id VARCHAR;
    rows_list ARRAY;
    item VARIANT;
    fqn VARCHAR;
    kind VARCHAR;
    stmt VARCHAR;
    v VARCHAR;
    c1 INTEGER;
    c2 INTEGER;
    report VARCHAR DEFAULT '';
    not_confirmed EXCEPTION (-20491,
        'Q9: SET confirm_template = ''A:<deploy_id>'' or ''B:<deploy_id>'' (PART 0), in the runway database.');
BEGIN
    SELECT $runway, $deploy_id, $confirm_template, $runway_db, $restore_schema_fqn INTO :rw, :did, :confirm, :db, :rsfq;
    IF ((confirm IS DISTINCT FROM 'A:' || did AND confirm IS DISTINCT FROM 'B:' || did)
        OR db IS DISTINCT FROM 'ALFA_EDW_' || rw OR CURRENT_DATABASE() IS DISTINCT FROM db) THEN
        RAISE not_confirmed;
    END IF;
    restore_id := 'RESTORE-' || did;
    SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('fqn', s.TASK_FQN, 'kind', s.STATE_BEFORE))
                        WITHIN GROUP (ORDER BY s.CAPTURED_TS), ARRAY_CONSTRUCT())
      INTO :rows_list
      FROM FLYWAY.DEPLOY_TASK_STATE AS s
     WHERE s.DEPLOY_ID = :restore_id;
    IF (ARRAY_SIZE(rows_list) > 0) THEN
        FOR i IN 1 TO ARRAY_SIZE(rows_list) DO
            item := GET(rows_list, i - 1);
            fqn := GET(item, 'fqn')::VARCHAR;
            kind := GET(item, 'kind')::VARCHAR;
            stmt := CASE kind
                        WHEN 'started' THEN 'SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('''
                                            || REPLACE(fqn, '''', '''''') || ''')'
                        WHEN 'alert:started' THEN 'ALTER ALERT ' || fqn || ' RESUME'
                        WHEN 'dynamic table:running' THEN 'ALTER DYNAMIC TABLE ' || fqn || ' RESUME'
                        WHEN 'pipe:running' THEN 'ALTER PIPE ' || fqn || ' SET PIPE_EXECUTION_PAUSED = FALSE'
                    END;
            IF (stmt IS NULL) THEN
                report := report || 'CHECK  unknown kind ' || kind || ' for ' || fqn || ': restart it by hand'
                    || CHR(10);
            ELSE
                BEGIN
                    EXECUTE IMMEDIATE :stmt;
                    report := report || '-- ok     ' || stmt || CHR(10);
                EXCEPTION
                    WHEN OTHER THEN
                        report := report || '-- FAILED ' || stmt || '  -- ' || SQLERRM || CHR(10);
                END;
            END IF;
        END FOR;
    END IF;

    IF (LEFT(confirm, 1) = 'B') THEN
        BEGIN
            EXECUTE IMMEDIATE 'SHOW TASKS IN SCHEMA ' || :rsfq;
            SELECT COUNT_IF(LOWER(t."state") = 'started') INTO :c1 FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
            EXECUTE IMMEDIATE 'SHOW ALERTS IN SCHEMA ' || :rsfq;
            SELECT COUNT_IF(LOWER(t."state") = 'started') INTO :c2 FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
            EXECUTE IMMEDIATE 'SHOW PIPES IN SCHEMA ' || :rsfq;
            SELECT LISTAGG(t."name", ', ') INTO :v FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
            report := report || IFF(c1 = 0 AND c2 = 0, 'PASS', 'FAIL') || '   evidence schema ' || rsfq || ': '
                || c1 || ' started task(s), ' || c2 || ' started alert(s) (want 0: suspend them)' || CHR(10)
                || 'INFO   its pipes (paused by Q0; keep them paused): ' || COALESCE(NULLIF(v, ''), 'none') || CHR(10);
        EXCEPTION
            WHEN OTHER THEN
                report := report || 'CHECK  evidence schema check failed: ' || SQLERRM || CHR(10);
        END;
    END IF;
    RETURN 'Q9: ' || ARRAY_SIZE(rows_list) || ' object(s) recorded as ' || restore_id || CHR(10) || report;
END;
$$;


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

-- C2  Move the current table (if any) aside, then UNDROP. Remove the "-- " of the two lines to run them:
-- ALTER TABLE IDENTIFIER($table_fqn) RENAME TO IDENTIFIER($damaged_table_fqn);
-- UNDROP TABLE IDENTIFIER($table_fqn);
--     To reach an OLDER dropped version, repeat the pair, but first give the next aside-copy its own name (the
--     previous iteration already used <table>_DAMAGED_<date>, so the RENAME would fail):
--       SET damaged_table_fqn = $table_fqn || '_DAMAGED_' || $restore_tag || '_2';     -- then _3, ...
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
--     and lags up to ~45 minutes; it keeps a year. Flyway sends a file's leading comment block as part of its first
--     statement, so QUERY_TEXT starts with the file's header comment: the filter looks for CREATE OR REPLACE / ALTER
--     anywhere in the text, and the statement you copy begins after that header.
USE ROLE ACCOUNTADMIN;
SELECT q.START_TIME, q.USER_NAME, q.ROLE_NAME, q.QUERY_ID, q.QUERY_TEXT
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY AS q
 WHERE q.START_TIME >= DATEADD('day', -365, CURRENT_TIMESTAMP())
   AND q.START_TIME < $recover_ts
   AND q.DATABASE_NAME = $runway_db
   AND q.EXECUTION_STATUS = 'SUCCESS'
   AND REGEXP_INSTR(UPPER(q.QUERY_TEXT), 'CREATE[[:space:]]+OR[[:space:]]+(REPLACE|ALTER)[[:space:]]') > 0
   AND CONTAINS(UPPER(q.QUERY_TEXT), UPPER($object_schema || '.' || $object_name))
 ORDER BY q.START_TIME DESC
 LIMIT 5;
USE ROLE IDENTIFIER($deploy_role);
-- D3  Run the chosen statement as the deploy role (copy it from D2 or from the substituted git file). Keep COPY
--     GRANTS in it (guard FG011): without it the replaced object loses its explicit grants.


-- =====================================================================================================================
-- PART 8 -- Flyway afterwards (workstation; GUIDE B7 for a manual Flyway session, RUNBOOKS R1 / R3)
-- =====================================================================================================================
--   eval "$(python3 scripts/fw.py env --runway <ID> --format shell)"                   # + B7 exports
--   flyway -environment=<ID> info
-- (fw.py exports EDW_* names only; export no FLYWAY_<SETTING> variable yourself: Flyway reads FLYWAY_TARGET and the
-- like from the environment as its own settings.)
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
-- * Task graphs the failed deploy left suspended: FLYWAY.DEPLOY_TASK_STATE holds the roots and members that were
--   started before it (afterMigrateError__99 lists them in the FAILED row); resume them once the fix is in (RUNBOOKS
--   R9).
-- * Unreserve the Jenkins lock ALFA_EDW_<ID>; revoke any break-glass grant; SET confirm_template = 'none';


-- =====================================================================================================================
-- PART 9 -- cleanup of the side-by-side copies, after sign-off (commented out on purpose; deploy role)
-- =====================================================================================================================
-- After template A the _RESTORE_ table holds the DAMAGED version plus every row written after the point; after C the
-- _DAMAGED_ table holds the version you moved aside. After B the _RESTORE_ schema holds the damaged schema AND every
-- object a schema clone does not carry (named internal stages with their files, pipes on them, external tables,
-- hybrid tables -- B2b listed them) plus the objects Q0 stopped: dropping it loses all of that, so first move out (or
-- re-create in the live schema) anything still needed. Each stays recoverable with UNDROP for its retention period.
--   DROP TABLE IF EXISTS IDENTIFIER($restore_table_fqn);
--   DROP SCHEMA IF EXISTS IDENTIFIER($restore_schema_fqn);
--   DROP TABLE IF EXISTS IDENTIFIER($damaged_table_fqn);
