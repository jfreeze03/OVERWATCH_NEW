-- Repeatable (layer 80: tasks): SALES.TASK_REFRESH_ORDER_SUMMARY (root of its task graph; one file per graph)
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : runs SALES.SP_REFRESH_ORDER_SUMMARY (layer 50) daily at 06:00 Central on the runway's ETL warehouse.
--          1. SUSPEND first: a task in a started graph must be suspended before it is changed.
--          2. CREATE OR ALTER, not CREATE OR REPLACE: the task keeps its ID, grants and history; a replace makes a
--             new task (new ID) and cancels the root's next scheduled run. Properties left out of the statement
--             are reset to their defaults, so this file is the task's complete definition.
--          3. Resume the graph ONLY where the runway runs tasks: the tasks_enabled placeholder is "true" on MGM and
--             PRD and "false" on DEV, the test runways and the rehearsal clones (flyway.toml). Elsewhere the task
--             stays suspended. SYSTEM$TASK_DEPENDENTS_ENABLE resumes the root and every dependent (guard FG014).
--          Flyway re-runs this file when its checksum changes; repeatable checksums include placeholder values,
--          so a runway re-runs it when its etl_wh or tasks_enabled value changes too.
--          Verify on first run (DEV): the task body names SCHEMA.PROC, and Snowflake's documentation does not state
--          which database a task body resolves names in; the task is created in the runway database and is
--          expected to resolve there. The deploy role needs USAGE on the ETL warehouse and EXECUTE TASK (bootstrap).
-- Rules  : docs/CONVENTIONS.md (Tasks). Schema-qualify every object (SCHEMA.OBJECT); never name the database.

ALTER TASK IF EXISTS SALES.TASK_REFRESH_ORDER_SUMMARY SUSPEND;

CREATE OR ALTER TASK SALES.TASK_REFRESH_ORDER_SUMMARY
    WAREHOUSE = ${etl_wh}
    SCHEDULE = 'USING CRON 0 6 * * * America/Chicago'
    USER_TASK_TIMEOUT_MS = 3600000
    COMMENT = 'Daily 06:00 America/Chicago rebuild of SALES.ORDER_SUMMARY (calls SALES.SP_REFRESH_ORDER_SUMMARY)'
AS
    CALL SALES.SP_REFRESH_ORDER_SUMMARY();

EXECUTE IMMEDIATE $$
BEGIN
    IF ('${tasks_enabled}' = 'true') THEN
        LET resumed VARCHAR := (SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('SALES.TASK_REFRESH_ORDER_SUMMARY'));
        RETURN 'SALES.TASK_REFRESH_ORDER_SUMMARY and its dependents resumed: ' || resumed;
    END IF;
    RETURN 'tasks_enabled is not true on this runway: SALES.TASK_REFRESH_ORDER_SUMMARY stays suspended';
END;
$$;
