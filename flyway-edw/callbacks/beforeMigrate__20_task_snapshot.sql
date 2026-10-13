-- Flyway callback: beforeMigrate (runs after beforeMigrate__10_deploy_log, before the first pending migration).
-- What   : records every task graph of the runway database whose ROOT is STARTED right now, member by member, in
--          FLYWAY.DEPLOY_TASK_STATE: the root, every dependent (INFORMATION_SCHEMA.TASK_DEPENDENTS, RECURSIVE) and the
--          root's finalizer task, one row each (DEPLOY_ID, quoted fully qualified TASK_FQN, STATE_BEFORE = its
--          lower-case state now, CAPTURED_TS, ROOT_FQN = the graph's root; a root row has TASK_FQN = ROOT_FQN).
--          Also checks that the tasks_enabled placeholder is exactly "true" or "false" and that the table has the
--          ROOT_FQN column (bootstrap/20 PART 2 adds it).
-- Why    : migrations may suspend a task graph to change it, a new or re-created member (CREATE OR REPLACE TASK)
--          starts suspended, and an ALTER TASK <root> RESUME resumes only the root. Snowflake DDL autocommits, so a
--          run can end with a started root whose member is suspended: every scheduled run then skips that member and
--          everything after it, without any error. afterMigrate__60_task_restore (tasks_enabled = "true") compares
--          every member of every graph recorded here with this snapshot and resumes what was started before (and new
--          members) but is suspended now; a member that was already suspended here (an operator skipping a step)
--          stays suspended, and so does a graph whose root was suspended (it is not recorded at all). After a FAILED
--          run, afterMigrateError__99 lists the recorded tasks still suspended (docs/RUNBOOKS.md R9).
--          A root has no predecessors (SHOW TASKS "predecessors" = []) and is not a finalizer (its "task_relations"
--          has no FinalizedRootTask): a finalizer also has no predecessors, but it follows its root. Only tasks this
--          deploy role can see are recorded.
--          The tasks_enabled check runs here, before anything is applied, because afterMigrate__60 suspends every
--          root when the value is not "true": a typo such as "TRUE" must stop the deploy, not stop production.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : SHOW TASKS IN DATABASE uses the session database (the runway). Its output is read with
--          RESULT_SCAN(LAST_QUERY_ID()) by the very next statement, so nothing may run between the two. SHOW output
--          columns are lower-case and must be double-quoted ("name", "schema_name", "state", "predecessors").
--          task_relations is read through OBJECT_CONSTRUCT(*) (NULL where a release lacks it), and the
--          TASK_DEPENDENTS columns case-insensitively (GET_IGNORE_CASE), so neither depends on column-name case.
--          Verify on first run (MGM) that TASK_DEPENDENTS accepts the quoted name and that the finalizer's
--          FinalizedRootTask names its root; a failure here stops the deploy with nothing applied.
--          TASK_FQN is "DATABASE"."SCHEMA"."TASK" with embedded double quotes doubled: exact for any name, and the
--          form afterMigrate__60 passes to ALTER TASK. Rows of one run are those with its DEPLOY_ID captured at or
--          after its STARTED row (manual runs all share DEPLOY_ID "manual").
--          A declared exception's message must be a literal, so a graph-read failure is raised from a one-line
--          anonymous block that EXECUTE IMMEDIATE runs; its error carries the message to the Flyway log.
--          The block is wrapped in EXECUTE IMMEDIATE with a dollar-quoted body (guard FG015).

EXECUTE IMMEDIATE $$
DECLARE
    bad_tasks_enabled EXCEPTION (-20120,
        'beforeMigrate: placeholder tasks_enabled must be exactly true or false (flyway.toml); nothing was applied.');
    no_root_column EXCEPTION (-20121,
        'beforeMigrate: FLYWAY.DEPLOY_TASK_STATE lacks ROOT_FQN: run bootstrap/20 PART 2; nothing was applied.');
    n_col INTEGER;
    roots ARRAY DEFAULT ARRAY_CONSTRUCT();
    item VARIANT;
    root_fqn VARCHAR;
    fin_fqn VARCHAR;
    fin_state VARCHAR;
    recorded INTEGER DEFAULT 0;
    msg VARCHAR;
    raise_sql VARCHAR;
BEGIN
    IF ('${tasks_enabled}' <> 'true' AND '${tasks_enabled}' <> 'false') THEN
        RAISE bad_tasks_enabled;
    END IF;

    SELECT COUNT(*) INTO :n_col
      FROM INFORMATION_SCHEMA.COLUMNS AS c
     WHERE c.TABLE_SCHEMA = 'FLYWAY' AND c.TABLE_NAME = 'DEPLOY_TASK_STATE' AND c.COLUMN_NAME = 'ROOT_FQN';
    IF (n_col = 0) THEN
        RAISE no_root_column;
    END IF;

    -- The started roots, each with its finalizer (if any). Grouping on the root's unquoted upper-case name pairs a
    -- root with the finalizer whose FinalizedRootTask names it.
    SHOW TASKS IN DATABASE;
    SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('root', g.ROOT_FQN, 'fin', g.FIN_FQN, 'fin_state', g.FIN_STATE))
                        WITHIN GROUP (ORDER BY g.ROOT_FQN), ARRAY_CONSTRUCT())
      INTO :roots
      FROM (SELECT MAX(IFF(x.FIN_OF IS NULL AND x.IS_ROOT AND x.STATE = 'started', x.FQN, NULL)) AS ROOT_FQN,
                   MAX(IFF(x.FIN_OF IS NULL, NULL, x.FQN)) AS FIN_FQN,
                   MAX(IFF(x.FIN_OF IS NULL, NULL, x.STATE)) AS FIN_STATE
              FROM (SELECT '"' || REPLACE(t."database_name", '"', '""') || '"."' || REPLACE(t."schema_name", '"', '""')
                               || '"."' || REPLACE(t."name", '"', '""') || '"' AS FQN,
                           LOWER(t."state") AS STATE,
                           REGEXP_REPLACE(COALESCE(t."predecessors"::VARCHAR, '[]'), '[[:space:]]', '') = '[]'
                               AS IS_ROOT,
                           NULLIF(UPPER(REPLACE(GET(TRY_PARSE_JSON(GET(OBJECT_CONSTRUCT(*),
                               'task_relations')::VARCHAR), 'FinalizedRootTask')::VARCHAR, '"', '')), '') AS FIN_OF
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS x
             GROUP BY COALESCE(x.FIN_OF, UPPER(REPLACE(x.FQN, '"', '')))) AS g
     WHERE g.ROOT_FQN IS NOT NULL;

    -- Every member of each started graph, with its own state now (MIN: 'started' wins over 'suspended' when the
    -- root appears both from TASK_DEPENDENTS and as itself).
    IF (ARRAY_SIZE(roots) > 0) THEN
        FOR i IN 0 TO ARRAY_SIZE(roots) - 1 DO
            item := GET(roots, i);
            root_fqn := GET(item, 'root')::VARCHAR;
            fin_fqn := GET(item, 'fin')::VARCHAR;
            fin_state := GET(item, 'fin_state')::VARCHAR;
            BEGIN
                INSERT INTO FLYWAY.DEPLOY_TASK_STATE (DEPLOY_ID, TASK_FQN, STATE_BEFORE, CAPTURED_TS, ROOT_FQN)
                SELECT '${deploy_id}', m.FQN, MIN(m.STATE), CURRENT_TIMESTAMP(), :root_fqn
                  FROM (SELECT '"' || REPLACE(GET_IGNORE_CASE(d.O, 'database_name')::VARCHAR, '"', '""') || '"."'
                                   || REPLACE(GET_IGNORE_CASE(d.O, 'schema_name')::VARCHAR, '"', '""') || '"."'
                                   || REPLACE(GET_IGNORE_CASE(d.O, 'name')::VARCHAR, '"', '""') || '"' AS FQN,
                               LOWER(GET_IGNORE_CASE(d.O, 'state')::VARCHAR) AS STATE
                          FROM (SELECT OBJECT_CONSTRUCT(*) AS O
                                  FROM TABLE(INFORMATION_SCHEMA.TASK_DEPENDENTS(TASK_NAME => :root_fqn,
                                                                                RECURSIVE => TRUE))) AS d
                        UNION ALL
                        SELECT :root_fqn, 'started'
                        UNION ALL
                        SELECT :fin_fqn, :fin_state) AS m
                 WHERE m.FQN IS NOT NULL
                 GROUP BY m.FQN;
                recorded := recorded + SQLROWCOUNT;
            EXCEPTION
                WHEN OTHER THEN
                    msg := LEFT('beforeMigrate task snapshot: could not record the graph of ' || root_fqn || ' ('
                        || SQLERRM || '). Nothing was applied; see the Notes in callbacks/beforeMigrate__20.', 1800);
                    raise_sql := 'DECLARE snapshot_failed EXCEPTION (-20122, '''
                        || REPLACE(REPLACE(msg, '\\', '\\\\'), '''', '''''')
                        || '''); BEGIN RAISE snapshot_failed; END;';
                    EXECUTE IMMEDIATE :raise_sql;
            END;
        END FOR;
    END IF;

    RETURN 'deploy ${deploy_id}: ' || recorded || ' task(s) of ' || ARRAY_SIZE(roots)
        || ' started task graph(s) recorded';
END;
$$;
