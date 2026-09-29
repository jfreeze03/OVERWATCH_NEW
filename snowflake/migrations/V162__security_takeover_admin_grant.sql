-- V162__security_takeover_admin_grant.sql
--
-- Next-Fifty #39 (owner decisions 2026-09-29): two HOURLY identity alerts that never auto-declare an incident.
--
-- WHY: the nightly SEC_FAILED_LOGINS [07] counts a user's failed logins per day, so a password spray or brute force
-- that GOT IN reads the same as a locked-out job and surfaces the next morning, and nothing names a user who was
-- just handed an admin-tier role (the BREAKGLASS_GRANTS_30D posture metric is a 30-day count). The app's Security >
-- Access "Account-takeover candidates" lens already correlates a failed burst with a later success, but only when
-- someone opens it.
--
--   + SEC_LOGIN_TAKEOVER (arm [26], hourly, ungated): at least THRESHOLD_NUM (seed 5, floor 2) failed logins by
--     one user within 15 minutes, then a successful login within 60 minutes of the burst's end. One event per episode,
--     anchored on the first such success (a further success within 60 minutes joins the episode). Severity is the
--     rule's (HIGH), CRITICAL when the anchor is off-hours (20:00-06:00 America/Chicago, or a Saturday/Sunday) or
--     the user directly held ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS or
--     SNOW_SYSADMINS at that moment. Every IS_SUCCESS = 'NO' row counts toward a burst (network-policy blocks and
--     JWT/SAML failures included, as in the app lens). Reads the last 27h of LOGIN_HISTORY; raises anchors from the
--     last 24h. The burst test is a linear equi-join on the per-user failure number, never a quadratic self-join,
--     so a brute-force storm cannot blow up the scan.
--   + SEC_ADMIN_GRANT (arm [27], hourly, ungated): one event per direct grant of one of those seven roles to a user
--     (GRANTS_TO_USERS CREATED_ON inside the last 26h), raised even when the grant was already revoked; flat HIGH;
--     the title flags an off-hours grant and a first-ever grant of that role to that user.
--   Both events are account-level (COMPANY 'ALL'): the Teams routes carry ALFA plus ALL, so a takeover of any user
--   still reaches Teams. The two dedupe keys end in a millisecond timestamp with an explicit format (the anchor
--   login in UTC; the grant's Central CREATED_ON), never a bare EVENT_ID: V117's snooze carry-forward reads a
--   10-character tail that parses as a date (a 10-digit integer is epoch seconds) as a date band, and would carry a
--   snooze to the user's NEXT takeover.
--   ~ SP_INCIDENT_AUTODECLARE re-derived from V154 (its current definer), byte-identical except TWO predicates.
--     In the crit CTE: AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT'). Neither rule ever opens an
--     incident, even if an operator later edits its severity to CRITICAL: a human declares after contacting the
--     user. In [attach]: a CRITICAL of either rule links only to an OPEN/MITIGATED incident that already holds
--     the SAME user (DEDUPE_KEY field 2); a CRITICAL for another user stays unlinked, keeps its V164 escalation
--     and can be declared on its own. Every other rule attaches as in V154; the [auto-mitigate] sweep is unchanged.
--   ~ SP_ALERT_SCAN re-derived from V157 (its current definer), byte-identical except: + counting arms [26] and
--     [27] after [21], before the [22] gate (so the self-alert, the V067 supersede sweep and the V117 carry-forward
--     see them in the same pass: a WARN -> CRIT crossing is superseded at once); tally 12 -> 14 (self-alert, [hb],
--     RETURN). The V157 cadence gates, [18]'s three roles, every sweep and [hb] are unchanged; the hourly
--     ACCOUNT_USAGE footprint stays COPY_HISTORY, CREDENTIALS, GRANTS_TO_ROLES, GRANTS_TO_USERS, LOGIN_HISTORY.
--   + ALERT_CONFIG SEC_LOGIN_TAKEOVER (SECURITY, HIGH, 5, 24h) and SEC_ADMIN_GRANT (SECURITY, HIGH, 0 = unused,
--     24h), WHEN NOT MATCHED only.
--
-- FILE ORDER: the autodeclare exclusion is replaced BEFORE the scan, so a CRITICAL takeover never meets the old
-- autodeclare; a partial apply (stop on the first error) always leaves a coherent prefix.
-- COST (estimated): ~3-5 s compile for [26] and ~2-3 s for [27] per hourly run (168 runs a week), plus XS execution
-- over 27h of LOGIN_HISTORY and GRANTS_TO_USERS -- about 0.1-0.2 cloud-services credits a week, roughly 1 USD a
-- week. Re-run DIAG_CS_SELF_COST a week after the apply; the lever is the 4-hourly security slot for [27].
-- LATENCY: hourly; ACCOUNT_USAGE lags up to ~2h, so an event arrives 1-3h after the login or grant.
-- FIRST RUN: the first hourly scan after apply raises every takeover episode of the last 24h and every admin grant
-- of the last 26h (CRITICAL ones included -- no incident is opened). PREFLIGHT_WAVE4.sql P162.1 / P162.2 list them.
-- Once V164 is applied, each first-run CRITICAL takeover nobody acknowledges is re-posted and emailed about 2-3h
-- after the apply: P162.4 lists them (V164's own census, P164.2, runs before they exist and cannot).
-- No procedure runs at apply time.
-- ROLLBACK (order matters): 1. Re-run V157's SP_ALERT_SCAN (tally back to 12; the two rules stop raising). That
-- is usually enough: this SP_INCIDENT_AUTODECLARE only narrows what it does for the two rules, so it can stay.
-- 2. Only to restore V154's SP_INCIDENT_AUTODECLARE too: FIRST wait 24h after step 1 (the crit CTE reads 24h), or
-- resolve every OPEN / ACK / SNOOZED SEC_LOGIN_TAKEOVER and SEC_ADMIN_GRANT event as EXPECTED (RUNBOOK section 12
-- has the UPDATE) -- V154 has no exclusion, so a CRITICAL takeover still open is auto-declared at the next hourly
-- run. Running the scan first only keeps out the events raised between the two steps; closing or aging out the
-- rest is what prevents the auto-declare. Roll V163 back before V162 (its [07] text points at the hourly
-- SEC_LOGIN_TAKEOVER), or accept that pointer. Optionally disable the two rules in Alerts > Rules; the seeds can stay.
-- Apply AFTER V161. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20162, 'V162 requires V161 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 161) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The two rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered. SEC_ADMIN_GRANT's THRESHOLD_NUM
-- is unused (one event per grant); AUTO_CLEAR_ENABLED keeps its default.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('SEC_LOGIN_TAKEOVER', 'SECURITY', 'Possible account takeover: at least THRESHOLD_NUM failed logins by one user within 15 minutes, then a successful login within 60 minutes (CRITICAL off-hours Central or for an admin-role holder)', TRUE, 'HIGH', 5, 24),
        ('SEC_ADMIN_GRANT', 'SECURITY', 'Admin-tier role granted directly to a user (ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS), one event per grant', TRUE, 'HIGH', 0, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- >>> derived:SP_INCIDENT_AUTODECLARE  (from V154; + no auto-declare for SEC_LOGIN_TAKEOVER / SEC_ADMIN_GRANT, V162)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    enabled VARCHAR;
    made INT DEFAULT 0;
    attached INT DEFAULT 0;
    mitigated INT DEFAULT 0;
    emsg VARCHAR;
BEGIN
    SELECT COALESCE(MAX(VALUE), 'TRUE') INTO :enabled
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'INCIDENT_AUTO_DECLARE_CRITICAL';
    IF (UPPER(:enabled) <> 'TRUE') THEN
        RETURN 'auto-declare off';
    END IF;

    CREATE OR REPLACE TEMPORARY TABLE _OW_AUTODECL AS
    WITH crit AS (
        SELECT e.EVENT_ID, e.COMPANY, e.SEVERITY, e.TITLE, e.RAISED_AT,
               SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE UPPER(e.SEVERITY) = 'CRITICAL'
          AND e.STATUS IN ('OPEN', 'ACK')
          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
          -- V162 (Next-Fifty #39, owner 2026-09-29): identity alerts never auto-declare -- a human declares after
          -- contacting the user. [attach] below still links them to an incident a human opened for that user.
          AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
    )
    SELECT UUID_STRING() AS INCIDENT_ID, FAMILY, COMPANY,
           MAX_BY(TITLE, RAISED_AT) AS TITLE,
           MIN(RAISED_AT) AS FIRST_TS
    FROM crit c
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT'
          AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND i.COMPANY = c.COMPANY
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY
    )
    GROUP BY FAMILY, COMPANY;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, STARTED_AT,
         ROOT_CAUSE_KIND, DECLARED_BY)
    SELECT INCIDENT_ID, LEFT('Auto: ' || TITLE, 300), 'CRITICAL', 'OPEN', COMPANY,
           CURRENT_TIMESTAMP(), FIRST_TS, 'UNKNOWN', 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
    SELECT d.INCIDENT_ID, 'ALERT', e.EVENT_ID, e.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL d
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
      ON e.COMPANY = d.COMPANY
     AND UPPER(e.SEVERITY) = 'CRITICAL'
     AND e.STATUS IN ('OPEN', 'ACK')
     AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
     AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY
     AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                     WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);

    -- [attach] V154 (Next-Fifty #12b): a later unlinked OPEN/ACK CRITICAL whose family ALREADY has an
    -- OPEN/MITIGATED incident in the SAME company is skipped by the family-already-open guard above
    -- (V099) and, before V154, was never linked anywhere -- timelines, RCA and member counts silently
    -- missed it. Link it to that incident instead. The candidate set is EXACTLY the guard's witness
    -- set (same company, an ALERT member of the same family, OPEN/MITIGATED), so every such critical
    -- is either declared above or attached here. One target per event: an incident already holding
    -- a member of the SAME entity (DEDUPE_KEY field 2) wins, then the newest DETECTED_AT, then
    -- INCIDENT_ID (deterministic). Never creates an incident and never changes a STATUS -- a re-fire
    -- on a MITIGATED incident shows as a live member, so it simply stops being 'ready to close'.
    -- Isolated: a failure logs and leaves the declare above intact.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
            (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
        SELECT t.INCIDENT_ID, 'ALERT', t.EVENT_ID, t.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
        FROM (
            SELECT e.EVENT_ID, e.RAISED_AT, i.INCIDENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i
              ON i.COMPANY = e.COMPANY
             AND i.STATUS IN ('OPEN', 'MITIGATED')
            JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
              ON m.INCIDENT_ID = i.INCIDENT_ID
             AND m.MEMBER_KIND = 'ALERT'
            JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a
              ON a.EVENT_ID = m.REF_ID
             AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1)
                 = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1)
            WHERE UPPER(e.SEVERITY) = 'CRITICAL'
              AND e.STATUS IN ('OPEN', 'ACK')
              AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                              WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID)
              -- V162 review fix: an identity alert attaches only to an incident that already holds the
              -- SAME user (DEDUPE_KEY field 2, upper-cased like the entity filter of SP_INCIDENT_DECLARE).
              -- A takeover of another user stays unlinked, so it keeps its own escalation (V164) and its
              -- own proposal, and never inherits an incident acknowledged or mitigated for someone else.
              AND (e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
                   OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2))
                      = UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)))
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.EVENT_ID
                ORDER BY IFF(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)
                             = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2), 0, 1),
                         i.DETECTED_AT DESC, i.INCIDENT_ID) = 1
        ) t;
        attached := SQLROWCOUNT;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'IncidentAutodeclare', 'incident_attach_failed', :emsg,
                   'V154 attach arm - the declare above is unaffected', CURRENT_ROLE();
    END;

    -- [auto-mitigate] V154 (Next-Fifty #12b): an OPEN incident whose EVERY ALERT member is RESOLVED
    -- (a member whose event row is gone counts as NOT resolved) moves forward-only to MITIGATED --
    -- never to RESOLVED: closing stays human so the root cause is captured (the app surfaces these
    -- as 'ready to close'). A member closed as SUPERSEDED / SNOOZE_SUPPRESSED is a machine hand-off
    -- to a successor, so it blocks while a same-rule same-company event raised at/after it is still
    -- OPEN/ACK/SNOOZED. >=1h dwell on the LAST resolve (anti-flap). MITIGATED_AT = when the last
    -- member resolved (not this sweep's clock), so time-to-mitigate is not inflated by the dwell or the
    -- hourly cadence -- and for a machine hand-off member, when its same-rule successor resolved (the
    -- condition really ended then; review fix: the member's own machine-close time understated MTTM and
    -- let the dwell pass on stale timestamps). MITIGATED_BY names the machine (a human Mark mitigated
    -- leaves it NULL). OWNER / ACK_AT are never touched here (MTTA stays a human number). Isolated like
    -- [attach].
    BEGIN
        CREATE OR REPLACE TEMPORARY TABLE _OW_INC_MITIGATE AS
        WITH live AS (
            SELECT RULE_ID, COMPANY, MAX(RAISED_AT) AS LAST_LIVE_AT
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED')
            GROUP BY RULE_ID, COMPANY
        )
        SELECT i.INCIDENT_ID,
               GREATEST(MAX(e.RESOLVED_AT), COALESCE(MAX(s.RESOLVED_AT), MAX(e.RESOLVED_AT)),
                        MAX(i.DETECTED_AT)) AS MITIGATED_TS
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
          ON m.INCIDENT_ID = i.INCIDENT_ID
         AND m.MEMBER_KIND = 'ALERT'
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          ON e.EVENT_ID = m.REF_ID
        LEFT JOIN live l
          ON l.RULE_ID = e.RULE_ID
         AND l.COMPANY = e.COMPANY
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
          ON COALESCE(e.RESOLUTION_KIND, '') IN ('SUPERSEDED', 'SNOOZE_SUPPRESSED')
         AND s.RULE_ID = e.RULE_ID
         AND s.COMPANY = e.COMPANY
         AND s.RAISED_AT >= e.RAISED_AT
         AND s.STATUS = 'RESOLVED'
        WHERE i.STATUS = 'OPEN'
        GROUP BY i.INCIDENT_ID
        HAVING COUNT_IF(e.EVENT_ID IS NULL OR e.STATUS <> 'RESOLVED') = 0
           AND COUNT_IF(COALESCE(e.RESOLUTION_KIND, '') IN ('SUPERSEDED', 'SNOOZE_SUPPRESSED')
                        AND l.LAST_LIVE_AT >= e.RAISED_AT) = 0
           AND GREATEST(MAX(e.RESOLVED_AT), COALESCE(MAX(s.RESOLVED_AT), MAX(e.RESOLVED_AT)))
               <= DATEADD('hour', -1, CURRENT_TIMESTAMP());

        UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENTS i
           SET STATUS = 'MITIGATED',
               MITIGATED_AT = r.MITIGATED_TS,
               MITIGATED_BY = 'SP_INCIDENT_AUTODECLARE',
               UPDATED_AT = CURRENT_TIMESTAMP()
          FROM _OW_INC_MITIGATE r
         WHERE i.INCIDENT_ID = r.INCIDENT_ID
           AND i.STATUS = 'OPEN';
        mitigated := SQLROWCOUNT;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'IncidentAutodeclare', 'incident_mitigate_failed', :emsg,
                   'V154 auto-mitigate sweep - declare/attach above unaffected', CURRENT_ROLE();
    END;

    SELECT COUNT(*) INTO :made FROM _OW_AUTODECL;
    RETURN 'auto-declared ' || :made || ' incident(s); attached ' || :attached || ' later critical(s); auto-mitigated ' || :mitigated || ' incident(s)';
END;
$$;

-- >>> derived:SP_ALERT_SCAN  (from V157; + [26] SEC_LOGIN_TAKEOVER + [27] SEC_ADMIN_GRANT hourly counting arms, tally 12 -> 14, V162)
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
    budget_usd FLOAT;
    credit_price FLOAT;
    ai_credit_price FLOAT;
    emsg VARCHAR;
    fails INT DEFAULT 0;
    ct_hour INT DEFAULT 5;   -- V157: the Central hour of this run ([cadence] below); 5 sits in both slots
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)
      INTO :budget_usd, :credit_price, :ai_credit_price
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
        -- PIPE_COPY_FAILURES: failed or partial file loads in the last 24h.
        -- Broken ingestion is the most preventable 'found out too late' class.
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(p.DB),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess
               IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY),
               p.DB || '.' || p.SCH || '.' || p.TBL || ': ' || p.FAILED_FILES || ' failed file load(s) (24h)',
               'Schema ' || p.DB || '.' || p.SCH ||
                   IFF(p.PIPE IS NOT NULL, ' | pipe ' || p.PIPE, ' | bulk COPY') ||
                   ' | sample error: ' || LEFT(COALESCE(p.SAMPLE_ERROR, 'n/a'), 300),
               p.FAILED_FILES,
               c.RULE_ID || '|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #1: band matches the CRITICAL severity so a HIGH->CRITICAL crossing re-fires
        FROM cfg c
        JOIN (
            SELECT TABLE_CATALOG_NAME AS DB, TABLE_SCHEMA_NAME AS SCH, TABLE_NAME AS TBL,
                   MAX(PIPE_NAME) AS PIPE,
                   COUNT(*) AS FAILED_FILES,
                   MAX(FIRST_ERROR_MESSAGE) AS SAMPLE_ERROR
            FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
            WHERE LAST_LOAD_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
              AND STATUS IN ('Load failed', 'Partially loaded')
            GROUP BY 1, 2, 3
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
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               nn.USER_NAME || ' logged in from new network ' || nn.CLIENT_IP,
               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline. Auth: '
                   || COALESCE(nn.AUTH_FACTOR, '?')
                   || '. Expected after travel/VPN/host changes; anything else is the finding.',
               nn.LOGINS,
               c.RULE_ID || '|' || nn.USER_NAME || '|' || nn.CLIENT_IP
        FROM cfg c
        JOIN (
            SELECT L.USER_NAME,
                   COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
                   MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
                   COUNT(*) AS LOGINS,
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
                   || '. Source: ACCOUNT_USAGE.GRANTS_TO_ROLES - review in Security -> Access.',
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
                            'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
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
                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
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
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'supersede_sweep_failed', :emsg, 'V067 #40 escalation supersede - other rules unaffected', CURRENT_ROLE();
    END;


    -- [auto-clear sweep] V091: resolve TODAY's still-OPEN live-window events whose
    -- scope has dropped back below the rule's CLEAR threshold (hysteresis, default
    -- 0.9 x THRESHOLD_NUM). Runs AFTER the raise arms + the supersede sweep so an
    -- escalated/superseded event is never also auto-cleared this pass. OPEN-only
    -- (manual RESOLVE wins and is never reopened; an active SNOOZE is left alone; an
    -- ACK is a human actively working it, so v1 leaves it too). The >=1h dwell plus
    -- below-CLEAR hysteresis mean an event cannot open and auto-close in one cadence.
    -- Only today's bucket (LIKE '%|<today>') is touched, so historical day-stamped
    -- exceedances are never rewritten. RESOLUTION_KIND='AUTO_CLEARED' is excluded from
    -- per-rule precision/MTTR in the app read-path exactly like SUPERSEDED. Wrapped so
    -- a sweep failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'
         WHERE ev.STATUS = 'OPEN'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                              WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')   -- V157: only rules whose still-firing set this sweep recomputes; other opt-ins have their own clear sweep
           AND ev.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())                    -- V096: recent window (was date-in-key); catches next-day-cleared 24h conditions
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
    -- only keys (IP, grant time) never end in a bare date so they are never stripped -- untouched.
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

    RETURN 'alert scan v13 (V162: + SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT hourly; V157 gates unchanged): ' || (14 - :fails) || '/14 rule blocks ok';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 162 AS VERSION,
       'Next-Fifty #39 (owner 2026-09-29): two hourly identity alerts that never auto-declare an incident. SEC_LOGIN_TAKEOVER (SP_ALERT_SCAN arm [26], ungated): at least THRESHOLD_NUM (seed 5, floor 2) failed logins by one user within 15 minutes, then a successful login within 60 minutes; one event per episode; CRITICAL when the login is off-hours (20:00-06:00 America/Chicago or a weekend) or the user directly held ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS or SNOW_SYSADMINS, else the rule severity (HIGH); 27h LOGIN_HISTORY read, 24h anchor window. SEC_ADMIN_GRANT (arm [27], ungated): one event per direct grant of one of those roles to a user in the last 26h, revoked or not; flat HIGH; the title flags off-hours and first-time grants. Both company ALL; keys end in an explicit millisecond timestamp (never a bare EVENT_ID, which the V117 carry-forward would read as a date). SP_INCIDENT_AUTODECLARE re-derived from V154, byte-identical except the crit CTE excludes both rules and [attach] links either rule only to an incident that already holds the same user ([auto-mitigate] unchanged). SP_ALERT_SCAN re-derived from V157, byte-identical except the two counting arms and the tally 12 -> 14. Seeds both rules WHEN NOT MATCHED only. No task change, no SETTINGS key, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 162);
