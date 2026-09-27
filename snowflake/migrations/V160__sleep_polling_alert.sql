-- V160__sleep_polling_alert.sql
--
-- COST_SLEEP_POLLING: the chronic SYSTEM$WAIT sleep-polling alert -- the DB-side push of v4.595's Cost > Spend panel
-- "Which statement families bill the most cloud services" (app/data/mart_sql.cloud_svc_billed_families).
--
-- WHY: the owner's DIAG (2026-09-26, 7 days) found the account's cloud services above the free 10%-of-compute
-- allowance EVERY day, so every marginal cloud-services credit is billed, and SYSTEM$WAIT polling at 57.3 of
-- 218.55 CS credits a week (~$211/week at 3.68 USD/credit): Control-M 'select system$wait(10)' on
-- WH_ALFA_TRANSFORM_PRD and SYSTEM tasks' CALL SYSTEM$WAIT(30/60/1200) on WH_TRXS_TRANSFORM. V150's
-- COST_CLOUD_SVC_ANOMALY can never fire on a chronic poller (it is its own 28-day baseline), so nothing pushed it.
--
--   + SLEEP_POLLING_WEEKLY (TRANSIENT): the weekly census -- one POLLER row per sleep poller, one ACCOUNT row that
--     is both the week's receipt and the panel-parity group total. Not a FACT_/MART_ table: no freshness stamp.
--   + SP_SCAN_SLEEP_POLLING(FORCE_RUN BOOLEAN) (new): a poller = warehouse x user, or x task owner role when the
--     statements run as SYSTEM (a task). Billed exactly like the panel: the 7 newest COMPLETE metering days (the
--     newest FACT_METERING_DAILY row is the UTC day in progress at the 06:45 load), per day
--     LEAST(poller CS, GREATEST(0, account CS + adjustment)) capped as ONE group, x CREDIT_PRICE_USD. A sleep is
--     the app's statement SHAPE (app/logic/system_wait.SLEEP_SQL_PATTERN, SELECT or CALL of the wait function
--     after optional leading comments; not DDL, not a multi-statement parent, not the unhashed bucket).
--     Raises MEDIUM when a poller slept on 5+ of the 7 days and billed >= THRESHOLD_NUM USD/week
--     (25 by default); HIGH at 5x. One event per poller per EPISODE: a live (OPEN/ACK/SNOOZED) event holds its
--     band; a NOISE/EXPECTED resolve mutes that band 28 days (a 5x HIGH still breaks through); an ACTIONED resolve
--     re-raises only on polling after the resolve day; a live HIGH supersedes the poller's MED from any week.
--     Resolves itself (CONDITION_ENDED, OPEN only, 1h dwell, AUTO_CLEAR_ENABLED) once the poller is absent, bills
--     under the clear level (CLEAR_THRESHOLD_NUM, else half the threshold) or was idle on the window's last
--     4 days.
--   ~ SP_ALERT_SCAN_DAILY re-derived from V157 (its current definer), byte-identical except: + counting arm [25]
--     CALLing the new proc (before [17]); tally 11 -> 12 (self-alert, heartbeat, RETURN). The proc gates ITSELF:
--     it works once per ISO week (Central) -- the first daily scan with no completed receipt and complete data --
--     and otherwise returns after one small read. A failure trips OPS_SCAN_DEGRADED and, with no receipt, the
--     next daily scan redoes the week.
--   + ALERT_CONFIG COST_SLEEP_POLLING (COST, MEDIUM, 25 USD/week, 168h, AUTO_CLEAR_ENABLED TRUE), WHEN NOT MATCHED only.
--
-- COST: ~2 small statements on 6 days a week, ~10 on the due day (one heavy census over 7 days of an OVERWATCH
-- mart) = a few compile-seconds a week, no ACCOUNT_USAGE view, no new task or warehouse resume.
-- LATENCY: weekly. A new poller raises at the next week's evaluation; a stopped one clears up to ~10 days later.
-- FIRST RUN: the first daily scan after apply evaluates the current ISO week (applied Sunday 2026-09-27, that is
-- Monday 2026-09-28 ~07:00 Central over 09-21..09-27); preview with the read-only PREFLIGHT_SLEEP_POLLING.sql.
-- Nothing runs at apply time.
-- ROLLBACK: re-run V157's SP_ALERT_SCAN_DAILY (tally back to 11; nothing calls the new proc), optionally disable
-- the rule and close its events as EXPECTED; teardown.sql drops the proc (the table line is commented).
-- Apply AFTER V159. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20160, 'V160 requires V159 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 159) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The weekly census + receipt (transient: rebuilt every week from the marts).
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY (
    WEEK_START           DATE          NOT NULL,   -- ISO Monday (Central) of the evaluating run = the DEDUPE_KEY date
    ROW_KIND             VARCHAR(10)   NOT NULL,   -- POLLER | ACCOUNT (the week's receipt + panel-parity total)
    POLLER_KEY           VARCHAR(260)  NOT NULL,   -- COST_SLEEP_POLLING|<WH>|<USER or SYSTEM:ROLE>|  (the key prefix)
    WAREHOUSE_NAME       VARCHAR(300),
    USER_NAME            VARCHAR(300),
    TASK_ROLE            VARCHAR(300),
    USER_TOP_APP         VARCHAR(300),
    OWNER_HINT           VARCHAR(700),
    CALLS                VARCHAR(400),
    FAMILIES             NUMBER(9,0),
    POLLERS              NUMBER(9,0),
    RUNS                 NUMBER(18,0),
    ACTIVE_DAYS          NUMBER(3,0),
    LAST_ACTIVE_DAY      DATE,
    CS_CREDITS           FLOAT,
    BILLED_CS_CREDITS    FLOAT,
    USD_WEEK             NUMBER(18,2),
    ABOVE_ALLOWANCE_DAYS NUMBER(3,0),
    WIN_START            DATE,
    WIN_END              DATE,
    CREDIT_PRICE_USD     FLOAT,
    RAISED               NUMBER(9,0),
    SUPERSEDED           NUMBER(9,0),
    CLEARED              NUMBER(9,0),
    EVALUATED_AT         TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    COMPLETED_AT         TIMESTAMP_NTZ
);

-- The rule. WHEN NOT MATCHED only -- an operator's edits are never clobbered. AUTO_CLEAR_ENABLED rides the seed:
-- since V157 the V091 sweep is scoped to its 3 PERF rules, so the flag only gates this rule's own clear.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_SLEEP_POLLING', 'COST', 'Chronic sleep polling: one poller (warehouse + user, or task owner role) slept on 5+ of the 7 newest complete days and billed at least the threshold in USD per week of cloud services', TRUE, 'MEDIUM', 25, 168, TRUE)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS, s.AUTO_CLEAR_ENABLED);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FORCE_RUN BOOLEAN)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- COST_SLEEP_POLLING (V160): chronic SYSTEM$WAIT sleep polling per POLLER (warehouse x user, or x task owner role when
-- USER_NAME = SYSTEM), priced exactly like Cost > Spend > Which statement families bill the most cloud services
-- (mart_sql.cloud_svc_billed_families, v4.595): the 7 newest COMPLETE metering days (the newest
-- FACT_METERING_DAILY row is the UTC day still in progress at the 06:45 load); per day LEAST(poller CS,
-- GREATEST(0, account CS + adjustment)) as ONE group; x CREDIT_PRICE_USD. Sleep = system_wait.SLEEP_SQL_PATTERN
-- (statement shape) on the family (hash x type x warehouse x user) window-MIN sample. CALLed daily by
-- SP_ALERT_SCAN_DAILY arm [25] (counting); works once per ISO week (Central) -- the first call with no
-- completed receipt and complete data -- else one small read. No EXCEPTION handler: a failure reaches arm [25]
-- (OPS_SCAN_DEGRADED) and, with no receipt, is redone by the next daily scan. Mart-only; no freshness stamp.
DECLARE
    today_ct DATE;
    week_start DATE;
    n_enabled INT DEFAULT 0;
    n_done INT DEFAULT 0;
    forced BOOLEAN DEFAULT FALSE;
    credit_price FLOAT DEFAULT 3.68;          -- FLOAT + TRY_TO_DOUBLE (V153: a NUMBER priced 3.68 as 4)
    newest DATE;
    n_days INT DEFAULT 0;
    n_pollers INT DEFAULT 0;
    n_raised INT DEFAULT 0;
    n_superseded INT DEFAULT 0;
    n_cleared INT DEFAULT 0;
BEGIN
    -- [gate] the ONLY statement on a not-due day
    SELECT MAX(k.TODAY), MAX(k.WEEK_START), COUNT(DISTINCT c.RULE_ID), COUNT(DISTINCT r.COMPLETED_AT)
      INTO :today_ct, :week_start, :n_enabled, :n_done
    FROM (SELECT d.TODAY, DATEADD('day', 1 - DAYOFWEEKISO(d.TODAY), d.TODAY) AS WEEK_START
          FROM (SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY) d) k
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
           ON c.RULE_ID = 'COST_SLEEP_POLLING' AND c.ENABLED
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY r
           ON r.WEEK_START = k.WEEK_START AND r.ROW_KIND = 'ACCOUNT' AND r.COMPLETED_AT IS NOT NULL;
    IF (COALESCE(:FORCE_RUN, FALSE)) THEN
        forced := TRUE;
    END IF;
    IF (n_enabled = 0) THEN
        RETURN 'sleep polling scan skipped (COST_SLEEP_POLLING disabled or missing)';
    END IF;
    IF (n_done > 0 AND NOT forced) THEN
        RETURN 'sleep polling scan skipped (week of ' || TO_VARCHAR(:week_start) || ' already evaluated)';
    END IF;

    -- [ready] price + the 7 newest complete metering days, each carrying statement rows
    SELECT MAX(px.PRICE), MAX(mx.NEWEST), COUNT(md.DAY)
      INTO :credit_price, :newest, :n_days
    FROM (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) px
    CROSS JOIN (SELECT MAX(z.DAY) AS NEWEST FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY z) mx
    LEFT JOIN (SELECT x.DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY x
               WHERE x.DAY >= DATEADD('day', -10, :today_ct) GROUP BY x.DAY) xd
           ON xd.DAY >= DATEADD('day', -7, mx.NEWEST) AND xd.DAY < mx.NEWEST
    LEFT JOIN (SELECT m.DAY FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m
               WHERE m.DAY >= DATEADD('day', -10, :today_ct) GROUP BY m.DAY) md
           ON md.DAY = xd.DAY;
    IF (newest IS NULL OR newest < DATEADD('day', -2, today_ct) OR n_days < 7) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'AlertScan', 'sleep_polling_scan_deferred',
               'newest metering day ' || COALESCE(TO_VARCHAR(:newest), 'none') || '; ' || :n_days
                   || ' of the 7 window days carry both metering and statement rows',
               'rule COST_SLEEP_POLLING - week of ' || TO_VARCHAR(:week_start)
                   || ' not evaluated; the next daily scan retries', CURRENT_ROLE();
        RETURN 'sleep polling scan deferred (data not complete)';
    END IF;

    -- [snapshot] the week's census (the ONE heavy statement): a POLLER row per sleep poller + the ACCOUNT receipt row
    DELETE FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY WHERE WEEK_START = :week_start;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY
        (WEEK_START, ROW_KIND, POLLER_KEY, WAREHOUSE_NAME, USER_NAME, TASK_ROLE, USER_TOP_APP, OWNER_HINT, CALLS,
         FAMILIES, POLLERS, RUNS, ACTIVE_DAYS, LAST_ACTIVE_DAY, CS_CREDITS, BILLED_CS_CREDITS, USD_WEEK,
         ABOVE_ALLOWANCE_DAYS, WIN_START, WIN_END, CREDIT_PRICE_USD)
    WITH bill AS (      -- = panel bill: account-wide, all service types, complete metering days only
        SELECT x.DAY, SUM(COALESCE(x.CREDITS_CLOUD_SVCS, 0) + COALESCE(x.CREDITS_ADJUSTMENT, 0)) AS CS_BILLED_DAY
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY x
        WHERE x.DAY >= DATEADD('day', -7, :newest) AND x.DAY < :newest
        GROUP BY x.DAY
    ),
    win AS (
        SELECT MIN(b.DAY) AS WIN_START, MAX(b.DAY) AS WIN_END, COUNT_IF(b.CS_BILLED_DAY > 0) AS ABOVE_DAYS FROM bill b
    ),
    fd AS (
        SELECT m.DAY, m.QUERY_PARAMETERIZED_HASH, m.QUERY_TYPE, m.WAREHOUSE_NAME, m.USER_NAME, m.ROLE_NAME,
               m.SAMPLE_TEXT, m.RUNS, m.CS_CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m
        JOIN bill b ON b.DAY = m.DAY
    ),
    fam AS (            -- panel family grain (hash x type x warehouse x user); sleep decided on the window-MIN sample
        SELECT fd.QUERY_PARAMETERIZED_HASH, fd.QUERY_TYPE, fd.WAREHOUSE_NAME, fd.USER_NAME,
               MIN(fd.SAMPLE_TEXT) AS SAMPLE_TEXT
        FROM fd
        GROUP BY fd.QUERY_PARAMETERIZED_HASH, fd.QUERY_TYPE, fd.WAREHOUSE_NAME, fd.USER_NAME
    ),
    sleepfam AS (       -- = mart_sql.cloud_svc_billed_families tagged.SLEEP_FLAG (tests/test_sleep_polling_parity.py)
        SELECT f.QUERY_PARAMETERIZED_HASH, f.QUERY_TYPE, f.WAREHOUSE_NAME, f.USER_NAME,
               COALESCE(REGEXP_SUBSTR(UPPER(f.SAMPLE_TEXT),
                                      '(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(][^)]{0,40}[)]'),
                        'SYSTEM$WAIT(...)') AS CALL_TEXT
        FROM fam f
        WHERE f.QUERY_PARAMETERIZED_HASH <> 'n/a'
          AND f.QUERY_TYPE NOT LIKE 'CREATE%' AND f.QUERY_TYPE NOT LIKE 'ALTER%' AND f.QUERY_TYPE NOT LIKE 'MULTI_STATEMENT%'
          AND REGEXP_INSTR(UPPER(f.SAMPLE_TEXT), '^[[:space:]]*((/[*]([^*]|[*]+[^*/])*[*]+/|--[^\n]*\n|//[^\n]*\n)[[:space:]]*)*(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(]') > 0
    ),
    sd AS (             -- sleep rows keyed by POLLER: warehouse x user, or x task owner role when USER_NAME = SYSTEM
        SELECT d.DAY, d.WAREHOUSE_NAME, d.USER_NAME, IFF(UPPER(d.USER_NAME) = 'SYSTEM', d.ROLE_NAME, '') AS TASK_ROLE,
               'COST_SLEEP_POLLING|' || LEFT(UPPER(REPLACE(d.WAREHOUSE_NAME, '|', '/')), 100) || '|'
                   || LEFT(UPPER(REPLACE(IFF(UPPER(d.USER_NAME) = 'SYSTEM', 'SYSTEM:' || d.ROLE_NAME, d.USER_NAME),
                                         '|', '/')), 120)
                   || '|' AS POLLER_KEY,
               d.QUERY_PARAMETERIZED_HASH || '|' || d.QUERY_TYPE AS FAMILY_ID, s.CALL_TEXT, d.RUNS, d.CS_CREDITS
        FROM fd d
        JOIN sleepfam s
          ON s.QUERY_PARAMETERIZED_HASH = d.QUERY_PARAMETERIZED_HASH AND s.QUERY_TYPE = d.QUERY_TYPE
         AND s.WAREHOUSE_NAME = d.WAREHOUSE_NAME AND s.USER_NAME = d.USER_NAME
    ),
    pd AS (             -- poller x day: the GROUP-cap unit (never a sum of per-family caps)
        SELECT sd.POLLER_KEY, sd.DAY, SUM(sd.RUNS) AS RUNS_DAY, SUM(sd.CS_CREDITS) AS CS_DAY
        FROM sd GROUP BY sd.POLLER_KEY, sd.DAY
    ),
    pb AS (
        SELECT p.POLLER_KEY, SUM(p.RUNS_DAY) AS RUNS, SUM(p.CS_DAY) AS CS_CREDITS, COUNT(*) AS ACTIVE_DAYS,
               MAX(p.DAY) AS LAST_ACTIVE_DAY, SUM(LEAST(p.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))) AS BILLED_CS_CREDITS
        FROM pd p JOIN bill b ON b.DAY = p.DAY
        GROUP BY p.POLLER_KEY
    ),
    pc AS (             -- identity + the waits it runs, largest first
        SELECT c.POLLER_KEY, MAX(c.WAREHOUSE_NAME) AS WAREHOUSE_NAME, MAX(c.USER_NAME) AS USER_NAME,
               MAX(c.TASK_ROLE) AS TASK_ROLE, SUM(c.FAMILIES) AS FAMILIES,
               LEFT(LISTAGG(c.CALL_TEXT || ' ' || TO_VARCHAR(ROUND(c.CS, 1)) || ' cr', '; ')
                    WITHIN GROUP (ORDER BY c.CS DESC, c.CALL_TEXT), 400) AS CALLS
        FROM (SELECT sd.POLLER_KEY, sd.CALL_TEXT, MAX(sd.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                     MAX(sd.USER_NAME) AS USER_NAME, MAX(sd.TASK_ROLE) AS TASK_ROLE,
                     COUNT(DISTINCT sd.FAMILY_ID) AS FAMILIES, SUM(sd.CS_CREDITS) AS CS
              FROM sd GROUP BY sd.POLLER_KEY, sd.CALL_TEXT) c
        GROUP BY c.POLLER_KEY
    ),
    app AS (            -- = panel app CTE (cs_driver.owner_hint's USER_TOP_APP), same days
        SELECT u.USER_NAME, MAX_BY(u.APPLICATION, u.APP_QUERIES) AS USER_TOP_APP
        FROM (SELECT a.USER_NAME, a.APPLICATION, SUM(a.QUERIES) AS APP_QUERIES
              FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY a
              WHERE a.DAY >= DATEADD('day', -7, :newest) AND a.DAY < :newest AND a.APPLICATION <> '(unknown)'
              GROUP BY a.USER_NAME, a.APPLICATION) u
        GROUP BY u.USER_NAME
    ),
    acct AS (           -- = panel SLEEP_BILLED_CS_CREDITS_ALL: every sleep poller capped per day as ONE group
        SELECT SUM(d.RUNS_DAY) AS RUNS, SUM(d.CS_DAY) AS CS_CREDITS, COUNT(*) AS ACTIVE_DAYS, MAX(d.DAY) AS LAST_ACTIVE_DAY,
               SUM(LEAST(d.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))) AS BILLED_CS_CREDITS
        FROM (SELECT p.DAY, SUM(p.RUNS_DAY) AS RUNS_DAY, SUM(p.CS_DAY) AS CS_DAY FROM pd p GROUP BY p.DAY) d
        JOIN bill b ON b.DAY = d.DAY
    ),
    nf AS (SELECT COUNT(*) AS N FROM sleepfam),
    np AS (SELECT COUNT(*) AS N FROM pb)
    SELECT w2.* FROM (
    SELECT :week_start, 'POLLER', a.POLLER_KEY, a.WAREHOUSE_NAME, a.USER_NAME, a.TASK_ROLE, ap.USER_TOP_APP,
           IFF(UPPER(a.USER_NAME) = 'SYSTEM',                                   -- SQL twin of cs_driver.owner_hint
               IFF(a.TASK_ROLE <> '', 'Task owner (role ' || a.TASK_ROLE || ')', 'Task owner'),
               IFF(COALESCE(ap.USER_TOP_APP, '') <> '', ap.USER_TOP_APP || ' · ' || a.USER_NAME, 'User ' || a.USER_NAME)),
           a.CALLS, a.FAMILIES, NULL, b.RUNS, b.ACTIVE_DAYS, b.LAST_ACTIVE_DAY, b.CS_CREDITS, b.BILLED_CS_CREDITS,
           ROUND(b.BILLED_CS_CREDITS * :credit_price, 2), w.ABOVE_DAYS, w.WIN_START, w.WIN_END, :credit_price
    FROM pc a
    JOIN pb b ON b.POLLER_KEY = a.POLLER_KEY
    CROSS JOIN win w
    LEFT JOIN app ap ON ap.USER_NAME = a.USER_NAME
    UNION ALL
    SELECT :week_start, 'ACCOUNT', 'COST_SLEEP_POLLING|*|*|', NULL, NULL, NULL, NULL, NULL, NULL,
           nf.N, np.N, s.RUNS, s.ACTIVE_DAYS, s.LAST_ACTIVE_DAY, s.CS_CREDITS, s.BILLED_CS_CREDITS,
           ROUND(COALESCE(s.BILLED_CS_CREDITS, 0) * :credit_price, 2), w.ABOVE_DAYS, w.WIN_START, w.WIN_END, :credit_price
    FROM acct s CROSS JOIN win w CROSS JOIN nf CROSS JOIN np
    ) w2;
    n_pollers := SQLROWCOUNT - 1;

    -- [raise] one event per chronic poller per episode (band-aware live/mute hold; uncorrelated LEFT JOIN)
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT RULE_ID, SEVERITY, GREATEST(COALESCE(THRESHOLD_NUM, 25), 1) AS THR
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
        WHERE RULE_ID = 'COST_SLEEP_POLLING' AND ENABLED
    ),
    ev AS (             -- key minus its fixed '<MED|HIGH>|YYYY-MM-DD' tail = POLLER_KEY
        SELECT LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - IFF(SUBSTR(e.DEDUPE_KEY, -16, 6) = '|HIGH|', 15, 14)) AS POLLER_KEY,
               IFF(SUBSTR(e.DEDUPE_KEY, -16, 6) = '|HIGH|', 2, 1) AS BAND_RANK,
               e.STATUS, COALESCE(e.RESOLUTION_KIND, 'ACTIONED') AS KIND, e.RESOLVED_AT
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.RULE_ID = 'COST_SLEEP_POLLING'
    ),
    hold AS (
        SELECT v.POLLER_KEY,
               MAX(IFF(v.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                       OR (v.STATUS = 'RESOLVED' AND v.KIND IN ('NOISE', 'EXPECTED')
                           AND v.RESOLVED_AT >= DATEADD('day', -28, CURRENT_TIMESTAMP())),
                       v.BAND_RANK, 0)) AS HOLD_RANK,
               MAX(IFF(v.STATUS = 'RESOLVED'
                       AND v.KIND NOT IN ('NOISE', 'EXPECTED', 'SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED',
                                          'CONDITION_ENDED'),
                       TO_DATE(v.RESOLVED_AT), NULL)) AS LAST_ACTIONED_DAY
        FROM ev v
        GROUP BY v.POLLER_KEY
    ),
    cand AS (
        SELECT c.RULE_ID, c.SEVERITY AS BASE_SEVERITY, IFF(s.USD_WEEK >= c.THR * 5, 2, 1) AS BAND_RANK,
               s.POLLER_KEY, s.WAREHOUSE_NAME, s.USER_NAME, s.OWNER_HINT, s.CALLS, s.FAMILIES, s.RUNS, s.ACTIVE_DAYS,
               s.LAST_ACTIVE_DAY, s.CS_CREDITS, s.BILLED_CS_CREDITS, s.USD_WEEK, s.ABOVE_ALLOWANCE_DAYS,
               s.WIN_START, s.WIN_END, s.CREDIT_PRICE_USD
        FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY s
        JOIN cfg c ON s.USD_WEEK >= c.THR
        WHERE s.WEEK_START = :week_start AND s.ROW_KIND = 'POLLER'
          AND s.ACTIVE_DAYS >= 5                                   -- chronic: 5+ of the 7 days
    )
    SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
    FROM (
    SELECT o.RULE_ID,
           IFF(o.WAREHOUSE_NAME = 'NONE', 'ALL',          -- no warehouse = account-level; else the house mapping
               COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL')),
           IFF(o.BAND_RANK = 2 AND o.BASE_SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', o.BASE_SEVERITY),
           LEFT(IFF(o.WAREHOUSE_NAME = 'NONE', 'No warehouse', o.WAREHOUSE_NAME) || ' sleep polling ~$'
                || ROUND(o.USD_WEEK)::INT || '/week: ' || o.OWNER_HINT, 300),
           LEFT('Sleep polling on ' || o.ACTIVE_DAYS || ' of the 7 complete days ' || TO_VARCHAR(o.WIN_START) || ' to '
                || TO_VARCHAR(o.WIN_END) || ' (last on ' || TO_VARCHAR(o.LAST_ACTIVE_DAY) || '): ' || o.RUNS
                || ' sleep run(s) in ' || o.FAMILIES || ' statement family(ies): ' || COALESCE(o.CALLS, 'a wait call')
                || '. Cloud services ' || ROUND(o.CS_CREDITS, 2) || ' credits used, ' || ROUND(o.BILLED_CS_CREDITS, 2)
                || ' billed (the account was above its free 10% allowance on ' || o.ABOVE_ALLOWANCE_DAYS
                || ' of 7 days) = ~$' || ROUND(o.USD_WEEK)::INT || '/week, ~$' || ROUND(o.USD_WEEK * 52 / 12)::INT
                || '/month at $' || ROUND(o.CREDIT_PRICE_USD, 2) || '/credit. Owner: ' || o.OWNER_HINT || '. Next step: '
                || IFF(UPPER(o.USER_NAME) = 'SYSTEM',
                       'Stop sleeping inside the task: run the dependent step AFTER its predecessor in a task graph, or trigger it when a stream has data, instead of a CALL SYSTEM$WAIT loop. A resize won''t help.',
                       'Move the wait out of Snowflake: let the scheduler own the interval (Control-M cyclic interval or file watcher, orchestrator sensor), or run one short readiness check per cycle instead of SYSTEM$WAIT; cloud-services credits accrue for the whole sleep. A resize won''t help.')
                || ' Verify: Cost Intelligence > Spend & Attribution > Cloud-services health, 7-day window, Which '
                || 'statement families bill the most cloud services. A sleep compiles in well under 0.1 s, so '
                || 'compile-ranked views never show it, and the per-warehouse cloud-services baseline rule never trips '
                || 'on a steady poller (it is its own baseline). Checked weekly; while OPEN, this event resolves itself '
                || 'once the poller bills under the clear level (half the threshold by default) or is idle on the last '
                || '4 complete days; an acknowledged event stays yours to close (resolve it as actioned '
                || 'once fixed).', 2000),
           o.USD_WEEK,
           o.POLLER_KEY || IFF(o.BAND_RANK = 2, 'HIGH', 'MED') || '|' || TO_VARCHAR(:week_start)
    FROM cand o
    LEFT JOIN hold h ON h.POLLER_KEY = o.POLLER_KEY
    WHERE COALESCE(h.HOLD_RANK, 0) < o.BAND_RANK
      AND (h.LAST_ACTIONED_DAY IS NULL OR o.LAST_ACTIVE_DAY > h.LAST_ACTIONED_DAY)
    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
    );
    n_raised := SQLROWCOUNT;

    -- [supersede] a live HIGH replaces the same poller's live MED from any week (V067 only pairs same-date keys);
    -- SNOOZED too, or the V086 wake would reopen the MED beside its HIGH.
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                WHERE RULE_ID = 'COST_SLEEP_POLLING' AND STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                  AND SUBSTR(DEDUPE_KEY, -16, 6) = '|HIGH|')) THEN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
         WHERE lo.RULE_ID = 'COST_SLEEP_POLLING' AND lo.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
           AND SUBSTR(lo.DEDUPE_KEY, -15, 5) = '|MED|'
           AND LEFT(lo.DEDUPE_KEY, LENGTH(lo.DEDUPE_KEY) - 14) IN (
               SELECT LEFT(hi.DEDUPE_KEY, LENGTH(hi.DEDUPE_KEY) - 15)
               FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS hi
               WHERE hi.RULE_ID = 'COST_SLEEP_POLLING' AND hi.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                 AND SUBSTR(hi.DEDUPE_KEY, -16, 6) = '|HIGH|');
        n_superseded := SQLROWCOUNT;
    END IF;

    -- [clear] CONDITION_ENDED: OPEN only, 1h dwell, opt-in flag. Positive evidence = the complete window [ready]
    -- proved: the poller is absent, bills under the clear level, or was idle on the window's last 4 days.
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                WHERE e.RULE_ID = 'COST_SLEEP_POLLING' AND e.STATUS = 'OPEN' AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
         WHERE ev.RULE_ID = 'COST_SLEEP_POLLING' AND ev.STATUS = 'OPEN'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
           AND LEFT(ev.DEDUPE_KEY, LENGTH(ev.DEDUPE_KEY) - IFF(SUBSTR(ev.DEDUPE_KEY, -16, 6) = '|HIGH|', 15, 14)) NOT IN (
               SELECT s.POLLER_KEY
               FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY s
               JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = 'COST_SLEEP_POLLING'
               WHERE s.WEEK_START = :week_start AND s.ROW_KIND = 'POLLER'
                 AND s.USD_WEEK >= LEAST(COALESCE(c.CLEAR_THRESHOLD_NUM, GREATEST(COALESCE(c.THRESHOLD_NUM, 25), 1) * 0.5),
                                         GREATEST(COALESCE(c.THRESHOLD_NUM, 25), 1))
                 AND s.LAST_ACTIVE_DAY >= DATEADD('day', -3, s.WIN_END));
        n_cleared := SQLROWCOUNT;
    END IF;

    -- [receipt] LAST: only a fully evaluated week stops the rest of the week's calls
    UPDATE DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY
       SET RAISED = :n_raised, SUPERSEDED = :n_superseded, CLEARED = :n_cleared,
           COMPLETED_AT = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
     WHERE WEEK_START = :week_start AND ROW_KIND = 'ACCOUNT';

    RETURN 'sleep polling scan week of ' || TO_VARCHAR(:week_start) || ': ' || :n_pollers || ' poller(s) over '
           || TO_VARCHAR(DATEADD('day', -7, :newest)) || '..' || TO_VARCHAR(DATEADD('day', -1, :newest))
           || '; raised ' || :n_raised || ', superseded ' || :n_superseded || ', cleared ' || :n_cleared;
END;
$$;

-- >>> derived:SP_ALERT_SCAN_DAILY  (from V157; + [25] COST_SLEEP_POLLING counting CALL arm, tally 11 -> 12, V160)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- C9: daily-cadence sibling of SP_ALERT_SCAN. The 6 rule blocks whose signal
-- is a DAILY-loaded fact (FACT_TASK_DAILY, FACT_LOGIN_DAILY, FACT_METERING_DAILY)
-- moved here and chained AFTER TASK_LOAD_DAILY, so they scan once the daily
-- facts are fresh instead of 24x/day over stale/partial rows. Same v7 per-block
-- isolation and the SAME SETTINGS read (budget + credit + AI price) as the
-- hourly scan. The self-alert uses a DISTINCT '|DAILY|' dedupe key so it never
-- collides with the hourly OPS_SCAN_DEGRADED event on the same date.
DECLARE
    budget_usd FLOAT;
    credit_price FLOAT;
    ai_credit_price FLOAT;
    emsg VARCHAR;
    fails INT DEFAULT 0;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)
      INTO :budget_usd, :credit_price, :ai_credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- [06] PIPE_TASK_FAILURES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, tk.COMPANY, c.SEVERITY,
               COALESCE(tk.DATABASE_NAME || '.', '') || COALESCE(tk.SCHEMA_NAME || '.', '')
                   || tk.TASK_NAME || ' failed ' || tk.FAILED || 'x on ' || tk.DAY,
               'Database: ' || COALESCE(tk.DATABASE_NAME, 'unknown') || '. '
                   || LEFT(COALESCE(tk.LAST_ERROR, 'No error text captured.'), 450),
               tk.FAILED,
               c.RULE_ID || '|' || COALESCE(tk.DATABASE_NAME, '') || '.' || COALESCE(tk.SCHEMA_NAME, '') || '.' || tk.TASK_NAME || '|' || tk.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY tk
          ON c.RULE_ID = 'PIPE_TASK_FAILURES'
         AND tk.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND tk.FAILED >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PIPE_TASK_FAILURES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [07] SEC_FAILED_LOGINS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, lg.COMPANY, c.SEVERITY,
               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY,
               'Investigate credential stuffing / lockouts.',
               lg.FAILED_LOGINS,
               c.RULE_ID || '|' || lg.USER_NAME || '|' || lg.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY lg
          ON c.RULE_ID = 'SEC_FAILED_LOGINS'
         AND lg.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND lg.FAILED_LOGINS >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_FAILED_LOGINS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [08] COST_BUDGET_PACE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            -- V065 rank2: run-rate over COMPLETE days only (DAY < today). MTD_USD above is
            -- the month-to-date base (today's partial included, once); dividing it by the
            -- full day-of-month understated the daily rate -> under-projected the month-end
            -- forecast (COST_FORECAST_BREACH) -> could suppress the breach. Day 1 has no
            -- complete day -> NULLIF -> NULL rate -> no forecast alert that day.
            (SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'MTD spend $' || ROUND(m.MTD_USD, 0) || ' is ' ||
                   ROUND(m.MTD_USD / NULLIF(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 2) ||
                   'x the budget pace',
               'Budget $' || ROUND(:budget_usd, 0) || '/mo; elapsed-share allowance $' ||
                   ROUND(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0) || '.',
               m.MTD_USD,
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_BUDGET_PACE'
         AND :budget_usd > 0
         AND m.DAY_OF_MONTH > 1
         AND m.MTD_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_BUDGET_PACE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [09] COST_FORECAST_BREACH
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        mtd AS (
        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute
        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:
        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.
        SELECT
            SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,
            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,
            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,
            -- V065 rank2: run-rate over COMPLETE days only (DAY < today). MTD_USD above is
            -- the month-to-date base (today's partial included, once); dividing it by the
            -- full day-of-month understated the daily rate -> under-projected the month-end
            -- forecast (COST_FORECAST_BREACH) -> could suppress the breach. Day 1 has no
            -- complete day -> NULLIF -> NULL rate -> no forecast alert that day.
            (SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'Projected month-end $' ||
                   ROUND(m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH), 0) ||
                   ' exceeds budget $' || ROUND(:budget_usd, 0),
               'MTD $' || ROUND(m.MTD_USD, 0) || ' + $' || ROUND(m.DAILY_RATE_USD, 0) ||
                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH) || ' remaining days.',
               m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH),
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_FORECAST_BREACH'
         AND :budget_usd > 0
         AND (m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH))
             > :budget_usd * c.THRESHOLD_NUM

        -- Credential expiry: one event per credential per week until rotated
        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_FORECAST_BREACH - other rules unaffected', CURRENT_ROLE();
    END;
    -- [13b] COST_AI_CREEP
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_AI_CREEP: the canonical AI/Cortex bucket (SERVICE_TYPE ILIKE
        -- '%CORTEX%'/'AI%'/'%INTELLIGENCE%') from FACT_METERING_DAILY growing
        -- week-over-week, dollarized at the AI credit rate (AI_CREDIT_PRICE_USD,
        -- NOT the compute rate). COST_SERVERLESS_CREEP carves AI out; this rule
        -- owns it. Re-alerts weekly while creeping.
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'AI/Cortex spend up ' || ROUND(a.GROWTH_PCT, 0) || '% week-over-week ($' ||
                   ROUND(a.THIS_WK_USD, 0) || ' vs $' || ROUND(a.PRIOR_WK_USD, 0) || ' prior 7d)',
               'Last 7d ' || ROUND(a.THIS_WK_CR, 2) || ' AI credits ($' || ROUND(a.THIS_WK_USD, 2) ||
                   ' @ $' || ROUND(:ai_credit_price, 2) || '/cr) vs ' || ROUND(a.PRIOR_WK_CR, 2) ||
                   ' credits prior. Cortex/AI usage grows silently - confirm the workload is ' ||
                   'intentional and priced in. Breakdown: Cost > Spend (by service).',
               a.GROWTH_PCT,
               c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM cfg c
        JOIN (
            SELECT SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS THIS_WK_CR,
                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS PRIOR_WK_CR,
                   SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS THIS_WK_USD,
                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS PRIOR_WK_USD,
                   -- Onset (prior week 0) is an infinite ratio: emit a finite 999%
                   -- sentinel so a brand-new AI workload FIRES (the case budget-pace
                   -- misses) instead of GROWTH_PCT going NULL and dropping the row.
                   CASE WHEN SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) = 0
                        THEN IFF(SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) > 0, 999, 0)
                        ELSE (SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))
                              / SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))
                              - 1) * 100 END AS GROWTH_PCT
            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
            WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())
              AND DAY < CURRENT_DATE()   -- V065 rank3: exclude today so THIS_WK and PRIOR_WK are equal 7 complete days
              AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%')
        ) a ON c.RULE_ID = 'COST_AI_CREEP'
           AND a.THIS_WK_CR >= 5 AND a.GROWTH_PCT > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_AI_CREEP - other rules unaffected', CURRENT_ROLE();
    END;
    -- [16] COST_CONTRACT_BREACH
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_CONTRACT_BREACH: current contract projected to exhaust within
        -- threshold days at the trailing 30 complete-day burn rate. Weekly-recurring
        -- until the contract or the burn changes; CRITICAL inside 14 days. Also fires once the contract is already EXHAUSTED (DAYS_LEFT <= 0, over-contract / on-demand overage) with a distinct EXHAUSTED band so the WARN -> CRIT -> EXHAUSTED crossings each re-fire (cost-hunt6).
        SELECT c.RULE_ID, 'ALL',
               IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY),
               IFF(p.DAYS_LEFT <= 0,
                   'Contract EXHAUSTED: ' || ROUND(p.CONSUMED - p.TOTAL, 0) ||
                       ' credits over (crossed ' || TO_VARCHAR(p.EXHAUST_DATE) || ', ' ||
                       ABS(p.DAYS_LEFT) || ' day(s) ago)',
                   'Contract projected to exhaust in ' || p.DAYS_LEFT || ' day(s) (' ||
                       TO_VARCHAR(p.EXHAUST_DATE) || ')'),
               'Consumed ' || ROUND(p.CONSUMED, 0) || ' of ' || ROUND(p.TOTAL, 0) ||
                   ' contracted credits; trailing 30 complete-day burn ' || ROUND(p.DAILY_BURN, 1) ||
                   ' credits/day (straight-line). Scenario planning: Cost > Contract > Renewal planner.',
               p.DAYS_LEFT,
               c.RULE_ID || '|' || IFF(p.DAYS_LEFT <= 0, 'EXH', IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN')) || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))  -- V066 #2: band matches the CRITICAL severity so a mid-week HIGH->CRITICAL crossing re-fires
        FROM cfg c
        JOIN (
            SELECT TOTAL, CONSUMED, DAILY_BURN,
                   CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)) AS DAYS_LEFT,
                   DATEADD('day', CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)),
                           CURRENT_DATE()) AS EXHAUST_DATE
            FROM (
                SELECT
                    (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TOTAL,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY >= COALESCE(
                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL)))
                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), CURRENT_DATE())) AS CONSUMED,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                                   AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN
            )
        ) p ON c.RULE_ID = 'COST_CONTRACT_BREACH'
           AND p.TOTAL > 0 AND p.DAILY_BURN > 0
           AND p.DAYS_LEFT <= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_CONTRACT_BREACH - other rules unaffected', CURRENT_ROLE();
    END;
    -- [12] COST_STORAGE_SURGE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_STORAGE_SURGE: day-over-day database growth above threshold GB
        -- (the '600 GB in 4 days' class of surprise).
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess
               c.SEVERITY,
               g.DATABASE_NAME || ' grew ' || ROUND(g.GROWTH_GB, 1) || ' GB in a day',
               'From ' || ROUND(g.PREV_GB, 1) || ' GB to ' || ROUND(g.CUR_GB, 1) ||
                   ' GB on ' || TO_VARCHAR(g.USAGE_DATE) ||
                   '. Check for unbounded loads, missing retention, or runaway CTAS. Movers: Cost > Optimization.',
               g.GROWTH_GB,
               c.RULE_ID || '|' || g.DATABASE_NAME || '|' || TO_VARCHAR(g.USAGE_DATE)
        FROM cfg c
        JOIN (
            SELECT DATABASE_NAME, USAGE_DATE,
                   AVERAGE_DATABASE_BYTES / POWER(1024, 3) AS CUR_GB,
                   LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE)
                       / POWER(1024, 3) AS PREV_GB,
                   (AVERAGE_DATABASE_BYTES
                    - LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE))
                       / POWER(1024, 3) AS GROWTH_GB
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE DESC) = 1
        ) g ON c.RULE_ID = 'COST_STORAGE_SURGE'
           AND g.PREV_GB IS NOT NULL AND g.GROWTH_GB > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_STORAGE_SURGE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [13] COST_SERVERLESS_CREEP
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_SERVERLESS_CREEP: any serverless/managed service type doubling
        -- week-over-week (auto-clustering, MV refresh, search optimization,
        -- SPCS, serverless tasks, pipes...). Warehouses have their own daily-
        -- credit rules and AI has COST_AI_CREEP, so both are excluded here.
        -- Re-alerts weekly while creeping.
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               s.SERVICE_TYPE || ' credits up ' || ROUND(s.GROWTH_PCT, 0) || '% week-over-week',
               'Last 7d ' || ROUND(s.THIS_WK, 2) || ' credits vs ' || ROUND(s.PRIOR_WK, 2) ||
                   ' prior. Serverless spend grows silently - verify the feature is intentional ' ||
                   'and priced in. Breakdown: Cost > Spend (by service).',
               s.GROWTH_PCT,
               c.RULE_ID || '|' || s.SERVICE_TYPE || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM cfg c
        JOIN (
            SELECT SERVICE_TYPE,
                   SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) AS THIS_WK,
                   SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) AS PRIOR_WK,
                   -- V067 #20: onset (prior week 0) is an infinite ratio -> emit a finite 999%
                   -- sentinel so a brand-new serverless service FIRES (mirrors COST_AI_CREEP).
                   CASE WHEN SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) = 0
                        THEN IFF(SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) > 0, 999, 0)
                        ELSE (SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) / SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) - 1) * 100 END AS GROWTH_PCT
            FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
              AND USAGE_DATE < CURRENT_DATE()   -- V066 #6: exclude today so THIS_WK/PRIOR_WK are equal 7 complete days (mirrors V065 COST_AI_CREEP)
              AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')
              AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%CORTEX%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE 'AI%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%INTELLIGENCE%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%COCO%' AND COALESCE(SERVICE_TYPE, '') NOT ILIKE '%COWORK%'
            GROUP BY 1
            HAVING SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) >= 5
        ) s ON c.RULE_ID = 'COST_SERVERLESS_CREEP' AND s.GROWTH_PCT > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_SERVERLESS_CREEP - other rules unaffected', CURRENT_ROLE();
    END;
    -- [19] COST_EGRESS_SPIKE (V043 — the r25 panel, with teeth)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'Egress ' || eg.GB_24H || ' GB in 24h (14d avg ' || eg.GB_AVG_14D || ' GB/day)',
               'Top destination: ' || COALESCE(eg.TOP_REGION, '(same region)')
                   || '. Source: DATA_TRANSFER_HISTORY - drill in Security -> Egress.',
               eg.GB_24H,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN (
            SELECT ROUND(SUM(IFF(START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP()),
                                 BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 1) AS GB_24H,
                   ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(TARGET_REGION, BYTES_TRANSFERRED) AS TOP_REGION
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
        ) eg
          ON c.RULE_ID = 'COST_EGRESS_SPIKE'
         AND eg.GB_24H >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_EGRESS_SPIKE - other rules unaffected', CURRENT_ROLE();
    END;
    -- [22] OPS_PIPELINE_DEGRADED (V157, Next-Fifty #10: OVERWATCH watches its own pipeline from inside BOTH
    --      task graphs. Byte-identical in SP_ALERT_SCAN and SP_ALERT_SCAN_DAILY with shared dedupe keys, so
    --      whichever graph is still alive raises each finding once. (a) STALE: a SOURCE_FRESHNESS_STATE row
    --      past the shared name-rule cadence (DAILY/METERING in the name 30h, else 3h -- the app health strip,
    --      Admin, Control Room and NATIVE_ALERT_STALE_FACTS judge it the same way), including the scans' own
    --      heartbeat rows ALERT_SCAN_HOURLY / ALERT_SCAN_DAILY -- at most one event per source per last-load
    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged and swallowed
    --      (its task still reads SUCCEEDED) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS -- one
    --      event per (type, source, Central day). The three OPTIONAL SP_LOAD_MARTS_V27 arm sources (tag
    --      coverage, task node, AI usage) are left to the STALE leg, so a persistently failing optional arm
    --      raises once per episode instead of every day. (c) NOTIFY: SP_NOTIFY_WEBHOOK has not acquired its
    --      sender lease for 3h while a delivery route is enabled (V064 stamps OW_SENDER_LEASE.ACQUIRED_AT at
    --      the start of every run that acquires the lease). Clocks are pinned to Central -- every NTZ stamp
    --      read here is Central wall-clock -- and each free-text value is LEFT()-bounded to its column.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        fresh AS (
            SELECT SOURCE_NAME, LAST_LOAD_TS, STATUS,
                   DATEDIFF('minute', LAST_LOAD_TS,
                            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) AS AGE_MIN,
                   IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIM_H
            FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
        ),
        errs AS (
            SELECT ERROR_TYPE, SPLIT_PART(COALESCE(CONTEXT, ''), ' ', 1) AS SRC,
                   TO_DATE(LOGGED_AT) AS ERR_DAY, COUNT(*) AS N,
                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG
            FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            WHERE ERROR_TYPE IN ('mart_load_failed', 'fact_load_failed', 'extract_load_failed',
                                 'cloud_svc_mart_failed', 'object_cost_load_failed')
              AND LOGGED_AT >= DATEADD('hour', -24,
                                       CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
              AND SPLIT_PART(COALESCE(CONTEXT, ''), ' ', 1) NOT IN ('MART_TAG_COVERAGE_DAILY', 'MART_TASK_NODE_DAILY', 'FACT_AI_USAGE_DAILY')
            GROUP BY 1, 2, 3
        ),
        lease AS (
            SELECT ACQUIRED_AT,
                   DATEDIFF('minute', ACQUIRED_AT,
                            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) AS AGE_MIN
            FROM DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
            WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
              AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r WHERE r.ENABLED)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT(f.SOURCE_NAME || IFF(f.LAST_LOAD_TS IS NULL, ' has never loaded',
                   ' is stale: ' || FLOOR(f.AGE_MIN / 60) || 'h'
                   || IFF(MOD(f.AGE_MIN, 60) > 0, ' ' || MOD(f.AGE_MIN, 60) || 'm', '')
                   || ' since its last load (limit ' || ROUND(f.LIM_H) || 'h)'), 300),
               LEFT(IFF(f.SOURCE_NAME IN ('ALERT_SCAN_HOURLY', 'ALERT_SCAN_DAILY'),
                   'Heartbeat of ' || IFF(f.SOURCE_NAME = 'ALERT_SCAN_HOURLY',
                       'SP_ALERT_SCAN (hourly graph TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> TASK_ALERT_SCAN)',
                       'SP_ALERT_SCAN_DAILY (daily graph TASK_LOAD_DAILY -> TASK_NIGHTLY_RECONCILE -> TASK_ALERT_SCAN_DAILY)')
                   || ': the scan stopped, or its heartbeat stamp failed (APP_ERROR_LOG scan_heartbeat_failed). '
                   || 'SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH -- a root auto-suspends after 10 consecutive '
                   || 'failures (V071).',
                   'Loader-owned freshness row (SOURCE_FRESHNESS_STATE, status ' || COALESCE(f.STATUS, '—')
                   || '). A stalled loader, a suspended task or a failed arm leaves this row behind while '
                   || 'TASK_HISTORY still reads SUCCEEDED. Admin > Migrations & freshness > Diagnose stale sources; '
                   || 'snowflake/loader_chain_check.sql.'), 2000),
               ROUND(f.AGE_MIN / 60.0, 1),
               c.RULE_ID || '|STALE|' || f.SOURCE_NAME || '|' || COALESCE(TO_VARCHAR(TO_DATE(f.LAST_LOAD_TS)), 'NEVER')
        FROM cfg c
        JOIN fresh f
          ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
         AND (f.LAST_LOAD_TS IS NULL OR f.AGE_MIN / 60.0 > f.LIM_H)
        UNION ALL
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT(x.ERROR_TYPE || ': ' || x.SRC || ' failed ' || x.N || 'x on ' || TO_VARCHAR(x.ERR_DAY), 300),
               LEFT('The loader logged this and returned normally, so its task still reads SUCCEEDED and readers '
                   || 'keep the previous fill. Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '
                   || COALESCE(LEFT(x.LAST_MSG, 600), '—') || '. Admin > Errors & telemetry (persisted error log).', 2000),
               x.N,
               c.RULE_ID || '|ERR|' || x.ERROR_TYPE || '|' || x.SRC || '|' || TO_VARCHAR(x.ERR_DAY)
        FROM cfg c
        JOIN errs x ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
        UNION ALL
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT('Alert notifier idle: SP_NOTIFY_WEBHOOK ' || IFF(l.ACQUIRED_AT IS NULL, 'has never run',
                   'has not started a run in ' || FLOOR(l.AGE_MIN / 60) || 'h'
                   || IFF(MOD(l.AGE_MIN, 60) > 0, ' ' || MOD(l.AGE_MIN, 60) || 'm', ''))
                   || ' while a delivery route is enabled', 300),
               LEFT('TASK_ALERT_NOTIFY runs after TASK_ALERT_SCAN and stamps OW_SENDER_LEASE.ACQUIRED_AT at the start '
                   || 'of every run that acquires the lease (V064). Teams/webhook delivery has stopped: SHOW TASKS '
                   || 'LIKE ''TASK_ALERT_NOTIFY'' IN SCHEMA DBA_MAINT_DB.OVERWATCH; fix the cause, then ALTER TASK '
                   || '... RESUME.', 2000),
               ROUND(l.AGE_MIN / 60.0, 1),
               c.RULE_ID || '|NOTIFY|' || COALESCE(TO_VARCHAR(TO_DATE(l.ACQUIRED_AT)), 'NEVER')
        FROM cfg c
        JOIN lease l
          ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
         AND (l.ACQUIRED_AT IS NULL OR l.AGE_MIN > 180)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule OPS_PIPELINE_DEGRADED - other rules unaffected', CURRENT_ROLE();
    END;
    -- [24] COST_IDLE_OPPORTUNITY (V157, Next-Fifty #13: weekly idle-waste push closing the loop alert -> one
    --      ALTER -> change scan + SP_LEDGER_AUTOBOOK. DB-side twin of the Cost Intelligence > Optimization &
    --      Savings > Idle & sizing ACTIONABLE figure (insights.idle_advisor + remediation.tighten_suspend_plan):
    --      FLAGGED = >=20% idle AND >=1 idle credit; recoverable = idle minus one 60s resume tail per active
    --      metered hour; only a settings-VERIFIED timer (the newest WAREHOUSE_CONFIG_SNAPSHOT batch, <=36h
    --      old -- a warehouse missing from it, dropped or renamed, never raises) that is
    --      disabled (<=0) or above 60s. Trailing 14 COMPLETE Central days, run-rated over the days the mart
    --      covers (at least 7). One event per warehouse per ISO week (Monday, Central); the HIGH band (>= 5x
    --      threshold) re-fires mid-week and the V067 sweep supersedes the MED one.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        win AS (
            SELECT WAREHOUSE_NAME, DAY, BILLED_HOURS, ACTIVE_HOURS, CREDITS_TOTAL,
                   COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100) AS IDLE_CR
            FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
            CROSS JOIN clk
            WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY
              AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
        ),
        cov AS (
            SELECT COUNT(DISTINCT DAY) AS COVERED_DAYS FROM win
        ),
        idle AS (
            SELECT WAREHOUSE_NAME,
                   SUM(BILLED_HOURS) AS METERED_HOURS,
                   GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS,
                   SUM(CREDITS_TOTAL) AS TOTAL_CREDITS,
                   SUM(IDLE_CR) AS IDLE_CREDITS
            FROM win
            GROUP BY WAREHOUSE_NAME
            HAVING SUM(CREDITS_TOTAL) > 0
        ),
        scored AS (
            SELECT i.WAREHOUSE_NAME, i.TOTAL_CREDITS, i.IDLE_CREDITS, v.COVERED_DAYS,
                   ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,
                   GREATEST(i.IDLE_CREDITS
                            - GREATEST(COALESCE(i.METERED_HOURS, 0) - i.IDLE_HOURS, 0) * (60 / 3600.0)
                              * COALESCE(i.TOTAL_CREDITS / NULLIF(i.METERED_HOURS, 0), 0), 0) AS RECOVERABLE_CREDITS
            FROM idle i
            CROSS JOIN cov v
        ),
        newest AS (
            SELECT MAX(SNAPSHOT_AT) AS BATCH_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('hour', -36, CURRENT_TIMESTAMP())
        ),
        cur AS (
            SELECT s.WAREHOUSE_NAME, s.AUTO_SUSPEND, s.SNAPSHOT_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT s
            JOIN newest n ON s.SNAPSHOT_AT >= DATEADD('minute', -10, n.BATCH_AT)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(s.WAREHOUSE_NAME) ORDER BY s.SNAPSHOT_AT DESC) = 1
        ),
        opp AS (
            SELECT s.WAREHOUSE_NAME, s.TOTAL_CREDITS, s.IDLE_CREDITS, s.COVERED_DAYS, s.IDLE_PCT,
                   s.RECOVERABLE_CREDITS, w.AUTO_SUSPEND, w.SNAPSHOT_AT,
                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,
                   GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC
            FROM scored s
            JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(s.WAREHOUSE_NAME)
            WHERE s.COVERED_DAYS >= 7
              AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1
              AND w.AUTO_SUSPEND IS NOT NULL
              AND (w.AUTO_SUSPEND <= 0 OR w.AUTO_SUSPEND > 60)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL'),
               IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', c.SEVERITY),
               LEFT(o.WAREHOUSE_NAME || ' idle waste ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo: AUTO_SUSPEND '
                   || IFF(o.AUTO_SUSPEND <= 0, 'disabled', o.AUTO_SUSPEND || 's') || ' -> ' || o.TARGET_SEC || 's', 300),
               LEFT('Trailing ' || o.COVERED_DAYS || ' complete day(s): ' || ROUND(o.IDLE_CREDITS, 1) || ' of '
                   || ROUND(o.TOTAL_CREDITS, 1) || ' credits burned in hours with zero queries (' || o.IDLE_PCT
                   || '% idle). After the ~60s resume tail per active hour ' || ROUND(o.RECOVERABLE_CREDITS, 1)
                   || ' credits are recoverable: ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo at $' || ROUND(:credit_price, 2)
                   || '/credit (about the ACTIONABLE figure Optimize shows with a 14-day window; this alert uses 14 '
                   || 'complete days). Timer verified by the daily SHOW WAREHOUSES snapshot at '
                   || TO_VARCHAR(o.SNAPSHOT_AT, 'YYYY-MM-DD HH24:MI') || '. Fix: '
                   || IFF(REGEXP_LIKE(o.WAREHOUSE_NAME, '^[A-Za-z_][A-Za-z0-9_$]*$'),
                          'ALTER WAREHOUSE ' || UPPER(o.WAREHOUSE_NAME) || ' SET AUTO_SUSPEND = ' || o.TARGET_SEC || ';',
                          'the name needs quoting - generate the statement in Cost Intelligence > Optimization & Savings > Remediation & ledger.')
                   || IFF(o.AUTO_SUSPEND > 0,
                          ' The next daily change scan registers the lower timer and SP_LEDGER_AUTOBOOK books and settles the measured saving (a $0 closed-loop row booked from this alert is adopted as that booking, not duplicated).',
                          ' Enabling a timer on a never-suspend warehouse is not auto-booked (SP_LEDGER_AUTOBOOK books only a decrease from a positive timer) - book it in Cost Intelligence > Optimization & Savings > Remediation & ledger.'),
                   2000),
               o.MONTHLY_USD,
               c.RULE_ID || '|' || UPPER(o.WAREHOUSE_NAME) || '|'
                   || IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', 'MED') || '|'
                   || TO_VARCHAR(DATEADD('day', 1 - DAYOFWEEKISO(k.TODAY), k.TODAY))
        FROM cfg c
        JOIN opp o
          ON c.RULE_ID = 'COST_IDLE_OPPORTUNITY'
         AND o.MONTHLY_USD >= c.THRESHOLD_NUM
        CROSS JOIN clk k

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_IDLE_OPPORTUNITY - other rules unaffected', CURRENT_ROLE();
    END;
    -- [25] COST_SLEEP_POLLING (V160: chronic SYSTEM$WAIT sleep polling, the DB-side push of Cost > Spend > Which
    --      statement families bill the most cloud services. SP_SCAN_SLEEP_POLLING gates ITSELF: it works once per
    --      ISO week (Central) -- the first daily scan with no completed receipt and complete metering -- and
    --      otherwise returns after one small read; a not-due day counts as ok. COUNTING, like [24]: an internal
    --      mart-only rule, so a failure trips OPS_SCAN_DEGRADED and, with no receipt, the next daily scan redoes it.)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_SLEEP_POLLING - other rules unaffected', CURRENT_ROLE();
    END;
    -- [17] PIPE_REF_GAP  (optional external-dependency add-on: NOT counted toward the
    -- 6-core-rule scan-health tally, because its scan reads customer staging/XLAT tables that
    -- are SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        g AS (
            SELECT CHECK_NAME,
                   COUNT(*) AS N,
                   LISTAGG(NEW_CODE, ', ') WITHIN GROUP (ORDER BY NEW_CODE) AS CODES
            FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS
            GROUP BY CHECK_NAME
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               g.CHECK_NAME || ': ' || g.N || ' new source code(s) missing from XLAT',
               'The nightly load will fail on the missing code(s). Add the XLAT translation '
                   || 'row(s) before the next cycle. New codes: ' || LEFT(g.CODES, 1700),
               g.N,
               c.RULE_ID || '|' || g.CHECK_NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN g ON c.RULE_ID = 'PIPE_REF_GAP' AND g.N >= COALESCE(c.THRESHOLD_NUM, 1)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (unlike the six core arms). The ref-gap
            -- scan depends on SELECT grants on EXTERNAL customer tables applied out-of-band, so a
            -- grant gap (or any ref-gap-specific error) is recorded in APP_ERROR_LOG but must not
            -- trip the OPS_SCAN_DEGRADED self-alert about the core internal rules. Self-heals the
            -- moment grants land.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'ref_gap_scan_failed', :emsg,
                   'rule PIPE_REF_GAP - optional external add-on; needs SELECT on staging/XLAT tables', CURRENT_ROLE();
    END;
    -- [18] DQ_RECON_ERROR  (optional external-dependency add-on: NOT counted toward the
    -- 6-core-rule scan-health tally, because its scan reads the customer RECON_MTRC_ERROR table
    -- SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        r AS (
            SELECT COUNT(*) AS METRICS,
                   COALESCE(SUM(N), 0) AS ERRORS,
                   LISTAGG(MTRC, ', ') WITHIN GROUP (ORDER BY N DESC) AS TOP_METRICS
            FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               r.ERRORS || ' reconciliation error(s) across ' || r.METRICS || ' metric(s)',
               'Source and target layers did not reconcile in the last '
                   || COALESCE(c.WINDOW_HOURS, 48) || 'h -- investigate before the numbers are '
                   || 'trusted downstream. Metric(s): ' || LEFT(r.TOP_METRICS, 1700),
               r.ERRORS,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN r ON c.RULE_ID = 'DQ_RECON_ERROR' AND r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (like the ref-gap arm). The recon scan depends
            -- on a SELECT grant on the EXTERNAL RECON_MTRC_ERROR table applied out-of-band, so a grant
            -- gap (or any recon-specific error) is recorded in APP_ERROR_LOG but must not trip the
            -- OPS_SCAN_DEGRADED self-alert about the core internal rules. Self-heals when grants land.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'recon_scan_failed', :emsg,
                   'rule DQ_RECON_ERROR - optional external add-on; needs SELECT on RECON_MTRC_ERROR', CURRENT_ROLE();
    END;
    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' of 12 daily alert rule block(s) failed this run',
               'APP_ERROR_LOG has the SQL errors (rule_block_failed). The other rules '
                   || 'kept firing - that is the point of the v7 decomposition.',
               :fails,
               c.RULE_ID || '|DAILY|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_SCAN_DEGRADED' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|DAILY|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;

    -- [hb] scan heartbeat (V157, Next-Fifty #10b): stamp this scan's own SOURCE_FRESHNESS_STATE row LAST --
    -- after every arm and sweep -- so a row older than its cadence means the scan stopped (or this stamp
    -- keeps failing: APP_ERROR_LOG scan_heartbeat_failed). The hourly graph's [22] arm, the app freshness
    -- boards and NATIVE_ALERT_STALE_FACTS read it with the shared name rule (ALERT_SCAN_HOURLY -> 3h,
    -- ALERT_SCAN_DAILY -> 30h). LAST_LOAD_TS is Central wall-clock like every loader stamp. Cheapest shape:
    -- ONE point UPDATE of this scan's own row; the INSERT runs only when it matched no row (the first run,
    -- or after the row was deleted), so the stamp self-heals. Isolated; does NOT touch :fails.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
           SET LAST_LOAD_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
               ROW_COUNT = (12 - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = 'alert scan daily ' || (12 - :fails) || '/12 rule blocks ok (daily)'
         WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT 'ALERT_SCAN_DAILY', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   (12 - :fails), 1, 'alert scan daily ' || (12 - :fails) || '/12 rule blocks ok (daily)';
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   'ALERT_SCAN_DAILY heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan daily v4 (V160: + COST_SLEEP_POLLING weekly sleep-polling push): ' || (12 - :fails) || '/12 rule blocks ok (daily)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 160 AS VERSION,
       'COST_SLEEP_POLLING: the chronic SYSTEM$WAIT sleep-polling alert, the DB-side push of the v4.595 Cost > Spend panel Which statement families bill the most cloud services (V150 COST_CLOUD_SVC_ANOMALY can never fire on a chronic poller, it is its own baseline). + SLEEP_POLLING_WEEKLY (transient weekly census: POLLER rows + an ACCOUNT receipt row = the panel group total). + SP_SCAN_SLEEP_POLLING(BOOLEAN): poller = warehouse x user, or x task owner role for SYSTEM; billed like the panel over the 7 newest complete metering days (per day the smaller of the poller credits and the account billed cloud services, capped as one group, x CREDIT_PRICE_USD); sleep = the app statement shape (system_wait.SLEEP_SQL_PATTERN, not DDL, not a multi-statement parent); MEDIUM at 5+ of 7 days and at least THRESHOLD_NUM USD/week (25), HIGH at 5x; one event per poller per episode (live hold, NOISE/EXPECTED mute 28 days per band, ACTIONED re-raises only on later polling, a live HIGH supersedes the MED); CONDITION_ENDED self-clear (OPEN only, 1h dwell) once the poller stops, bills under the clear level or is idle 4 days. SP_ALERT_SCAN_DAILY re-derived from V157, byte-identical except the counting arm [25] (the proc works once per ISO week behind a receipt, retried daily) and tally 11 -> 12. Seeds COST_SLEEP_POLLING (COST, MEDIUM, 25, 168h, AUTO_CLEAR_ENABLED TRUE) WHEN NOT MATCHED only. No task change, no SETTINGS key, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 160);
