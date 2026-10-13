-- Flyway callback: beforeMigrate (once per `flyway migrate`, before the first pending migration; also when nothing
--                  is pending). Callbacks of one event run in file-name order: 10_deploy_log, then 20_task_snapshot.
-- What   : 1. refuses to start when the session database is not the edw_db placeholder (the database this Flyway
--             environment is meant to change), and 2. writes the STARTED row of FLYWAY.DEPLOY_LOG.
-- Why    : 1. the rehearsal environments take their database from FLYWAY_RH_DB at run time: a wrong value would
--             point a "rehearsal" (tasks_enabled=false) at a real runway. Failing here applies no migration:
--             Flyway runs beforeMigrate before the first migration, and a beforeMigrate failure does not trigger
--             afterMigrateError, so the deploy simply stops.
--          2. the audit trail: who deployed which build to which runway, approved by whom. afterMigrate__99 /
--             afterMigrateError__99 close it with SUCCEEDED / FAILED; afterMigrate__50 and __60 may add FAILED or
--             TASKS_* rows. A STARTED row with no later row for its DEPLOY_ID means the run died mid-way.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : FLYWAY.DEPLOY_LOG is created by bootstrap/20_runway_database.sql in every runway database; the callback
--          names only FLYWAY.* (the session database is the runway, the session schema is FLYWAY).
--          Flyway runs callbacks on their own connection and, on Snowflake, without a transaction (its Snowflake
--          support uses a plain execution template), so each statement commits on its own.
--          Placeholders are replaced as text BEFORE the SQL is parsed, so every value is quoted as a string
--          literal. The pipeline passes plain values (build ids, git SHA, Jenkins URL, approver id); a value that
--          contains a single quote breaks this statement, which stops the deploy before anything is applied.
--          The Snowflake Scripting block is wrapped in EXECUTE IMMEDIATE with a dollar-quoted body: Flyway's
--          Snowflake parser splits a bare DECLARE / BEGIN block in the wrong places (guard FG015).

EXECUTE IMMEDIATE $$
DECLARE
    wrong_database EXCEPTION (-20110,
        'beforeMigrate: the session database is not ${edw_db}; check the JDBC url db= (or FLYWAY_RH_DB).');
BEGIN
    IF (UPPER(COALESCE(CURRENT_DATABASE(), '')) <> UPPER('${edw_db}')) THEN
        RAISE wrong_database;
    END IF;

    INSERT INTO FLYWAY.DEPLOY_LOG
        (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
    SELECT
        '${deploy_id}',
        'STARTED',
        '${env}',
        '${git_sha}',
        '${build_url}',
        '${approver}',
        '${flyway:user}',
        CURRENT_TIMESTAMP(),
        'flyway_environment=${flyway:environment} database=' || CURRENT_DATABASE()
            || ' role=' || COALESCE(CURRENT_ROLE(), '(none)')
            || ' warehouse=' || COALESCE(CURRENT_WAREHOUSE(), '(none)')
            || ' tasks_enabled=${tasks_enabled}';

    RETURN 'deploy ${deploy_id}: STARTED on ' || CURRENT_DATABASE();
END;
$$;
