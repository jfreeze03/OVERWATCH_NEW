-- Flyway callback: afterMigrateError (runs when a migration of this `flyway migrate` FAILED; the afterMigrate
--                  callbacks do not run then: no task restore, no cross-environment scan, no SUCCEEDED row).
-- What   : writes the FAILED row of FLYWAY.DEPLOY_LOG, best effort, with what the operator needs first:
--          the migrations Flyway recorded as failed in this run and, where tasks_enabled = "true", the root tasks
--          that were started before the run (beforeMigrate__20 snapshot) and are suspended now.
-- Why    : Snowflake DDL autocommits, so a failed migration is partly applied (Flyway records success=false) and
--          the fix is manual cleanup + `flyway repair` + fix forward (docs/RUNBOOKS.md). Task graphs a migration
--          suspended stay suspended ON PURPOSE: resuming production work against a half-applied change is worse
--          than a paused schedule. The listed roots are resumed by hand once the change is fixed.
--          Flyway calls this callback from inside its own error handling and rethrows the migration error
--          afterwards; an exception escaping from here would replace that original error. So the context query
--          and the INSERT each sit in their own BEGIN ... EXCEPTION WHEN OTHER THEN ... END: a missing table or a
--          privilege gap shortens or skips the row, it never hides why the deploy failed.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : FLYWAY.DEPLOY_LOG / FLYWAY.DEPLOY_TASK_STATE come from bootstrap/20_runway_database.sql. This run =
--          rows at or after the latest STARTED row for this DEPLOY_ID (beforeMigrate__10 succeeded with the same
--          placeholder values, or migrate would not have started). SHOW TASKS is read by the next statement.
--          Wrapped in EXECUTE IMMEDIATE with a dollar-quoted body (guard FG015).

EXECUTE IMMEDIATE $$
DECLARE
    started_ts TIMESTAMP_LTZ;
    failed_list VARCHAR DEFAULT '';
    stranded VARCHAR DEFAULT '';
    detail VARCHAR;
BEGIN
    -- Context for the row. Any error here only shortens the row.
    BEGIN
        SELECT MAX(l.EVENT_TS) INTO :started_ts
          FROM FLYWAY.DEPLOY_LOG AS l
         WHERE l.DEPLOY_ID = '${deploy_id}'
           AND l.EVENT = 'STARTED';

        SELECT COALESCE(LISTAGG(COALESCE(h."version", 'repeatable') || ' ' || h."description", '; ')
                            WITHIN GROUP (ORDER BY h."installed_rank"), '')
          INTO :failed_list
          FROM FLYWAY."flyway_schema_history" AS h
         WHERE NOT h."success"
           AND h."installed_on" >= :started_ts;

        IF ('${tasks_enabled}' = 'true') THEN
            SHOW TASKS IN DATABASE;
            SELECT COALESCE(LISTAGG(DISTINCT s.TASK_FQN, ', '), '') INTO :stranded
              FROM FLYWAY.DEPLOY_TASK_STATE AS s
              JOIN TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                ON s.TASK_FQN = '"' || REPLACE(t."database_name", '"', '""') || '"."'
                       || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '"'
             WHERE s.DEPLOY_ID = '${deploy_id}'
               AND s.CAPTURED_TS >= :started_ts
               AND s.STATE_BEFORE = 'started'
               AND LOWER(t."state") <> 'started';
        END IF;

        detail := 'migrate failed on ' || CURRENT_DATABASE() || '; failed migration(s): '
            || IFF(failed_list = '', 'none recorded in the history table', failed_list)
            || IFF(stranded = '', '',
                   '; root task(s) started before this run and suspended now (resume after the fix): ' || stranded)
            || '; afterMigrate callbacks (task restore, cross-environment guard) did not run';
    EXCEPTION
        WHEN OTHER THEN
            detail := 'migrate failed on ' || CURRENT_DATABASE() || ' (context query failed: ' || SQLERRM || ')';
    END;

    -- The row itself. An error here is reported in the return value, never raised.
    BEGIN
        INSERT INTO FLYWAY.DEPLOY_LOG
            (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
        SELECT '${deploy_id}', 'FAILED', '${env}', '${git_sha}', '${build_url}', '${approver}', '${flyway:user}',
               CURRENT_TIMESTAMP(), LEFT(:detail, 4000);
    EXCEPTION
        WHEN OTHER THEN
            RETURN 'afterMigrateError: FAILED row not written (' || SQLERRM || '); the migrate error stands';
    END;

    RETURN 'deploy ${deploy_id}: FAILED row written';
END;
$$;
