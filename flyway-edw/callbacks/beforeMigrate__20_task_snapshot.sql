-- Flyway callback: beforeMigrate (runs after beforeMigrate__10_deploy_log, before the first pending migration).
-- What   : records every ROOT task of the runway database that is STARTED right now in FLYWAY.DEPLOY_TASK_STATE
--          (one row per task: DEPLOY_ID, quoted fully qualified name, STATE_BEFORE = 'started', CAPTURED_TS).
--          Also checks that the tasks_enabled placeholder is exactly "true" or "false".
-- Why    : migrations may suspend a task graph to change it (a task in a started graph must be suspended before
--          it is modified) and Snowflake DDL autocommits, so a run can end with graphs suspended that were running
--          before. afterMigrate__60_task_restore resumes, on runways with tasks_enabled = "true", exactly the roots
--          recorded here; after a FAILED run, afterMigrateError__99 lists the recorded roots that are still
--          suspended, and docs/RUNBOOKS.md resumes them by hand once the failure is fixed.
--          A root task is one with no predecessors (SHOW TASKS "predecessors" = []); resuming a root with
--          SYSTEM$TASK_DEPENDENTS_ENABLE resumes its whole graph. Only roots this deploy role can see are recorded.
--          The tasks_enabled check runs here, before anything is applied, because afterMigrate__60 suspends every
--          root when the value is not "true": a typo such as "TRUE" must stop the deploy, not stop production.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : SHOW TASKS IN DATABASE uses the session database (the runway). Its output is read with
--          RESULT_SCAN(LAST_QUERY_ID()) by the very next statement, so nothing may run between the two. SHOW output
--          columns are lower-case and must be double-quoted ("name", "schema_name", "state", "predecessors").
--          TASK_FQN is "DATABASE"."SCHEMA"."TASK" with embedded double quotes doubled: exact for any name, and the
--          form afterMigrate__60 passes to SYSTEM$TASK_DEPENDENTS_ENABLE. Rows of one run are those with its
--          DEPLOY_ID captured at or after its STARTED row (manual runs all share DEPLOY_ID "manual").
--          The block is wrapped in EXECUTE IMMEDIATE with a dollar-quoted body (guard FG015).

EXECUTE IMMEDIATE $$
DECLARE
    bad_tasks_enabled EXCEPTION (-20120,
        'beforeMigrate: placeholder tasks_enabled must be exactly true or false (flyway.toml); nothing was applied.');
    recorded INTEGER DEFAULT 0;
BEGIN
    IF ('${tasks_enabled}' <> 'true' AND '${tasks_enabled}' <> 'false') THEN
        RAISE bad_tasks_enabled;
    END IF;

    SHOW TASKS IN DATABASE;
    INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS)
    SELECT
        '${deploy_id}',
        '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
            || '"."' || REPLACE(t."name", '"', '""') || '"',
        LOWER(t."state"),
        CURRENT_TIMESTAMP()
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
    WHERE LOWER(t."state") = 'started'
      AND REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]';
    recorded := SQLROWCOUNT;

    RETURN 'deploy ${deploy_id}: ' || recorded || ' started root task(s) recorded';
END;
$$;
