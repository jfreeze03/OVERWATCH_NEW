-- PREFLIGHT_SLEEP_POLLING.sql -- READ-ONLY preview of V160's COST_SLEEP_POLLING first evaluation.
-- Run BEFORE applying V160 (or any time). Lists every sleep poller over the 7 newest COMPLETE metering days with
-- its billed USD/week and WOULD_RAISE (active on 5+ of the 7 days and USD_WEEK >= threshold; HIGH at 5x),
-- plus the ACCOUNT row (= the Spend panel's sleep-polling billed credits for the same days). Changes nothing.
-- The threshold falls back to 25 USD/week until V160 seeds the rule. DEDUPE_KEY_PREVIEW carries THIS
-- ISO week's Monday; the scan's real key uses the Monday of the day the daily scan evaluates (run on a Sunday,
-- the preview shows the week just ending while Monday's evaluation keys the new week).
WITH clk AS (
        SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY,
               DATEADD('day', 1 - DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE),
                       CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS WEEK_START
    ),
    mx AS (SELECT MAX(z.DAY) AS NEWEST FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY z),
    px AS (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE
           FROM DBA_MAINT_DB.OVERWATCH.SETTINGS),
    thr AS (SELECT COALESCE(MAX(IFF(c.ENABLED, GREATEST(COALESCE(c.THRESHOLD_NUM, 25), 1), NULL)), 25) AS THR
            FROM (SELECT 1 AS ONE) o
            LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = 'COST_SLEEP_POLLING'),
    bill AS (      -- = panel bill: account-wide, all service types, complete metering days only
        SELECT x.DAY, SUM(COALESCE(x.CREDITS_CLOUD_SVCS, 0) + COALESCE(x.CREDITS_ADJUSTMENT, 0)) AS CS_BILLED_DAY
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY x
        WHERE x.DAY >= DATEADD('day', -7, (SELECT NEWEST FROM mx)) AND x.DAY < (SELECT NEWEST FROM mx)
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
              WHERE a.DAY >= DATEADD('day', -7, (SELECT NEWEST FROM mx)) AND a.DAY < (SELECT NEWEST FROM mx) AND a.APPLICATION <> '(unknown)'
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
    np AS (SELECT COUNT(*) AS N FROM pb),
    census (WEEK_START, ROW_KIND, POLLER_KEY, WAREHOUSE_NAME, USER_NAME, TASK_ROLE, USER_TOP_APP, OWNER_HINT, CALLS, FAMILIES, POLLERS, RUNS, ACTIVE_DAYS, LAST_ACTIVE_DAY, CS_CREDITS, BILLED_CS_CREDITS, USD_WEEK, ABOVE_ALLOWANCE_DAYS, WIN_START, WIN_END, CREDIT_PRICE_USD) AS (
    SELECT (SELECT WEEK_START FROM clk), 'POLLER', a.POLLER_KEY, a.WAREHOUSE_NAME, a.USER_NAME, a.TASK_ROLE, ap.USER_TOP_APP,
           IFF(UPPER(a.USER_NAME) = 'SYSTEM',                                   -- SQL twin of cs_driver.owner_hint
               IFF(a.TASK_ROLE <> '', 'Task owner (role ' || a.TASK_ROLE || ')', 'Task owner'),
               IFF(COALESCE(ap.USER_TOP_APP, '') <> '', ap.USER_TOP_APP || ' · ' || a.USER_NAME, 'User ' || a.USER_NAME)),
           a.CALLS, a.FAMILIES, NULL, b.RUNS, b.ACTIVE_DAYS, b.LAST_ACTIVE_DAY, b.CS_CREDITS, b.BILLED_CS_CREDITS,
           ROUND(b.BILLED_CS_CREDITS * (SELECT PRICE FROM px), 2), w.ABOVE_DAYS, w.WIN_START, w.WIN_END, (SELECT PRICE FROM px)
    FROM pc a
    JOIN pb b ON b.POLLER_KEY = a.POLLER_KEY
    CROSS JOIN win w
    LEFT JOIN app ap ON ap.USER_NAME = a.USER_NAME
    UNION ALL
    SELECT (SELECT WEEK_START FROM clk), 'ACCOUNT', 'COST_SLEEP_POLLING|*|*|', NULL, NULL, NULL, NULL, NULL, NULL,
           nf.N, np.N, s.RUNS, s.ACTIVE_DAYS, s.LAST_ACTIVE_DAY, s.CS_CREDITS, s.BILLED_CS_CREDITS,
           ROUND(COALESCE(s.BILLED_CS_CREDITS, 0) * (SELECT PRICE FROM px), 2), w.ABOVE_DAYS, w.WIN_START, w.WIN_END, (SELECT PRICE FROM px)
    FROM acct s CROSS JOIN win w CROSS JOIN nf CROSS JOIN np
    )
SELECT c.ROW_KIND, c.WAREHOUSE_NAME, c.OWNER_HINT, c.CALLS, c.FAMILIES, c.RUNS, c.ACTIVE_DAYS, c.LAST_ACTIVE_DAY,
       ROUND(c.CS_CREDITS, 2) AS CS_CREDITS, ROUND(c.BILLED_CS_CREDITS, 2) AS BILLED_CS_CREDITS, c.USD_WEEK,
       c.ABOVE_ALLOWANCE_DAYS, c.WIN_START, c.WIN_END,
       IFF(c.ROW_KIND = 'POLLER', c.ACTIVE_DAYS >= 5 AND c.USD_WEEK >= t.THR, NULL) AS WOULD_RAISE,
       IFF(c.ROW_KIND = 'POLLER', IFF(c.USD_WEEK >= t.THR * 5, 'HIGH', 'MED'), NULL) AS BAND,
       IFF(c.ROW_KIND = 'POLLER', c.POLLER_KEY || IFF(c.USD_WEEK >= t.THR * 5, 'HIGH', 'MED') || '|'
                                  || TO_VARCHAR(c.WEEK_START), NULL) AS DEDUPE_KEY_PREVIEW
FROM census c
CROSS JOIN thr t
ORDER BY c.ROW_KIND DESC, c.USD_WEEK DESC NULLS LAST;
