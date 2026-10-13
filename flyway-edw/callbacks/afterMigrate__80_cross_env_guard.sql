-- Flyway callback: afterMigrate (runs only after a SUCCESSFUL migrate, after 60_task_restore, before 99_deploy_log).
-- What   : scans the stored definitions in the runway database - view, function and procedure bodies
--          (INFORMATION_SCHEMA.VIEWS / FUNCTIONS / PROCEDURES) and task bodies (SHOW TASKS "definition") - for
--          database names that start with ALFA_EDW_ (regular expression on the upper-cased text, then compared).
--          A name other than the edw_db placeholder (this database) or home_db (the runway a rehearsal clone was
--          taken from) is a cross-environment reference: the callback writes a FAILED row to FLYWAY.DEPLOY_LOG
--          listing up to 20 offending objects and raises, so the deploy goes red.
-- Why    : a view in DEV that reads the PRD database still "works" in DEV, and the same text promoted to PRD
--          reads DEV. Guard rule FG009 stops the literal in the repository; this catches what reached the database
--          anyway (a placeholder value, a hand-made object, a definition built by dynamic SQL in a procedure).
--          Same-database three-part names are allowed here: they are a clone hazard (a zero-copy clone would still
--          point at the source), reported by FG009 / docs/CONVENTIONS.md, not a cross-environment leak.
--          The migrations are already committed when this runs: the fix is a forward migration that rewrites the
--          object with SCHEMA.OBJECT names. afterMigrate__99 does not run after the raise, and Flyway does not
--          call afterMigrateError for an afterMigrate failure, which is why the FAILED row is written here first.
-- Owner  : EDW platform team (kit code; projects do not edit callbacks). Ticket: EDW-0000.
-- Notes  : cheap by design: INFORMATION_SCHEMA and SHOW TASKS of the session database only, no ACCOUNT_USAGE.
--          If one INFORMATION_SCHEMA query over the whole database is refused (Snowflake asks for more selective
--          predicates when an INFORMATION_SCHEMA result is too large), the scan is repeated schema by schema.
--          Only definitions the deploy role can read are scanned (it owns every object it deploys).
--          Pipes, streams, alerts and dynamic tables are covered by the repository checks (FG009, FG016).
--          A declared exception's message must be a literal, so the failure message is built into a one-line
--          anonymous block that EXECUTE IMMEDIATE runs; its error carries the message to the Flyway log.
--          Wrapped in EXECUTE IMMEDIATE with a dollar-quoted body (guard FG015). The pattern below has no
--          database name in it (code-review rule ALFA01 matches only a real name after the prefix).

EXECUTE IMMEDIATE $$
DECLARE
    hits ARRAY DEFAULT ARRAY_CONSTRUCT();
    part ARRAY DEFAULT ARRAY_CONSTRUCT();
    one_schema ARRAY DEFAULT ARRAY_CONSTRUCT();
    schema_names ARRAY DEFAULT ARRAY_CONSTRUCT();
    sname VARCHAR;
    allowed VARCHAR DEFAULT IFF(UPPER('${edw_db}') = UPPER('${home_db}'), UPPER('${edw_db}'),
                                UPPER('${edw_db}') || ' or ' || UPPER('${home_db}'));
    msg VARCHAR;
    raise_sql VARCHAR;
BEGIN
    -- 1. Task bodies. RESULT_SCAN must read the SHOW output in the very next statement.
    SHOW TASKS IN DATABASE;
    SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT()) INTO :hits
      FROM (SELECT 'TASK ' || t."schema_name" || '.' || t."name" || ' -> '
                   || LISTAGG(DISTINCT m.VALUE::VARCHAR, ',') AS HIT
              FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t,
                   LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(UPPER(t."definition"), 'ALFA_EDW_[A-Z0-9_]+')) AS m
             WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
             GROUP BY t."schema_name", t."name") AS x;

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
                       LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(UPPER(o.DEF), 'ALFA_EDW_[A-Z0-9_]+')) AS m
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
                SELECT COALESCE(ARRAY_AGG(x.HIT) WITHIN GROUP (ORDER BY x.HIT), ARRAY_CONSTRUCT())
                  INTO :one_schema
                  FROM (
                SELECT o.KIND || ' ' || o.SCHEMA_NAME || '.' || o.OBJECT_NAME || ' -> '
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
                       LATERAL FLATTEN(INPUT => REGEXP_SUBSTR_ALL(UPPER(o.DEF), 'ALFA_EDW_[A-Z0-9_]+')) AS m
                 WHERE m.VALUE::VARCHAR NOT IN (UPPER('${edw_db}'), UPPER('${home_db}'))
                 GROUP BY o.KIND, o.SCHEMA_NAME, o.OBJECT_NAME
                       ) AS x;
                part := ARRAY_CAT(part, one_schema);
            END FOR;
    END;
    hits := ARRAY_CAT(hits, part);

    IF (ARRAY_SIZE(hits) = 0) THEN
        RETURN 'cross-environment guard: no stored definition in ' || CURRENT_DATABASE()
            || ' names a database other than ' || allowed;
    END IF;

    msg := 'Cross-environment reference in ' || CURRENT_DATABASE() || ': ' || ARRAY_SIZE(hits)
        || ' object(s) name a database other than ' || allowed || ': '
        || ARRAY_TO_STRING(ARRAY_SLICE(hits, 0, 20), '; ')
        || IFF(ARRAY_SIZE(hits) > 20, '; ...', '')
        || '. Migrations are applied; fix forward with SCHEMA.OBJECT names (docs/CONVENTIONS.md).';
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
