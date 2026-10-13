-- Flyway callback: afterMigrate (runs only after a SUCCESSFUL migrate; second of 50_cross_env_guard, 60_task_restore,
--                  99_deploy_log, so it runs only when the cross-environment guard passed). A failed run goes to
--                  afterMigrateError__99_deploy_log instead.
-- What   : makes the runway's task graphs match the tasks_enabled placeholder.
--          "true"  (MGM, PRD): for every graph whose root beforeMigrate__20 recorded as started for THIS run and that
--                  still exists as a root, every member (the root, its dependents from TASK_DEPENDENTS, its finalizer)
--                  that is not started now and was either recorded as started or is new (not in the snapshot) is
--                  resumed: root suspended (if started), those members resumed, root resumed LAST.
--          "false" (DEV, SAN, SEA, PHX, rehearsal clones): every started root task in the database is suspended, so
--                  a test runway never runs scheduled work it was not meant to (cost, writes to shared targets).
--                  Finalizer tasks follow their root and are not suspended separately.
--          Then it checks again; if any graph is still in the wrong state it writes a FAILED row to
--          FLYWAY.DEPLOY_LOG and raises, so the deploy goes red instead of silently leaving production stopped.
--          Work done is logged as one TASKS_RESUMED / TASKS_SUSPENDED row in FLYWAY.DEPLOY_LOG.
-- Why    : migrations suspend graphs to change them, a new or re-created member starts suspended, and Snowflake DDL
--          autocommits; repeatable task files resume only the graph they define (layer 80). This callback is the
--          net under everything else, and it checks MEMBERS, not only roots: a started root with a suspended member
--          skips that member and everything after it on every run, without any error.
--          It resumes only what was running before the deploy (plus members the deploy added to a running graph):
--          a graph whose root an operator had suspended stays suspended, and a member that was suspended at snapshot
--          time (an operator skipping a step) stays suspended too: members are resumed one by one (ALTER TASK ...
--          RESUME), never with SYSTEM$TASK_DEPENDENTS_ENABLE, which would resume every dependent. The other side of
--          that rule: a migration that only SUSPENDS a running graph is undone here; retire a graph with DROP TASK
--          (and its file) instead.
--          Raising here (after the migrations committed) fails the Flyway command: afterMigrate__99 does not run
--          and Flyway does not call afterMigrateError for it, which is why the FAILED row is written here first.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : FLYWAY.DEPLOY_TASK_STATE / FLYWAY.DEPLOY_LOG come from bootstrap/20_runway_database.sql. This run's
--          snapshot = rows with its DEPLOY_ID captured at or after its latest STARTED row (beforeMigrate__10 runs
--          before __20); root rows have TASK_FQN = ROOT_FQN. Each SHOW TASKS is read by the statement right after it
--          (RESULT_SCAN(LAST_QUERY_ID())). A child can be resumed only while its root is suspended, hence the order.
--          Task names are "DATABASE"."SCHEMA"."TASK" with embedded quotes doubled, built into ALTER TASK statements
--          and run with EXECUTE IMMEDIATE, each in its own handler: a failure is collected, the rest continue, and
--          the verification pass reports what is still wrong instead of passing silently. Verify on first run (MGM)
--          that TASK_DEPENDENTS accepts the quoted name (an error there is reported the same way).
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
    roots ARRAY DEFAULT ARRAY_CONSTRUCT();
    pending ARRAY DEFAULT ARRAY_CONSTRUCT();
    children ARRAY DEFAULT ARRAY_CONSTRUCT();
    stmts ARRAY DEFAULT ARRAY_CONSTRUCT();
    item VARIANT;
    root_fqn VARCHAR;
    root_state VARCHAR;
    fin_fqn VARCHAR;
    fin_state VARCHAR;
    graph_err VARCHAR;
    stmt VARCHAR;
    n_acted INTEGER DEFAULT 0;
    acted_txt VARCHAR DEFAULT '';
    wrong_txt VARCHAR;
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

    -- Attempt 1 finds the graphs in the wrong state and acts on them; attempt 2 verifies the result.
    FOR attempt IN 1 TO 2 DO
        pending := ARRAY_CONSTRUCT();
        IF (want_started) THEN
            -- The roots recorded as started for this run that are still roots, with their state now and their
            -- finalizer (paired on the root's unquoted upper-case name, as in beforeMigrate__20).
            SHOW TASKS IN DATABASE;
            SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('root', g.ROOT_FQN, 'state', g.ROOT_STATE,
                                                       'fin', g.FIN_FQN, 'fin_state', g.FIN_STATE))
                                WITHIN GROUP (ORDER BY g.ROOT_FQN), ARRAY_CONSTRUCT())
              INTO :roots
              FROM (SELECT MAX(IFF(x.FIN_OF IS NULL AND x.IS_ROOT, x.FQN, NULL)) AS ROOT_FQN,
                           MAX(IFF(x.FIN_OF IS NULL AND x.IS_ROOT, x.STATE, NULL)) AS ROOT_STATE,
                           MAX(IFF(x.FIN_OF IS NULL, NULL, x.FQN)) AS FIN_FQN,
                           MAX(IFF(x.FIN_OF IS NULL, NULL, x.STATE)) AS FIN_STATE
                      FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."'
                                       || REPLACE(t."schema_name", '"', '""') || '"."'
                                       || REPLACE(t."name", '"', '""') || '"' AS FQN,
                                   LOWER(t."state") AS STATE,
                                   REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]'
                                       AS IS_ROOT,
                                   NULLIF(UPPER(REPLACE(GET(TRY_PARSE_JSON(GET(OBJECT_CONSTRUCT(*),
                                       'task_relations')::VARCHAR), 'FinalizedRootTask')::VARCHAR, '"', '')), '')
                                       AS FIN_OF
                              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS x
                     GROUP BY COALESCE(x.FIN_OF, UPPER(REPLACE(x.FQN, '"', '')))) AS g
             WHERE g.ROOT_FQN IN (SELECT s.TASK_FQN
                                    FROM FLYWAY.DEPLOY_TASK_STATE AS s
                                   WHERE s.DEPLOY_ID = '${deploy_id}'
                                     AND s.CAPTURED_TS >= :started_ts
                                     AND s.STATE_BEFORE = 'started'
                                     AND s.TASK_FQN = s.ROOT_FQN);

            -- Per graph: the members to resume = not started now AND (recorded started OR not in the snapshot).
            IF (ARRAY_SIZE(roots) > 0) THEN
                FOR i IN 0 TO ARRAY_SIZE(roots) - 1 DO
                    item := GET(roots, i);
                    root_fqn := GET(item, 'root')::VARCHAR;
                    root_state := GET(item, 'state')::VARCHAR;
                    fin_fqn := GET(item, 'fin')::VARCHAR;
                    fin_state := GET(item, 'fin_state')::VARCHAR;
                    graph_err := NULL;
                    BEGIN
                        SELECT COALESCE(ARRAY_AGG(DISTINCT m.FQN), ARRAY_CONSTRUCT())
                          INTO :children
                          FROM (SELECT '"' || REPLACE(GET_IGNORE_CASE(d.O, 'database_name')::VARCHAR, '"', '""')
                                           || '"."' || REPLACE(GET_IGNORE_CASE(d.O, 'schema_name')::VARCHAR, '"', '""')
                                           || '"."' || REPLACE(GET_IGNORE_CASE(d.O, 'name')::VARCHAR, '"', '""')
                                           || '"' AS FQN,
                                       LOWER(GET_IGNORE_CASE(d.O, 'state')::VARCHAR) AS STATE
                                  FROM (SELECT OBJECT_CONSTRUCT(*) AS O
                                          FROM TABLE(INFORMATION_SCHEMA.TASK_DEPENDENTS(TASK_NAME => :root_fqn,
                                                                                        RECURSIVE => TRUE))) AS d
                                UNION ALL
                                SELECT :fin_fqn, :fin_state) AS m
                          LEFT JOIN (SELECT s.TASK_FQN, MIN(s.STATE_BEFORE) AS STATE_BEFORE
                                       FROM FLYWAY.DEPLOY_TASK_STATE AS s
                                      WHERE s.DEPLOY_ID = '${deploy_id}'
                                        AND s.CAPTURED_TS >= :started_ts
                                      GROUP BY s.TASK_FQN) AS b
                            ON b.TASK_FQN = m.FQN
                         WHERE m.FQN IS NOT NULL
                           AND m.FQN <> :root_fqn
                           AND m.STATE IS DISTINCT FROM 'started'
                           AND (b.STATE_BEFORE = 'started' OR b.TASK_FQN IS NULL);
                    EXCEPTION
                        WHEN OTHER THEN
                            children := ARRAY_CONSTRUCT();
                            graph_err := SQLERRM;
                    END;
                    IF (root_state IS DISTINCT FROM 'started' OR ARRAY_SIZE(children) > 0 OR graph_err IS NOT NULL)
                    THEN
                        pending := ARRAY_APPEND(pending, OBJECT_CONSTRUCT('root', root_fqn, 'state', root_state,
                                                                          'children', children, 'err', graph_err));
                    END IF;
                END FOR;
            END IF;
        ELSE
            SHOW TASKS IN DATABASE;
            SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('root', x.FQN, 'state', x.STATE, 'children', ARRAY_CONSTRUCT()))
                                WITHIN GROUP (ORDER BY x.FQN), ARRAY_CONSTRUCT())
              INTO :pending
              FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."'
                               || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""')
                               || '"' AS FQN,
                           LOWER(t."state") AS STATE,
                           REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]'
                               AS IS_ROOT,
                           NULLIF(GET(TRY_PARSE_JSON(GET(OBJECT_CONSTRUCT(*), 'task_relations')::VARCHAR),
                                      'FinalizedRootTask')::VARCHAR, '') AS FIN_OF
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS x
             WHERE x.STATE = 'started'
               AND x.IS_ROOT
               AND x.FIN_OF IS NULL;
        END IF;

        IF (ARRAY_SIZE(pending) = 0) THEN
            BREAK;
        END IF;

        -- What is (still) wrong, one entry per graph: root (state now) [members to resume] {graph read error}.
        wrong_txt := '';
        FOR p IN 0 TO ARRAY_SIZE(pending) - 1 DO
            item := GET(pending, p);
            children := COALESCE(GET(item, 'children')::ARRAY, ARRAY_CONSTRUCT());
            wrong_txt := wrong_txt || IFF(p > 0, '; ', '') || GET(item, 'root')::VARCHAR
                || ' (' || COALESCE(GET(item, 'state')::VARCHAR, '?') || ')'
                || IFF(ARRAY_SIZE(children) > 0, ' members ' || ARRAY_TO_STRING(children, ', '), '')
                || COALESCE(' graph read failed: ' || GET(item, 'err')::VARCHAR, '');
        END FOR;

        IF (attempt = 2) THEN
            msg := 'afterMigrate task restore (tasks_enabled=${tasks_enabled}) on ' || CURRENT_DATABASE() || ': '
                || ARRAY_SIZE(pending) || ' task graph(s) could not be ' || IFF(want_started, 'resumed', 'suspended')
                || ': ' || wrong_txt
                || IFF(errs = '', '', '. Errors:' || errs)
                || '. Migrations are applied; fix the task state by hand (docs/RUNBOOKS.md R9).';
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

        -- Act. "false": suspend the root. "true": suspend a started root that has members to resume (a child can be
        -- resumed only while its root is suspended), resume those members, resume the root last.
        n_acted := ARRAY_SIZE(pending);
        acted_txt := wrong_txt;
        FOR p IN 0 TO ARRAY_SIZE(pending) - 1 DO
            item := GET(pending, p);
            root_fqn := GET(item, 'root')::VARCHAR;
            root_state := GET(item, 'state')::VARCHAR;
            children := COALESCE(GET(item, 'children')::ARRAY, ARRAY_CONSTRUCT());
            stmts := ARRAY_CONSTRUCT();
            IF (NOT want_started) THEN
                stmts := ARRAY_APPEND(stmts, 'ALTER TASK ' || root_fqn || ' SUSPEND');
            ELSE
                IF (ARRAY_SIZE(children) > 0) THEN
                    IF (root_state = 'started') THEN
                        stmts := ARRAY_APPEND(stmts, 'ALTER TASK ' || root_fqn || ' SUSPEND');
                    END IF;
                    FOR j IN 0 TO ARRAY_SIZE(children) - 1 DO
                        stmts := ARRAY_APPEND(stmts, 'ALTER TASK ' || GET(children, j)::VARCHAR || ' RESUME');
                    END FOR;
                END IF;
                stmts := ARRAY_APPEND(stmts, 'ALTER TASK ' || root_fqn || ' RESUME');
            END IF;
            FOR k IN 0 TO ARRAY_SIZE(stmts) - 1 DO
                stmt := GET(stmts, k)::VARCHAR;
                BEGIN
                    EXECUTE IMMEDIATE :stmt;
                EXCEPTION
                    WHEN OTHER THEN
                        errs := errs || ' ' || stmt || ': ' || SQLERRM || ';';
                END;
            END FOR;
        END FOR;
    END FOR;

    IF (n_acted = 0) THEN
        RETURN 'deploy ${deploy_id}: no task graph to ' || verb;
    END IF;

    msg := LEFT(n_acted || ' task graph(s): ' || acted_txt, 4000);
    INSERT INTO FLYWAY.DEPLOY_LOG
        (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
    SELECT '${deploy_id}', :event_name, '${env}', '${git_sha}', '${build_url}', '${approver}', '${flyway:user}',
           CURRENT_TIMESTAMP(), :msg;
    RETURN 'deploy ${deploy_id}: ' || event_name || ' ' || msg;
END;
$$;
