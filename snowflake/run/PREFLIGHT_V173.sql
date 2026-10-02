-- Run as SNOW_ACCOUNTADMINS (read-only).
USE ROLE SNOW_ACCOUNTADMINS;
-- ====================================================================================================
--  V173 PREFLIGHT (read-only; run BEFORE applying V173). Each grid runs the arms' OWN V173 text
--  (outputs/gen_v173.py). The first statement pins the session to Central: RAISED_AT, LOGGED_AT and APPLIED_AT are
--  Central wall-clock. Changes nothing.
-- ====================================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P173.1 the evidence since V168's apply: the two failing arms (rule_block_failed for SEC_NEW_ADMIN_NETWORK hourly,
--        COST_IDLE_OPPORTUNITY daily) and three watch items the subquery sweep rated possible, not likely (the V067
--        supersede sweep, arm [10] SEC_CRED_EXPIRY, the V171 ref-gap scan). Expect rows for the first two only.
SELECT l.ERROR_TYPE, l.CONTEXT, COUNT(*) AS N, MIN(l.LOGGED_AT) AS FIRST_AT, MAX(l.LOGGED_AT) AS LAST_AT,
       LEFT(MAX_BY(l.ERROR_MESSAGE, l.LOGGED_AT), 300) AS LAST_MESSAGE
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
WHERE l.LOGGED_AT >= DATEADD('day', -2, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168))
  AND ((l.ERROR_TYPE = 'rule_block_failed'
        AND (l.CONTEXT LIKE 'rule SEC_NEW_ADMIN_NETWORK %' OR l.CONTEXT LIKE 'rule COST_IDLE_OPPORTUNITY %'
             OR l.CONTEXT LIKE 'rule SEC_CRED_EXPIRY %'))
       OR l.ERROR_TYPE IN ('supersede_sweep_failed', 'ref_gap_scan_failed', 'ref_gap_check_failed'))
GROUP BY 1, 2
ORDER BY 1, 2;

-- P173.2 what the next hourly scan raises for SEC_NEW_ADMIN_NETWORK after V173 (arm [18]'s own statement, its new guard
--        included): admin pairs first seen in the last 24h that have no event yet.
WITH cfg AS (
    SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
),
recent AS (
    -- V173: this rule's events raised in the last 48h, the date-stripped head precomputed. It
    -- carries the RULE_ID = b.RULE_ID (every b row is this rule) and 48h legs of the V168 guard,
    -- uncorrelated; a NULL key can never satisfy the two guards that read it.
    SELECT DEDUPE_KEY,
           LENGTH(DEDUPE_KEY) AS KEY_LEN,
           LEFT(DEDUPE_KEY, LENGTH(DEDUPE_KEY) - 10) AS KEY_HEAD
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
      AND RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
      AND DEDUPE_KEY IS NOT NULL
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT c.RULE_ID, 'ALL', c.SEVERITY,
       LEFT(nn.USER_NAME || IFF(nn.SUCCESSES > 0,
           ' logged in from new network ' || nn.CLIENT_IP,
           ': ' || nn.LOGINS || ' failed login attempt(s) from new network ' || nn.CLIENT_IP
               || ' (0 successful)'), 300),
       'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline; successful '
           || nn.SUCCESSES || ' of ' || nn.LOGINS || ' attempt(s). Auth: '
           || COALESCE(nn.AUTH_FACTOR, '?')
           || IFF(nn.SUCCESSES > 0,
                  '. Expected after travel/VPN/host changes; anything else is the finding.',
                  '. No attempt got in; a success from this IP inside its first 24h raises a separate event.'),
       nn.LOGINS,
       c.RULE_ID || '|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, '', '|FAILED')
           || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', nn.FIRST_SEEN)), 'YYYY-MM-DD')
FROM cfg c
JOIN (
    SELECT L.USER_NAME,
           COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
           MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
           COUNT(*) AS LOGINS,
           COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,
           MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
    JOIN (
        SELECT DISTINCT GRANTEE_NAME
        FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
        WHERE DELETED_ON IS NULL
          AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
    ) A ON A.GRANTEE_NAME = L.USER_NAME
    WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1, 2
    HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
) nn
  ON c.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
 AND nn.LOGINS >= c.THRESHOLD_NUM

) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
-- V173: the V168 guard as three AND-ed NOT EXISTS, each correlated by plain equalities only. V168 had one
-- NOT EXISTS whose every outer reference sat under an OR, which Snowflake cannot decorrelate ('Unsupported
-- subquery type cannot be evaluated', every hourly run from 2026-10-02 07:08). NOT EXISTS (A OR B OR C) =
-- NOT EXISTS (A) AND NOT EXISTS (B) AND NOT EXISTS (C): the same rows are kept.
WHERE NOT EXISTS (   -- (1) the exact key (user|IP[|FAILED]|first-seen day), any age: R2-036
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
)
  AND NOT EXISTS (   -- (2) this pair's V162 undated key (date and '|FAILED' stripped), last 48h
    SELECT 1 FROM recent r
    WHERE r.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')
)
  AND NOT EXISTS (   -- (3) the same exact base and outcome on another first-seen day, last 48h: R2-039
    SELECT 1 FROM recent r
    WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)
      AND r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)
);

-- P173.3 a preview: every admin user + IP pair first seen (against the 90-day baseline) from 24h before V168's apply
--        (V162's last good run could not see the pairs LOGIN_HISTORY had not landed yet) and whether any
--        SEC_NEW_ADMIN_NETWORK event exists for it. IN_ARM_WINDOW_NOW is measured now, not at the first scan after the apply:
--        the exact list to review by hand is PART B V173.4, once PART B V173.2 reads OK.
WITH nn AS (
            SELECT L.USER_NAME,
                   COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
                   MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
                   COUNT(*) AS LOGINS,
                   COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,
                   MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
            JOIN (
                SELECT DISTINCT GRANTEE_NAME
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
            ) A ON A.GRANTEE_NAME = L.USER_NAME
            WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
            GROUP BY 1, 2
            HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168))
        ),
ev AS (
    SELECT DISTINCT SPLIT_PART(DEDUPE_KEY, '|', 2) AS USER_PART, SPLIT_PART(DEDUPE_KEY, '|', 3) AS IP_PART
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
      AND RAISED_AT >= DATEADD('day', -2, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168))
)
SELECT nn.USER_NAME, nn.CLIENT_IP, nn.FIRST_SEEN, nn.LOGINS, nn.SUCCESSES,
       nn.FIRST_SEEN >= DATEADD('hour', -24, CURRENT_TIMESTAMP()) AS IN_ARM_WINDOW_NOW,
       ev.USER_PART IS NULL AS NOT_ALERTED
FROM nn
LEFT JOIN ev
       ON ev.USER_PART = LEFT(nn.USER_NAME, 200)
      AND ev.IP_PART = nn.CLIENT_IP
ORDER BY nn.FIRST_SEEN;

-- P173.4 the zero-credit warehouses of arm [24]'s 14-day window (the arm's own clk + win CTEs): the rows that
--        reached IDLE_CREDITS / TOTAL_CREDITS before the HAVING dropped them. V173 never divides by them.
WITH clk AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
),
win AS (
    SELECT WAREHOUSE_NAME, DAY, BILLED_HOURS, ACTIVE_HOURS, CREDITS_TOTAL,
           COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100) AS IDLE_CR
    FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
    CROSS JOIN clk
    WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY
      AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
)
SELECT WAREHOUSE_NAME, COUNT(*) AS DAYS, SUM(CREDITS_TOTAL) AS TOTAL_CREDITS, SUM(BILLED_HOURS) AS BILLED_HOURS,
       SUM(ACTIVE_HOURS) AS ACTIVE_HOURS
FROM win
GROUP BY WAREHOUSE_NAME
HAVING SUM(CREDITS_TOTAL) = 0
ORDER BY WAREHOUSE_NAME;

-- P173.5 what the next daily scan raises for COST_IDLE_OPPORTUNITY after V173 (arm [24]'s own statement, the price
--        read from SETTINGS like the scan).
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
           ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) AS IDLE_PCT,
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
           s.RECOVERABLE_CREDITS, COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND, w.SNAPSHOT_AT,
           ROUND(s.RECOVERABLE_CREDITS * (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) / NULLIF(s.COVERED_DAYS, 0) * 30, 2) AS MONTHLY_USD,
           GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC
    FROM scored s
    JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(s.WAREHOUSE_NAME)
    WHERE s.COVERED_DAYS >= 7
      AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1
      AND (COALESCE(w.AUTO_SUSPEND, 0) <= 0 OR w.AUTO_SUSPEND > 60)
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
           || ' credits are recoverable: ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo at $' || ROUND((SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), 2)
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
