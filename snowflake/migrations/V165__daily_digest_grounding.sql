-- V165__daily_digest_grounding.sql
--
-- Next-Fifty #24: the morning digest is checked, not assumed.
--
-- WHY: SP_DAILY_DIGEST (V112, re-derived from V070) sends a Cortex-written three-paragraph digest to every digest
-- route each morning and the app labels it "grounded", but nothing ever checked its numbers. The facts handed
-- the model dollars under the key CREDITS (COALESCE(VALUE_USD, VALUE) since V007), a Cortex failure became the
-- digest body and went to Teams, and the text reached the Teams Workflows JSON body template unescaped (a raw
-- CHR(10) plus raw prose: V026 recorded that Teams rejects that card, and the send is asynchronous, so
-- 'sent 1/1' can hide it). Owner decision 2026-09-29: store the facts and a MEASURED grounding result; when the
-- AI draft's figures do not match the facts, or Cortex fails, send a templated digest labelled not AI-written.
--
--   + DAILY_DIGEST columns (ADD COLUMN IF NOT EXISTS, all nullable): FACTS (the named facts given to the model),
--     GROUNDING_OK (TRUE = every figure matched, FALSE = at least one did not, NULL = not measured: Cortex
--     returned nothing, or a row written before V165), FIGURES_CHECKED, UNGROUNDED (the unmatched figures),
--     BODY_SOURCE ('AI' or 'TEMPLATE': which version BODY holds and the routes received) and AI_BODY (the
--     Cortex draft, kept for audit; a withheld draft is never sent).
--   ~ SP_DAILY_DIGEST re-derived from V112 (its current definer), byte-identical except:
--       D1 typed fact / grounding DECLAREs; D2 one conditional aggregation over the same MART_EXEC_BOARD KPI rows
--       into typed variables, COUNT_IF alert counts (an empty ALERT_EVENTS reads 0, not NULL), FACTS as named
--       facts with one unit per key (SPEND_USD and CREDITS separate); D3 a prompt that allows only FACT values,
--       copied or rounded, units in full, unnumbered paragraphs; D4 a Cortex failure leaves the body empty and is
--       ledgered as digest_ai_failed (no error text as the digest); D5 the measured grounding (each figure must
--       equal a fact within half a step of its shown precision or 0.5%, bound by unit -- $ only *_USD, % only
--       *_PCT -- and by the noun after it) and, when GROUNDING_OK is NULL or FALSE, a template built only from the
--       facts; D6 the INSERT stores the six new columns inside the unchanged V070 transaction; D7 the sent text is
--       JSON-escaped with SP_NOTIFY_WEBHOOK's five REPLACEs (V064) and cut back past a trailing backslash; D8 the
--       send uses that text; D9 the RETURN names the version written and why. Kept byte-identical: V112's
--       CRITICAL-only route filter, V070's DELIVER_DIGEST gate, the atomic DELETE + INSERT, digest_send_failed
--       and digest_undelivered.
--
-- COST: the same one Cortex call a day; the check adds a few small SQL statements to the 07:20 task (no
-- ACCOUNT_USAGE read, no new task or warehouse resume).
-- LATENCY: unchanged; TASK_DAILY_DIGEST runs 07:20 America/Chicago.
-- FIRST RUN: the next 07:20 run writes the first measured row. Nothing runs at apply time (a CALL would spend
-- Cortex credits and post to Teams). Preview with PREFLIGHT_WAVE4.sql P165.1-P165.3 (read-only).
-- ROLLBACK: re-run V112's SP_DAILY_DIGEST (V112__daily_digest_skips_paging_routes.sql, the CREATE PROCEDURE);
-- the columns can stay (new rows then carry NULLs, which the app shows as grounding not measured).
-- Apply AFTER V164. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20165, 'V165 requires V164 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 164) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The measured-digest columns (all nullable; the table is TRANSIENT, V007). Before the proc: an apply that
-- stops here leaves V112's proc writing its four named columns, which still works.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS FACTS VARCHAR(4000);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS GROUNDING_OK BOOLEAN;
ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS FIGURES_CHECKED NUMBER(6,0);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS UNGROUNDED VARCHAR(1000);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS BODY_SOURCE VARCHAR(20);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS AI_BODY VARCHAR(8000);

-- >>> derived:SP_DAILY_DIGEST  (from V112; FACTS + measured GROUNDING_OK + templated digest on mismatch + JSON-safe send, V165)
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
    SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
      INTO :model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- D2 >>> V165 #24: FACTS are named numbers with one unit per key (*_USD dollars, *_PCT percent, the rest
    -- counts, credits, minutes or GB), so the draft's figures can be checked against them. V007-V112 sent
    -- 'CREDITS=<dollars>' (COALESCE(VALUE_USD, VALUE) under the CREDITS metric); spend now has its own key.
    SELECT MAX(IFF(METRIC = 'CREDITS', VALUE_USD, NULL)), MAX(IFF(METRIC = 'CREDITS', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUERIES', VALUE, NULL)), MAX(IFF(METRIC = 'FAILED_QUERIES', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUEUED_MINUTES', VALUE, NULL)), MAX(IFF(METRIC = 'SPILL_GB', VALUE, NULL)),
           MAX(IFF(METRIC = 'TASK_RUNS', VALUE, NULL)), MAX(IFF(METRIC = 'TASK_FAILURES', VALUE, NULL))
      INTO :f_spend_usd, :f_credits, :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb, :f_task_runs, :f_task_fail
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';

    -- COUNT_IF, not V112's SUM(IFF(...)): an empty ALERT_EVENTS reads 0, never a NULL that blanks the string
    SELECT COUNT_IF(SEVERITY = 'CRITICAL' AND STATUS IN ('OPEN','ACK')),
           COUNT_IF(SEVERITY = 'HIGH' AND STATUS IN ('OPEN','ACK')),
           COUNT_IF(RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()))
      INTO :a_open_crit, :a_open_high, :a_raised_24h
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;

    f_failed_q_pct := ROUND(100 * :f_failed_q / NULLIF(:f_queries, 0), 2);
    f_task_fail_pct := ROUND(100 * :f_task_fail / NULLIF(:f_task_runs, 0), 2);

    facts := 'WINDOW_DAYS=7; SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
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
        || 'Use ONLY the FACTS below (the last 7 days, all companies; alert counts are open now or raised in the last 24 hours). '
        || 'Every number you write must be a FACT value, copied or rounded (thousands separators are fine): never calculate '
        || 'totals, averages, differences or new percentages, and never write dates or times. Dollar amounts come only from '
        || '*_USD facts and percentages only from *_PCT facts; CREDITS are Snowflake credits, not dollars. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and spend in plain language; '
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
    -- stripped first. app/logic/digest_grounding.py mirrors this rule; tests/test_digest_grounding_parity.py
    -- locks every literal below to it. Backslash-free patterns on purpose ([0-9], [.], [$]): V022/V026.
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
             AND ABS(f.FVAL - t.VAL) <= t.TOL
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
             || 'Last 7 days, all companies: spend ' || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a') || ' credits); '
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

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 165 AS VERSION,
       'Morning digest grounding (Next-Fifty #24): DAILY_DIGEST + FACTS, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, BODY_SOURCE, AI_BODY (nullable). SP_DAILY_DIGEST re-derived from V112, byte-identical except: FACTS are named facts with one unit per key (SPEND_USD and CREDITS separate; V007-V112 sent dollars under CREDITS), COUNT_IF alert counts; a prompt that allows only FACT values; every figure in the Cortex draft is checked against the facts (unit and noun bound, within half a step of its precision or 0.5 percent); when a figure does not match, or Cortex fails (digest_ai_failed, no error text as the digest), a templated digest built only from the facts and labelled not AI-written is written and sent, the draft kept in AI_BODY; the sent text is JSON-escaped like SP_NOTIFY_WEBHOOK (V064) so a Teams Workflows card is valid. Kept: the CRITICAL-only route filter, the DELIVER_DIGEST gate, the atomic write, the per-route ledger. No task change, no SETTINGS key, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 165);
