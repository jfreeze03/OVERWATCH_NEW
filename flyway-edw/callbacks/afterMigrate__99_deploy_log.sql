-- Flyway callback: afterMigrate (runs only after a SUCCESSFUL migrate; last of 60_task_restore, 80_cross_env_guard,
--                  99_deploy_log). It is reached only when both earlier callbacks passed.
-- What   : writes the SUCCEEDED row of FLYWAY.DEPLOY_LOG for this deploy, with what the run applied: the number
--          of versioned and repeatable migrations Flyway recorded since this run's STARTED row, and their versions.
-- Why    : closes the audit pair opened by beforeMigrate__10 (STARTED). With "applied=0" it also records a no-op
--          deploy (nothing pending), which is still a deploy of that build to that runway.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : a single INSERT ... SELECT (no Snowflake Scripting needed). FLYWAY.DEPLOY_LOG comes from
--          bootstrap/20_runway_database.sql; FLYWAY."flyway_schema_history" is Flyway's history table, whose name
--          and columns are quoted lower-case. This run = history rows installed at or after the latest STARTED
--          row for this DEPLOY_ID. Placeholders are replaced as text before parsing, so they are string literals.

INSERT INTO FLYWAY.DEPLOY_LOG
    (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
SELECT
    '${deploy_id}',
    'SUCCEEDED',
    '${env}',
    '${git_sha}',
    '${build_url}',
    '${approver}',
    '${flyway:user}',
    CURRENT_TIMESTAMP(),
    LEFT('applied=' || COUNT(h."installed_rank")
         || ' versioned=' || COUNT(h."version")
         || ' repeatable=' || (COUNT(h."installed_rank") - COUNT(h."version"))
         || ' versions=' || COALESCE(LISTAGG(h."version", ',') WITHIN GROUP (ORDER BY h."installed_rank"), '')
         || ' database=' || CURRENT_DATABASE(), 4000)
FROM (SELECT MAX(l.EVENT_TS) AS STARTED_TS
        FROM FLYWAY.DEPLOY_LOG AS l
       WHERE l.DEPLOY_ID = '${deploy_id}'
         AND l.EVENT = 'STARTED') AS r
LEFT JOIN FLYWAY."flyway_schema_history" AS h
       ON h."installed_on" >= r.STARTED_TS
      AND h."success";
