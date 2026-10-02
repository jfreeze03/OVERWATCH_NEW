-- V171__ops_selfwatch_digest_refgaps_seed.sql
--
-- Round-2 ops / data-quality fixes: the canary sentinel says what it can see, the morning digest reports the
-- complete days it claims (warehouse compute, labelled), one failing reference-gap check no longer silences the
-- others, and the deploy-gate switch validate.sql reads gets its SETTINGS row.
--
-- WHY:
--   R2-026  SP_CANARY_SENTINEL (V017) probes each source with SELECT 1, which names no column, yet its
--           OPS_CANARY_FAIL detail blamed ACCOUNT_USAGE column drift; and its render-SLA handler always logged
--           'APP_USAGE.RENDER_MS not readable', whatever actually failed. Its OPS_SLOW_RENDER title printed the
--           p95 as raw seconds (95.5s), against the owner rule that every duration reads in Hr/Min/Sec.
--   R1-228  SP_DAILY_DIGEST (V165) read the exec board's 7-day KPI rows, which are today-INCLUSIVE (seven full days
--           plus today so far: about 7.25 days at the 07:20 run), and called warehouse metering plain spend in the
--           facts, the prompt and the template.
--   CORTEX  SP_DAILY_DIGEST fell back to the default model only on NULL: a blank, padded, mixed-case or invalid
--           CORTEX_MODEL value went straight into COMPLETE, so the scheduled digest ran a different model from the
--           app (app.core.ai.normalize_model) or failed to the template.
--   R2-019  SP_SCAN_REF_GAPS (V129) ran every configured check in ONE INSERT after its DELETE: one check that
--   R2-104  throws (a missing grant, a renamed table) blanked ETL_REF_GAP_RESULTS for all of them and the HIGH
--           PIPE_REF_GAP page went silent; and its MINUS operands were uncast, so a NUMBER or DATE code column
--           forced a numeric coercion of the VARCHAR XLAT value (the app twin casts both since v4.527).
--   SEED    CREDIT_PRICE_OVERRIDE (read only by snowflake/validate.sql) had no SETTINGS row.
--
--   + SETTINGS row CREDIT_PRICE_OVERRIDE = 'FALSE', WHEN NOT MATCHED only: an existing TRUE override (a contracted
--     non-3.68 rate) is never touched, and FALSE is outside validate's TRUE / Y / YES / 1 list, so the -20013
--     gate stays armed exactly as an absent row did.
--   ~ SP_CANARY_SENTINEL re-derived from V017 (its current definer; V016 has no render-SLA block), byte-identical
--     except K1 the OPS_CANARY_FAIL detail (a missing or renamed object or lost access, never column drift),
--     K2 the render-SLA handler logs 'render SLA check failed: ' plus the error, and K3 the OPS_SLOW_RENDER title
--     shows the p95 in Hr/Min/Sec like the app (1m 36s, not 95.5s; METRIC_VALUE and the threshold compare stay
--     in seconds). Kept: the 24 probes, fails, the 180-day purge and RETURN 'sentinel v2: '.
--   ~ SP_DAILY_DIGEST re-derived from V165, byte-identical except C1 the CORTEX_MODEL read normalized like the
--     app (trimmed, lower-case, a valid name else llama3.1-8b); E1 the facts cover the 7 complete days ending
--     yesterday (spend = the board's ALL / 7-day DAILY_SPEND rows for those days, queries from FACT_QUERY_DAILY,
--     task runs from FACT_TASK_DAILY); E2 the keys WAREHOUSE_SPEND_USD and WAREHOUSE_CREDITS (they still bind
--     through the _USD suffix and the CREDIT substring); E3 / E4 the prompt and the template name the window and
--     say warehouse compute only, never total spend. The grounding check is unchanged.
--   ~ SP_SCAN_REF_GAPS re-derived from V129, byte-identical except R1 the per-check DECLAREs and all_failed
--     (-20662); R2 one MINUS statement per check with both operands TO_VARCHAR; R3 one EXECUTE IMMEDIATE per check
--     in its own EXCEPTION block (ref_gap_check_failed names the check), all_failed raised only when every check
--     failed, RETURN 'ref-gap scan complete (N ok, M failed)'. The name allowlist line is unchanged.
--
-- COST: the digest reads three small day-grain ranges (the board and two app-owned facts, 7 days) instead of one
-- board read: no ACCOUNT_USAGE, same one Cortex call. The ref-gap scan runs one statement per configured check
-- (a handful) instead of one per run. The canary probes are unchanged (the title CASE is per slow page).
-- LATENCY: unchanged; TASK_DAILY_DIGEST runs 07:20 America/Chicago, the canary Mondays 05:30, the ref-gap scan
-- inside the daily alert scan.
-- FIRST RUN: nothing runs at apply time (a digest CALL spends Cortex credits and posts to Teams). The next 07:20
-- digest writes the first complete-day row; the next daily alert scan runs the isolated ref-gap checks.
-- ROLLBACK: re-run each base CREATE PROCEDURE: SP_CANARY_SENTINEL from V017__hardening_v7.sql, SP_DAILY_DIGEST
-- from V165__daily_digest_grounding.sql, SP_SCAN_REF_GAPS from V129__pipe_ref_gap_alert.sql. The FALSE SETTINGS
-- row can stay (validate reads it as no override).
-- Apply AFTER V170. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20171, 'V171 requires V170 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 170) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- CREDIT-PRICE-SEED: the deploy-gate switch snowflake/validate.sql reads (an absent row already read FALSE), so
-- Admin > Settings lists it and app/config.py can carry it in DEFAULT_SETTINGS. WHEN NOT MATCHED only: an existing
-- override (TRUE for a contracted non-3.68 rate) is never touched, and FALSE is outside validate's TRUE / Y / YES /
-- 1 list, so this seed can never silence -20013. To run a non-default rate, UPDATE this row (or use Admin): never
-- a second INSERT (SETTINGS.KEY is an unenforced primary key).
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('CREDIT_PRICE_OVERRIDE', 'FALSE')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- >>> derived:SP_CANARY_SENTINEL  (from V017; OPS_CANARY_FAIL detail no longer blames column drift + render-SLA handler logs SQLERRM + OPS_SLOW_RENDER title in Hr/Min/Sec, V171)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CANARY_SENTINEL()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    checks ARRAY DEFAULT [
        'SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS',
        'SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.USERS',
        'SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS',
        'SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES',
        'SNOWFLAKE.ACCOUNT_USAGE.ROLES',
        'SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS',
        'SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS',
        'SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY',
        'SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY',
        'DBA_MAINT_DB.OVERWATCH.SETTINGS',
        'DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY',
        'DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD',
        'DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG'
    ];
    cname VARCHAR;
    emsg VARCHAR;
    fails INT DEFAULT 0;
    i INT;
BEGIN
    FOR i IN 0 TO ARRAY_SIZE(:checks) - 1 DO
        cname := GET(:checks, i)::VARCHAR;
        BEGIN
            EXECUTE IMMEDIATE 'SELECT 1 FROM ' || :cname || ' LIMIT 1';
            INSERT INTO DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS (CHECK_NAME, STATUS)
            SELECT :cname, 'PASS';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                fails := fails + 1;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS (CHECK_NAME, STATUS, ERROR)
                SELECT :cname, 'FAIL', LEFT(:emsg, 500);
        END;
    END FOR;

    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' source dependency check(s) failing',
               'CANARY_RESULTS.ERROR holds the error for each failing object. This probe (SELECT 1) ' ||
                   'only sees a missing or renamed object or lost access (a revoked grant, or ' ||
                   'IMPORTED PRIVILEGES on SNOWFLAKE); it cannot see column drift. Run the Admin ' ||
                   'canary for the per-builder picture.',
               :fails,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_CANARY_FAIL' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;


    -- Render-time SLA (guarded): p95 first-paint per page from APP_USAGE.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               -- K3: V171 - the p95 in Hr/Min/Sec like the app (formulas.humanize_duration; the HD template the
               -- V172 change scans use). HALF_TO_EVEN needs a fixed-point operand, hence the NUMBER(18, 1) cast
               -- (P95_S is already rounded to 0.1 s). METRIC_VALUE and the THRESHOLD_NUM compare stay in seconds.
               r.PAGE || ' p95 first paint '
               || CASE WHEN (r.P95_S::NUMBER(18, 1)) IS NULL THEN '?'
                       WHEN ROUND((r.P95_S::NUMBER(18, 1)) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (r.P95_S::NUMBER(18, 1)) < 1 THEN ROUND((r.P95_S::NUMBER(18, 1)) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (r.P95_S::NUMBER(18, 1)) < 10 THEN TO_VARCHAR(ROUND((r.P95_S::NUMBER(18, 1)), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((r.P95_S::NUMBER(18, 1)), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' (7d, n=' || r.N || ')',
               'Persisted first-paint times (APP_USAGE.RENDER_MS). Admin > Performance ' ||
                   'shows the slow statement families; lazy sections and run_batch are the levers.',
               r.P95_S,
               c.RULE_ID || '|' || r.PAGE || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT PAGE, ROUND(APPROX_PERCENTILE(RENDER_MS, 0.95) / 1000, 1) AS P95_S,
                   COUNT(*) AS N
            FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE
            WHERE AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
              AND RENDER_MS IS NOT NULL
            GROUP BY 1
            HAVING COUNT(*) >= 20
        ) r ON c.RULE_ID = 'OPS_SLOW_RENDER' AND c.ENABLED AND r.P95_S > c.THRESHOLD_NUM
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || r.PAGE || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;   -- K2: V171 R2-026 - log the real cause (the probe loop is done, emsg is free)
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'CanarySentinel', 'render_check_unavailable',
                   'render SLA check failed: ' || LEFT(:emsg, 500), 'source probes unaffected', CURRENT_ROLE();
    END;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS
     WHERE RUN_AT < DATEADD('day', -180, CURRENT_TIMESTAMP());

    RETURN 'sentinel v2: ' || :fails || ' failure(s)';
END;
$$;

-- >>> derived:SP_DAILY_DIGEST  (from V165; 7 complete days to yesterday + warehouse compute spend keys and wording + CORTEX_MODEL normalized like the app, V171)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    model VARCHAR;
    facts VARCHAR;
    alerts VARCHAR;
    prompt VARCHAR;
    body VARCHAR;
    -- D1 >>> V165 #24: the facts as numbers, the measured grounding and the version sent
    f_spend_usd NUMBER(38,2);
    f_credits NUMBER(38,2);
    f_queries NUMBER(38,0);
    f_failed_q NUMBER(38,0);
    f_queued_min NUMBER(38,1);
    f_spill_gb NUMBER(38,2);
    f_task_runs NUMBER(38,0);
    f_task_fail NUMBER(38,0);
    f_failed_q_pct NUMBER(38,2);
    f_task_fail_pct NUMBER(38,2);
    a_open_crit NUMBER(38,0);
    a_open_high NUMBER(38,0);
    a_raised_24h NUMBER(38,0);
    ai_err VARCHAR;
    ai_body VARCHAR;
    clean VARCHAR;
    n_checked INT;
    n_bad INT DEFAULT 0;
    ungrounded VARCHAR;
    grounding_ok BOOLEAN;
    body_source VARCHAR DEFAULT 'AI';
    msg VARCHAR;
    -- <<< D1
    routes_total INT DEFAULT 0;   -- V070 #23: M = enabled routes walked
    routes_sent INT DEFAULT 0;    -- V070 #23: N = routes the digest reached
    emsg VARCHAR;
    r_route_id VARCHAR;
    r_integration VARCHAR;
    c_routes CURSOR FOR
        SELECT r.ROUTE_ID, r.INTEGRATION_NAME
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED AND r.DELIVER_DIGEST   -- V070 #11: only digest-eligible routes
          AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'   -- alerting-hunt: never send the exec digest to a CRITICAL-only (paging) route (DELIVER_DIGEST defaults TRUE, and Snowflake cannot ALTER that default)
        ORDER BY r.ROUTE_ID;
BEGIN
    -- C1: V171 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a valid
    -- name else the default): a blank, padded, mixed-case or invalid stored value no longer reaches COMPLETE
    SELECT IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')
      INTO :model
    FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);

    -- D2 >>> V165 #24: FACTS are named numbers with one unit per key (*_USD dollars, *_PCT percent, the rest
    -- counts, credits, minutes or GB), so the draft's figures can be checked against them. V007-V112 sent
    -- 'CREDITS=<dollars>' (COALESCE(VALUE_USD, VALUE) under the CREDITS metric); spend now has its own key.
    -- E1 >>> V171 R1-228: the 7 COMPLETE days ending yesterday (the still-filling partial day of today left
    -- out, the Overview Spend, last N days convention). The board 7-day KPI rows are today-INCLUSIVE (DAY >=
    -- today-7: seven full days plus today so far), so they are no longer read. Spend is the board ALL / 7-day
    -- DAILY_SPEND rows for those days (warehouse metering at CREDIT_PRICE_USD; one row per day once COMPANY and
    -- WINDOW_DAYS are pinned); queries and task runs come from the facts the board aggregates, same days.
    SELECT SUM(VALUE_USD), SUM(VALUE)
      INTO :f_spend_usd, :f_credits
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE PANEL = 'DAILY_SPEND' AND METRIC = 'CREDITS' AND COMPANY = 'ALL' AND WINDOW_DAYS = 7
      AND PERIOD_START >= DATEADD('day', -7, CURRENT_DATE()) AND PERIOD_START < CURRENT_DATE();
    SELECT SUM(QUERY_COUNT), SUM(FAILED_COUNT), ROUND(SUM(QUEUED_SEC_SUM) / 60, 1), ROUND(SUM(SPILL_REMOTE_GB), 2)
      INTO :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb
    FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
    WHERE DAY >= DATEADD('day', -7, CURRENT_DATE()) AND DAY < CURRENT_DATE();
    SELECT SUM(RUNS), SUM(FAILED)
      INTO :f_task_runs, :f_task_fail
    FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
    WHERE DAY >= DATEADD('day', -7, CURRENT_DATE()) AND DAY < CURRENT_DATE();
    -- <<< E1

    -- COUNT_IF, not V112's SUM(IFF(...)): an empty ALERT_EVENTS reads 0, never a NULL that blanks the string
    SELECT COUNT_IF(SEVERITY = 'CRITICAL' AND STATUS IN ('OPEN','ACK')),
           COUNT_IF(SEVERITY = 'HIGH' AND STATUS IN ('OPEN','ACK')),
           COUNT_IF(RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()))
      INTO :a_open_crit, :a_open_high, :a_raised_24h
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;

    f_failed_q_pct := ROUND(100 * :f_failed_q / NULLIF(:f_queries, 0), 2);
    f_task_fail_pct := ROUND(100 * :f_task_fail / NULLIF(:f_task_runs, 0), 2);

    -- E2: V171 R1-228 - the keys say warehouse compute (serverless, AI and storage are not in them)
    facts := 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; WAREHOUSE_CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
          || '; QUERIES=' || COALESCE(TO_VARCHAR(:f_queries), 'n/a')
          || '; FAILED_QUERIES=' || COALESCE(TO_VARCHAR(:f_failed_q), 'n/a')
          || '; FAILED_QUERY_PCT=' || COALESCE(TO_VARCHAR(:f_failed_q_pct), 'n/a')
          || '; QUERY_SUCCESS_PCT=' || COALESCE(TO_VARCHAR(100 - :f_failed_q_pct), 'n/a')
          || '; QUEUED_MINUTES=' || COALESCE(TO_VARCHAR(:f_queued_min), 'n/a')
          || '; SPILL_GB=' || COALESCE(TO_VARCHAR(:f_spill_gb), 'n/a')
          || '; TASK_RUNS=' || COALESCE(TO_VARCHAR(:f_task_runs), 'n/a')
          || '; TASK_FAILURES=' || COALESCE(TO_VARCHAR(:f_task_fail), 'n/a')
          || '; TASK_FAILURE_PCT=' || COALESCE(TO_VARCHAR(:f_task_fail_pct), 'n/a')
          || '; TASK_SUCCESS_PCT=' || COALESCE(TO_VARCHAR(100 - :f_task_fail_pct), 'n/a');
    alerts := 'ALERT_WINDOW_HOURS=24; OPEN_CRITICAL_ALERTS=' || :a_open_crit
           || '; OPEN_HIGH_ALERTS=' || :a_open_high || '; ALERTS_RAISED_24H=' || :a_raised_24h;
    facts := :facts || '; ' || :alerts;
    -- <<< D2

    -- D3 >>> V165 #24: the prompt forbids derived numbers (the check would reject them) and names the units
    prompt := LEFT(
        'You are a senior Snowflake DBA writing the morning digest for ALFA/Trexis leadership. '
        || 'Use ONLY the FACTS below (the 7 complete days ending yesterday, all companies; alert counts are open now or raised in the last 24 hours). '
        || 'Every number you write must be a FACT value, copied or rounded (thousands separators are fine): never calculate '
        || 'totals, averages, differences or new percentages, and never write dates or times. Dollar amounts come only from '
        || '*_USD facts and percentages only from *_PCT facts; WAREHOUSE_CREDITS are Snowflake credits, not dollars. '
        || 'WAREHOUSE_SPEND_USD and WAREHOUSE_CREDITS cover warehouse compute only (serverless, AI and storage are not '
        || 'included): call it warehouse compute spend, never total spend. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and warehouse compute spend in plain language; '
        || 'what needs attention today and why; one recommended focus. No preamble. '
        || 'FACTS: ' || COALESCE(:facts, 'none') || '.',
        6000);
    -- <<< D3

    BEGIN
        body := SNOWFLAKE.CORTEX.COMPLETE(:model, :prompt);
    EXCEPTION
        WHEN OTHER THEN
            -- D4: V165 #24 - no error text as the digest; the template goes out and the failure is ledgered below
            body := NULL;
            ai_err := SQLERRM;
    END;
    ai_body := LEFT(:body, 8000);   -- D4: the draft, kept for audit whichever version is sent

    -- D5 >>> V165 #24: measured grounding. Every figure the draft states must equal a FACT within half a step
    -- of its shown precision or 0.5% (app/logic/ai_grounding.check_grounding's tolerance): a $ figure only a
    -- *_USD fact, a % figure only a *_PCT fact, a figure followed by a known noun (credits, critical, high,
    -- minutes, GB, queries, failed, tasks, alerts, hours, days) only a fact whose key names it, any other
    -- figure any fact. Dates, clock times, identifier-like tokens (WH_X1, p95, V112) and list markers are
    -- stripped first. The half step is inclusive: TOL * 1.000000001 absorbs DOUBLE noise (fact 1.25 shown as 1.3).
    -- app/logic/digest_grounding.py mirrors this rule; tests/test_digest_grounding_parity.py locks every
    -- literal below to it. Backslash-free patterns on purpose ([0-9], [.], [$]): V022/V026.
    IF (:body IS NOT NULL AND TRIM(:body) <> '') THEN
        clean := REGEXP_REPLACE(:body, '[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{1,2}:[0-9]{2}(:[0-9]{2})?)?', ' ');
        clean := REGEXP_REPLACE(:clean, '[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?', ' ');
        clean := REGEXP_REPLACE(:clean, '[A-Za-z_]+[0-9][A-Za-z0-9_]*', ' ');
        clean := REGEXP_REPLACE(:clean, '[(][0-9]{1,2}[)]|#[0-9]{1,2}', ' ');
        clean := REGEXP_REPLACE(:clean, '^[ *#]*[0-9]{1,2}[.)] ', ' ', 1, 0, 'm');
        SELECT COUNT(*), COUNT_IF(NOT g.MATCHED),
               NULLIF(LEFT(LISTAGG(IFF(g.MATCHED, NULL, g.TOK), ', ') WITHIN GROUP (ORDER BY g.POS), 1000), '')
          INTO :n_checked, :n_bad, :ungrounded
        FROM (
            SELECT t.TOK, MIN(t.POS) AS POS, COUNT(f.FVAL) > 0 AS MATCHED
            FROM (
                SELECT u.POS, u.TOK, u.UNIT, u.KEYWORD,
                       u.NUM_VAL * u.SCALE AS VAL,
                       GREATEST(0.5 * POWER(10, -u.DECIMALS) * u.SCALE, 0.005 * u.NUM_VAL * u.SCALE) AS TOL
                FROM (
                    SELECT w.POS, w.TOK,
                           TRY_TO_DOUBLE(REPLACE(w.NUM, ',', '')) AS NUM_VAL,
                           IFF(CONTAINS(w.NUM, '.'), LENGTH(SPLIT_PART(w.NUM, '.', 2)), 0) AS DECIMALS,
                           CASE WHEN STARTSWITH(w.TOK, '$') THEN 'usd'
                                WHEN CONTAINS(w.TOK, '%') OR w.WORD IN ('percent', 'pct') THEN 'pct'
                                ELSE 'num' END AS UNIT,
                           CASE WHEN w.WORD IN ('k', 'thousand') THEN 1000
                                WHEN w.WORD IN ('m', 'mm', 'mn', 'million') THEN 1000000
                                WHEN w.WORD IN ('b', 'bn', 'billion') THEN 1000000000
                                ELSE 1 END AS SCALE,
                           CASE WHEN w.WORD LIKE 'credit%' THEN 'CREDIT'
                                WHEN w.WORD = 'critical' THEN 'CRITICAL'
                                WHEN w.WORD = 'high' THEN 'HIGH'
                                WHEN w.WORD LIKE 'minute%' THEN 'MINUTE'
                                WHEN w.WORD = 'gb' THEN '_GB'
                                WHEN w.WORD LIKE 'quer%' THEN 'QUER'
                                WHEN w.WORD LIKE 'fail%' THEN 'FAIL'
                                WHEN w.WORD LIKE 'task%' THEN 'TASK'
                                WHEN w.WORD LIKE 'alert%' THEN 'ALERT'
                                WHEN w.WORD LIKE 'hour%' THEN 'HOUR'
                                WHEN w.WORD LIKE 'day%' THEN 'DAY'
                                ELSE NULL END AS KEYWORD
                    FROM (
                        SELECT x.INDEX AS POS, TRIM(x.VALUE::VARCHAR) AS TOK,
                               REGEXP_SUBSTR(x.VALUE::VARCHAR, '[0-9][0-9,]*([.][0-9]+)?') AS NUM,
                               LOWER(REGEXP_SUBSTR(x.VALUE::VARCHAR, '[A-Za-z]+')) AS WORD
                        FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:clean,
                             '[$]?([0-9]{1,3}(,[0-9]{3})+|[0-9]+)([.][0-9]+)?( ?[a-z]+)?( ?%)?', 1, 1, 'i'))) x
                    ) w
                ) u
            ) t
            LEFT JOIN (
                SELECT SPLIT_PART(p.VALUE::VARCHAR, '=', 1) AS FKEY,
                       TRY_TO_DOUBLE(SPLIT_PART(p.VALUE::VARCHAR, '=', 2)) AS FVAL
                FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:facts, '[A-Z][A-Z0-9_]*=[0-9]+([.][0-9]+)?'))) p
            ) f
              ON (t.UNIT = 'num' OR (t.UNIT = 'usd' AND ENDSWITH(f.FKEY, '_USD'))
                                 OR (t.UNIT = 'pct' AND ENDSWITH(f.FKEY, '_PCT')))
             AND (t.KEYWORD IS NULL OR CONTAINS(f.FKEY, t.KEYWORD))
             AND ABS(f.FVAL - t.VAL) <= t.TOL * 1.000000001
            GROUP BY t.TOK
        ) g;
        grounding_ok := (n_bad = 0);
    END IF;

    -- V165 #24: the templated digest, built ONLY from the fact variables above and labelled not AI-written.
    -- Sent when the draft states a figure no fact supports (GROUNDING_OK = FALSE) or Cortex returned nothing
    -- (GROUNDING_OK NULL). The withheld draft stays in AI_BODY for audit and is never sent.
    IF (grounding_ok IS NULL OR NOT grounding_ok) THEN
        body_source := 'TEMPLATE';
        body := 'Templated digest (not AI-written): '
             || IFF(:grounding_ok IS NULL,
                    'Cortex returned no digest this morning, so OVERWATCH sent the exec-board facts directly.',
                    'the AI draft stated figures that do not match the exec-board facts, so OVERWATCH sent the facts directly.')
             || CHR(10) || CHR(10)
             || 'The 7 complete days to yesterday, all companies: warehouse compute spend '
             || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a')
             || ' credits; serverless, AI and storage not included); '
             || COALESCE(TRIM(TO_VARCHAR(:f_queries, '999,999,999,990')), 'n/a') || ' queries, '
             || COALESCE(TRIM(TO_VARCHAR(:f_failed_q, '999,999,999,990')), 'n/a') || ' failed ('
             || COALESCE(TO_VARCHAR(:f_failed_q_pct), 'n/a') || '%); '
             || COALESCE(TRIM(TO_VARCHAR(:f_queued_min, '999,999,999,990.0')), 'n/a') || ' minutes queued; '
             || COALESCE(TRIM(TO_VARCHAR(:f_spill_gb, '999,999,999,990.00')), 'n/a') || ' GB spilled to remote storage; '
             || COALESCE(TRIM(TO_VARCHAR(:f_task_runs, '999,999,999,990')), 'n/a') || ' task runs, '
             || COALESCE(TRIM(TO_VARCHAR(:f_task_fail, '999,999,999,990')), 'n/a') || ' failed ('
             || COALESCE(TO_VARCHAR(:f_task_fail_pct), 'n/a') || '%).'
             || CHR(10) || CHR(10)
             || 'Alerts: ' || :a_open_crit || ' critical and ' || :a_open_high || ' high open; '
             || :a_raised_24h || ' raised in the last 24 hours.'
             || CHR(10) || CHR(10)
             || 'Focus: ' || CASE WHEN :a_open_crit > 0 THEN 'clear the ' || :a_open_crit || ' open critical alert(s) first.'
                                  WHEN :a_open_high > 0 THEN 'work the ' || :a_open_high || ' open high alert(s).'
                                  WHEN COALESCE(:f_task_fail, 0) > 0 THEN 'review the ' || :f_task_fail || ' failed task run(s).'
                                  ELSE 'nothing is open at critical or high; no action is needed today.' END;
    END IF;
    -- <<< D5

    -- V070 #39: replace today's digest atomically. Under autocommit a crash between
    -- the DELETE and the INSERT would leave today's digest BLANK; an explicit transaction
    -- makes it all-or-nothing (on any error ROLLBACK restores the prior row and re-raise).
    BEGIN TRANSACTION;
    BEGIN
        DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();
        -- D6: V165 #24 - the facts, the measured result and both texts
        INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
            (DIGEST_DATE, COMPANY, MODEL, BODY, FACTS, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, BODY_SOURCE, AI_BODY)
        VALUES (CURRENT_DATE(), 'ALL', :model, LEFT(:body, 8000), LEFT(:facts, 4000), :grounding_ok,
                :n_checked, :ungrounded, :body_source, :ai_body);
        COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            RAISE;
    END;

    -- D7 >>> V165 #24: a Cortex failure is ledgered (it used to become the digest body and go to Teams)
    IF (ai_err IS NOT NULL) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'DailyDigest', 'digest_ai_failed', :ai_err,
               'Cortex COMPLETE failed for model ' || :model || ' - the templated digest was written and sent instead',
               CURRENT_ROLE();
    END IF;

    -- V165 #24: the SENT text is the chosen BODY (never the withheld draft), JSON-escaped exactly like
    -- SP_NOTIFY_WEBHOOK (V026 v3, V064): every route's body template splices the message INSIDE a JSON
    -- string, and V070/V112 sent a raw CHR(10) plus raw Cortex prose, which Teams Workflows rejects.
    msg := 'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) || LEFT(:body, 3000)
        || IFF(:body_source = 'AI',
               CHR(10) || CHR(10) || 'AI-written (' || :model || '); '
               || IFF(:n_checked = 0, 'it states no figures.',
                      'all ' || :n_checked || ' of its figures match the exec-board facts.'),
               '');
    msg := REPLACE(:msg, CHR(92), CHR(92) || CHR(92));
    msg := REPLACE(:msg, CHR(34), CHR(92) || CHR(34));
    msg := REPLACE(:msg, CHR(10), CHR(92) || 'n');
    msg := REPLACE(:msg, CHR(13), '');
    msg := REPLACE(:msg, CHR(9),  CHR(92) || 't');
    msg := RTRIM(LEFT(:msg, 3000), CHR(92));   -- a cut escape pair must not escape the template's closing quote
    -- <<< D7

    -- V070 #23: deliver the digest through EVERY enabled ALERT_ROUTES row's own
    -- integration (SP_NOTIFY_WEBHOOK's per-route walk idiom, V034), not the retired
    -- hardcoded Slack integration that does not exist on a Teams-only account. Each
    -- route's outcome is LEDGERED: a failed send logs one 'digest_send_failed' row to
    -- APP_ERROR_LOG naming the integration, replacing the old blanket WHEN OTHER THEN
    -- NULL that hid a never-delivered digest behind a 'delivery attempted' string. The
    -- in-app digest was already written above and stands regardless of any send.
    FOR rec IN c_routes DO
        r_route_id := rec.ROUTE_ID;
        r_integration := rec.INTEGRATION_NAME;
        routes_total := routes_total + 1;
        BEGIN
            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(:msg),   -- D8: V165 #24
                SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
            routes_sent := routes_sent + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'DailyDigest', 'digest_send_failed', :emsg,
                       'route ' || :r_route_id || ' integration ' || :r_integration ||
                       ' - digest still written in-app; other routes unaffected',
                       CURRENT_ROLE();
        END;
    END FOR;

    -- V070 #12: without this a fully-failed run is silent — only per-route failures were
    -- logged and the proc still returned a bland 'sent 0/M' string. Log one loud
    -- 'digest_undelivered' row when routes were eligible but NONE received the digest, so
    -- an all-failed run is observable, and mark the zero-success case in the return string.
    IF (routes_total > 0 AND routes_sent = 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'DailyDigest', 'digest_undelivered',
               'digest written in-app but delivered to 0 of ' || :routes_total || ' enabled route(s)',
               'every enabled digest route failed - see digest_send_failed rows for per-route detail',
               CURRENT_ROLE();
    END IF;

    -- D9: V165 #24 - the return names the version written and why
    RETURN 'digest written (' || :body_source
           || IFF(:grounding_ok = FALSE, '; the AI draft had ' || :n_bad || ' unmatched figure(s)', '')
           || IFF(:ai_err IS NOT NULL, '; Cortex failed', '')
           || '); sent ' || :routes_sent || '/' || :routes_total || ' routes'
           || IFF(:routes_total > 0 AND :routes_sent = 0, ' [UNDELIVERED]', '');
END;
$$;

-- >>> derived:SP_SCAN_REF_GAPS  (from V129; both MINUS operands TO_VARCHAR + one EXECUTE IMMEDIATE per check in its own EXCEPTION block, R2-019/R2-104, V171)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Config-driven reference-data gap scan (ETL Phase 1b). Reads ETL_REF_GAP_XLAT + ETL_REF_GAP_CHECKS
-- from SETTINGS and replaces ETL_REF_GAP_RESULTS with the codes present in each configured staging
-- table but MISSING from the XLAT reference table (SRC_IDNTFTN_NM = the check name, value column
-- SRC_IDNTFTN_VAL). The PIPE_REF_GAP arm of SP_ALERT_SCAN_DAILY reads that table and raises one alert
-- per code type. A leading '*' pin marker (a panel-only concern) is stripped; the alert scans every
-- configured check. Identifiers are allowlist-validated so the built SQL is always well-formed; the
-- caller wraps CALL in its own EXCEPTION guard, so a missing grant on the source tables is contained.
DECLARE
    xlat STRING;
    checks STRING;
    enabled_cnt INT;
    ins_sql STRING;
    -- R1 >>> V171 R2-019: one statement per check, each in its own EXCEPTION block
    res RESULTSET;
    nm STRING;
    emsg STRING;
    n_ok INT DEFAULT 0;
    n_failed INT DEFAULT 0;
    all_failed EXCEPTION (-20662, 'ref-gap scan: every configured check failed - see APP_ERROR_LOG ref_gap_check_failed');
    -- <<< R1
BEGIN
    -- gate: only scan when PIPE_REF_GAP exists AND is enabled (skip the cross-DB read otherwise).
    SELECT COUNT(*) INTO :enabled_cnt
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PIPE_REF_GAP' AND ENABLED;

    -- always clear last run's rows first, so stale gaps never linger after a fix or a disable.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS;
    IF (:enabled_cnt = 0) THEN
        RETURN 'ref-gap scan skipped (rule disabled)';
    END IF;

    SELECT MAX(IFF(KEY = 'ETL_REF_GAP_XLAT', VALUE, NULL)),
           MAX(IFF(KEY = 'ETL_REF_GAP_CHECKS', VALUE, NULL))
      INTO :xlat, :checks
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    IF (:xlat IS NULL OR TRIM(:xlat) = '' OR :checks IS NULL OR TRIM(:checks) = ''
        OR NOT RLIKE(TRIM(:xlat), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')) THEN
        RETURN 'ref-gap scan skipped (unconfigured or invalid XLAT)';
    END IF;

    -- Parse entries (newline- or ';'-separated), strip a leading '*' pin, allowlist-validate the
    -- family NAME + staging FQN + code column, and build ONE MINUS statement per check (R2 >>> V171: each runs
    -- alone below, R2-019; R2-104: BOTH operands TO_VARCHAR, the shape of the app twin
    -- etl_control_sql._check_sql since v4.527, so a NUMBER or DATE code column compares as text and never
    -- coerces the VARCHAR SRC_IDNTFTN_VAL).
    -- q_name is the apostrophe-escaped name literal; the name allowlist also excludes backslashes and
    -- quotes, so the built literal can never be broken or injected (matches the app's sql_literal).
    res := (
    SELECT nm_clean AS CHECK_NAME,
             'SELECT ' || q_name || ' AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM ( '
             || 'SELECT TO_VARCHAR(s.' || col || ') AS NEW_CODE FROM ' || fqn || ' s WHERE s.' || col
             || ' IS NOT NULL MINUS SELECT TO_VARCHAR(x.SRC_IDNTFTN_VAL) FROM ' || :xlat
             || ' x WHERE x.SRC_IDNTFTN_NM = ' || q_name || ' ) g' AS CHECK_SQL
    FROM (
        SELECT nm_clean, '''' || REPLACE(nm_clean, '''', '''''') || '''' AS q_name, fqn, col
        FROM (
            SELECT
                TRIM(REGEXP_REPLACE(TRIM(SPLIT_PART(entry, '|', 1)), '^[*][ ]*', '')) AS nm_clean,
                TRIM(SPLIT_PART(entry, '|', 2)) AS fqn,
                TRIM(SPLIT_PART(entry, '|', 3)) AS col
            FROM (
                SELECT TRIM(VALUE) AS entry
                FROM TABLE(SPLIT_TO_TABLE(REPLACE(REPLACE(:checks, ';', CHR(10)), CHR(13), ''), CHR(10)))
            )
            WHERE entry <> '' AND NOT STARTSWITH(entry, '#')
              AND ARRAY_SIZE(SPLIT(entry, '|')) = 3
        )
        WHERE nm_clean <> '' AND fqn <> '' AND col <> ''
          AND RLIKE(nm_clean, '^[-A-Za-z0-9_.:/ ]+$')
          AND RLIKE(fqn, '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')
          AND RLIKE(col, '^[A-Za-z0-9_$]+$')
    )
    ORDER BY CHECK_NAME
    );

    -- R3 >>> V171 R2-019: one INSERT per check, each in its own EXCEPTION block. A check that throws (a missing
    -- SELECT grant on one staging table, a renamed table, a code longer than NEW_CODE) logs one
    -- ref_gap_check_failed row naming it, and the other checks still write their gaps. V129 ran every check in
    -- ONE statement after the DELETE, so one bad check blanked the PIPE_REF_GAP page for all of them.
    LET c_checks CURSOR FOR res;
    FOR r IN c_checks DO
        nm := r.CHECK_NAME;
        BEGIN
            ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS (CHECK_NAME, NEW_CODE) '
                       || r.CHECK_SQL;
            EXECUTE IMMEDIATE :ins_sql;
            n_ok := n_ok + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                n_failed := n_failed + 1;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'AlertScan', 'ref_gap_check_failed', LEFT(:emsg, 2000),
                       'PIPE_REF_GAP check ' || LEFT(:nm, 200) || ' - other ref-gap checks unaffected', CURRENT_ROLE();
        END;
    END FOR;

    IF (:n_ok + :n_failed = 0) THEN
        RETURN 'ref-gap scan: no valid checks configured';
    END IF;
    IF (:n_ok = 0) THEN
        RAISE all_failed;   -- every check failed (e.g. no XLAT grant): arm [17] still logs ref_gap_scan_failed
    END IF;

    RETURN 'ref-gap scan complete (' || :n_ok || ' ok, ' || :n_failed || ' failed)';
    -- <<< R3
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 171 AS VERSION,
       'Ops self-watch, digest window, ref-gap isolation, override seed (round 2). SETTINGS CREDIT_PRICE_OVERRIDE seeded FALSE (WHEN NOT MATCHED; validate.sql reads FALSE as no override). SP_CANARY_SENTINEL re-derived from V017: the OPS_CANARY_FAIL detail no longer blames column drift (SELECT 1 sees a missing object or lost access only), the render-SLA handler logs the real error, and the OPS_SLOW_RENDER title shows the p95 in Hr/Min/Sec (METRIC_VALUE stays seconds). SP_DAILY_DIGEST re-derived from V165: facts cover the 7 complete days ending yesterday (the board ALL 7-day DAILY_SPEND rows for those days, FACT_QUERY_DAILY, FACT_TASK_DAILY; the today-inclusive KPI rows are no longer read), keys WAREHOUSE_SPEND_USD and WAREHOUSE_CREDITS, the prompt and template say warehouse compute only, never total spend; CORTEX_MODEL is normalized like the app (trimmed, lower-case, a valid name else llama3.1-8b). SP_SCAN_REF_GAPS re-derived from V129: both MINUS operands TO_VARCHAR (parity with the Operations panel) and one statement per check in its own EXCEPTION block (ref_gap_check_failed names a failing check; the scan raises only when every check failed). No task change, no new object, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 171);
