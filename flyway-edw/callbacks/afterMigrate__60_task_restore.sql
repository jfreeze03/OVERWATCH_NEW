-- Flyway callback: afterMigrate (runs only after a SUCCESSFUL migrate; first of 60_task_restore,
--                  80_cross_env_guard, 99_deploy_log). A failed run goes to afterMigrateError__99_deploy_log instead.
-- What   : makes the runway's root tasks match the tasks_enabled placeholder.
--          "true"  (MGM, PRD): every root task that beforeMigrate__20 recorded as started for THIS run, still exists
--                  and is not started now, is resumed with SYSTEM$TASK_DEPENDENTS_ENABLE (root plus its graph).
--          "false" (DEV, SAN, SEA, PHX, rehearsal clones): every started root task in the database is suspended, so
--                  a test runway never runs scheduled work it was not meant to (cost, writes to shared targets).
--          Then it re-reads SHOW TASKS; if any root is still in the wrong state it writes a FAILED row to
--          FLYWAY.DEPLOY_LOG and raises, so the deploy goes red instead of silently leaving production stopped.
--          Work done is logged as one TASKS_RESUMED / TASKS_SUSPENDED row in FLYWAY.DEPLOY_LOG.
-- Why    : migrations suspend graphs to change them and Snowflake DDL autocommits; repeatable task files resume
--          only the graph they define (layer 80). This callback is the net under everything else. It resumes only
--          what was running before the deploy: a graph an operator had suspended stays suspended.
--          Raising here (after the migrations committed) fails the Flyway command: afterMigrate__99 does not run
--          and Flyway does not call afterMigrateError for it, which is why the FAILED row is written here first.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : FLYWAY.DEPLOY_TASK_STATE / FLYWAY.DEPLOY_LOG come from bootstrap/20_runway_database.sql. This run's
--          snapshot = rows with its DEPLOY_ID captured at or after its latest STARTED row (beforeMigrate__10 runs
--          before __20). Each SHOW TASKS is read by the statement right after it (RESULT_SCAN(LAST_QUERY_ID())).
--          Task names are passed as "DATABASE"."SCHEMA"."TASK" with embedded quotes doubled, built into a statement
--          string and run with EXECUTE IMMEDIATE (SYSTEM$ functions take the name as a string literal).
--          A declared exception's message must be a literal, so the failure message is built into a one-line
--          anonymous block that EXECUTE IMMEDIATE runs; its error carries the message to the Flyway log.
--          Wrapped in EXECUTE IMMEDIATE with a dollar-quoted body (guard FG015).

EXECUTE IMMEDIATE $$
DECLARE
    bad_tasks_enabled EXCEPTION (-20160,
        'afterMigrate task restore: placeholder tasks_enabled must be exactly true or false (flyway.toml).');
    want_started BOOLEAN DEFAULT ('${tasks_enabled}' = 'true');
    verb VARCHAR DEFAULT IFF('${tasks_enabled}' = 'true', 'resume', 'suspend');
    event_name VARCHAR DEFAULT IFF('${tasks_enabled}' = 'true', 'TASKS_RESUMED', 'TASKS_SUSPENDED');
    started_ts TIMESTAMP_LTZ;
    pending ARRAY DEFAULT ARRAY_CONSTRUCT();
    acted ARRAY DEFAULT ARRAY_CONSTRUCT();
    fqn VARCHAR;
    stmt VARCHAR;
    errs VARCHAR DEFAULT '';
    msg VARCHAR;
    raise_sql VARCHAR;
BEGIN
    IF ('${tasks_enabled}' <> 'true' AND '${tasks_enabled}' <> 'false') THEN
        RAISE bad_tasks_enabled;
    END IF;

    SELECT MAX(l.EVENT_TS) INTO :started_ts
      FROM FLYWAY.DEPLOY_LOG AS l
     WHERE l.DEPLOY_ID = '${deploy_id}'
       AND l.EVENT = 'STARTED';

    -- Attempt 1 finds the roots in the wrong state and acts on them; attempt 2 verifies the result.
    FOR attempt IN 1 TO 2 DO
        IF (want_started) THEN
            SHOW TASKS IN DATABASE;
            SELECT COALESCE(ARRAY_AGG(DISTINCT s.TASK_FQN), ARRAY_CONSTRUCT()) INTO :pending
              FROM FLYWAY.DEPLOY_TASK_STATE AS s
              JOIN TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                ON s.TASK_FQN = '"' || REPLACE(t."database_name", '"', '""') || '"."'
                       || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '"'
             WHERE s.DEPLOY_ID = '${deploy_id}'
               AND s.CAPTURED_TS >= :started_ts
               AND s.STATE_BEFORE = 'started'
               AND LOWER(t."state") <> 'started';
        ELSE
            SHOW TASKS IN DATABASE;
            SELECT COALESCE(ARRAY_AGG(x.FQN), ARRAY_CONSTRUCT()) INTO :pending
              FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."'
                           || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""')
                           || '"' AS FQN
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                     WHERE LOWER(t."state") = 'started'
                       AND REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]'
                   ) AS x;
        END IF;

        IF (ARRAY_SIZE(pending) = 0) THEN
            BREAK;
        END IF;

        IF (attempt = 2) THEN
            msg := 'afterMigrate task restore (tasks_enabled=${tasks_enabled}) on ' || CURRENT_DATABASE() || ': '
                || ARRAY_SIZE(pending) || ' root task(s) could not be ' || IFF(want_started, 'resumed', 'suspended')
                || ': ' || ARRAY_TO_STRING(ARRAY_SLICE(pending, 0, 20), ', ')
                || IFF(ARRAY_SIZE(pending) > 20, ', ...', '')
                || IFF(errs = '', '', '. Errors:' || errs)
                || '. Migrations are applied; fix the task state by hand (docs/RUNBOOKS.md).';
            msg := LEFT(msg, 1800);
            INSERT INTO FLYWAY.DEPLOY_LOG
                (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
            SELECT '${deploy_id}', 'FAILED', '${env}', '${git_sha}', '${build_url}', '${approver}', '${flyway:user}',
                   CURRENT_TIMESTAMP(), :msg;
            raise_sql := 'DECLARE task_restore_failed EXCEPTION (-20161, '''
                || REPLACE(REPLACE(msg, '\\', '\\\\'), '''', '''''')
                || '''); BEGIN RAISE task_restore_failed; END;';
            EXECUTE IMMEDIATE :raise_sql;
        END IF;

        acted := pending;
        FOR i IN 0 TO ARRAY_SIZE(acted) - 1 DO
            fqn := GET(acted, i)::VARCHAR;
            IF (want_started) THEN
                stmt := 'SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('''
                    || REPLACE(REPLACE(fqn, '\\', '\\\\'), '''', '''''') || ''')';
            ELSE
                stmt := 'ALTER TASK ' || fqn || ' SUSPEND';
            END IF;
            BEGIN
                EXECUTE IMMEDIATE :stmt;
            EXCEPTION
                WHEN OTHER THEN
                    errs := errs || ' ' || fqn || ': ' || SQLERRM || ';';
            END;
        END FOR;
    END FOR;

    IF (ARRAY_SIZE(acted) = 0) THEN
        RETURN 'deploy ${deploy_id}: no root task to ' || verb;
    END IF;

    msg := LEFT(ARRAY_SIZE(acted) || ' root task(s): ' || ARRAY_TO_STRING(acted, ', '), 4000);
    INSERT INTO FLYWAY.DEPLOY_LOG
        (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
    SELECT '${deploy_id}', :event_name, '${env}', '${git_sha}', '${build_url}', '${approver}', '${flyway:user}',
           CURRENT_TIMESTAMP(), :msg;
    RETURN 'deploy ${deploy_id}: ' || event_name || ' ' || msg;
END;
$$;
