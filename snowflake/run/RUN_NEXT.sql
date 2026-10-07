-- #####################################################################
--  OVERWATCH -- RUN_NEXT.sql (2026-10-07): APPLY V175 -- the admin-access lookup as an owner-run procedure
--  (SP_ADMIN_ROLE_MEMBERS), so DSA admins keep their admin rights after SNOW_SYSADMINS takes over the app.
--  The app decides who is an admin by listing SNOW_PRI_GFR_PRD_ALFA_DSA's users. Today it runs SHOW GRANTS OF ROLE as
--  the app owner, and SHOW lists only what the owner role can see. V175 creates a procedure that runs the same SHOW
--  as SNOW_ACCOUNTADMINS whatever role owns the app; app 4.610.2 CALLs it once V175 is applied.
--  Safe to apply now: until the owner switch the CALL lists exactly what SHOW lists. Nothing runs at apply time.
--
--  This file carries V174 first because V175 refuses to run without it (-20175). If V174 is already applied
--  (PART_B_V174.sql's V174.1 reads OK), re-running its section is harmless (it re-creates SP_ALERT_SCAN identically);
--  or start at the ">>> V175" banner.
--
--  ORDER (all files in snowflake/run/):
--   1. PREFLIGHT_V175.sql (read-only): P175.0 where the migrations stand; P175.1 the DSA users the procedure will
--      return; P175.2 SNOW_SYSADMINS exists; P175.3 the procedure is new; P175.4 no future OWNERSHIP grant on
--      procedures (any row there: stop and paste it back).
--   2. THIS FILE, top to bottom, as SNOW_ACCOUNTADMINS. Any time of day; no app redeploy needed for it.
--   3. PART_B_V175.sql right after: V175.1 (version row, procedure owned by SNOW_ACCOUNTADMINS, USAGE for
--      SNOW_SYSADMINS) and V175.2 (the CALL lists the same users as SHOW). V175.3 (optional, run last) repeats the
--      CALL as SNOW_SYSADMINS: the cutover check.
--   If V174 was applied by this file (not before), also run PART_B_V174.sql (V174.1 now, V174.2 after the next hourly
--   scan).
--  The migration text below is byte-identical to snowflake/migrations/V174__alert_scan_dsa_admin_role.sql and
--  V175__admin_role_members_proc.sql on branch v46102-admin-role-members (commit 64dca788).
-- #####################################################################

USE ROLE SNOW_ACCOUNTADMINS;
USE SCHEMA DBA_MAINT_DB.OVERWATCH;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- >>> V174__alert_scan_dsa_admin_role.sql
-- V174__alert_scan_dsa_admin_role.sql
--
-- The owner's 2026-10-05 access decision: every direct holder of SNOW_PRI_GFR_PRD_ALFA_DSA is an OVERWATCH
-- admin (app 4.610: every page, the in-app writes and the account-level levers, like the named admins). So a grant
-- of that role is an admin grant and its holders are admin users, and the hourly security arms that enumerate admin
-- roles now watch it too (design D14).
--
-- WHY: until V174 a grant of SNOW_PRI_GFR_PRD_ALFA_DSA to a user raised no SEC_ADMIN_GRANT (only a risk-80 PRIVILEGE
-- change in Security > Changes, V166), a takeover of a holder was CRITICAL only off-hours, and a holder's login from a
-- new network raised no SEC_NEW_ADMIN_NETWORK: an OVERWATCH admin, watched as a regular user.
--
--   ~ SP_ALERT_SCAN re-derived from V173 (its current definer), byte-identical except three arms -- the only arms of
--     either scan whose SQL lists admin roles -- each adding SNOW_PRI_GFR_PRD_ALFA_DSA at the end of its one list, plus
--     a two-line V174 note above the arm's BEGIN: [18] SEC_NEW_ADMIN_NETWORK (the admin users whose new networks it
--     watches), [26] SEC_LOGIN_TAKEOVER (the admin-tier roles that make a takeover CRITICAL) and [27] SEC_ADMIN_GRANT
--     (the admin-tier roles whose direct grant raises). [26] and [27] keep one list, the app's ALERT_ADMIN_ROLES.
--   ~ The SEC_ADMIN_GRANT rule NAME lists the role too (only while NAME still equals its V162 seed).
--   SP_ALERT_SCAN_DAILY is unchanged (no daily arm lists admin roles); so are the V166 / V167 break-glass lists
--   (ACCOUNTADMIN and SNOW_ACCOUNTADMINS, a different tier). The RETURN labels, the 14-block tally, every other arm,
--   sweep and gate are unchanged.
--
-- COST: none (three IN lists one role longer; no new read).
-- LATENCY: hourly (:07 Central), as before.
-- FIRST RUN: the next hourly scan raises, for SNOW_PRI_GFR_PRD_ALFA_DSA only, what a watched role would have raised in
-- the arms' own windows: one SEC_ADMIN_GRANT (HIGH) per direct grant created in the last 26h (a grant made earlier
-- never raises), a SEC_NEW_ADMIN_NETWORK for a holder's user + IP pair first seen in the last 24h, and a CRITICAL
-- SEC_LOGIN_TAKEOVER for a holder's episode in the last 24h -- one already raised as the WARN band re-raises as CRIT.
-- The V067 sweep supersedes that WARN only while it is OPEN or ACK: a WARN already resolved or snoozed re-opens as a
-- fresh CRITICAL that stays OPEN (the snooze does not carry over: the CRIT key is not the WARN key) and routes and
-- escalates like any CRITICAL, so resolve or snooze it the same way. PREFLIGHT P174.1 lists the direct holders,
-- P174.2-P174.4 what each arm will raise (P174.3 with each CRIT twin's WARN state); PART B V174.3 lists what it did
-- raise. SEC_ADMIN_GRANT and SEC_LOGIN_TAKEOVER never auto-declare an incident (V162).
-- ROLLBACK: re-run V173's SP_ALERT_SCAN (the CREATE PROCEDURE in V173__alert_scan_supported_subquery_and_div0.sql);
-- the rule NAME refresh is cosmetic and can stay. Prefer disabling a rule in Alerts > Rules.
-- Apply AFTER V173 (alone, any time; no repairs). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20174, 'V174 requires V173 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 173) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN  (from V173; + SNOW_PRI_GFR_PRD_ALFA_DSA at the end of the admin-role lists of arms [18] SEC_NEW_ADMIN_NETWORK, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT -- owner access decision 2026-10-05, V174)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- v7: every rule block runs in its OWN isolated INSERT with per-block
-- exception capture. One broken rule (revoked view, bad division, drift)
-- logs and increments a counter instead of silently killing ALL alerting —
-- the review's 'ticking bomb' finding, defused. Dedupe semantics unchanged.
DECLARE
    credit_price FLOAT;
    emsg VARCHAR;
    fails INT DEFAULT 0;
    ct_hour INT DEFAULT 5;   -- V157: the Central hour of this run ([cadence] below); 5 sits in both slots
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)   -- V168: the rate arm [17] binds
      INTO :credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- [cadence] V157 compile diet (Next-Fifty wave 2b rework): the Central hour this run started in, read
    -- ONCE. A gated block skips its whole statement -- nothing compiles, no ACCOUNT_USAGE read -- and a
    -- gated-off arm counts as ok (it never touches :fails):
    --   MOD(ct_hour, 4) = 1  (01,05,09,13,17,21 Central): arms [10] SEC_CRED_EXPIRY and [20] SEC_NEW_EXPOSURE,
    --                        and each rule's condition-ended clear (a clear rides its raise arm's slot);
    --   MOD(ct_hour, 3) = 2  (02,05,08,11,14,17,20,23 Central): [22] OPS_PIPELINE_DEGRADED (the daily scan's
    --                        copy stays daily).
    -- TASK_LOAD_HOURLY fires at :07 Central (CRON, DST-aware), so each slot is one run a day (a DST night can
    -- repeat or skip one slot; the dedupe keys absorb a repeat). If this read ever fails, ct_hour keeps its
    -- DEFAULT 5 -- inside BOTH slots -- so every gated block runs (fail-open to hourly) and the
    -- failure is logged (cadence_gate_failed). Does NOT touch :fails.
    BEGIN
        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'cadence_gate_failed', :emsg,
                   'V157 Central-hour read - every gated block runs this pass', CURRENT_ROLE();
    END;

    -- [wake] V086: return expired per-event snoozes to the triage feed. A snoozed
    -- event sits at STATUS='SNOOZED' (off the OPEN/ACK feed); once its wake time has
    -- passed it goes back to OPEN so it re-surfaces. Isolated + does NOT touch `fails`.
    BEGIN
        -- Restore the TRUE prior status: an ACK'd event that was snoozed wakes back
        -- to ACK (its ACK_BY/ACK_AT are intact), a never-acked one to OPEN. Waking an
        -- acked event to OPEN would strand a stale ACK_AT on an 'open' row and let a
        -- re-ack overwrite it (inflating MTTA). Clear the transient snooze metadata.
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
           SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN'),
               SNOOZED_UNTIL = NULL, SNOOZE_BY = NULL, SNOOZE_REASON = NULL
         WHERE STATUS = 'SNOOZED'
           AND SNOOZED_UNTIL IS NOT NULL
           AND SNOOZED_UNTIL <= CURRENT_TIMESTAMP();
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'snooze_wake_failed', :emsg,
                   'V086 un-snooze - other rules unaffected', CURRENT_ROLE();
    END;

    -- [01] COST_DAILY_CREDITS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL' AS COMPANY, c.SEVERITY,
               'Account daily credits ' || ROUND(f.CREDITS, 1) || ' >= ' || c.THRESHOLD_NUM AS TITLE,
               'Warehouse metering total for ' || f.DAY AS DETAIL,
               f.CREDITS AS METRIC_VALUE,
               c.RULE_ID || '|ALL|' || f.DAY AS DEDUPE_KEY
        FROM cfg c
        JOIN (
            SELECT DAY, SUM(CREDITS_TOTAL) AS CREDITS
            FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
            WHERE DAY >= DATEADD('day', -1, CURRENT_DATE())
            GROUP BY DAY
        ) f ON c.RULE_ID = 'COST_DAILY_CREDITS' AND f.CREDITS >= c.THRESHOLD_NUM

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
                   'rule COST_DAILY_CREDITS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [02] COST_WH_DAILY_CREDITS
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, f.COMPANY, c.SEVERITY,
               f.WAREHOUSE_NAME || ' used ' || ROUND(f.CREDITS_TOTAL, 1) || ' credits on ' || f.DAY,
               'Per-warehouse daily metering.',
               f.CREDITS_TOTAL,
               c.RULE_ID || '|' || f.WAREHOUSE_NAME || '|' || f.DAY
        FROM cfg c
        JOIN DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY f
          ON c.RULE_ID = 'COST_WH_DAILY_CREDITS'
         AND f.DAY >= DATEADD('day', -1, CURRENT_DATE())
         AND f.CREDITS_TOTAL >= c.THRESHOLD_NUM

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
                   'rule COST_WH_DAILY_CREDITS - other rules unaffected', CURRENT_ROLE();
    END;
    -- [03] PERF_QUERY_FAIL_PCT
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               'Query failure rate ' || ROUND(q.FAIL_PCT, 1) || '% >= ' || c.THRESHOLD_NUM || '%',
               q.FAILED || ' of ' || q.TOTAL || ' queries failed in last 24h.',
               q.FAIL_PCT,
               c.RULE_ID || '|' || q.COMPANY || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, SUM(FAILED_COUNT) AS FAILED, SUM(QUERY_COUNT) AS TOTAL,
                   IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY COMPANY
            HAVING SUM(QUERY_COUNT) >= 20
        ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT' AND q.FAIL_PCT >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PERF_QUERY_FAIL_PCT - other rules unaffected', CURRENT_ROLE();
    END;
    -- [04] PERF_QUEUED_MINUTES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               q.WAREHOUSE_NAME || ' queued ' || ROUND(q.QUEUED_MIN, 1) || ' min in 24h',
               'Queued overload + provisioning time.',
               q.QUEUED_MIN,
               c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_NAME IS NOT NULL
            GROUP BY COMPANY, WAREHOUSE_NAME
        ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES' AND q.QUEUED_MIN >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PERF_QUEUED_MINUTES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [05] PERF_SPILL_GB
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, q.COMPANY, c.SEVERITY,
               q.WAREHOUSE_NAME || ' spilled ' || ROUND(q.SPILL_GB, 1) || ' GB remote in 24h',
               'Remote spill indicates undersized memory for the workload.',
               q.SPILL_GB,
               c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
        FROM cfg c
        JOIN (
            SELECT COMPANY, WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
            FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
            WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND WAREHOUSE_NAME IS NOT NULL
            GROUP BY COMPANY, WAREHOUSE_NAME
        ) q ON c.RULE_ID = 'PERF_SPILL_GB' AND q.SPILL_GB >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule PERF_SPILL_GB - other rules unaffected', CURRENT_ROLE();
    END;
    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [10] every 4h (01,05,09,13,17,21 Central)
    -- [10] SEC_CRED_EXPIRY
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(cr.USER_NAME),
               IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'CRITICAL', c.SEVERITY),
               cr.USER_NAME || ' ' || LOWER(cr.TYPE) || ' ''' || cr.NAME || ''' ' ||
                   IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(),
                       'EXPIRED ' || ABS(DATEDIFF('day', cr.EXPIRATION_DATE, CURRENT_TIMESTAMP())) || ' day(s) ago',
                       'expires in ' || DATEDIFF('day', CURRENT_TIMESTAMP(), cr.EXPIRATION_DATE) || ' day(s)'),
               -- V157: this date is the cycle id the dedupe below matches; pinned to Central so a hand-run scan in another
               -- session timezone writes the same date the scheduled scans always have
               'Rotate before ' || TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', cr.EXPIRATION_DATE)::TIMESTAMP_NTZ, 'YYYY-MM-DD') ||
                   ' to avoid auth failures for jobs and integrations using this credential.',
               DATEDIFF('day', CURRENT_TIMESTAMP(), cr.EXPIRATION_DATE),
               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING'),
               cr.EXPIRATION_DATE    -- V157: EXP_TS (this cycle's expiry: the dedupe's cycle id, not inserted)
        FROM cfg c
        JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr
          ON c.RULE_ID = 'SEC_CRED_EXPIRY'
         -- v9: CREDENTIALS on this account has no DELETED_ON column (the
         -- sibling of the EXPIRES_AT discovery v8 fixed) - live error
         -- 2026-07-08. Without this fix, applying v8 swaps the hourly
         -- EXPIRES_AT failure for an hourly DELETED_ON failure.
         AND cr.EXPIRATION_DATE IS NOT NULL
         AND cr.EXPIRATION_DATE <= DATEADD('day', c.THRESHOLD_NUM, CURRENT_TIMESTAMP())

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY, EXP_TS)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') NOT IN ('CONDITION_ENDED', 'SUPERSEDED')   -- V157: a machine close never blocks
              -- V157: the key has no date, so the cycle id is the expiry date every arm [10] since V009 writes at the head
              -- of DETAIL (Rotate before YYYY-MM-DD, both bands). A CLOSED row (by anyone: ACTIONED, NOISE, EXPECTED, a bulk
              -- clear, however late) blocks only when it was raised for THIS expiry, never by when it was raised or closed,
              -- so a rotated credential's next expiry re-alerts. A live row (RESOLVED_AT NULL) always blocks.
              AND (e.RESOLVED_AT IS NULL
                   OR e.DETAIL LIKE ('Rotate before ' || TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', b.EXP_TS)::TIMESTAMP_NTZ, 'YYYY-MM-DD') || '%'))
        )
          -- V157: never mint EXPIRING while this credential's EXPIRED event is live (the supersede sweep would resolve it in the same run: hourly churn)
          AND NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS h
            WHERE b.DEDUPE_KEY LIKE '%|EXPIRING'
              AND h.RULE_ID = b.RULE_ID
              AND h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')
              AND h.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_CRED_EXPIRY - other rules unaffected', CURRENT_ROLE();
    END;
    END IF;   -- /V157 cadence gate: [10] every 4h (01,05,09,13,17,21 Central)
    -- [14] PIPE_COPY_FAILURES
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- PIPE_COPY_FAILURES: failed or partial file loads per Central failure day (yesterday + today).
        -- Broken ingestion is the most preventable 'found out too late' class. V168: keyed by the day the
        -- files FAILED over whole Central days (not the scan day over a rolling 24h), so the count of a day only
        -- grows -- the same files never re-raise after midnight and a band never steps back down.
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(p.DB),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess
               IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY),
               p.DB || '.' || p.SCH || '.' || p.TBL || ': ' || p.FAILED_FILES || ' failed file load(s) on ' || TO_VARCHAR(p.FAIL_DAY),
               'Schema ' || p.DB || '.' || p.SCH ||
                   IFF(p.PIPE IS NOT NULL, ' | pipe ' || p.PIPE, ' | bulk COPY') ||
                   ' | sample error: ' || LEFT(COALESCE(p.SAMPLE_ERROR, 'n/a'), 300),
               p.FAILED_FILES,
               c.RULE_ID || '|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(p.FAIL_DAY)  -- V066 #1: band matches the CRITICAL severity so a HIGH->CRITICAL crossing re-fires; V168: the failure day
        FROM cfg c
        JOIN (
            SELECT TABLE_CATALOG_NAME AS DB, TABLE_SCHEMA_NAME AS SCH, TABLE_NAME AS TBL,
                   TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY,
                   MAX(PIPE_NAME) AS PIPE,
                   COUNT(*) AS FAILED_FILES,
                   MAX(FIRST_ERROR_MESSAGE) AS SAMPLE_ERROR
            FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
            WHERE LAST_LOAD_TIME >= DATEADD('hour', -50, CURRENT_TIMESTAMP())   -- V168: prune only; yesterday 00:00 Central is at most 49h back (DST fall-back included)
              AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME))
                  >= DATEADD('day', -1, TO_DATE(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())))
              AND STATUS IN ('Load failed', 'Partially loaded')
            GROUP BY 1, 2, 3, 4
        ) p ON c.RULE_ID = 'PIPE_COPY_FAILURES' AND p.FAILED_FILES > c.THRESHOLD_NUM

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
                   'rule PIPE_COPY_FAILURES - other rules unaffected', CURRENT_ROLE();
    END;
    -- [17] COST_DEPT_BUDGET_PACE
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        -- COST_DEPT_BUDGET_PACE: department MTD spend ahead of its monthly
        -- budget pace (threshold = % over pace). Budgets live in
        -- DEPT_BUDGETS; spend = the department's warehouses (exact billing).
        SELECT c.RULE_ID, 'ALL',
               IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', c.SEVERITY),
               d.DEPARTMENT || ' is ' || ROUND(d.OVER_PCT, 0) || '% over budget pace (MTD ' ||
                   ROUND(d.MTD_USD, 0) || ' USD of ' || ROUND(d.BUDGET_USD, 0) || ')',
               'Month is ' || ROUND(d.TIME_SHARE * 100, 0) || '% elapsed. Owner lens: ' ||
                   'Cost > Chargeback (warehouses are exact; roles are allocated).',
               d.OVER_PCT,
               c.RULE_ID || '|' || d.DEPARTMENT || '|' || IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', 'MED') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #11: band matches the HIGH severity so a MEDIUM->HIGH crossing re-fires
        FROM cfg c
        JOIN (
            SELECT DEPARTMENT, BUDGET_USD, MTD_USD, TIME_SHARE,
                   (MTD_USD / NULLIF(BUDGET_USD * TIME_SHARE, 0) - 1) * 100 AS OVER_PCT
            FROM (
                SELECT b.DEPARTMENT, b.MONTHLY_BUDGET_USD AS BUDGET_USD,
                       COALESCE(SUM(f.CREDITS_TOTAL), 0) * :credit_price AS MTD_USD,
                       (DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE
                FROM DBA_MAINT_DB.OVERWATCH.DEPT_BUDGETS b
                LEFT JOIN DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP m
                  ON m.MAP_TYPE = 'WAREHOUSE' AND UPPER(m.DEPARTMENT) = UPPER(b.DEPARTMENT)
                LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY f
                  ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)
                 AND f.DAY >= DATE_TRUNC('month', CURRENT_DATE())
                 AND f.DAY < CURRENT_DATE()
                WHERE b.MONTHLY_BUDGET_USD > 0
                GROUP BY 1, 2
            )
        ) d ON c.RULE_ID = 'COST_DEPT_BUDGET_PACE'
           AND d.OVER_PCT > c.THRESHOLD_NUM AND d.MTD_USD >= 50
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
                   'rule COST_DEPT_BUDGET_PACE - other rules unaffected', CURRENT_ROLE();
    END;

    -- Self-alert when any block failed: the scan reports its own degradation.
    -- [18] SEC_NEW_ADMIN_NETWORK (V043 — the r25 panel, with teeth)
    --      V168 (R2-039): SUCCESSES counts the attempts that got in; the TITLE says 'logged in' only then, else
    --      'N failed login attempt(s) from new network <IP> (0 successful)'. A failures-only pair keys
    --      user|IP|FAILED|<day>, so a later success in the same 24h raises its own event (the V067 sweep then
    --      supersedes the failed one). V168 (R2-036): the key ends in the pair's first-seen Central day, so a
    --      network quiet 90+ days alerts again (the rule name, playbook and Security panel promise the
    --      re-flag; the undated V043 key matched the pair's first event forever). The 48h guard compares the
    --      EXACT date-stripped base, so a late earlier login across Central midnight, or a pre-V168 undated
    --      key, never raises one episode twice, and a failures-only key never swallows the success.
    --      V174 (owner 2026-10-05): + SNOW_PRI_GFR_PRD_ALFA_DSA. Its direct holders are OVERWATCH admins (in-app
    --      writes and the account levers), so the arm watches their logins like the three it watched.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
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
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
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
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_NEW_ADMIN_NETWORK - other rules unaffected', CURRENT_ROLE();
    END;
    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20] every 4h (01,05,09,13,17,21 Central)
    -- [20] SEC_NEW_EXPOSURE (V084 - CoCo Sec36: a new grant to PUBLIC widens the blast radius)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        pub AS (
            -- One row per distinct new grant to PUBLIC. A batch GRANT ON ALL ...
            -- shares one CREATED_ON, so it collapses to a single event counting
            -- its objects (N_OBJECTS) rather than flooding one alert per object.
            SELECT PRIVILEGE, GRANTED_ON, CREATED_ON,
                   COUNT(*) AS N_OBJECTS,
                   MAX(GRANTED_BY) AS GRANTED_BY,
                   MAX(NAME) AS SAMPLE_NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
            WHERE GRANTEE_NAME = 'PUBLIC'
              AND DELETED_ON IS NULL
              AND CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY PRIVILEGE, GRANTED_ON, CREATED_ON
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'New grant to PUBLIC: ' || p.PRIVILEGE || ' ON ' || p.GRANTED_ON
                   || IFF(p.N_OBJECTS > 1, ' (x' || p.N_OBJECTS || ' objects)',
                          ' ' || COALESCE(p.SAMPLE_NAME, '')),
               'A privilege granted to PUBLIC is inherited by every role in the account. '
                   || 'Granted ' || p.CREATED_ON || ' by ' || COALESCE(p.GRANTED_BY, '?')
                   || '. Source: ACCOUNT_USAGE.GRANTS_TO_ROLES - review in Security -> Changes (Recent grant changes).',
               p.N_OBJECTS,
               c.RULE_ID || '|' || p.PRIVILEGE || '|' || p.GRANTED_ON || '|' || TO_VARCHAR(p.CREATED_ON)
        FROM cfg c
        JOIN pub p
          ON c.RULE_ID = 'SEC_NEW_EXPOSURE'
         AND p.N_OBJECTS >= c.THRESHOLD_NUM

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
                   'rule SEC_NEW_EXPOSURE - other rules unaffected', CURRENT_ROLE();
    END;
    END IF;   -- /V157 cadence gate: [20] every 4h (01,05,09,13,17,21 Central)
    -- [21] SEC_POSTURE_METRIC (V087 - CoCo Sec35: generic, data-driven posture monitor
    --      keyed by ALERT_CONFIG.METRIC_NAME; every operator-created posture-metric rule
    --      raises here, so posture self-monitors after a finding is turned into a rule.
    --      INVARIANT: every MART_SECURITY_POSTURE_DAILY metric is a problem COUNT
    --      (higher = worse), so the comparator is a fixed VALUE >= THRESHOLD_NUM, and the
    --      app builder (posture_alert_rule_sql) only creates rules for that count
    --      vocabulary. A future lower-is-worse metric would need a comparator column.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
            WHERE ENABLED AND COALESCE(METRIC_NAME, '') <> ''
        ),
        latest AS (
            -- newest posture reading per (metric, company)
            SELECT METRIC, COMPANY, VALUE, DAY
            FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
            QUALIFY ROW_NUMBER() OVER (PARTITION BY METRIC, COMPANY ORDER BY DAY DESC) = 1
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, m.COMPANY, c.SEVERITY,
               c.NAME || ': ' || m.METRIC || ' = ' || m.VALUE::INT
                   || ' (threshold >= ' || c.THRESHOLD_NUM || ')',
               'Security posture metric ' || m.METRIC || ' is ' || m.VALUE::INT || ' as of ' || m.DAY
                   || ', at or over its configured threshold ' || c.THRESHOLD_NUM
                   || '. Source: MART_SECURITY_POSTURE_DAILY - review in Security.',
               m.VALUE,
               c.RULE_ID || '|' || m.COMPANY || '|' || TO_VARCHAR(m.DAY)
        FROM cfg c
        JOIN latest m
          ON UPPER(m.METRIC) = UPPER(c.METRIC_NAME)
         AND m.VALUE >= c.THRESHOLD_NUM
         AND m.DAY >= DATEADD('day', -2, CURRENT_DATE())

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
                   'rule posture-metric (generic) - other rules unaffected', CURRENT_ROLE();
    END;
    -- [26] SEC_LOGIN_TAKEOVER (V162, Next-Fifty #39a: a failed-login burst that ends in a success -- the brute-force
    --      breakthrough the daily count [07] cannot see). Hourly, ungated (owner 2026-09-29). A burst = at least
    --      THRESHOLD_NUM (floor 2) failed logins by one user inside 15 minutes; a takeover = a successful login by that
    --      user within 60 minutes after a burst ends. One event per EPISODE: the first such success with no other such
    --      success by the user in the 60 minutes before it (the anchor). CRITICAL band when the anchor falls off-hours
    --      (20:00-06:00 America/Chicago, or Saturday/Sunday) or the user held an admin-tier role at that moment
    --      (direct grant; owner list); otherwise the rule's severity (HIGH). Never auto-declares an incident:
    --      SP_INCIDENT_AUTODECLARE excludes this rule (V162); a human declares after contacting the user. Company
    --      ALL, so the event reaches the Teams route whoever the user is. Reads 27h of LOGIN_HISTORY = 24h of
    --      alertable anchors + 3h of evidence behind the oldest (60-min prior-anchor check + 60-min gap + 15-min
    --      burst), so every raised anchor is judged on complete evidence; ACCOUNT_USAGE lags up to ~2h, so an episode
    --      surfaces 1-3h after it happens and stays in the 24h window for about 20 more hourly runs. The key ends in
    --      the anchor's UTC time to the millisecond (never the run date, never a bare EVENT_ID -- the V117 snooze
    --      carry-forward would read a 10-digit tail as a date), so re-reads, overlaps and late rows never re-raise
    --      it; a CRIT/WARN band crossing re-fires and the V067 sweep supersedes the WARN row.
    --      V174 (owner 2026-10-05): the admin-tier list adds SNOW_PRI_GFR_PRD_ALFA_DSA (its direct
    --      holders are OVERWATCH admins), so a takeover of one is CRITICAL at any hour.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        k AS (
            SELECT GREATEST(CEIL(COALESCE(MAX(THRESHOLD_NUM), 5)), 2) AS N
            FROM cfg WHERE RULE_ID = 'SEC_LOGIN_TAKEOVER'
            HAVING COUNT(*) > 0
        ),
        ev AS (
            SELECT USER_NAME, EVENT_ID, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP, REPORTED_CLIENT_TYPE,
                   FIRST_AUTHENTICATION_FACTOR, ERROR_MESSAGE
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
            WHERE EVENT_TIMESTAMP >= DATEADD('hour', -27, CURRENT_TIMESTAMP())
        ),
        fl AS (
            SELECT USER_NAME, EVENT_TIMESTAMP,
                   ROW_NUMBER() OVER (PARTITION BY USER_NAME ORDER BY EVENT_TIMESTAMP, EVENT_ID) AS RN
            FROM ev
            WHERE IS_SUCCESS = 'NO'
        ),
        bend AS (
            -- a failure that closes >= N failures by the same user inside 15 minutes (the one N-1 rows back is <= 15 min older)
            SELECT f2.USER_NAME, f2.EVENT_TIMESTAMP AS TS
            FROM fl f2
            CROSS JOIN k
            JOIN fl f1
              ON f1.USER_NAME = f2.USER_NAME
             AND f1.RN = f2.RN - (k.N - 1)
             AND f1.EVENT_TIMESTAMP >= DATEADD('minute', -15, f2.EVENT_TIMESTAMP)
        ),
        seq AS (
            -- one time-ordered stream per burst user: burst ends, then successes; LAST_BEND = newest burst end before the row
            SELECT s.USER_NAME, s.TS, s.IS_SUCC, s.EVENT_ID, s.CLIENT_IP, s.CLIENT_TYPE, s.AUTH_FACTOR,
                   MAX(IFF(s.IS_SUCC, NULL, s.TS)) OVER (PARTITION BY s.USER_NAME ORDER BY s.TS, s.IS_SUCC
                                                         ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS LAST_BEND
            FROM (
                SELECT USER_NAME, TS, FALSE AS IS_SUCC, NULL AS EVENT_ID, NULL AS CLIENT_IP, NULL AS CLIENT_TYPE,
                       NULL AS AUTH_FACTOR
                FROM bend
                UNION ALL
                SELECT USER_NAME, EVENT_TIMESTAMP, TRUE, EVENT_ID, CLIENT_IP, REPORTED_CLIENT_TYPE,
                       FIRST_AUTHENTICATION_FACTOR
                FROM ev
                WHERE IS_SUCCESS = 'YES'
                  AND USER_NAME IN (SELECT USER_NAME FROM bend)
            ) s
        ),
        brk AS (
            -- a success within 60 minutes after a burst ended; PREV_TS = the user's previous such success
            SELECT USER_NAME, TS, EVENT_ID, CLIENT_IP, CLIENT_TYPE, AUTH_FACTOR, LAST_BEND,
                   LAG(TS) OVER (PARTITION BY USER_NAME ORDER BY TS, EVENT_ID) AS PREV_TS
            FROM seq
            WHERE IS_SUCC
              AND LAST_BEND >= DATEADD('minute', -60, TS)
        ),
        anc AS (
            -- the episode anchor, raised only inside the alert window (its evidence lies wholly inside ev)
            SELECT USER_NAME, TS, EVENT_ID, CLIENT_IP, CLIENT_TYPE, AUTH_FACTOR, LAST_BEND,
                   CONVERT_TIMEZONE('America/Chicago', TS)::TIMESTAMP_NTZ AS TS_CT
            FROM brk
            WHERE (PREV_TS IS NULL OR PREV_TS < DATEADD('minute', -60, TS))
              AND TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
        ),
        det AS (
            -- the anchor's evidence: every failure in the 75 minutes up to it (>= N by construction, a failure
            -- stamped in the same millisecond as the success included: seq orders it first)
            SELECT a.EVENT_ID, COUNT(*) AS N_FAIL, COUNT(DISTINCT f.CLIENT_IP) AS N_FAIL_IPS,
                   MIN(f.EVENT_TIMESTAMP) AS FIRST_FAIL, MAX(LEFT(COALESCE(f.ERROR_MESSAGE, ''), 200)) AS SAMPLE_ERROR
            FROM anc a
            JOIN ev f
              ON f.USER_NAME = a.USER_NAME
             AND f.IS_SUCCESS = 'NO'
             AND f.EVENT_TIMESTAMP <= a.TS
             AND f.EVENT_TIMESTAMP >= DATEADD('minute', -75, a.TS)
            GROUP BY a.EVENT_ID
        ),
        adm AS (
            -- admin-tier roles the user held AT the anchor (direct grants; owner list, same as SEC_ADMIN_GRANT)
            SELECT a.EVENT_ID, MIN(g.ROLE) AS ADMIN_ROLE, COUNT(DISTINCT g.ROLE) AS N_ADMIN_ROLES
            FROM anc a
            JOIN SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
              ON g.GRANTEE_NAME = a.USER_NAME
             AND g.ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                            'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
             AND g.CREATED_ON <= a.TS
             AND (g.DELETED_ON IS NULL OR g.DELETED_ON > a.TS)
            GROUP BY a.EVENT_ID
        ),
        x AS (
            SELECT a.USER_NAME, a.EVENT_ID, a.TS_CT, a.LAST_BEND, a.TS, a.CLIENT_IP, a.CLIENT_TYPE, a.AUTH_FACTOR,
                   d.N_FAIL, d.N_FAIL_IPS, d.FIRST_FAIL, d.SAMPLE_ERROR, m.ADMIN_ROLE, m.N_ADMIN_ROLES,
                   (HOUR(a.TS_CT) >= 20 OR HOUR(a.TS_CT) < 6 OR DAYOFWEEKISO(a.TS_CT) >= 6) AS OFF_HOURS
            FROM anc a
            JOIN det d ON d.EVENT_ID = a.EVENT_ID
            LEFT JOIN adm m ON m.EVENT_ID = a.EVENT_ID
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               'ALL',
               IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRITICAL', c.SEVERITY),
               LEFT('Possible account takeover: ' || x.USER_NAME || ' logged in ' ||
                    DATEDIFF('minute', x.LAST_BEND, x.TS) || ' min after a burst of ' || x.N_FAIL || ' failed logins' ||
                    IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL,
                        ' (' || IFF(x.OFF_HOURS, 'off-hours', '') ||
                        IFF(x.OFF_HOURS AND x.ADMIN_ROLE IS NOT NULL, ', ', '') ||
                        IFF(x.ADMIN_ROLE IS NOT NULL, 'admin role ' || x.ADMIN_ROLE, '') || ')', ''), 300),
               LEFT('Success ' || TO_VARCHAR(x.TS_CT, 'YYYY-MM-DD HH24:MI') || ' Central from ' ||
                    COALESCE(x.CLIENT_IP, '?') || ' (' || COALESCE(x.CLIENT_TYPE, '?') || ', ' ||
                    COALESCE(x.AUTH_FACTOR, '?') || ') after ' || x.N_FAIL || ' failed logins from ' ||
                    x.N_FAIL_IPS || ' IP(s) since ' ||
                    TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', x.FIRST_FAIL)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI') ||
                    ' Central. Off-hours: ' || IFF(x.OFF_HOURS, 'yes', 'no') || '. Admin roles held: ' ||
                    COALESCE(x.ADMIN_ROLE || IFF(x.N_ADMIN_ROLES > 1, ' +' || (x.N_ADMIN_ROLES - 1) || ' more', ''), 'none') ||
                    '. Sample error: ' || COALESCE(NULLIF(x.SAMPLE_ERROR, ''), 'n/a') ||
                    ' | Confirm with the user; unexplained = disable the user and rotate credentials (playbook). Never auto-declares an incident: declare one by hand. Source: ACCOUNT_USAGE.LOGIN_HISTORY - review in Security -> Access', 2000),
               x.N_FAIL,
               c.RULE_ID || '|' || LEFT(x.USER_NAME, 200) || '|' || IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRIT', 'WARN') ||
                   '|' || TO_VARCHAR(CONVERT_TIMEZONE('UTC', x.TS)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI:SS.FF3')
        FROM cfg c
        JOIN x ON c.RULE_ID = 'SEC_LOGIN_TAKEOVER'

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        )
          -- never mint the WARN band once this anchor's CRIT event exists (any status): a CRIT row is never downgraded
          AND NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS h
            WHERE b.DEDUPE_KEY LIKE '%|WARN|%'
              AND h.RULE_ID = b.RULE_ID
              AND h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|WARN|', '|CRIT|')
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_LOGIN_TAKEOVER - other rules unaffected', CURRENT_ROLE();
    END;
    -- [27] SEC_ADMIN_GRANT (V162, Next-Fifty #39b: every new grant of an admin-tier role to a user, named -- the
    --      30-day BREAKGLASS_GRANTS_30D posture count never names the grantee). Hourly, ungated. Owner list:
    --      ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS. One event
    --      per grant row (grantee, role, CREATED_ON), raised once however many hourly reads see it: CREATED_ON inside
    --      the last 26h (GRANTS_TO_USERS lags up to ~2h; the rest tolerates missed runs). A grant already revoked
    --      still raises (a short-lived elevation is the suspicious shape). PRIOR_GRANTS reads the same view's
    --      history (revoked rows kept) to tell a first-ever elevation from a re-grant. Severity is the rule's
    --      (HIGH); company ALL; never auto-declares an incident (SP_INCIDENT_AUTODECLARE excludes it, V162). The key
    --      ends in the grant's Central CREATED_ON to the millisecond with an explicit format.
    --      V174 (owner 2026-10-05): the owner list adds SNOW_PRI_GFR_PRD_ALFA_DSA; a direct grant of it
    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        ag AS (
            SELECT GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY,
                   ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS,
                   CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT
            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
            WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               'ALL',
               c.SEVERITY,
               LEFT('Admin role ' || g.ROLE || ' granted to ' || g.GRANTEE_NAME ||
                    IFF(HOUR(g.CREATED_CT) >= 20 OR HOUR(g.CREATED_CT) < 6 OR DAYOFWEEKISO(g.CREATED_CT) >= 6,
                        ' (off-hours)', '') ||
                    IFF(g.PRIOR_GRANTS = 0, ' - first time', ''), 300),
               LEFT('Granted ' || TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI') || ' Central by ' ||
                    COALESCE(g.GRANTED_BY, '?') || '. ' ||
                    IFF(g.PRIOR_GRANTS = 0, 'First grant of this role to this user on record.',
                        'Re-grant: ' || g.PRIOR_GRANTS || ' earlier grant(s) of this role to this user on record.') ||
                    IFF(g.DELETED_ON IS NULL, ' Still held.',
                        ' Since revoked ' ||
                        TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', g.DELETED_ON)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI') ||
                        ' Central.') ||
                    ' | Confirm the change was approved; unexplained = revoke it and review what the user ran (playbook). Source: ACCOUNT_USAGE.GRANTS_TO_USERS - review in Security -> Changes', 2000),
               1,
               c.RULE_ID || '|' || LEFT(g.GRANTEE_NAME, 200) || '|' || g.ROLE || '|' ||
                   TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI:SS.FF3')
        FROM cfg c
        JOIN ag g
          ON c.RULE_ID = 'SEC_ADMIN_GRANT'
         AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())

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
                   'rule SEC_ADMIN_GRANT - other rules unaffected', CURRENT_ROLE();
    END;
    IF (MOD(ct_hour, 3) = 2) THEN   -- V157 cadence gate: [22] every 3h (02,05,08,11,14,17,20,23 Central)
    -- [22] OPS_PIPELINE_DEGRADED (V157, Next-Fifty #10: OVERWATCH watches its own pipeline from inside BOTH
    --      task graphs. Byte-identical in SP_ALERT_SCAN and SP_ALERT_SCAN_DAILY with shared dedupe keys, so
    --      whichever graph is still alive raises each finding once. (a) STALE: a SOURCE_FRESHNESS_STATE row
    --      past the shared name-rule cadence (DAILY/METERING in the name 30h, else 3h -- the app health strip,
    --      Admin, Control Room and NATIVE_ALERT_STALE_FACTS judge it the same way), including the scans' own
    --      heartbeat rows ALERT_SCAN_HOURLY / ALERT_SCAN_DAILY -- at most one event per source per last-load
    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged -- most
    --      loaders swallow it (their task still reads SUCCEEDED); V166's SP_LOAD_APP_COST and
    --      SP_LOAD_STORAGE_TRUTH roll back to the previous fill and re-raise (their task reads FAILED), and
    --      the DETAIL says which (RERAISED, V168 + V169) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS -- one
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
                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG,
                   MAX(IFF(PAGE IN ('AppCost', 'StorageTruth'), 1, 0)) AS RERAISED   -- the V166 loads that roll back and re-raise
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
               LEFT(IFF(x.RERAISED = 1,
                        'The loader rolled back to its previous fill, logged this and re-raised: the run FAILED '
                        || '(a scheduled run shows FAILED in TASK_HISTORY; a hand CALL raised the error to its '
                        || 'caller) and readers keep the previous fill.',
                        'The loader logged this and returned normally, so its task still reads SUCCEEDED and '
                        || 'readers keep the previous fill.')
                   || ' Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '
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
    END IF;   -- /V157 cadence gate: [22] every 3h (02,05,08,11,14,17,20,23 Central)
    -- [23] PIPE_ETL_CYCLE  (V157, Next-Fifty #2; optional external-dependency add-on: NOT counted toward the
    -- core scan-health tally, because SP_SCAN_ETL_CYCLE (V156) reads the customer Informatica CONTROL_STATUS
    -- table SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert. The scan
    -- raises PIPE_ETL_CYCLE_LATE / PIPE_ETL_CYCLE_NOT_STARTED / PIPE_ETL_TASK_FAILED itself. It runs BEFORE the
    -- supersede + snooze carry-forward sweeps below, so a WARN->CRIT->EXH escalation and a snoozed re-raise
    -- are settled in the same pass.)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (same contract as the daily scan's PIPE_REF_GAP /
            -- DQ_RECON_ERROR add-ons, V129/V137): logged, never fatal, self-heals when the grant lands.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'etl_cycle_scan_failed', LEFT(:emsg, 2000),
                   'rules PIPE_ETL_CYCLE_LATE / PIPE_ETL_CYCLE_NOT_STARTED / PIPE_ETL_TASK_FAILED - optional external add-on; needs SELECT on CONTROL_STATUS', CURRENT_ROLE();
    END;
    IF (fails > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               :fails || ' of 14 alert rule block(s) failed this run',
               'APP_ERROR_LOG has the SQL errors (rule_block_failed). The other rules ' ||
                   'kept firing - that is the point of the v7 decomposition.',
               :fails,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        WHERE c.RULE_ID = 'OPS_SCAN_DEGRADED' AND c.ENABLED
          AND NOT EXISTS (
              SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
          );
    END IF;

    -- V067 #40: supersede the lower-severity OPEN event on escalation. V066's severity-band
    -- dedupe keys re-fire the HIGHER band as a NEW event but leave the prior lower-band event
    -- OPEN, double-counting one incident in the severity tallies + score penalties. Resolve a
    -- WARN/MED event when its CRIT/HIGH sibling (the SAME dedupe key with only the band token
    -- swapped) is also OPEN. RESOLUTION_KIND='SUPERSEDED' is excluded from the per-rule
    -- precision score (which counts only ACTIONED/NOISE), so it does not distort it. The band
    -- tokens '|WARN|'/'|MED|'/'|HIGH|'/'|EXPIRING|' occur only in banded/state keys, so this
    -- is a no-op for every other rule (V096 adds |HIGH|->|CRIT| for the SLO burn band and
    -- |EXPIRING|->|EXPIRED| for cred expiry). Wrapped so a sweep failure never breaks the scan.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
         WHERE lo.STATUS IN ('OPEN', 'ACK')
           AND EXISTS (
               SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS hi
               WHERE hi.STATUS IN ('OPEN', 'ACK')
                 AND hi.RULE_ID = lo.RULE_ID
                 AND hi.DEDUPE_KEY <> lo.DEDUPE_KEY
                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|CRIT|', '|EXH|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|EXH|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')
                      -- V168 (R2-039): a failures-only SEC_NEW_ADMIN_NETWORK event (user|IP|FAILED|<day>) is
                      -- superseded once the same user + IP success event (user|IP|<day>) opens within 48h of it.
                      OR (lo.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
                          AND lo.DEDUPE_KEY LIKE '%|FAILED|____-__-__'
                          AND LEFT(hi.DEDUPE_KEY, LENGTH(hi.DEDUPE_KEY) - 11)
                              = REPLACE(LEFT(lo.DEDUPE_KEY, LENGTH(lo.DEDUPE_KEY) - 11), '|FAILED', '')
                          AND hi.RAISED_AT >= lo.RAISED_AT
                          AND hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)))
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'supersede_sweep_failed', :emsg, 'V067 #40 escalation supersede - other rules unaffected', CURRENT_ROLE();
    END;


    -- [auto-clear sweep] V091: resolve still-OPEN live-window events (any raise day, V168) whose
    -- scope has dropped back below the rule's CLEAR threshold (hysteresis, default
    -- 0.9 x THRESHOLD_NUM). Runs AFTER the raise arms + the supersede sweep so an
    -- escalated/superseded event is never also auto-cleared this pass. OPEN-only
    -- (manual RESOLVE wins and is never reopened; an active SNOOZE is left alone; an
    -- ACK is a human actively working it, so v1 leaves it too). The >=1h dwell plus
    -- below-CLEAR hysteresis mean an event cannot open and auto-close in one cadence.
    -- V168: no age bound (the V096 >= -48h bound is gone). Every OPEN event of the 3 rules is re-checked each
    -- scan on its date-stripped identity (V119), so a multi-day or hysteresis-held condition never
    -- strands its older day-stamped events OPEN. RESOLUTION_KIND='AUTO_CLEARED' is excluded from
    -- per-rule precision/MTTR in the app read-path exactly like SUPERSEDED. Wrapped so
    -- a sweep failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'
         WHERE ev.STATUS = 'OPEN'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                              WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')   -- V157: only rules whose still-firing set this sweep recomputes; other opt-ins have their own clear sweep
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
           AND (ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)) NOT IN (
               -- scopes STILL firing at the CLEAR threshold. Same candidate subqueries
               -- as raise arms [03]/[04]/[05], recomputed at COALESCE(CLEAR, 0.9 x RAISE).
               WITH cfg AS (
                   SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                   WHERE ENABLED AND AUTO_CLEAR_ENABLED
               )
               SELECT c.RULE_ID || '|' || q.COMPANY AS DEDUPE_KEY
               FROM cfg c
               JOIN (
                   SELECT COMPANY,
                          IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   GROUP BY COMPANY
                   HAVING SUM(QUERY_COUNT) >= 20
               ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT'
                  AND q.FAIL_PCT >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES'
                  AND q.QUEUED_MIN >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_SPILL_GB'
                  AND q.SPILL_GB >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'autoclear_sweep_failed', :emsg, 'V091 auto-clear sweep - other rules unaffected', CURRENT_ROLE();
    END;

    -- [condition-ended sweep] V157 (Next-Fifty #12c): resolve a still-OPEN SECURITY event whose
    -- underlying STATE has ended, re-verified against the SAME ACCOUNT_USAGE source its raise arm
    -- reads -- a state check, not the V091 metric hysteresis above (which V157 scopes to its 3 PERF
    -- rules). Opt-in per rule (ALERT_CONFIG.ENABLED AND AUTO_CLEAR_ENABLED; V157 seeds it for these
    -- two rules). OPEN only: an ACK is a human working it, an active SNOOZE and every RESOLVED row are
    -- never touched. >=1h dwell (anti-flap; also covers the ACCOUNT_USAGE lag the raise arm already
    -- tolerates). A clear needs POSITIVE evidence:
    --   SEC_CRED_EXPIRY  -- no CREDENTIALS row for the event's (USER_NAME, NAME) is still expiring
    --                       inside GREATEST(THRESHOLD_NUM, COALESCE(CLEAR_THRESHOLD_NUM,
    --                       THRESHOLD_NUM)) days (rotated or removed). The clear window is never
    --                       narrower than the raise window, so arm [10] cannot re-raise what this
    --                       clears; an empty CREDENTIALS read or a NULL threshold never clears.
    --   SEC_NEW_EXPOSURE -- the event's (PRIVILEGE, GRANTED_ON, CREATED_ON) PUBLIC grant batch still
    --                       has rows in GRANTS_TO_ROLES and EVERY one carries DELETED_ON (a key that
    --                       matches no row never clears; batches older than 400 days are not
    --                       re-read, so such an event stays for a human).
    -- Keys are rebuilt with arm [10] / [20]'s exact expressions, never parsed out of DEDUPE_KEY, so a
    -- pipe inside a user/credential/object name cannot mis-split. Each rule is its own isolated block
    -- and skips its ACCOUNT_USAGE read entirely unless it has an OPEN event and is opted in (one EXISTS
    -- with a join -- no nested scalar subquery). The machine close CONDITION_ENDED is excluded from
    -- precision/MTTR in the app read-path like SUPERSEDED/AUTO_CLEARED/SNOOZE_SUPPRESSED.
    -- Cadence (compile diet): each rule's clear runs only in the slot its raise arm runs (MOD(ct_hour, 4) = 1),
    -- still behind its EXISTS-OPEN gate. Off-slot hours skip the probe and the ACCOUNT_USAGE read alike, so a
    -- clear lands in the first security slot after the evidence shows (up to ~4h later). An event raised in a
    -- slot is re-checked no sooner than the next one, so the 1h dwell always holds.
    -- Does NOT touch :fails.
    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: SEC_CRED_EXPIRY clear rides arm [10] every 4h (01,05,09,13,17,21 Central)
    BEGIN
        IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                    WHERE e.RULE_ID = 'SEC_CRED_EXPIRY' AND e.STATUS = 'OPEN'
                      AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
               SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
             WHERE ev.STATUS = 'OPEN'
               AND ev.RULE_ID = 'SEC_CRED_EXPIRY'
               AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                                  WHERE ENABLED AND AUTO_CLEAR_ENABLED AND THRESHOLD_NUM IS NOT NULL)
               AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
               AND EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS)   -- an empty read never clears
               AND ev.DEDUPE_KEY NOT IN (
                   -- arm [10]'s key for every credential STILL expiring inside the clear window (both bands)
                   SELECT k.DEDUPE_KEY
                   FROM (
                       SELECT c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || bd.BAND AS DEDUPE_KEY
                       FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
                       JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr
                         ON c.RULE_ID = 'SEC_CRED_EXPIRY'
                        AND cr.EXPIRATION_DATE IS NOT NULL
                        AND cr.EXPIRATION_DATE <= DATEADD('day',
                                GREATEST(c.THRESHOLD_NUM, COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM)),
                                CURRENT_TIMESTAMP())
                       CROSS JOIN (SELECT 'EXPIRING' AS BAND UNION ALL SELECT 'EXPIRED') bd
                   ) k
                   WHERE k.DEDUPE_KEY IS NOT NULL
               );
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'condition_ended_sweep_failed', :emsg, 'V157 condition-ended sweep SEC_CRED_EXPIRY - other rules unaffected', CURRENT_ROLE();
    END;
    END IF;   -- /V157 cadence gate: SEC_CRED_EXPIRY clear rides arm [10] every 4h (01,05,09,13,17,21 Central)
    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: SEC_NEW_EXPOSURE clear rides arm [20] every 4h (01,05,09,13,17,21 Central)
    BEGIN
        IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                    WHERE e.RULE_ID = 'SEC_NEW_EXPOSURE' AND e.STATUS = 'OPEN'
                      AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
               SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
             WHERE ev.STATUS = 'OPEN'
               AND ev.RULE_ID = 'SEC_NEW_EXPOSURE'
               AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                                  WHERE ENABLED AND AUTO_CLEAR_ENABLED)
               AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
               AND ev.DEDUPE_KEY IN (
                   -- arm [20]'s key for every PUBLIC grant batch that still exists and is now FULLY revoked
                   SELECT 'SEC_NEW_EXPOSURE' || '|' || g.PRIVILEGE || '|' || g.GRANTED_ON || '|' || TO_VARCHAR(g.CREATED_ON)
                   FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
                   WHERE g.GRANTEE_NAME = 'PUBLIC'
                     AND g.CREATED_ON >= DATEADD('day', -400, CURRENT_TIMESTAMP())
                   GROUP BY g.PRIVILEGE, g.GRANTED_ON, g.CREATED_ON
                   HAVING COUNT_IF(g.DELETED_ON IS NULL) = 0
               );
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'condition_ended_sweep_failed', :emsg, 'V157 condition-ended sweep SEC_NEW_EXPOSURE - other rules unaffected', CURRENT_ROLE();
    END;
    END IF;   -- /V157 cadence gate: SEC_NEW_EXPOSURE clear rides arm [20] every 4h (01,05,09,13,17,21 Central)

    -- [snooze carry-forward sweep] V117: a per-event snooze keeps the event's date-banded
    -- DEDUPE_KEY, so when the day/week band rolls the raise arms above mint a NEW OPEN event for
    -- the SAME rule+entity even though it is snoozed -- silently defeating a multi-day snooze.
    -- Carry the snooze FORWARD onto the re-raise (do NOT resolve it: a resolved row would occupy
    -- the day's key and, after a mid-day wake, block the current band from re-minting so only a
    -- STALE-numbers original showed). (1) snooze the fresh same-identity re-raise, inheriting the
    -- active snooze's wake time, so it carries the CURRENT band's data and wakes on schedule;
    -- (2) resolve the now-superseded older snoozed row so exactly ONE snoozed row (the latest
    -- band, current data) survives and reopens once on wake. Band-independent identity strips a
    -- trailing |YYYY-MM-DD via TRY_TO_DATE (no regex). ev.RAISED_AT > s.RAISED_AT restricts to
    -- GENUINE future re-raises, leaving a pre-existing untriaged OPEN sibling for a human. Entity-
    -- only keys (grant time) never end in a bare date so they are never stripped -- untouched. The
    -- [18] first-seen day (V168) strips to user|IP or user|IP|FAILED; a re-raise needs 90 quiet days.
    -- RESOLUTION_KIND='SNOOZE_SUPPRESSED' is a machine close excluded from precision. Wrapped so a
    -- sweep failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'SNOOZED',
               SNOOZED_UNTIL = s.SNOOZED_UNTIL,
               SNOOZE_BY = s.SNOOZE_BY,
               SNOOZE_REASON = s.SNOOZE_REASON
          FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
         WHERE ev.STATUS = 'OPEN'
           AND s.STATUS = 'SNOOZED'
           AND s.SNOOZED_UNTIL > CURRENT_TIMESTAMP()
           AND s.RULE_ID = ev.RULE_ID
           AND s.EVENT_ID <> ev.EVENT_ID
           AND ev.RAISED_AT > s.RAISED_AT
           AND IFF(SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'
                     AND TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10)) IS NOT NULL,
                     LEFT(s.DEDUPE_KEY, LENGTH(s.DEDUPE_KEY) - 11), s.DEDUPE_KEY)
               = IFF(SUBSTR(ev.DEDUPE_KEY, -11, 1) = '|'
                     AND TRY_TO_DATE(RIGHT(ev.DEDUPE_KEY, 10)) IS NOT NULL,
                     LEFT(ev.DEDUPE_KEY, LENGTH(ev.DEDUPE_KEY) - 11), ev.DEDUPE_KEY);
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'
         WHERE s.STATUS = 'SNOOZED'
           AND EXISTS (
               SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s2
               WHERE s2.STATUS = 'SNOOZED'
                 AND s2.EVENT_ID <> s.EVENT_ID
                 AND s2.RULE_ID = s.RULE_ID
                 AND s2.RAISED_AT > s.RAISED_AT
                 AND IFF(SUBSTR(s2.DEDUPE_KEY, -11, 1) = '|'
                     AND TRY_TO_DATE(RIGHT(s2.DEDUPE_KEY, 10)) IS NOT NULL,
                     LEFT(s2.DEDUPE_KEY, LENGTH(s2.DEDUPE_KEY) - 11), s2.DEDUPE_KEY)
                     = IFF(SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'
                     AND TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10)) IS NOT NULL,
                     LEFT(s.DEDUPE_KEY, LENGTH(s.DEDUPE_KEY) - 11), s.DEDUPE_KEY)
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'snooze_carry_forward_failed', :emsg, 'V117 snooze carry-forward sweep - other rules unaffected', CURRENT_ROLE();
    END;

    -- [hb] scan heartbeat (V157, Next-Fifty #10b): stamp this scan's own SOURCE_FRESHNESS_STATE row LAST --
    -- after every arm and sweep -- so a row older than its cadence means the scan stopped (or this stamp
    -- keeps failing: APP_ERROR_LOG scan_heartbeat_failed). The daily graph's [22] arm, the app freshness
    -- boards and NATIVE_ALERT_STALE_FACTS read it with the shared name rule (ALERT_SCAN_HOURLY -> 3h,
    -- ALERT_SCAN_DAILY -> 30h). LAST_LOAD_TS is Central wall-clock like every loader stamp. Cheapest shape:
    -- ONE point UPDATE of this scan's own row; the INSERT runs only when it matched no row (the first run,
    -- or after the row was deleted), so the stamp self-heals. Isolated; does NOT touch :fails.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
           SET LAST_LOAD_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
               ROW_COUNT = (14 - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = 'alert scan ' || (14 - :fails) || '/14 rule blocks ok'
         WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT 'ALERT_SCAN_HOURLY', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   (14 - :fails), 1, 'alert scan ' || (14 - :fails) || '/14 rule blocks ok';
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   'ALERT_SCAN_HOURLY heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan v14 (V168: [14] failure-day key, [18] outcome + first-seen-day key, auto-clear any raise day; V162 arms and V157 gates unchanged): ' || (14 - :fails) || '/14 rule blocks ok';
END;
$$;

-- Rule NAME text follows the new list. The refresh touches the row only while NAME still equals its V162 seed, so an
-- operator's own edit survives; a re-run is a no-op. ALERT_CONFIG has no DESCRIPTION column.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = 'Admin-tier role granted directly to a user (ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS, SNOW_PRI_GFR_PRD_ALFA_DSA), one event per grant'
 WHERE RULE_ID = 'SEC_ADMIN_GRANT'
   AND NAME = 'Admin-tier role granted directly to a user (ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS), one event per grant';

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 174 AS VERSION,
       'Owner access decision 2026-10-05: a direct holder of SNOW_PRI_GFR_PRD_ALFA_DSA is an OVERWATCH admin, so the hourly security arms watch the role. SP_ALERT_SCAN re-derived from V173, byte-identical except the role added at the end of the admin-role lists of arms [18] SEC_NEW_ADMIN_NETWORK, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT (one two-line note per arm): a direct grant raises SEC_ADMIN_GRANT, a holder takeover is CRITICAL, a holder new network raises. The SEC_ADMIN_GRANT rule NAME lists it (guarded on the V162 seed). SP_ALERT_SCAN_DAILY, RETURN labels and tallies unchanged. No task change, no new object, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174);

-- >>> V175__admin_role_members_proc.sql
-- V175__admin_role_members_proc.sql
--
-- The admin-access lookup as an owner-run procedure, so it keeps answering after the app's owner changes
-- (owner decision 2026-10-06: SNOW_SYSADMINS will own and run OVERWATCH_APP; design decision D4).
--
-- WHY: since 4.610 the app decides who is an admin by running SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA as
-- the app owner. SHOW lists only what the CURRENT role can see. If SNOW_SYSADMINS sees fewer DSA grantees than
-- SNOW_ACCOUNTADMINS does (SYSADMINS_OWNER_PREFLIGHT_v2: S2 differs from Z2), every DSA-only admin silently
-- drops to read-only at the cutover (the lookup fails closed). The named admins (config OPERATOR_USERS) are
-- never affected: they need no lookup.
--
--   + SP_ADMIN_ROLE_MEMBERS(): EXECUTE AS OWNER, so it runs as the role that applies this file
--     (SNOW_ACCOUNTADMINS, the owner of every OVERWATCH object). It runs the SAME SHOW GRANTS OF ROLE the app runs
--     today and returns its two columns, granted_to and grantee_name, unfiltered: the app keeps only the direct
--     USER rows and reports a ROLE grantee as nested, exactly as it does with SHOW. The role is HARD-CODED: no
--     argument, so the procedure can list this one role's grantees and nothing else.
--   + COPY GRANTS: a later CREATE OR REPLACE of this procedure keeps its USAGE grants (without it, a re-create
--     drops them and the lookup fails closed again).
--   + GRANT USAGE to SNOW_SYSADMINS: the future app owner can CALL it. In this file, so a re-run (or a rebuild
--     replay) re-grants it. SNOW_SYSADMINS already reads the same membership through
--     SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS; this discloses nothing new, it only removes the up-to-2h lag.
--
-- The app (4.610.2+) CALLs it once this version row exists (schema_gate.has_migration(175)) and runs SHOW
-- before that, so one build works before and after the apply, under either owner. The fail-closed paths are
-- unchanged: a failed CALL is lookup_failed, an answer with no USER row is unverified, neither ever makes an admin.
--
-- COST: none at apply time. Per lookup: one CALL in place of one SHOW, at the same cadence (once per viewer session
-- per 5 min, plus the write-time re-check). Inside it, the SHOW (cloud services) and one RESULT_SCAN of its few rows,
-- which runs on the caller's warehouse (the app's own).
-- NEEDED: before the SNOW_SYSADMINS cutover when the preflight's S2 differs from Z2. Harmless before that: under
-- today's owner the CALL returns what SHOW returns.
-- ROLLBACK: do not drop the procedure while this version row exists: the app would CALL a missing procedure and
-- every DSA-only admin would be read-only (fail closed). RUNBOOK §12, "Rolling back V175".
-- Apply AFTER V174, as SNOW_ACCOUNTADMINS (the role that owns the OVERWATCH schema). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20175, 'V175 requires V174 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 174) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS()
COPY GRANTS
RETURNS TABLE ("granted_to" VARCHAR, "grantee_name" VARCHAR)
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- V175: who holds SNOW_PRI_GFR_PRD_ALFA_DSA (config.ADMIN_ACCESS_ROLE), as this procedure's owner sees it. Read-only.
DECLARE
    res RESULTSET;
BEGIN
    SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
    res := (SELECT "granted_to", "grantee_name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    RETURN TABLE(res);
END;
$$;

GRANT USAGE ON PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS() TO ROLE SNOW_SYSADMINS;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 175 AS VERSION,
       'Owner decision 2026-10-06 (SNOW_SYSADMINS will own the app): SP_ADMIN_ROLE_MEMBERS(), an EXECUTE AS OWNER procedure that runs SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA (hard-coded) and returns granted_to and grantee_name, created WITH COPY GRANTS, USAGE granted to SNOW_SYSADMINS. The app CALLs it for the admin-access lookup once this row exists, so the lookup answers as this procedure''s owner whatever role owns the app. Read-only; no task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 175);

-- >>> quick check: expect rows for 174 and 175
SELECT VERSION, APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION >= 174 ORDER BY VERSION;

ALTER SESSION UNSET TIMEZONE;
