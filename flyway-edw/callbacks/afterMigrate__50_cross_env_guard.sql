-- Flyway callback: afterMigrate (runs only after a SUCCESSFUL migrate; first of 50_cross_env_guard, 60_task_restore,
--                  99_deploy_log, so it runs BEFORE the task restore resumes anything).
-- What   : scans the stored definitions in the runway database for database QUALIFIERS that start with ALFA_EDW_:
--          view, function and procedure bodies (INFORMATION_SCHEMA.VIEWS / FUNCTIONS / PROCEDURES), task bodies and
--          WHEN conditions (SHOW TASKS "definition" / "condition"), dynamic-table queries (SHOW DYNAMIC TABLES "text")
--          and alert conditions and actions (SHOW ALERTS "condition" / "action"). A qualifier is ALFA_EDW_<X>, quoted
--          or not, followed by '.' (ALFA_EDW_<X>.SCHEMA..., "ALFA_EDW_<X>"."SCHEMA"...). SQL comments in the text
--          (-- to end of line, /* ... */) are removed first, and a name not followed by '.' (a column such as
--          ALFA_EDW_LOAD_TS, a string such as CURRENT_DATABASE() = '<name>') is not a qualifier. A qualifier other
--          than the edw_db placeholder (this database) or home_db (the runway a rehearsal clone was taken from) is a
--          cross-environment reference: the callback writes a FAILED row to FLYWAY.DEPLOY_LOG listing up to 20
--          offending objects and raises, so the deploy goes red.
-- Why    : a view in DEV that reads the PRD database still "works" in DEV, and the same text promoted to PRD
--          reads DEV. Guard rule FG009 stops the literal in the repository; this catches what reached the database
--          anyway (a placeholder value, a hand-made object, a definition built by dynamic SQL in a procedure, which
--          is why string literals ARE scanned: 'ALFA_EDW_<X>.SALES.T' in dynamic SQL is a qualifier).
--          Same-database three-part names are allowed here: they are a clone hazard (a zero-copy clone would still
--          point at the source), reported by FG009 / docs/CONVENTIONS.md, not a cross-environment leak.
--          It runs before afterMigrate__60_task_restore on purpose: when it raises, the task restore does not run, so
--          a graph this run suspended (or re-created) stays suspended like after a failed migration, instead of
--          being resumed with a definition that reads or writes another runway. The FAILED row names those tasks
--          (resume them after the fix: docs/RUNBOOKS.md R9). A flagged TASK whose graph is still started (not
--          touched by this run) keeps running: suspend its root by hand (RUNBOOKS R10, contain).
--          The migrations are already committed when this runs: the fix is a forward migration that rewrites the
--          object with SCHEMA.OBJECT names. afterMigrate__60 and __99 do not run after the raise, and Flyway does
--          not call afterMigrateError for an afterMigrate failure, which is why the FAILED row is written here first.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : cheap by design: INFORMATION_SCHEMA and SHOW ... IN DATABASE of the session database only, no ACCOUNT_USAGE.
--          If one INFORMATION_SCHEMA query over the whole database is refused (Snowflake asks for more selective
--          predicates when an INFORMATION_SCHEMA result is too large), the scan is repeated schema by schema.
--          Only definitions the deploy role can read are scanned (it owns every object it deploys). A dynamic-table
--          or alert scan that fails is named in the return value; it does not fail the deploy.
--          Pipes and streams are covered by the repository checks (FG009, FG016).
--          Each SHOW is read with RESULT_SCAN(LAST_QUERY_ID()) by the very next statement; columns that differ
--          between Snowflake releases are read through OBJECT_CONSTRUCT(*) (NULL when absent).
--          A declared exception's message must be a literal, so the failure message is built into a one-line
--          anonymous block that EXECUTE IMMEDIATE runs; its error carries the message to the Flyway log.
--          Wrapped in EXECUTE IMMEDIATE with a dollar-quoted body (guard FG015). The patterns below have no
--          database name in them (code-review rule ALFA01 matches only a real name after the prefix).

EXECUTE IMMEDIATE $$
DECLARE
    -- a database qualifier: optional quote, the name (group 2), optional quote, optional blanks, then '.'
    qual VARCHAR DEFAULT '(^|[^A-Z0-9_$])"?(ALFA_EDW_[A-Z0-9_]+)"?[[:space:]]*[.]';
    block_cmt VARCHAR DEFAULT '/[*]([^*]|[*]+[^*/])*[*]+/';
    line_cmt VARCHAR DEFAULT '--[^\n]*';
    hits ARRAY DEFAULT ARRAY_CONSTRUCT();
    part ARRAY DEFAULT ARRAY_CONSTRUCT();
    one_schema ARRAY DEFAULT ARRAY_CONSTRUCT();
    schema_names ARRAY DEFAULT ARRAY_CONSTRUCT();
    sname VARCHAR;
    allowed VARCHAR DEFAULT IFF(UPPER('${edw_db}') = UPPER('${home_db}'), UPPER('${edw_db}'),
                                UPPER('${edw_db}') || ' or ' || UPPER('${home_db}'));
    notes VARCHAR DEFAULT '';
    started_ts TIMESTAMP_LTZ;
    stranded VARCHAR DEFAULT '';
    msg VARCHAR;
    raise_sql VARCHAR;
BEGIN
    -- 1. Task bodies and WHEN conditions. RESULT_SCAN must read the SHOW output in the very next statement.
    SHOW TASKS IN DATABASE;
    SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT()) INTO :hits
      FROM (SELECT 'TASK ' || q.SCHEMA_NAME || '.' || q.OBJECT_NAME || ' -> '
                   || LISTAGG(DISTINCT m.VALUE::VARCHAR, ',') AS HIT
              FROM (SELECT t."schema_name" AS SCHEMA_NAME, t."name" AS OBJECT_NAME,
                           REGEXP_REPLACE(REGEXP_REPLACE(UPPER(COALESCE(t."definition"::VARCHAR, '') || CHR(10)
                               || COALESCE(GET(OBJECT_CONSTRUCT(*), 'condition')::VARCHAR, '')),
                               :block_cmt, ' '), :line_cmt, ' ') AS DEF
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS q,
                   LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(q.DEF, :qual, 1, 1, 'e', 2)) AS m
             WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
             GROUP BY q.SCHEMA_NAME, q.OBJECT_NAME) AS x;

    -- 2. View, function and procedure bodies: one query for the whole database ...
    BEGIN
        SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT()) INTO :part
          FROM (SELECT o.KIND || ' ' || o.SCHEMA_NAME || '.' || o.OBJECT_NAME || ' -> '
                       || LISTAGG(DISTINCT m.VALUE::VARCHAR, ',') AS HIT
                  FROM (SELECT 'VIEW' AS KIND, v.TABLE_SCHEMA AS SCHEMA_NAME, v.TABLE_NAME AS OBJECT_NAME,
                               v.VIEW_DEFINITION AS DEF
                          FROM INFORMATION_SCHEMA.VIEWS AS v
                         WHERE v.TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
                        UNION ALL
                        SELECT 'FUNCTION', f.FUNCTION_SCHEMA, f.FUNCTION_NAME || f.ARGUMENT_SIGNATURE,
                               f.FUNCTION_DEFINITION
                          FROM INFORMATION_SCHEMA.FUNCTIONS AS f
                        UNION ALL
                        SELECT 'PROCEDURE', p.PROCEDURE_SCHEMA, p.PROCEDURE_NAME || p.ARGUMENT_SIGNATURE,
                               p.PROCEDURE_DEFINITION
                          FROM INFORMATION_SCHEMA.PROCEDURES AS p) AS o,
                       LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(
                           REGEXP_REPLACE(REGEXP_REPLACE(UPPER(o.DEF), :block_cmt, ' '), :line_cmt, ' '),
                           :qual, 1, 1, 'e', 2)) AS m
                 WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
                 GROUP BY o.KIND, o.SCHEMA_NAME, o.OBJECT_NAME) AS x;
    EXCEPTION
        -- ... or, when Snowflake refuses that as too large, the same query one schema at a time.
        WHEN STATEMENT_ERROR THEN
            part := ARRAY_CONSTRUCT();
            SELECT COALESCE(ARRAY_AGG(s.SCHEMA_NAME), ARRAY_CONSTRUCT()) INTO :schema_names
              FROM INFORMATION_SCHEMA.SCHEMATA AS s
             WHERE s.SCHEMA_NAME <> 'INFORMATION_SCHEMA';
            FOR i IN 0 TO ARRAY_SIZE(schema_names) - 1 DO
                sname := GET(schema_names, i)::VARCHAR;
                SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT()) INTO :one_schema
                  FROM (SELECT o.KIND || ' ' || o.SCHEMA_NAME || '.' || o.OBJECT_NAME || ' -> '
                               || LISTAGG(DISTINCT m.VALUE::VARCHAR, ',') AS HIT
                          FROM (SELECT 'VIEW' AS KIND, v.TABLE_SCHEMA AS SCHEMA_NAME, v.TABLE_NAME AS OBJECT_NAME,
                                       v.VIEW_DEFINITION AS DEF
                                  FROM INFORMATION_SCHEMA.VIEWS AS v
                                 WHERE v.TABLE_SCHEMA = :sname
                                UNION ALL
                                SELECT 'FUNCTION', f.FUNCTION_SCHEMA, f.FUNCTION_NAME || f.ARGUMENT_SIGNATURE,
                                       f.FUNCTION_DEFINITION
                                  FROM INFORMATION_SCHEMA.FUNCTIONS AS f
                                 WHERE f.FUNCTION_SCHEMA = :sname
                                UNION ALL
                                SELECT 'PROCEDURE', p.PROCEDURE_SCHEMA, p.PROCEDURE_NAME || p.ARGUMENT_SIGNATURE,
                                       p.PROCEDURE_DEFINITION
                                  FROM INFORMATION_SCHEMA.PROCEDURES AS p
                                 WHERE p.PROCEDURE_SCHEMA = :sname) AS o,
                               LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(
                                   REGEXP_REPLACE(REGEXP_REPLACE(UPPER(o.DEF), :block_cmt, ' '), :line_cmt, ' '),
                                   :qual, 1, 1, 'e', 2)) AS m
                         WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
                         GROUP BY o.KIND, o.SCHEMA_NAME, o.OBJECT_NAME) AS x;
                part := ARRAY_CAT(part, one_schema);
            END FOR;
    END;
    hits := ARRAY_CAT(hits, part);

    -- 3. Dynamic-table queries (layer 40).
    BEGIN
        SHOW DYNAMIC TABLES IN DATABASE;
        SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT()) INTO :part
          FROM (SELECT 'DYNAMIC TABLE ' || q.SCHEMA_NAME || '.' || q.OBJECT_NAME || ' -> '
                       || LISTAGG(DISTINCT m.VALUE::VARCHAR, ',') AS HIT
                  FROM (SELECT t."schema_name" AS SCHEMA_NAME, t."name" AS OBJECT_NAME,
                               REGEXP_REPLACE(REGEXP_REPLACE(UPPER(COALESCE(GET(OBJECT_CONSTRUCT(*), 'text')::VARCHAR,
                                   '')), :block_cmt, ' '), :line_cmt, ' ') AS DEF
                          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS q,
                       LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(q.DEF, :qual, 1, 1, 'e', 2)) AS m
                 WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
                 GROUP BY q.SCHEMA_NAME, q.OBJECT_NAME) AS x;
        hits := ARRAY_CAT(hits, part);
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '; dynamic tables NOT scanned (' || SQLERRM || ')';
    END;

    -- 4. Alert conditions and actions (layer 85).
    BEGIN
        SHOW ALERTS IN DATABASE;
        SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT()) INTO :part
          FROM (SELECT 'ALERT ' || q.SCHEMA_NAME || '.' || q.OBJECT_NAME || ' -> '
                       || LISTAGG(DISTINCT m.VALUE::VARCHAR, ',') AS HIT
                  FROM (SELECT t."schema_name" AS SCHEMA_NAME, t."name" AS OBJECT_NAME,
                               REGEXP_REPLACE(REGEXP_REPLACE(UPPER(
                                   COALESCE(GET(OBJECT_CONSTRUCT(*), 'condition')::VARCHAR, '') || CHR(10)
                                   || COALESCE(GET(OBJECT_CONSTRUCT(*), 'action')::VARCHAR, '')),
                                   :block_cmt, ' '), :line_cmt, ' ') AS DEF
                          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t) AS q,
                       LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(q.DEF, :qual, 1, 1, 'e', 2)) AS m
                 WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
                 GROUP BY q.SCHEMA_NAME, q.OBJECT_NAME) AS x;
        hits := ARRAY_CAT(hits, part);
    EXCEPTION
        WHEN OTHER THEN
            notes := notes || '; alerts NOT scanned (' || SQLERRM || ')';
    END;

    IF (ARRAY_SIZE(hits) = 0) THEN
        RETURN 'cross-environment guard: no stored definition in ' || CURRENT_DATABASE()
            || ' names a database other than ' || allowed || notes;
    END IF;

    -- afterMigrate__60_task_restore does not run after this raise: name the tasks this run left suspended.
    IF ('${tasks_enabled}' = 'true') THEN
        BEGIN
            SELECT MAX(l.EVENT_TS) INTO :started_ts
              FROM FLYWAY.DEPLOY_LOG AS l
             WHERE l.DEPLOY_ID = '${deploy_id}'
               AND l.EVENT = 'STARTED';
            SHOW TASKS IN DATABASE;
            SELECT COALESCE(LISTAGG(DISTINCT s.TASK_FQN || IFF(s.TASK_FQN = s.ROOT_FQN, ' (root)', ''), ', '), '')
              INTO :stranded
              FROM FLYWAY.DEPLOY_TASK_STATE AS s
              JOIN TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                ON s.TASK_FQN = '"' || REPLACE(t."database_name", '"', '""') || '"."'
                       || REPLACE(t."schema_name", '"', '""') || '"."' || REPLACE(t."name", '"', '""') || '"'
             WHERE s.DEPLOY_ID = '${deploy_id}'
               AND s.CAPTURED_TS >= :started_ts
               AND s.STATE_BEFORE = 'started'
               AND LOWER(t."state") <> 'started';
        EXCEPTION
            WHEN OTHER THEN
                stranded := '(could not be determined: ' || SQLERRM || ')';
        END;
    END IF;

    msg := 'Cross-environment reference in ' || CURRENT_DATABASE() || ': ' || ARRAY_SIZE(hits)
        || ' object(s) name a database other than ' || allowed || ': '
        || LEFT(ARRAY_TO_STRING(ARRAY_SLICE(hits, 0, 20), '; ') || IFF(ARRAY_SIZE(hits) > 20, '; ...', ''), 900)
        || '. Migrations are applied; the task restore (afterMigrate__60) did not run'
        || IFF(stranded = '', '', '; task(s) started before this run and suspended now (resume after the fix, '
                                  || 'RUNBOOKS R9): ' || LEFT(stranded, 500))
        || '. Fix forward with SCHEMA.OBJECT names (docs/CONVENTIONS.md, RUNBOOKS R10)' || notes || '.';
    msg := LEFT(msg, 1800);
    INSERT INTO FLYWAY.DEPLOY_LOG
        (DEPLOY_ID, EVENT, ENV, GIT_SHA, BUILD_URL, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL)
    SELECT '${deploy_id}', 'FAILED', '${env}', '${git_sha}', '${build_url}', '${approver}', '${flyway:user}',
           CURRENT_TIMESTAMP(), :msg;
    raise_sql := 'DECLARE cross_env_reference EXCEPTION (-20180, '''
        || REPLACE(REPLACE(msg, '\\', '\\\\'), '''', '''''')
        || '''); BEGIN RAISE cross_env_reference; END;';
    EXECUTE IMMEDIATE :raise_sql;
END;
$$;
