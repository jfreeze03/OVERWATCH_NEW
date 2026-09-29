-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  APPLY V162, V163, V164, V165 (in order) + PART B verify. FOUR pending migrations (Next-Fifty wave 4;
--  app v4.602.0 reads all four, each behind its own schema gate).
--
--  BEFORE THIS FILE:
--    1. If not done yet, return the snowflake/run/PROBES_NEXT_FIFTY_WAVE4.sql grids S4b, S6 and S7 (the #39 and
--       #37 volumes); the rest can follow the apply.
--    2. Run snowflake/run/PREFLIGHT_WAVE4.sql (read-only) and read P162.1, P162.4, P163.1, P164.1 and P164.2:
--       - THE ESCALATION EMAIL CHOICE IS REQUIRED. Your 2026-09-29 C4b grid showed OVERWATCH_EMAIL has NO
--         DEFAULT_RECIPIENTS, so P164.1 reads DEFAULT_RECIPIENTS_SET FALSE (still check its
--         SNOW_ACCOUNTADMINS_CAN_USE). Pick ONE before the V164 section:
--         (A) Email on: as the integration's owner, SET DEFAULT_RECIPIENTS on OVERWATCH_EMAIL in Snowsight to the
--             address already in its ALLOWED_RECIPIENTS (docs/EMAIL_RECIPIENT_RUNBOOK.md, requirement 4; type the
--             address only in Snowsight, never in a file). Every unacknowledged CRITICAL is then emailed about
--             2 h after its first notification, INCLUDING the TREXIS / UNKNOWN ones the ALFA-only Teams route
--             never carries (P164.2's EMAIL_ONLY rows): for those the escalation email is the first message.
--         (B) Teams-only: FIRST run the first seed below. TREXIS / UNKNOWN CRITICALs then never escalate.
--         With neither, the email leg fails on every hourly run while an email-only CRITICAL is open (logged as
--         escalation_email_failed; Alerts > Native delivery counts the failed sends).
--       - P164.2 or P162.4 list CRITICALs you do not want re-posted and emailed: acknowledge or resolve them
--         (the P162.4 takeovers within 2 hours of the first hourly scan), or FIRST run the second seed below
--         (escalation off; turn it on later in Admin > Settings).
--       Both seeds survive V164's MERGE (WHEN NOT MATCHED only). Uncomment one only when you mean it:
--         -- INSERT INTO DBA_MAINT_DB.OVERWATCH.SETTINGS (KEY, VALUE) SELECT 'ESCALATE_EMAIL_INTEGRATION', ''
--         --  WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'ESCALATE_EMAIL_INTEGRATION');
--         -- INSERT INTO DBA_MAINT_DB.OVERWATCH.SETTINGS (KEY, VALUE) SELECT 'ESCALATE_AFTER_MIN', '0'
--         --  WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'ESCALATE_AFTER_MIN');
--    3. Deploy 4.602.0 FIRST: snow streamlit deploy --replace. Its playbooks, navigation and Admin editors must
--       be live before the first new event (and the 4.601 Admin page would list the new SETTINGS rows as
--       "no longer read"). Every new app read and caption is gated on its own migration, so the deployed app
--       keeps the pre-apply behaviour until each one lands.
--  (The still-pending V161.13 change-risk re-check moved to snowflake/run/VERIFY_V161_FOLLOWUP.sql, read-only;
--   the V160 Monday grids stay in VERIFY_V160_MONDAY.sql.)
--
--  Apply top-to-bottom as SNOW_ACCOUNTADMINS and STOP ON THE FIRST ERROR. The guard chain is
--  V162 -> V163 -> V164 -> V165 (each refuses to run before the one before it); all four are idempotent, so an
--  already-applied one no-ops. There is NO apply-time CALL: a hand CALL of a scan, the notifier or the digest
--  can page or email, so none is in this file (PART B's one commented CALL, V162.4, is optional and sends
--  nothing).
--
--  V162 (#39):  SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT, HOURLY (scan arms [26]/[27], ungated): a failed-login burst
--               (5 in 15 min) then a success within 60 min, CRITICAL off-hours Central (20:00-06:00, weekends) or
--               for a direct admin-tier role holder, else HIGH; one HIGH event per direct admin-tier grant to a
--               user. Company ALL. SP_INCIDENT_AUTODECLARE (from V154) is replaced FIRST: it never declares for
--               either rule, and its [attach] links them only to an incident that already holds the same user;
--               then SP_ALERT_SCAN (from V157), tally 12 -> 14. First hourly run: the last 24h of episodes and
--               26h of grants (P162.1 / P162.2; the CRITICAL takeovers among them: P162.4).
--  V163 (#37a, #44b, #39): SP_ALERT_SCAN_DAILY (from V160) + [28] COST_AI_USER_RUNAWAY (> 2x the 15-credit cap
--               AND robust z >= 3.5 vs the user's own prior 90 active days; < 5 days = cap alone; HIGH) and [29]
--               SEC_TRUST_REGRESSION (a CRITICAL/HIGH Trust Center scanner's at-risk count up vs its previous
--               snapshot day; HIGH); [07] failed-logins text says lockout vs got in; tally 12 -> 14. Seeds 2
--               rules + AI_RUNAWAY_ROBUST_Z / AI_RUNAWAY_INCLUDE_FUNCTIONS. First daily run: the last 3 complete
--               days and today's / yesterday's Trust Center rises (P163.1 / P163.3).
--  V164 (#40):  SP_NOTIFY_WEBHOOK (from V064): lines read '[SEV] title | company | detail | event <id>'; a
--               CRITICAL nobody acknowledged ESCALATE_AFTER_MIN (120) minutes after its first notification is
--               re-posted once to the route(s) that delivered it and emailed through OVERWATCH_EMAIL (its
--               DEFAULT_RECIPIENTS); an incident counts only when a person acknowledged, mitigated or closed it
--               after the alert joined it. + ALERT_EVENTS.ESCALATED_AT, + 2 SETTINGS. First hourly run: every
--               OPEN, unacknowledged CRITICAL of the last 7 days first notified 120+ min ago (P164.2).
--               !! OWNER SMOKE TEST: the send, the ARRAY handling and the nested cursor loop are runtime-only
--               (PART B V164.3 / V164.4).
--  V165 (#24):  SP_DAILY_DIGEST (from V112): FACTS + measured GROUNDING_OK; a templated digest labelled not
--               AI-written on a figure mismatch or a Cortex failure; the Teams text JSON-escaped (a raw newline or
--               quote made Teams Workflows reject the card). + 6 nullable DAILY_DIGEST columns. First run: the
--               next 07:20 CT digest.
--
--  EXPECT on the Security page: no DROP and no CREATE OR REPLACE TABLE in this file, so no DESTRUCTIVE / CRITICAL
--  CHANGE RISK row (unlike V161). At most LOW / MEDIUM CREATE and ALTER rows: the five CREATE OR REPLACE
--  PROCEDUREs (V162 two, V163, V164 and V165 one each), the seven ALTER TABLE ... ADD COLUMNs (V164 one, V165
--  six) and the two ALTER SESSION lines; the MERGEs and INSERTs are DML, not change rows. PART B's last grid
--  reads it the next day. Nothing emails or pages at apply time; the first new events arrive with the next
--  hourly chain (~:07 Central) and the next morning's daily scan and digest.
--  Rollback: RUNBOOK section 12, "Rolling back wave 4" (reverse order; V162 = roll V163 back first, then
--  V157's scan, usually enough; V154's autodeclare only after 24h or after resolving the identity events as
--  EXPECTED; V164 soft = ESCALATE_AFTER_MIN 0).
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE SCHEMA DBA_MAINT_DB.OVERWATCH;   -- the security loader records each statement's current database (a *PROD* one scores higher)
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- the task clock: every gate and stamp is Central

-- =====================================================================
--  MIGRATION 1 of 4 -- APPLY V162 (idempotent; GUARDS on V161). Source: snowflake/migrations/V162__security_takeover_admin_grant.sql
-- =====================================================================
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

-- =====================================================================
--  MIGRATION 2 of 4 -- APPLY V163 (idempotent; GUARDS on V162). Source: snowflake/migrations/V163__ai_runaway_trust_regression.sql
-- =====================================================================
-- V163__ai_runaway_trust_regression.sql
--
-- Next Fifty wave 4 (#37a, #44b, #39): two new counting arms in the nightly SP_ALERT_SCAN_DAILY -- a per-user
-- AI runaway and a Trust Center regression -- and clearer text for the nightly failed-logins alert.
--
-- WHY: (#37a) one user burning AI credits was visible only to someone who opened Cost Intelligence > Chargeback
-- & AI; the account-level COST_AI_CREEP (week over week) cannot see a single user. (#44b) nothing pushed a Trust
-- Center regression: V_SECURITY_TRUST_DELTA is pull-only and compares only the two newest snapshot days. (#39)
-- the nightly SEC_FAILED_LOGINS text read the same whether or not the burst got in; since V162 the hourly
-- SEC_LOGIN_TAKEOVER pages a burst that ended in a success.
--
--   ~ SP_ALERT_SCAN_DAILY re-derived from V160 (its current definer), byte-identical except:
--     + counting arm [28] COST_AI_USER_RUNAWAY (FACT_AI_USAGE_DAILY, mart-only): a user's AI credits on one complete
--       day above THRESHOLD_NUM (2) x COCO_DAILY_CAP_CREDITS (15) AND a robust z >= AI_RUNAWAY_ROBUST_Z (3.5)
--       against the user's own active days in the 90 days before (median/MAD, the V150 engine; the scored day
--       is never its own baseline). Fewer than 5 prior active days = no baseline yet: the cap alone decides
--       (owner 2026-09-29). AI Functions rows count only with AI_RUNAWAY_INCLUDE_FUNCTIONS = TRUE and a named
--       user (inert today: the loader books them to ACCOUNT). The last 3 complete days are re-scored each
--       morning, one event per user-day. HIGH; METRIC_VALUE = the cap multiple; COMPANY = COMPANY_FOR_USER,
--       ALL when UNKNOWN.
--     + counting arm [29] SEC_TRUST_REGRESSION (SECURITY_TRUST_SNAPSHOT via LAG, not V_SECURITY_TRUST_DELTA): a
--       CRITICAL or HIGH scanner's at-risk count rose by >= THRESHOLD_NUM (1) against its previous
--       snapshot day; today's and yesterday's rows are checked, so a rise after the morning scan lands the next
--       morning -- unless that morning already raised for the scanner-day: one event per scanner per snapshot
--       day (the counts of the scan that raised it), so a further rise the same day is not pushed again. A
--       first-ever snapshot never raises. HIGH, company ALL.
--     ~ [07] SEC_FAILED_LOGINS: TITLE and DETAIL say whether the day also had successful logins ('so far' on
--       today's partial row, which the ~06:45 load covers only in part and is never re-raised), and point a
--       burst that got in to the hourly SEC_LOGIN_TAKEOVER while that rule is enabled, and to the
--       Account-takeover candidates lens either way. Predicate, severity and key unchanged (no re-fire).
--     ~ tally 12 -> 14 (self-alert, heartbeat, RETURN).
--   + ALERT_CONFIG COST_AI_USER_RUNAWAY (COST, HIGH, 2, 24h) and SEC_TRUST_REGRESSION (SECURITY, HIGH,
--     1, 24h), WHEN NOT MATCHED only; AUTO_CLEAR_ENABLED keeps its default.
--   + SETTINGS AI_RUNAWAY_ROBUST_Z = 3.5 and AI_RUNAWAY_INCLUDE_FUNCTIONS = FALSE, WHEN NOT MATCHED only.
--
-- COST: two mart-only INSERTs per nightly run (a few compile-seconds a day); COMPANY_FOR_USER runs only on a
-- raised row. No new table, view, task, proc or UDF.
-- LATENCY: daily (~07:00 Central). A runaway day the mart had not loaded yet is raised on the next morning's run;
-- a Trust Center rise after the morning scan, the next morning (unless that scanner-day already raised).
-- FIRST RUN: the next daily scan raises runaways from the last 3 complete mart days and regressions dated today
-- or yesterday; preview both with the read-only PREFLIGHT (P163.1, P163.3). Nothing runs at apply time.
-- ROLLBACK: re-run V160's SP_ALERT_SCAN_DAILY (the tally goes back to 12 and the old [07] text returns);
-- optionally disable the two rules in Alerts > Rules. The settings can stay.
-- Apply AFTER V162 (the [07] text names the hourly SEC_LOGIN_TAKEOVER). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20163, 'V163 requires V162 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 162) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The two rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered. THRESHOLD_NUM is the cap
-- multiple for the runaway (the rule editor tunes it; the robust-z bar is the AI_RUNAWAY_ROBUST_Z setting) and
-- the at-risk entities added for the regression.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_AI_USER_RUNAWAY', 'COST', 'Per-user AI runaway: one user''s AI credits on a day above THRESHOLD_NUM x the daily cap (COCO_DAILY_CAP_CREDITS) and a robust-z outlier against their own prior 90 days', TRUE, 'HIGH', 2, 24),
        ('SEC_TRUST_REGRESSION', 'SECURITY', 'Trust Center regression: a CRITICAL or HIGH scanner''s at-risk entity count rose by at least THRESHOLD_NUM since its previous snapshot day', TRUE, 'HIGH', 1, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- The runaway's two knobs (Admin > Settings). WHEN NOT MATCHED only.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('AI_RUNAWAY_ROBUST_Z', '3.5'),
        ('AI_RUNAWAY_INCLUDE_FUNCTIONS', 'FALSE')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- >>> derived:SP_ALERT_SCAN_DAILY  (from V160; + [28] COST_AI_USER_RUNAWAY + [29] SEC_TRUST_REGRESSION counting arms, [07] burst-vs-lockout wording, tally 12 -> 14, V163)
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
               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY
                   || IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,
                          ', ' || (lg.LOGINS - lg.FAILED_LOGINS) || ' successful'
                              || IFF(lg.DAY >= CURRENT_DATE(), ' so far', ''),
                          IFF(lg.DAY >= CURRENT_DATE(), ' and no successful login so far today',
                              ' and no successful login')),
               IFF(lg.DAY >= CURRENT_DATE(),
                   'Partial day: today counts only what the ~06:45 Central daily load saw (LOGIN_HISTORY lags up '
                   || 'to 2 h), and this event is not updated when the rest of the day loads. ',
                   '')
               || IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,
                   'The same day also had successful logins. While the hourly SEC_LOGIN_TAKEOVER rule is enabled '
                   || '(Alerts > Rules), a failed burst followed within 60 minutes by a success raises it (CRITICAL '
                   || 'off-hours or for an admin role); either way, check Security > Access > Authentication > '
                   || 'Account-takeover candidates. ',
                   'No successful login ' || IFF(lg.DAY >= CURRENT_DATE(), 'so far today', 'that day')
                   || ': most likely a lockout or a job still sending an old secret '
                   || '(a guessing attempt that never got in looks the same). ')
                   || 'Review Security > Access > Authentication: failed-login reasons and client IPs.',
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
    -- [28] COST_AI_USER_RUNAWAY (V163, Next-Fifty #37a: a runaway AI user, raised the morning after. BOTH signals:
    --      the user's AI credits on one complete mart DAY exceed THRESHOLD_NUM x COCO_DAILY_CAP_CREDITS
    --      (2 x 15 by default) AND sit >= AI_RUNAWAY_ROBUST_Z (3.5) robust-z above the user's OWN active days
    --      in the 90 days before it (median/MAD 0.6745, mean-absolute-deviation 0.7979 fallback -- the V150
    --      engine, but the scored day is never its own baseline). Fewer than 5 prior active days = no baseline
    --      yet: the cap leg alone decides (owner 2026-09-29), so a brand-new heavy user is not silent.
    --      FACT_AI_USAGE_DAILY, Cortex Code (Snowsight + CLI); AI Functions rows count only when
    --      AI_RUNAWAY_INCLUDE_FUNCTIONS is TRUE AND the row names a user (the loader books them to ACCOUNT
    --      today, so the switch is inert until it attributes them). The last 3 complete days are re-scored
    --      with a per-user-day key: this scan is a sibling of TASK_LOAD_MARTS_V27_DAILY under
    --      TASK_NIGHTLY_RECONCILE (V071), so a day the mart had not loaded yet is raised on the next run, once.
    --      METRIC_VALUE = the cap multiple (THRESHOLD_NUM units); COMPANY = the user's company, ALL when it is
    --      UNKNOWN. HIGH (c.SEVERITY), never a literal CRITICAL.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        knob AS (
            SELECT COALESCE(NULLIF(GREATEST(COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'COCO_DAILY_CAP_CREDITS', VALUE, NULL))), 15), 0), 0), 15) AS CAP_CR,
                   COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_RUNAWAY_ROBUST_Z', VALUE, NULL))), 3.5) AS Z_MIN,
                   COALESCE(TRY_TO_BOOLEAN(MAX(IFF(KEY = 'AI_RUNAWAY_INCLUDE_FUNCTIONS', VALUE, NULL))), FALSE) AS INCL_FN
            FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
        ),
        ud AS (
            SELECT f.USER_NAME, f.DAY, SUM(f.CREDITS) AS CR,
                   LISTAGG(DISTINCT f.SOURCE, '+') WITHIN GROUP (ORDER BY f.SOURCE) AS SOURCES
            FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY f
            CROSS JOIN clk k
            CROSS JOIN knob n
            WHERE f.DAY >= DATEADD('day', -93, k.TODAY) AND f.DAY < k.TODAY
              AND f.USER_NAME NOT IN ('ACCOUNT', 'UNKNOWN')
              AND (f.SOURCE <> 'Functions' OR n.INCL_FN)
            GROUP BY f.USER_NAME, f.DAY
            HAVING SUM(f.CREDITS) > 0
        ),
        cand AS (
            SELECT u.USER_NAME, u.DAY, u.CR, u.SOURCES
            FROM ud u
            CROSS JOIN clk k
            CROSS JOIN knob n
            JOIN cfg c ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
            WHERE u.DAY >= DATEADD('day', -3, k.TODAY)
              AND u.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2)
        ),
        hist AS (
            SELECT c.USER_NAME, c.DAY, h.CR AS HCR
            FROM cand c
            JOIN ud h ON h.USER_NAME = c.USER_NAME AND h.DAY < c.DAY AND h.DAY >= DATEADD('day', -90, c.DAY)
        ),
        med AS (
            SELECT USER_NAME, DAY, MEDIAN(HCR) AS MED, COUNT(*) AS N_HIST
            FROM hist GROUP BY USER_NAME, DAY
        ),
        disp AS (
            SELECT h.USER_NAME, h.DAY, m.MED, m.N_HIST,
                   MEDIAN(ABS(h.HCR - m.MED)) AS MAD, AVG(ABS(h.HCR - m.MED)) AS MEAN_AD
            FROM hist h
            JOIN med m ON m.USER_NAME = h.USER_NAME AND m.DAY = h.DAY
            GROUP BY h.USER_NAME, h.DAY, m.MED, m.N_HIST
        ),
        scored AS (
            SELECT c.USER_NAME, c.DAY, c.CR, c.SOURCES, d.MED, COALESCE(d.N_HIST, 0) AS N_HIST,
                   CASE WHEN COALESCE(d.N_HIST, 0) < 5 THEN NULL
                        WHEN d.MAD > 0 THEN 0.6745 * (c.CR - d.MED) / d.MAD
                        WHEN d.MEAN_AD > 0 THEN 0.7979 * (c.CR - d.MED) / d.MEAN_AD
                        ELSE IFF(c.CR > d.MED, 999, 0) END AS Z
            FROM cand c
            LEFT JOIN disp d ON d.USER_NAME = c.USER_NAME AND d.DAY = c.DAY
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(s.USER_NAME), 'UNKNOWN'), 'ALL'),
               c.SEVERITY,
               LEFT(s.USER_NAME || ' used ' || ROUND(s.CR, 1) || ' AI credits on ' || TO_VARCHAR(s.DAY)
                   || ' (~$' || ROUND(s.CR * :ai_credit_price)::INT || '): ' || ROUND(s.CR / n.CAP_CR, 1)
                   || 'x the ' || ROUND(n.CAP_CR, 1) || '-credit daily cap', 300),
               LEFT(IFF(s.Z IS NULL,
                        'No baseline yet: ' || s.N_HIST || ' active day(s) in the 90 days before, fewer than 5, '
                            || 'so the cap alone raised this (a new or newly active heavy user). ',
                        'Robust z ' || ROUND(s.Z, 1) || ' (bar ' || n.Z_MIN || ', AI_RUNAWAY_ROBUST_Z) against '
                            || 'this user''s own median of ' || ROUND(s.MED, 2) || ' credits/day over '
                            || s.N_HIST || ' active day(s) in the 90 days before. ')
                   || 'Cap: above ' || ROUND(n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2), 1) || ' credits (THRESHOLD_NUM '
                   || 'x COCO_DAILY_CAP_CREDITS ' || ROUND(n.CAP_CR, 1) || '). Sources: ' || s.SOURCES || '. About '
                   || ROUND(s.CR * :ai_credit_price)::INT || ' USD at ' || ROUND(:ai_credit_price, 2)
                   || ' USD/credit (AI_CREDIT_PRICE_USD). Per-user daily credits: Cost Intelligence > Chargeback '
                   || '& AI > AI users. A per-user AI quota (Snowsight, Cost Management) caps it; size one with the '
                   || 'suggested per-user quota table under Per-user AI quotas & blocks there.', 2000),
               ROUND(s.CR / n.CAP_CR, 4),
               c.RULE_ID || '|' || s.USER_NAME || '|' || TO_VARCHAR(s.DAY)
        FROM cfg c
        JOIN scored s
          ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
        CROSS JOIN knob n
        WHERE s.Z IS NULL OR s.Z >= n.Z_MIN

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
                   'rule COST_AI_USER_RUNAWAY - other rules unaffected', CURRENT_ROLE();
    END;
    -- [29] SEC_TRUST_REGRESSION (V163, Next-Fifty #44b: a Trust Center scanner's at-risk count went UP. Reads
    --      SECURITY_TRUST_SNAPSHOT (hourly SP_LOAD_SECURITY_FACTS: one row per scanner per Central day, today's
    --      row rewritten each hour) and compares each of the last two snapshot days with the scanner's previous
    --      snapshot day -- the V_SECURITY_TRUST_DELTA REGRESSED test, but not that view: it compares only the
    --      newest day with the one before, so a once-a-day reader at ~07:00 would miss a rise that landed after
    --      07:00 (by the next morning both days carry the new count). CRITICAL/HIGH scanners only; a scanner's
    --      first-ever snapshot (no previous day) never raises, so enabling a package does not flood. The loader
    --      books a scanner missing from FINDINGS as 0, so its return reads as a rise (the DETAIL says so). One
    --      event per scanner per snapshot day, carrying the counts of the scan that raised it: a rise after
    --      ~07:00 lands the next morning only when that scanner-day had not raised yet -- a further rise on a
    --      day that already raised is NOT pushed again (that event stays open, no self-clear; Security > Trust
    --      Center shows the live count). Company ALL, HIGH (c.SEVERITY).)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        snap AS (
            SELECT t.SCANNER_ID, t.SCANNER_NAME, UPPER(t.SEVERITY) AS SEV, t.DAY, t.SCANNED_AT,
                   t.TOTAL_AT_RISK_COUNT AS CUR_N,
                   LAG(t.TOTAL_AT_RISK_COUNT) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_N,
                   LAG(t.DAY) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_DAY
            FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT t
            CROSS JOIN clk k
            WHERE t.DAY >= DATEADD('day', -30, k.TODAY)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT('Trust Center ' || s.SEV || ' scanner regressed: ' || COALESCE(s.SCANNER_NAME, s.SCANNER_ID)
                   || ' at-risk entities ' || s.PRIOR_N || ' -> ' || s.CUR_N || ' on ' || TO_VARCHAR(s.DAY), 300),
               LEFT('Scanner ' || s.SCANNER_ID || ': ' || s.CUR_N || ' entities at risk in the ' || TO_VARCHAR(s.DAY)
                   || ' snapshot (latest scan ' || COALESCE(TO_VARCHAR(s.SCANNED_AT, 'YYYY-MM-DD HH24:MI'), 'unknown')
                   || '), up from ' || s.PRIOR_N || ' on ' || TO_VARCHAR(s.PRIOR_DAY) || '. '
                   || IFF(s.PRIOR_N = 0,
                          'The previous day read 0: the loader books a scanner missing from FINDINGS as 0, so this '
                              || 'may be the scanner returning rather than new risk. ',
                          '')
                   || 'Security > Trust Center for the scanner delta; the entities and the suggested fix are in '
                   || 'Snowsight > Monitoring > Trust Center > Findings. Fix there, then re-scan.', 2000),
               s.CUR_N - s.PRIOR_N,
               c.RULE_ID || '|' || s.SCANNER_ID || '|' || TO_VARCHAR(s.DAY)
        FROM cfg c
        JOIN snap s
          ON c.RULE_ID = 'SEC_TRUST_REGRESSION'
         AND s.PRIOR_N IS NOT NULL
         AND s.CUR_N > 0
         AND s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)
         AND s.SEV IN ('CRITICAL', 'HIGH')
        CROSS JOIN clk k
        WHERE s.DAY >= DATEADD('day', -1, k.TODAY)

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
                   'rule SEC_TRUST_REGRESSION - other rules unaffected', CURRENT_ROLE();
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
               :fails || ' of 14 daily alert rule block(s) failed this run',
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
               ROW_COUNT = (14 - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = 'alert scan daily ' || (14 - :fails) || '/14 rule blocks ok (daily)'
         WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT 'ALERT_SCAN_DAILY', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   (14 - :fails), 1, 'alert scan daily ' || (14 - :fails) || '/14 rule blocks ok (daily)';
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   'ALERT_SCAN_DAILY heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan daily v5 (V163: + COST_AI_USER_RUNAWAY + SEC_TRUST_REGRESSION, [07] burst-vs-lockout wording): ' || (14 - :fails) || '/14 rule blocks ok (daily)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 163 AS VERSION,
       'Next Fifty wave 4 (#37a, #44b, #39). SP_ALERT_SCAN_DAILY re-derived from V160, byte-identical except: + counting arm [28] COST_AI_USER_RUNAWAY (FACT_AI_USAGE_DAILY, mart-only): the AI credits of one user on a complete day above THRESHOLD_NUM (2) x COCO_DAILY_CAP_CREDITS AND a robust z at least AI_RUNAWAY_ROBUST_Z (3.5) against the active days of that user in the 90 days before (median/MAD; fewer than 5 prior active days = no baseline, the cap alone decides); Cortex Code only unless AI_RUNAWAY_INCLUDE_FUNCTIONS and a named user; the last 3 complete days re-scored, one event per user-day; HIGH; METRIC_VALUE = the cap multiple; COMPANY = COMPANY_FOR_USER, ALL when UNKNOWN. + counting arm [29] SEC_TRUST_REGRESSION (SECURITY_TRUST_SNAPSHOT via LAG, not V_SECURITY_TRUST_DELTA): the at-risk count of a CRITICAL or HIGH scanner rose by at least THRESHOLD_NUM (1) against its previous snapshot day, today and yesterday checked; one event per scanner per snapshot day (a further rise the same day is not pushed again); a first snapshot never raises; HIGH, company ALL. [07] SEC_FAILED_LOGINS TITLE and DETAIL now say whether the day had successful logins (so far, on the partial current day) and point a burst that got in to the hourly SEC_LOGIN_TAKEOVER while that rule is enabled and to the Account-takeover candidates lens (predicate, severity and key unchanged). Tally 12 -> 14. Seeds the two rules and the settings AI_RUNAWAY_ROBUST_Z (3.5) and AI_RUNAWAY_INCLUDE_FUNCTIONS (FALSE), WHEN NOT MATCHED only. No task change, no new object, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 163);

-- =====================================================================
--  MIGRATION 3 of 4 -- APPLY V164 (idempotent; GUARDS on V163; OWNER SMOKE TEST in PART B). Source: snowflake/migrations/V164__notify_actionable_lines_escalation.sql
-- =====================================================================
-- V164__notify_actionable_lines_escalation.sql
--
-- Next-Fifty #40 (owner decisions 2026-09-29): Teams lines you can act on, and a one-time escalation of a
-- CRITICAL nobody acknowledged.
--
-- WHY: a Teams line said only '[SEV] <title>' -- no company, no detail, no event id to find it by -- and a
-- CRITICAL was posted once and then sat unanswered: nothing re-paged it and nothing reached anyone off Teams.
--
--   + ALERT_EVENTS.ESCALATED_AT TIMESTAMP_NTZ (ADD COLUMN IF NOT EXISTS): the once-per-event escalation marker.
--   + SETTINGS ESCALATE_AFTER_MIN '120' (0 or not a number = escalation off) and ESCALATE_EMAIL_INTEGRATION
--     'OVERWATCH_EMAIL' ('' = no email leg), WHEN NOT MATCHED only: a value set before the apply is kept.
--   ~ SP_NOTIFY_WEBHOOK re-derived from V064 (its current definer; V070, V112, V157 and V160 only mention it),
--     byte-identical except:
--       N1 13 escalation variables and cursor c2 (the enabled routes) in the DECLARE;
--       N2 every line reads '[SEV] <title, 140> | <company> | <detail, one line, 100> | event <EVENT_ID>' (ASCII),
--          identical in the 3000-char fit and in the LISTAGG, so the fit still equals what is sent (a worst-case
--          escaped line is under 900 chars, so a batch always holds at least one event);
--       N3 the escalation pass, inside the sender lease, after the drain and before the expired tail: a CRITICAL
--          still OPEN with no ACK_AT, not in an incident a human acknowledged, mitigated or closed AFTER the alert
--          joined it (INCIDENT_MEMBERS.LINKED_AT; V154's machine auto-mitigate, MITIGATED_BY =
--          SP_INCIDENT_AUTODECLARE, does not count), never snoozed (an ALERT_AUDIT SNOOZE row, or a V117
--          carry-forward of a snooze onto it: a SNOOZE_SUPPRESSED predecessor with the same band-stripped key),
--          raised inside the 7-day CRITICAL send window, whose rule still exists, and first notified
--          (NOTIFIED_AT, else RAISED_AT) ESCALATE_AFTER_MIN+ minutes ago escalates ONCE: re-posted to every
--          enabled route that already delivered it, and emailed through ESCALATE_EMAIL_INTEGRATION with the
--          notification-integration send, i.e. to that integration's DEFAULT_RECIPIENTS (no address is stored in
--          OVERWATCH). With the email leg off, only an event some enabled route delivered is eligible.
--          Capture-once (route-delivered events first, then oldest first, 3000 escaped chars -- an email-only
--          event never holds the batch while the email fails); right after each channel's send succeeds, the
--          ESCALATED_AT stamp for the ids it took (so a later error never re-posts them), then one ALERT_AUDIT
--          'ESCALATE' row per event the run stamped (every channel failing = nothing stamped, retried next run
--          inside the 7 days). Its own handler logs escalation_failed and never re-raises, so the expired tail,
--          the lease release and the RETURN still run. A re-post failure logs route_send_failed with the V064
--          CONTEXT prefix (the Native delivery card attributes it to its route); an email failure logs
--          escalation_email_failed;
--       N4 the RETURN adds '<n> CRITICAL(s) escalated' and a note (off / email failed / pass failed).
--
-- !! OWNER SMOKE TEST (like V064): the notification send, the ARRAY handling and the nested cursor loop are
--    runtime-only; RUN_NEXT PART B V164.3 / V164.4 prove them. The email leg needs DEFAULT_RECIPIENTS on the
--    integration and USAGE on it for the proc owner (PREFLIGHT P164.1).
-- COST: one SETTINGS read and one small capture SELECT on ALERT_EVENTS per hourly TASK_ALERT_NOTIFY run, plus sends
--    only when something is eligible: a few compile-seconds an hour. No new table, task, view or warehouse.
-- LATENCY: hourly (TASK_ALERT_NOTIFY runs in the chain off TASK_LOAD_HOURLY), so with 120 minutes an escalation
--    goes out about 120-185 minutes after the first notification.
-- THROUGHPUT: lines are about 3x longer, so a 3000-char batch holds about 8-13 events instead of about 40;
--    max_batches stays 6 (PREFLIGHT P164.3 counts the runs that would have spilled to the next hour).
-- FIRST RUN: every OPEN, never-acknowledged CRITICAL of the last 7 days first notified 120+ minutes ago escalates on
--    the next hourly run (one 3000-char batch per run; the rest follow). PREFLIGHT P164.2 lists them: acknowledge or
--    resolve stale ones first, or seed SETTINGS ('ESCALATE_AFTER_MIN', '0') before the apply (the MERGE keeps it).
-- ROLLBACK: soft -- set SETTINGS ESCALATE_AFTER_MIN to '0'; hard -- re-run ONLY V064's SP_NOTIFY_WEBHOOK CREATE
--    (V064 lines 74-351; never the whole V064 file, which would also roll back three other procs), which also
--    restores the old line format. The column and the settings can stay.
-- Nothing runs at apply time (no CALL: a hand CALL of the notifier can page and email).
-- Apply AFTER V163. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20164, 'V164 requires V163 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 163) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The once-per-event escalation marker (NULL = never escalated). Additive; existing rows read NULL.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS ESCALATED_AT TIMESTAMP_NTZ;

-- Escalation knobs (Admin > Settings). WHEN NOT MATCHED only: a value set before the apply (for example
-- ESCALATE_AFTER_MIN '0' to start with escalation off) is never overwritten. The integration is a NAME, not an
-- address: the email goes to that integration's DEFAULT_RECIPIENTS.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ESCALATE_AFTER_MIN', '120'),
        ('ESCALATE_EMAIL_INTEGRATION', 'OVERWATCH_EMAIL')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- >>> derived:SP_NOTIFY_WEBHOOK  (from V064; actionable lines + one-time CRITICAL escalation pass, Next-Fifty #40, V164)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    sent_total INT DEFAULT 0;
    routes_hit INT DEFAULT 0;
    expired INT DEFAULT 0;
    batches INT DEFAULT 0;              -- V064 rec8: total message batches sent this run
    r_batch INT DEFAULT 0;             -- V064 rec8: batches drained for the current route
    r_sent_any BOOLEAN DEFAULT FALSE;  -- V064 rec8: this route delivered >= 1 batch
    send_failed BOOLEAN DEFAULT FALSE; -- V064 rec8: this route's send raised -> stop draining it
    max_batches INT DEFAULT 6;         -- V064 rec8: bound batches/route/run (backlog spills to next run, never floods)
    message VARCHAR;
    emsg VARCHAR;
    r_route_id VARCHAR;
    r_family VARCHAR;
    r_minsev VARCHAR;
    r_integration VARCHAR;
    r_compfilter VARCHAR;   -- v4: per-route company scope (owner: Teams = ALFA-only for now)
    fits_ids ARRAY;         -- V063: frozen fitting EVENT_IDs (VARCHAR EVENT_ID) shared by message + ledger + NOTIFIED_AT
    esc_after_s VARCHAR;    -- V164 #40: SETTINGS.ESCALATE_AFTER_MIN (absent = '120'; 0 / not a number = off)
    esc_after NUMBER DEFAULT 0;
    esc_email VARCHAR;      -- V164 #40: SETTINGS.ESCALATE_EMAIL_INTEGRATION (absent = 'OVERWATCH_EMAIL'; '' = no email)
    esc_now TIMESTAMP_NTZ;  -- V164 #40: one frozen clock for the whole escalation pass
    esc_ids ARRAY;          -- V164 #40: the frozen escalation set (capture-once, the B9 invariant)
    r_esc_ids ARRAY;        -- V164 #40: the part of esc_ids THIS route already delivered
    esc_ok ARRAY;           -- V164 #40: ids at least one channel accepted (audited + stamped)
    esc_msg VARCHAR;
    esc_sent BOOLEAN DEFAULT FALSE;  -- V164 #40: this channel's send succeeded (stamp right after it)
    esc_routes INT DEFAULT 0;
    esc_emailed BOOLEAN DEFAULT FALSE;
    escalated INT DEFAULT 0;
    esc_note VARCHAR DEFAULT '';
    c1 CURSOR FOR
        SELECT r.ROUTE_ID, r.FAMILY, r.MIN_SEVERITY, r.INTEGRATION_NAME,
               COALESCE(r.COMPANY_FILTER, 'ALL') AS COMPANY_FILTER
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.ROUTE_ID;
    c2 CURSOR FOR           -- V164 #40: enabled routes, for the escalation re-post
        SELECT r.ROUTE_ID, r.INTEGRATION_NAME
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.ROUTE_ID;
BEGIN
    -- V064 #26: single-flight concurrency guard. The send-then-ledger ordering
    -- inside the loop is INTENTIONAL for paging -- at-least-once bias: a
    -- claim-before-send outbox could LOSE a page if the claim commits but the send
    -- is dropped, which is worse for an alerting system than a rare duplicate page.
    -- That ordering does leave one race: two OVERLAPPING runs can both read the same
    -- eligible events before either writes its ledger rows, and re-send. This lease
    -- closes ONLY that window; it does NOT change the send-first ordering. Acquire a
    -- single sentinel row at entry; bail if another live run holds it; release at
    -- exit. A lease older than 1h is treated as abandoned (a crashed run that never
    -- released) and reclaimed, so the sender can never wedge itself out forever.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
       SET HELD = TRUE, HOLDER = CURRENT_SESSION(), ACQUIRED_AT = CURRENT_TIMESTAMP()
     WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
       AND (HELD = FALSE OR ACQUIRED_AT < DATEADD('hour', -1, CURRENT_TIMESTAMP()));
    IF (SQLROWCOUNT = 0) THEN
        RETURN 'skipped - another SP_NOTIFY_WEBHOOK run holds the sender lease';
    END IF;

    FOR rec IN c1 DO
        r_route_id := rec.ROUTE_ID;
        r_family := rec.FAMILY;
        r_minsev := rec.MIN_SEVERITY;
        r_integration := rec.INTEGRATION_NAME;
        r_compfilter := rec.COMPANY_FILTER;
        r_batch := 0;
        r_sent_any := FALSE;
        send_failed := FALSE;

        -- V064 rec8: OLDEST-FIRST BOUNDED DRAIN. Each iteration captures-once
        -- (frozen ARRAY, the B9 invariant) the OLDEST eligible-for-this-route
        -- events that fit 3000 escaped chars, sends them, and ledgers +
        -- NOTIFIED_AT-stamps THAT SAME set. The ledger write makes the next
        -- iteration's eligibility exclude what was just sent, so the loop drains
        -- strictly forward and terminates when no eligible event remains (or at
        -- max_batches). A send failure stops THIS route this run; siblings drain.
        LOOP
            -- Capture-once: the OLDEST eligible events (open, within the send window --
            -- 24h, or 7d for CRITICAL per #10-slice -- matching this route's
            -- family/company/severity, not yet delivered to THIS
            -- route) whose cumulative JSON-escaped length (each line + 2 per '\n'
            -- separator) stays <= 3000, frozen into an ARRAY in send order.
            SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.RAISED_AT ASC, f.EVENT_ID)
              INTO :fits_ids
            FROM (
                SELECT e.EVENT_ID, e.RAISED_AT,
                       SUM(LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                           '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID,
                           CHR(92), CHR(92) || CHR(92)),
                           CHR(34), CHR(92) || CHR(34)),
                           CHR(10), CHR(92) || 'n'),
                           CHR(13), ''),
                           CHR(9),  CHR(92) || 't')) + 2)
                         OVER (ORDER BY e.RAISED_AT ASC, e.EVENT_ID
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                WHERE e.STATUS = 'OPEN'
                  -- V064 #10-slice: an undelivered CRITICAL must not starve past the 24h
                  -- send floor while it is still OPEN. Extend the send-eligibility window
                  -- to 7d for CRITICAL ONLY (still gated STATUS='OPEN' + the per-route
                  -- NOT EXISTS below, still drained oldest-first); every other severity
                  -- keeps the 24h floor. This is the CHEAP slice -- no dead-letter/replay
                  -- state machine. The expiry watchdog + #40 once-per-24h guard are intact
                  -- below (a CRITICAL delivered in this run's drain gets a ledger row, so
                  -- the watchdog's per-route NOT EXISTS excludes it that same run).
                  AND e.RAISED_AT >= CASE WHEN e.SEVERITY = 'CRITICAL'
                                          THEN DATEADD('day', -7, CURRENT_TIMESTAMP())
                                          ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END
                  AND (:r_family = 'ALL' OR c.FAMILY = :r_family)
                  AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')
                  AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
                      >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id)
            ) f
            WHERE f.CUM_LEN <= 3000;

            -- Nothing eligible left for this route -> done draining it.
            IF (fits_ids IS NULL OR ARRAY_SIZE(:fits_ids) = 0) THEN
                EXIT;
            END IF;

            -- Build the message from the frozen fits set ONLY, in the SAME
            -- (RAISED_AT ASC, EVENT_ID) order used to compute the fit, so its
            -- escaped length is <= 3000 by construction and LEFT(:message, 3000)
            -- never truncates mid-event.
            SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID, '\n')
                   WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)
              INTO :message
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);

            -- Non-empty fits set -> non-empty message; guard is defence only.
            IF (:message IS NULL OR :message = '') THEN
                EXIT;
            END IF;

            -- v3: JSON-escape (backslash first, then quote, newline, CR, tab).
            message := REPLACE(:message, CHR(92), CHR(92) || CHR(92));
            message := REPLACE(:message, CHR(34), CHR(92) || CHR(34));
            message := REPLACE(:message, CHR(10), CHR(92) || 'n');
            message := REPLACE(:message, CHR(13), '');
            message := REPLACE(:message, CHR(9),  CHR(92) || 't');

            BEGIN
                CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                    SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                        'OVERWATCH alerts:' || CHR(92) || 'n' || LEFT(:message, 3000)),
                    SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));

                -- Ledger rows for THIS route from the SAME frozen fits set (NOT a
                -- re-derivation). NOT EXISTS keeps it idempotent on retry.
                INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)
                SELECT e.EVENT_ID, :r_route_id
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);
                sent_total := sent_total + SQLROWCOUNT;

                -- Back-compat: NOTIFIED_AT means "delivered somewhere at least
                -- once" (drill / delivery chip / MTTA read it). Only for the
                -- frozen fits set actually sent; a non-fitting event stays NULL
                -- and re-drains a later run.
                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()
                 WHERE e.NOTIFIED_AT IS NULL
                   AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);

                r_sent_any := TRUE;
                batches := batches + 1;
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'NotifyWebhook', 'route_send_failed', :emsg,
                           'route ' || :r_route_id || ' integration ' || :r_integration ||
                           ' - will retry next run; other routes unaffected',
                           CURRENT_ROLE();
                    send_failed := TRUE;
            END;

            -- Integration down: stop draining THIS route this run (more batches
            -- would just fail). Other routes keep going.
            IF (send_failed) THEN
                EXIT;
            END IF;

            r_batch := r_batch + 1;
            IF (r_batch >= max_batches) THEN
                EXIT;   -- bounded: leave the remaining backlog for the next run
            END IF;
        END LOOP;

        IF (r_sent_any) THEN
            routes_hit := routes_hit + 1;
        END IF;
    END FOR;

    -- V164 #40: CRITICAL ESCALATION PASS (Next-Fifty #40, owner decision 2026-09-29). A CRITICAL still OPEN
    -- and never acknowledged (no ACK_AT; not in an incident a human acknowledged, mitigated or closed after
    -- the alert joined it -- the V154 machine auto-mitigate does not count; never snoozed -- an ALERT_AUDIT
    -- SNOOZE row, or a V117 carry-forward of a snooze onto it) whose first notification (NOTIFIED_AT, or
    -- RAISED_AT when no route ever took it) is ESCALATE_AFTER_MIN+ minutes old escalates ONCE: re-posted to
    -- every enabled route that already delivered it (the same Teams route), and emailed through
    -- ESCALATE_EMAIL_INTEGRATION, i.e. to that integration's DEFAULT_RECIPIENTS -- no address is written here.
    -- With the email leg off ('') only an event some enabled route delivered is eligible, and route-delivered
    -- events always fill the batch first, so an email-only one never holds it (even while the email fails).
    -- Capture-once: the ids are frozen (route-delivered first, then oldest first) into esc_ids within 3000
    -- escaped chars (the V063 B9 invariant), and every message, stamp and audit row derives from that one set.
    -- Send, then stamp at once: right after each channel's send succeeds, ESCALATED_AT (= the frozen pass clock)
    -- is set for the ids it took, so an error later in the pass can never re-post them next hour; an
    -- all-channel failure stamps nothing and retries next run inside the 7-day CRITICAL window (at-least-once,
    -- like the drain). Then one ALERT_AUDIT 'ESCALATE' row per event this run stamped. Inside the sender
    -- lease, so two runs never double-escalate. Isolated: an error here is logged (escalation_failed) and never
    -- re-raised -- the deliveries above, the expired tail, the lease release and the RETURN below still run.
    BEGIN
        SELECT COALESCE(MAX(IFF(KEY = 'ESCALATE_AFTER_MIN', VALUE, NULL)), '120'),
               COALESCE(MAX(IFF(KEY = 'ESCALATE_EMAIL_INTEGRATION', VALUE, NULL)), 'OVERWATCH_EMAIL')
          INTO :esc_after_s, :esc_email
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
         WHERE KEY IN ('ESCALATE_AFTER_MIN', 'ESCALATE_EMAIL_INTEGRATION');
        esc_after := COALESCE(TRY_TO_NUMBER(TRIM(:esc_after_s)), 0);
        IF (esc_after <= 0) THEN
            esc_note := '; escalation off (ESCALATE_AFTER_MIN)';
        ELSE
            esc_now := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
            esc_ok := ARRAY_CONSTRUCT();
            SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.EMAIL_ONLY, f.RAISED_AT ASC, f.EVENT_ID)
              INTO :esc_ids
            FROM (
                SELECT e.EVENT_ID, e.RAISED_AT, IFF(rd.EVENT_ID IS NULL, 1, 0) AS EMAIL_ONLY,
                       SUM(LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                           'ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID,
                           CHR(92), CHR(92) || CHR(92)),
                           CHR(34), CHR(92) || CHR(34)),
                           CHR(10), CHR(92) || 'n'),
                           CHR(13), ''),
                           CHR(9),  CHR(92) || 't')) + 2)
                         OVER (ORDER BY IFF(rd.EVENT_ID IS NULL, 1, 0), e.RAISED_AT ASC, e.EVENT_ID
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                LEFT JOIN (SELECT DISTINCT d.EVENT_ID
                           FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                           JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
                             ON r.ROUTE_ID = d.ROUTE_ID AND r.ENABLED) rd
                  ON rd.EVENT_ID = e.EVENT_ID
                WHERE e.SEVERITY = 'CRITICAL'
                  AND e.STATUS = 'OPEN'
                  AND e.ACK_AT IS NULL
                  AND e.ESCALATED_AT IS NULL
                  AND e.RAISED_AT >= DATEADD('day', -7, :esc_now)
                  AND COALESCE(e.NOTIFIED_AT, e.RAISED_AT) <= DATEADD('minute', -1 * :esc_after, :esc_now)
                  AND NOT EXISTS (SELECT 1
                                  FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                  JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
                                  WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID
                                    AND (i.ACK_AT >= m.LINKED_AT
                                         OR (i.STATUS = 'MITIGATED' AND i.MITIGATED_AT >= m.LINKED_AT
                                             AND COALESCE(i.MITIGATED_BY, '') <> 'SP_INCIDENT_AUTODECLARE')
                                         OR (i.STATUS = 'RESOLVED' AND i.RESOLVED_AT >= m.LINKED_AT)))
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a
                                  WHERE a.EVENT_ID = e.EVENT_ID AND a.ACTION = 'SNOOZE')
                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS s
                                  WHERE s.RULE_ID = e.RULE_ID
                                    AND s.RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'
                                    AND s.RAISED_AT < e.RAISED_AT
                                    AND s.RESOLVED_AT >= e.RAISED_AT
                                    AND IFF(SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'
                                              AND TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10)) IS NOT NULL,
                                            LEFT(s.DEDUPE_KEY, LENGTH(s.DEDUPE_KEY) - 11), s.DEDUPE_KEY)
                                        = IFF(SUBSTR(e.DEDUPE_KEY, -11, 1) = '|'
                                              AND TRY_TO_DATE(RIGHT(e.DEDUPE_KEY, 10)) IS NOT NULL,
                                            LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - 11), e.DEDUPE_KEY))
                  AND (COALESCE(TRIM(:esc_email), '') <> '' OR rd.EVENT_ID IS NOT NULL)
            ) f
            WHERE f.CUM_LEN <= 3000;

            IF (esc_ids IS NOT NULL AND ARRAY_SIZE(:esc_ids) > 0) THEN
                -- Teams leg: re-post only what THIS route already delivered (the same route that paged).
                FOR erec IN c2 DO
                    r_route_id := erec.ROUTE_ID;
                    r_integration := erec.INTEGRATION_NAME;
                    SELECT ARRAY_AGG(DISTINCT d.EVENT_ID)
                      INTO :r_esc_ids
                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                    WHERE d.ROUTE_ID = :r_route_id
                      AND ARRAY_CONTAINS(d.EVENT_ID::VARIANT, :esc_ids);
                    IF (r_esc_ids IS NOT NULL AND ARRAY_SIZE(:r_esc_ids) > 0) THEN
                        SELECT LISTAGG('ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID, '\n')
                               WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)
                          INTO :esc_msg
                        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                        WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :r_esc_ids);
                        -- the V064 JSON escape (backslash first, then quote, newline, CR, tab)
                        esc_msg := REPLACE(:esc_msg, CHR(92), CHR(92) || CHR(92));
                        esc_msg := REPLACE(:esc_msg, CHR(34), CHR(92) || CHR(34));
                        esc_msg := REPLACE(:esc_msg, CHR(10), CHR(92) || 'n');
                        esc_msg := REPLACE(:esc_msg, CHR(13), '');
                        esc_msg := REPLACE(:esc_msg, CHR(9),  CHR(92) || 't');
                        esc_sent := FALSE;
                        BEGIN
                            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                                    'OVERWATCH ESCALATION - CRITICAL unacknowledged ' || :esc_after || '+ min:'
                                    || CHR(92) || 'n' || LEFT(:esc_msg, 3000)),
                                SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
                            esc_sent := TRUE;
                        EXCEPTION
                            WHEN OTHER THEN
                                emsg := SQLERRM;
                                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                                SELECT 'NotifyWebhook', 'route_send_failed', :emsg,
                                       'route ' || :r_route_id || ' integration ' || :r_integration ||
                                       ' - escalation re-post; the email leg is unaffected',
                                       CURRENT_ROLE();
                        END;
                        IF (esc_sent) THEN
                            -- stamp at once: nothing later in this pass can make the next run re-post these
                            esc_ok := ARRAY_CAT(:esc_ok, :r_esc_ids);
                            esc_routes := esc_routes + 1;
                            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                               SET ESCALATED_AT = :esc_now
                             WHERE e.ESCALATED_AT IS NULL
                               AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :r_esc_ids);
                            escalated := escalated + SQLROWCOUNT;
                        END IF;
                    END IF;
                END FOR;

                -- Email leg: every escalated id, to the integration's DEFAULT_RECIPIENTS (never an address here).
                IF (COALESCE(TRIM(:esc_email), '') <> '') THEN
                    SELECT LISTAGG('ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now) || ' min) ' || '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140) || ' | ' || e.COMPANY || IFF(COALESCE(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), '') = '', '', ' | ' || LEFT(TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   ')), 100)) || ' | event ' || e.EVENT_ID, '\n')
                           WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)
                      INTO :esc_msg
                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ids);
                    esc_sent := FALSE;
                    BEGIN
                        CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                            SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                                'OVERWATCH ESCALATION - CRITICAL alert(s) nobody acknowledged within '
                                || :esc_after || ' minutes:' || CHR(10) || CHR(10) || :esc_msg || CHR(10) || CHR(10)
                                || 'Acknowledge in OVERWATCH > Alerts > Open events. Each alert escalates once.'),
                            SNOWFLAKE.NOTIFICATION.INTEGRATION(TRIM(:esc_email)));
                        esc_sent := TRUE;
                    EXCEPTION
                        WHEN OTHER THEN
                            emsg := SQLERRM;
                            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                            SELECT 'NotifyWebhook', 'escalation_email_failed', :emsg,
                                   'integration ' || :esc_email || ' - ' || ARRAY_SIZE(:esc_ids) ||
                                   ' escalation(s) not emailed; check DEFAULT_RECIPIENTS and USAGE on the integration',
                                   CURRENT_ROLE();
                            esc_note := :esc_note || '; escalation email failed (APP_ERROR_LOG escalation_email_failed)';
                    END;
                    IF (esc_sent) THEN
                        esc_ok := ARRAY_CAT(:esc_ok, :esc_ids);
                        esc_emailed := TRUE;
                        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                           SET ESCALATED_AT = :esc_now
                         WHERE e.ESCALATED_AT IS NULL
                           AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ids);
                        escalated := escalated + SQLROWCOUNT;
                    END IF;
                END IF;

                -- Then the audit: one ESCALATE row per event THIS run stamped (ESCALATED_AT = the frozen
                -- clock), written after every send so the note carries the whole run's outcome.
                IF (ARRAY_SIZE(:esc_ok) > 0) THEN
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)
                    SELECT e.EVENT_ID, 'ESCALATE',
                           'unacknowledged ' || :esc_after || '+ min; this run re-posted to ' || :esc_routes ||
                           ' route(s), email ' || IFF(:esc_emailed, 'sent', 'not sent'),
                           'SP_NOTIFY_WEBHOOK'
                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    WHERE e.ESCALATED_AT = :esc_now
                      AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ok);
                END IF;
            END IF;
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'NotifyWebhook', 'escalation_failed', :emsg,
                   'escalation pass - the deliveries above are unaffected; retries next run',
                   CURRENT_ROLE();
            esc_note := '; escalation pass FAILED (APP_ERROR_LOG escalation_failed)';
    END;

    -- Loud, not silent: an (event,route) pair aging past the 24h window with NO
    -- delivery FOR THAT ROUTE gets one error-log row per episode.
    -- V064 #27 (PER-ROUTE expiry): the old test keyed on ALERT_EVENTS.NOTIFIED_AT,
    -- which is stamped after the FIRST route delivers -- so an event that reached
    -- route A but never route B was NEVER flagged expired for B. Expiry is now per
    -- (event,route): a pair is expired when the event is past the 24h window and has
    -- NO row in ALERT_DELIVERIES for THAT route (NOT EXISTS on event+route), instead
    -- of keying on NOTIFIED_AT. The candidate route set is still the SEND eligibility
    -- (family + company + severity), so a flagged pair was genuinely eligible to that
    -- route but undelivered.
    -- (An OPEN event whose rule row was deleted from ALERT_CONFIG is undeliverable in
    -- BOTH the send and expired paths -- both INNER-join config -- so it is
    -- intentionally not flagged; it stays visibly OPEN in-app.)
    -- V064 #40 (log-inflation guard): a persistent backlog must NOT re-insert the
    -- same pair every sender run -- that would make a 30d failure KPI count RUNS, not
    -- events. Each (event,route) pair is logged at most once per 24h via NOT EXISTS
    -- on a prior 'undelivered_expired' row with the same event/route key (CONTEXT).
    -- The first signal is never lost; a still-stuck pair re-logs at most once a day.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
    SELECT 'NotifyWebhook', 'undelivered_expired',
           'open event ' || e.EVENT_ID || ' [' || e.SEVERITY ||
           '] aged past the 24h window undelivered to route ' || r2.ROUTE_ID ||
           ' (integration ' || r2.INTEGRATION_NAME || '); event remains OPEN in-app',
           'event ' || e.EVENT_ID || ' route ' || r2.ROUTE_ID,
           CURRENT_ROLE()
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r2
      ON r2.ENABLED
     AND (r2.FAMILY = 'ALL' OR c.FAMILY = r2.FAMILY)
     AND (COALESCE(r2.COMPANY_FILTER, 'ALL') = 'ALL'
          OR e.COMPANY = r2.COMPANY_FILTER
          OR UPPER(e.COMPANY) = 'ALL')
     AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
         >= CASE r2.MIN_SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END
    WHERE e.STATUS = 'OPEN'
      AND e.RAISED_AT < DATEADD('hour', -24, CURRENT_TIMESTAMP())
      AND e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r2.ROUTE_ID)
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG a
                      WHERE a.ERROR_TYPE = 'undelivered_expired'
                        AND a.CONTEXT = 'event ' || e.EVENT_ID || ' route ' || r2.ROUTE_ID
                        AND a.LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()));
    expired := SQLROWCOUNT;

    -- V064 #26: release the single-flight lease -- FENCED to this session. Without the
    -- HOLDER = CURRENT_SESSION() check a stalled run (>1h) whose lease was already
    -- reclaimed by a successor would, on finally finishing, clear the SUCCESSOR's lease
    -- and let a third run start concurrently -- defeating single-flight in exactly the
    -- crash/stall case the 1h window exists for. The fence makes release a no-op unless
    -- we still hold it. Best-effort otherwise: an unhandled failure above leaves
    -- HELD=TRUE, and the next run's acquire reclaims it after the 1h staleness window --
    -- the sender can never wedge itself out permanently.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
       SET HELD = FALSE, HOLDER = NULL
     WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
       AND HOLDER = CURRENT_SESSION();

    RETURN 'sent ' || :sent_total || ' event-route pair(s) across ' || :routes_hit ||
           ' route(s) in ' || :batches || ' batch(es); ' || :expired ||
           ' newly expired-undelivered (event,route) pair(s) flagged; ' || :escalated ||
           ' CRITICAL(s) escalated' || :esc_note;
EXCEPTION
    -- V064 #9 OUTER LEASE HANDLER. The lease is acquired at entry (HELD=TRUE) and
    -- released on the normal RETURN path above (fenced to CURRENT_SESSION). But any
    -- UNHANDLED error mid-proc -- a failed capture SELECT, the message LISTAGG, the
    -- NOTIFIED_AT stamp, the expired-tail INSERT -- would skip that release and leave
    -- HELD=TRUE, wedging ALL delivery until the 1h stale-reclaim. This handler performs
    -- the SAME fenced release (a no-op unless we still hold it, so it is safe even if the
    -- entry acquire itself was what failed), logs the error, and RE-RAISEs so the task
    -- run FAILs loudly rather than swallowing. The inner per-route send handler is
    -- unchanged: a route send failure is caught there (send_failed:=TRUE) and does NOT
    -- reach here, so one integration being down never trips the outer handler.
    WHEN OTHER THEN
        emsg := SQLERRM;
        UPDATE DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
           SET HELD = FALSE, HOLDER = NULL
         WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
           AND HOLDER = CURRENT_SESSION();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'NotifyWebhook', 'webhook_run_failed', :emsg,
               'unhandled error in SP_NOTIFY_WEBHOOK - fenced sender lease released, run re-raised', CURRENT_ROLE();
        RAISE;
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 164 AS VERSION,
       'Next-Fifty #40: actionable Teams lines and a one-time CRITICAL escalation. SP_NOTIFY_WEBHOOK re-derived from V064, byte-identical except: every line reads [SEV] title (140) | company | detail (one line, 100) | event id, identical in the 3000-char fit and the LISTAGG (max_batches stays 6); and an escalation pass inside the sender lease, after the drain and before the expired tail. A CRITICAL still OPEN with no ACK_AT, not in an incident a human acknowledged, mitigated or closed after the alert joined it (the machine auto-mitigate does not count), never snoozed (an audit SNOOZE row or a V117 carry-forward), raised in the 7-day CRITICAL window, whose rule exists, and first notified (NOTIFIED_AT, else RAISED_AT) ESCALATE_AFTER_MIN (120) minutes ago escalates once: re-posted to every enabled route that already delivered it, and emailed through ESCALATE_EMAIL_INTEGRATION (OVERWATCH_EMAIL; that integration DEFAULT_RECIPIENTS, no address stored). Capture-once, route-delivered events first, then oldest first; right after each channel send succeeds, the new ALERT_EVENTS.ESCALATED_AT stamp for the ids it took, then one ALERT_AUDIT ESCALATE row per event stamped; every channel failing retries next run. Isolated: escalation_failed is logged and never re-raised; a re-post failure logs route_send_failed, an email failure escalation_email_failed. The RETURN adds the escalated count. Seeds SETTINGS ESCALATE_AFTER_MIN 120 (0 = off) and ESCALATE_EMAIL_INTEGRATION OVERWATCH_EMAIL (blank = no email) WHEN NOT MATCHED only. No task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 164);

-- =====================================================================
--  MIGRATION 4 of 4 -- APPLY V165 (idempotent; GUARDS on V164). Source: snowflake/migrations/V165__daily_digest_grounding.sql
-- =====================================================================
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

-- =====================================================================
--  PART B -- VERIFY (read-only; the one commented CALL, V162.4, is optional). Paste every grid back; an
--  empty grid is an answer ('no rows'). Run the 'now' grids right after the apply; the rest when their
--  note says: after the next hourly chain (~:07 Central, then scan, then notify), the next morning
--  (daily scan ~07:00 CT, digest 07:20 CT), or the next day. In a new worksheet run the USE ROLE and
--  ALTER SESSION lines at the top of this file first.
-- =====================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- ---- V162 checks -----------------------------------------------------
-- (V162.1, now) the two rules are seeded.
SELECT RULE_ID, FAMILY, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
WHERE RULE_ID IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
ORDER BY RULE_ID;   -- 2 rows: SEC_ADMIN_GRANT SECURITY TRUE HIGH 0 24; SEC_LOGIN_TAKEOVER SECURITY TRUE HIGH 5 24

-- (V162.2, now) SP_ALERT_SCAN is V162's.
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SEC_LOGIN_TAKEOVER') AS ARM26,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SEC_ADMIN_GRANT') AS ARM27,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v13 (V162:') AS RETURN_V162,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), '/14 rule blocks ok') AS TALLY_14,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'LAST_BEND') AS BURST_CHAIN,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'HH24:MI:SS.FF3') AS MS_KEY_FORMAT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'IF (MOD(ct_hour, 4) = 1) THEN') AS V157_GATES_KEPT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SP_SCAN_ETL_CYCLE') AS V157_ADDON_KEPT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), '/12 rule blocks ok') AS OLD_TALLY_12;   -- all TRUE except OLD_TALLY_12 = FALSE

-- (V162.3, now) SP_INCIDENT_AUTODECLARE excludes the two rules and attaches them only to the same user's incident.
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), 'e.RULE_ID NOT IN (') AS CRIT_EXCLUSION,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), 'V162 (Next-Fifty #39') AS V162_COMMENT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), 'OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY') AS ATTACH_SAME_USER,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), 'incident_attach_failed') AS V154_ATTACH_KEPT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()'), 'incident_mitigate_failed') AS V154_MITIGATE_KEPT;   -- all TRUE

-- (V162.4, now, OPTIONAL) the same work the hourly TASK_INCIDENT_AUTODECLARE does -- it raises no alert and sends
-- no email (the V154 precedent). Expect 'auto-declared N incident(s); attached N later critical(s); ...'.
-- CALL DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE();

-- (V162.5, after the next :07 Central hourly graph, ~1h) the scan ran all 14 blocks; compare the new events with
-- PREFLIGHT P162.1 / P162.2 (IN_FIRST_RUN_WINDOW rows).
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, STATUS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY';   -- STATUS = alert scan 14/14 rule blocks ok; LAST_LOAD_TS within the hour
SELECT LOGGED_AT, CONTEXT, LEFT(ERROR_MESSAGE, 300) AS MSG
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
  AND (CONTEXT LIKE 'rule SEC_LOGIN_TAKEOVER%' OR CONTEXT LIKE 'rule SEC_ADMIN_GRANT%')
  AND LOGGED_AT >= DATEADD('hour', -3, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
ORDER BY LOGGED_AT DESC;   -- expect 0 rows
SELECT RULE_ID, SEVERITY, STATUS, COUNT(*) AS N, MIN(RAISED_AT) AS FIRST_RAISED, MAX(RAISED_AT) AS LAST_RAISED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
GROUP BY RULE_ID, SEVERITY, STATUS
ORDER BY RULE_ID, SEVERITY, STATUS;   -- the counts P162.1 / P162.2 flagged IN_FIRST_RUN_WINDOW
SELECT RULE_ID, SEVERITY, TITLE, DEDUPE_KEY, RAISED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
ORDER BY RAISED_AT DESC
LIMIT 50;   -- keys end in a YYYY-MM-DD HH24:MI:SS.FF3 timestamp

-- (V162.6, after 24h) neither rule auto-declared an incident.
SELECT i.INCIDENT_ID, i.DETECTED_AT, e.RULE_ID, e.SEVERITY, m.LINKED_BY
FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e ON e.EVENT_ID = m.REF_ID
WHERE m.MEMBER_KIND = 'ALERT'
  AND m.LINKED_BY = 'SP_INCIDENT_AUTODECLARE'
  AND i.DECLARED_BY = 'SP_INCIDENT_AUTODECLARE'
  AND e.RULE_ID IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT');   -- expect 0 rows

-- ---- V163 checks -----------------------------------------------------
-- PART B -- V163 verify (READ-ONLY). V163.1 + V163.2 right after the apply; V163.3 the next morning, after the
-- ~07:00 Central daily scan. Every RESULT should read OK (or the count named); paste the grids back.
SELECT 'V163.1 SCHEMA_VERSION has 163' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 163) = 1,
           'OK', 'FAIL: V163 did not finish') AS RESULT
UNION ALL
SELECT 'V163.1 COST_AI_USER_RUNAWAY seeded (enabled, HIGH, 2)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
             WHERE RULE_ID = 'COST_AI_USER_RUNAWAY' AND ENABLED AND SEVERITY = 'HIGH' AND THRESHOLD_NUM = 2) = 1,
           'OK', 'CHECK: missing, or edited since the seed')
UNION ALL
SELECT 'V163.1 SEC_TRUST_REGRESSION seeded (enabled, HIGH, 1)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
             WHERE RULE_ID = 'SEC_TRUST_REGRESSION' AND ENABLED AND SEVERITY = 'HIGH' AND THRESHOLD_NUM = 1) = 1,
           'OK', 'CHECK: missing, or edited since the seed')
UNION ALL
SELECT 'V163.1 SETTINGS AI_RUNAWAY_ROBUST_Z = 3.5, AI_RUNAWAY_INCLUDE_FUNCTIONS = FALSE',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
             WHERE (KEY = 'AI_RUNAWAY_ROBUST_Z' AND VALUE = '3.5')
                OR (KEY = 'AI_RUNAWAY_INCLUDE_FUNCTIONS' AND VALUE = 'FALSE')) = 2,
           'OK', 'CHECK: a row is missing or was set before the apply')
UNION ALL
SELECT 'V163.2 daily scan DDL has: /14 rule blocks ok (daily)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), '/14 rule blocks ok (daily)'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL has: COST_AI_USER_RUNAWAY', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'COST_AI_USER_RUNAWAY'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL has: SEC_TRUST_REGRESSION', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'SEC_TRUST_REGRESSION'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL has: and no successful login', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'and no successful login'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL has: AI_RUNAWAY_INCLUDE_FUNCTIONS', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'AI_RUNAWAY_INCLUDE_FUNCTIONS'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL has: alert scan daily v5 (V163:', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'alert scan daily v5 (V163:'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL has: SP_SCAN_SLEEP_POLLING(FALSE)', IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'SP_SCAN_SLEEP_POLLING(FALSE)'), 'OK', 'FAIL: not the V163 body')
UNION ALL
SELECT 'V163.2 daily scan DDL lacks: /12 rule blocks ok (daily)', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), '/12 rule blocks ok (daily)'), 'OK', 'FAIL: still the V160 text')
UNION ALL
SELECT 'V163.2 daily scan DDL lacks: credential stuffing', IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'credential stuffing'), 'OK', 'FAIL: still the V160 text');

-- V163.3 the next morning, after the ~07:00 Central daily scan. Compare the event counts with PREFLIGHT P163.1
--        (FIRST_RUN_RAISES) and P163.3 (WOULD_RAISE and IN_FIRST_RUN).
SELECT 'V163.3 daily scan heartbeat reads 14/14' AS CHECK_NAME,
       COALESCE((SELECT IFF(MAX(STATUS) = 'alert scan daily 14/14 rule blocks ok (daily)', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY'),
                'FAIL: no ALERT_SCAN_DAILY row') AS RESULT
UNION ALL
SELECT 'V163.3 no rule_block_failed for the two rules (24h)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE ERROR_TYPE = 'rule_block_failed'
               AND (CONTEXT LIKE 'rule COST_AI_USER_RUNAWAY %' OR CONTEXT LIKE 'rule SEC_TRUST_REGRESSION %')
               AND LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE for the rule')
UNION ALL
SELECT 'V163.3 COST_AI_USER_RUNAWAY events raised',
       (SELECT COUNT(*)::VARCHAR || ' event(s)' FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS WHERE RULE_ID = 'COST_AI_USER_RUNAWAY')
UNION ALL
SELECT 'V163.3 SEC_TRUST_REGRESSION events raised',
       (SELECT COUNT(*)::VARCHAR || ' event(s)' FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS WHERE RULE_ID = 'SEC_TRUST_REGRESSION');

-- ---- V164 checks -----------------------------------------------------
-- PART B -- V164 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.
-- V164.1 / V164.2 now; V164.3 after the next hourly chain (TASK_LOAD_HOURLY :07 Central, then scan, then notify);
-- V164.4 by eye on the next Teams card.
SELECT 'V164.1a ALERT_EVENTS.ESCALATED_AT exists' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ALERT_EVENTS'
               AND COLUMN_NAME = 'ESCALATED_AT') = 1,
           'OK', 'FAIL: the column is missing -- V164 did not finish') AS RESULT
UNION ALL
SELECT 'V164.1b ESCALATE_AFTER_MIN seeded (value)',
       COALESCE((SELECT MAX(VALUE) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'ESCALATE_AFTER_MIN'), 'FAIL: no row')
UNION ALL
SELECT 'V164.1c ESCALATE_EMAIL_INTEGRATION seeded (value; blank = no email leg)',
       COALESCE((SELECT '[' || MAX(VALUE) || ']' FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'ESCALATE_EMAIL_INTEGRATION'),
                'FAIL: no row')
UNION ALL
SELECT 'V164.2 SP_NOTIFY_WEBHOOK is the V164 definition',
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), ' | event ')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'ESCALATED_AT')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'escalation_failed')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'escalation_email_failed')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'CRITICAL(s) escalated')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'INTEGRATION(TRIM(:esc_email))')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'ALERT_AUDIT a')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'max_batches INT DEFAULT 6')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'i.MITIGATED_BY')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'SNOOZE_SUPPRESSED')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'f.EMAIL_ONLY')
           AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'SET ESCALATED_AT = :esc_now')
           AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()'), 'LEFT(e.TITLE, 140),'),
           'OK', 'FAIL: the stored proc is not V164 -- re-run the V164 CREATE')
UNION ALL
SELECT 'V164.1d SCHEMA_VERSION has 164',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 164) = 1, 'OK', 'FAIL: V164 did not finish');

-- V164.3 (after the next hourly chain) the notifier ran the pass. expect: STATE SUCCEEDED and no ERROR_TEXT.
--        RETURN_VALUE stays NULL: a task whose body CALLs a proc does not publish the proc's return string (the
--        V160.6 finding), so the '... CRITICAL(s) escalated' tally shows only on a hand CALL -- never run one, it
--        can page and email. The two grids below are the evidence.
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, RETURN_VALUE, LEFT(ERROR_MESSAGE, 200) AS ERROR_TEXT
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
       SCHEDULED_TIME_RANGE_START => DATEADD('hour', -3, CURRENT_TIMESTAMP()),
       TASK_NAME => 'TASK_ALERT_NOTIFY'))
ORDER BY SCHEDULED_TIME DESC;
--        expect: no escalation_failed and no escalation_email_failed row (route_send_failed = a route refusing).
SELECT ERROR_TYPE, COUNT(*) AS N, MAX(LOGGED_AT) AS LAST_AT, ANY_VALUE(LEFT(ERROR_MESSAGE, 200)) AS SAMPLE_MSG
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'NotifyWebhook' AND LOGGED_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP())
GROUP BY ERROR_TYPE
ORDER BY N DESC;
--        expect: ESCALATE_AUDIT_ROWS = ESCALATED_EVENTS, and the events are PREFLIGHT P164.2's WOULD_SEND_FIRST_RUN
--        rows (fewer if someone acknowledged in between).
SELECT (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT WHERE ACTION = 'ESCALATE') AS ESCALATE_AUDIT_ROWS,
       (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS WHERE ESCALATED_AT IS NOT NULL) AS ESCALATED_EVENTS,
       (SELECT MAX(ESCALATED_AT) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS) AS LAST_ESCALATED_AT;

-- V164.3c (after the first escalation) did the escalation email go out? The send only ENQUEUES: a delivery
--        failure after the enqueue still stamps the event and its audit note says 'email sent', so read
--        Snowflake's own delivery log for the escalation integration (read from SETTINGS, as the proc does)
--        from 2 minutes before the latest escalation. expect: STATUS SUCCESS. No row after an escalation =
--        the email leg is off (ESCALATE_EMAIL_INTEGRATION '') or the send failed before the enqueue (V164.3's
--        escalation_email_failed row). A native e-mail alert sent in the same minutes also shows here.
SELECT CONVERT_TIMEZONE('America/Chicago', CREATED)::TIMESTAMP_NTZ AS CREATED_CT, INTEGRATION_NAME, STATUS,
       LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
           START_TIME => DATEADD('day', -1, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))
 WHERE UPPER(INTEGRATION_NAME) = (SELECT UPPER(TRIM(COALESCE(MAX(VALUE), 'OVERWATCH_EMAIL'))) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
                                   WHERE KEY = 'ESCALATE_EMAIL_INTEGRATION')
   AND CONVERT_TIMEZONE('America/Chicago', CREATED)::TIMESTAMP_NTZ
       >= DATEADD('minute', -2, (SELECT MAX(ESCALATED_AT) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS))
 ORDER BY CREATED DESC;

-- V164.4 (manual) the next Teams card shows lines as '[SEV] <title> | <company> | <detail> | event <id>'. A full
--        end-to-end check: leave the next monthly OPS_ALERT_DRILL CRITICAL unacknowledged for 2 hours (only when
--        P164.2 was empty) -- expect one 'OVERWATCH ESCALATION' Teams post and one email to DEFAULT_RECIPIENTS.

-- ---- V165 checks -----------------------------------------------------
-- PART B -- V165 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.
-- V165.1 / V165.2 now.
SELECT 'V165.1 DAILY_DIGEST has the 6 grounding columns' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'DAILY_DIGEST'
               AND COLUMN_NAME IN ('FACTS', 'GROUNDING_OK', 'FIGURES_CHECKED', 'UNGROUNDED', 'BODY_SOURCE', 'AI_BODY')) = 6,
           'OK', 'FAIL: a V165 column is missing') AS RESULT
UNION ALL
SELECT 'V165.2 SP_DAILY_DIGEST is the V165 proc',
       IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'Templated digest (not AI-written)') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'GROUNDING_OK') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'digest_ai_failed') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'TEXT_PLAIN(:msg)') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'REGEXP_SUBSTR_ALL') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'SPEND_USD=') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'RTRIM(LEFT(:msg, 3000), CHR(92))') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'UPPER(COALESCE(r.MIN_SEVERITY, ') AND CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'digest written (')
           AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'Digest unavailable') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'open_critical=') AND NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()'), 'digest written; sent'),
           'OK', 'FAIL: SP_DAILY_DIGEST is not the V165 text');

-- V165.3 the next morning, after the 07:20 CT run. expect BODY_SOURCE AI (GROUNDING_OK TRUE, FIGURES_CHECKED >= 0) or
--        TEMPLATE (GROUNDING_OK FALSE with UNGROUNDED listed, or NULL when Cortex failed), and FACTS filled in. Then
--        confirm in Teams that the morning card arrived.
SELECT DIGEST_DATE, MODEL, BODY_SOURCE, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, LEFT(FACTS, 300) AS FACTS_START,
       LEFT(BODY, 200) AS BODY_START, CREATED_AT
  FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
 ORDER BY DIGEST_DATE DESC
 LIMIT 1;

-- V165.3b expect STATE SUCCEEDED (ACCOUNT_USAGE, up to ~45 min behind). RETURN_VALUE stays NULL (a task that CALLs a
--         proc does not publish the proc's 'digest written (...)' string); V165.3 above is the evidence.
SELECT SCHEDULED_TIME, STATE, RETURN_VALUE, ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH' AND NAME = 'TASK_DAILY_DIGEST'
   AND SCHEDULED_TIME >= DATEADD('day', -2, CURRENT_TIMESTAMP())
 ORDER BY SCHEDULED_TIME DESC;

-- V165.3c expect SUCCESS for each digest route around 07:20 CT today; the card should now render in Teams.
SELECT CONVERT_TIMEZONE('America/Chicago', CREATED)::TIMESTAMP_NTZ AS CREATED_CT, INTEGRATION_NAME, STATUS,
       LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.NOTIFICATION_HISTORY(
           START_TIME => DATEADD('day', -1, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))
 WHERE INTEGRATION_NAME IN (SELECT r.INTEGRATION_NAME FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
                             WHERE r.ENABLED AND r.DELIVER_DIGEST AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL')
   AND HOUR(CONVERT_TIMEZONE('America/Chicago', CREATED)) = 7
 ORDER BY CREATED DESC;

-- ---- all four -----------------------------------------------------------------
-- (all, now) V162 through V165 are registered: expect 4 rows.
SELECT VERSION, APPLIED_AT, APPLIED_BY
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION BETWEEN 162 AND 165
ORDER BY VERSION;

-- (all, the next day: FACT_SECURITY_CHANGE loads from the hourly extract) the apply session's change-risk footprint,
-- anchored on the SCHEMA_VERSION rows (Central wall clock, as this file's session stamped them), so it works any day.
-- expect: no DESTRUCTIVE row and no HIGH / CRITICAL row; only LOW / MEDIUM CREATE and ALTER rows (the procedures, the
-- ADD COLUMNs, the ALTER SESSION lines), or none. Never delete these rows.
SELECT f.QUERY_TYPE, f.CHANGE_KIND, f.RISK_LEVEL, COUNT(*) AS ROWS_IN_APPLY_WINDOW,
       MIN(LEFT(f.QUERY_PREVIEW, 90)) AS EXAMPLE
FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE f
CROSS JOIN (SELECT MIN(APPLIED_AT) AS FIRST_AT, MAX(APPLIED_AT) AS LAST_AT, MAX(APPLIED_BY) AS BY_USER
            FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
            WHERE VERSION BETWEEN 162 AND 165) a
WHERE f.USER_NAME = a.BY_USER
  AND CONVERT_TIMEZONE('America/Chicago', f.EVENT_TS)::TIMESTAMP_NTZ
      BETWEEN DATEADD('minute', -30, a.FIRST_AT) AND DATEADD('minute', 30, a.LAST_AT)
GROUP BY f.QUERY_TYPE, f.CHANGE_KIND, f.RISK_LEVEL
ORDER BY f.RISK_LEVEL, f.CHANGE_KIND, f.QUERY_TYPE;

ALTER SESSION UNSET TIMEZONE;
