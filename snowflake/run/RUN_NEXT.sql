-- >>> 2026-10-02 08:30: DO NOT RE-RUN THIS FILE. Production shows V168 is applied (its SEC_NEW_ADMIN_NETWORK arm
-- >>> fails with "Unsupported subquery type"), so this package already ran. Re-running it would put OLDER proc
-- >>> versions back. Run snowflake/run/STATUS_2026-10-02.sql (read-only) and paste the grids back; the forward
-- >>> fix (V173) will be staged as its own file.
-- >>> FIRST (2026-10-02): run snowflake/run/EMAIL_FIX_2026-10-02.sql. It sets JDees@alfains.com as the
-- >>> default recipient of OVERWATCH_EMAIL so V164's escalation email stops failing. It is independent of
-- >>> everything below and safe to run now; this V162 -> V172 package still waits for app 4.609.0.
-- #####################################################################
--  OVERWATCH -- RUN_NEXT.sql  (app 4.609.0 package): APPLY V162 -> V172 IN ORDER, IN TWO STAGES, ONE SITTING.
--  ELEVEN pending migrations. This note overrides the stage-1 header below wherever the two differ.
--
--  1. DEPLOY APP 4.609.0 FIRST: snow streamlit deploy --replace. (The stage-1 header says 4.602.0; 4.609.0
--     carries every app change since; each app read, caption or CALL that needs a new column, proc or text is
--     gated on its own migration (V162-V165 as before; V166, V167, V169, V170, V171 and V172 in this wave; V168
--     needs none), so the deployed app keeps the pre-apply behaviour until each one lands -- e.g. it CALLs the
--     new 5-arg SP_INCIDENT_DECLARE only once V170 is registered, re-reading SCHEMA_VERSION every 30 s until then.)
--  2. STOP AT THE FIRST ERROR. The guard chain is V162 -> V163 -> ... -> V172: each migration opens with an
--     EXECUTE IMMEDIATE guard that raises (-20162 .. -20172) unless the migration before it is registered, so
--     V166 and everything after it REFUSE TO RUN until V165 is applied. A guard protects only if you stop
--     there: fix the cause, then re-run from that migration's banner (every migration is idempotent).
--  3. The escalation email choice in the stage-1 header (step 2, before the V164 section) still applies.
--     Type an address only in Snowsight, never in this file.
--  4. Read-only previews first, and paste the grids back: snowflake/run/PREFLIGHT_WAVE4.sql (V162-V165) and
--     snowflake/run/PREFLIGHT_V166_V172.sql (V166-V172).
--  5. TIMING: apply OUTSIDE 06:30-07:30 Central (the nightly chain: V166 needs 06:30-07:15 clear, V167
--     06:40-07:30), and leave time to run OWNER_REPAIRS_V166_V172.sql PART 1 right after this file -- its V167
--     AI re-key must run before the next 06:45 Central DAILY marts run (until then the ~3 days around the
--     apply double-count some evening Cortex Code usage).
--
--  LAYOUT
--   STAGE 1  the wave-4 file staged at runbox b57d206e, BYTE-IDENTICAL: its header, V162, V163, V164, V165,
--            its PART B and its closing ALTER SESSION UNSET. Its banners still read 'MIGRATION n of 4'.
--   STAGE 2  V166 -> V172, banners 'STAGE 2 -- MIGRATION n of 7'. It re-pins the role, the schema and the
--            Central session first (stage 1 ends with UNSET), then each migration byte-identical to
--            snowflake/migrations/ in app 4.609.0, then a read-only registry check (expect 11 rows).
--   The file is large (~730 KB). If the worksheet struggles, run stage 1 first, then paste from the STAGE 2
--   banner down into a new worksheet: stage 2 re-pins its own role, schema and session.
--   Stage 1's PART B 'now' grids sit between V165 and V166: run them there. After stage 2 replaces SP_ALERT_SCAN
--   (V168) and SP_ALERT_SCAN_DAILY (V169), two of those grids read FALSE / FAIL BY DESIGN if run again later:
--   V162.2 RETURN_V162 and V163.2 'daily scan DDL has: alert scan daily v5 (V163:' (the RETURN text is now
--   V168's / V169's; the arms, tallies and heartbeats those grids check are kept). Stage 1's next-day
--   change-risk grid (+/- 30 minutes around V162-V165) also takes in stage 2's statements and, when
--   OWNER_REPAIRS PART 1 starts within 30 minutes of V165's stamp, PART 1.1's loader CALL: expect no DESTRUCTIVE
--   and no HIGH / CRITICAL row EXCEPT one MEDIUM DESTRUCTIVE CREATE_TABLE_AS_SELECT (or CREATE_TABLE) row whose
--   EXAMPLE reads 'CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR' (or _OW_ALLOC_BASE, from PART 2's V167
--   step 3): SP_LOAD_MARTS_V27's session scratch table, which the security loader scores like any CREATE OR
--   REPLACE ... TABLE -- not a change (PART_B_V166_V172.sql ALL.2 labels it). If a stage-1 PART B grid errors
--   (read-only), note it and continue from the STAGE 2 banner.
--
--  STAGE 2 AT A GLANCE (no DROP, no CREATE OR REPLACE TABLE, NOTHING is CALLed, nothing pages or emails at apply):
--   V166  4 loader procs + in-migration repair: one bounded MERGE of FACT_STORAGE_DAILY's multi-ID name-days.
--   V167  + SOURCE_FRESHNESS_STATE.COVERAGE_FROM, 3 mart procs + in-migration repair: a scan-free DELETE of
--         stale-company MART_PATTERN_COST_DAILY twins (preview: PREFLIGHT P167.1).
--   V168  SP_ALERT_SCAN + two guarded ALERT_CONFIG NAME refreshes.   V169  SP_ALERT_SCAN_DAILY + one.
--   V170  SP_INCIDENT_DECLARE: the 4-arg overload re-derived + a NEW 5-arg overload; INCIDENT_PROPOSALS view.
--   V171  SETTINGS CREDIT_PRICE_OVERRIDE seed (FALSE, WHEN NOT MATCHED) + SP_CANARY_SENTINEL, SP_DAILY_DIGEST,
--         SP_SCAN_REF_GAPS.
--   V172  5 detection procs + in-migration repairs: registry / live-alert COMPANY re-stamps, the TASK baseline
--         re-freeze and, last, the first-apply null of suffix-collided PROCEDURE baselines. DELIVERY (its
--         header): alerts on a database with no COMPANY_SCOPE row become company UNKNOWN and stop posting to an
--         ALFA-only route; map the database or add an ALL / UNKNOWN route to keep them posting.
--  AFTER THIS FILE (snowflake/run/README_V166_V172.md has the full order):
--   a. OWNER_REPAIRS_V166_V172.sql PART 1 at once (V167 AI re-key; R172.0 recommended).
--   b. PART_B_V166_V172.sql 'now' grids; the rest at the times each grid names.
--   c. OWNER_REPAIRS_V166_V172.sql PART 2 off-peak, one block at a time.
--  ROLLBACK (RUNBOOK section 12): each stage-2 migration header names the base CREATE(s) to re-run; V172's
--  also nulls the tracking TASK / PROCEDURE baselines right after V140's CREATE, before the next change-impact
--  scan. To undo both waves go from V172 down to V162 in reverse apply order: V168, V169 and V171 re-derive
--  procs whose bases are V162, V163 and V165, so a wave-4 rollback ("Rolling back wave 4") comes after them.
-- #####################################################################

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

-- #####################################################################
--  STAGE 2 -- APPLY V166, V167, V168, V169, V170, V171, V172 (in order). SEVEN pending migrations (app 4.609.0,
--  the round-2 bug-hunt fixes). Same rules as stage 1: apply top-to-bottom as SNOW_ACCOUNTADMINS and STOP ON THE
--  FIRST ERROR; every migration is idempotent; there is NO apply-time CALL. The guard chain continues
--  V165 -> V166 -> ... -> V172, so V166 refuses to run until V165 is registered.
--  Stage 1 ended with ALTER SESSION UNSET TIMEZONE, so the three lines below re-pin the session. The Central pin
--  matters: SCHEMA_VERSION.APPLIED_AT is stamped in this session's zone and PART B V172.4 compares it with stamps
--  the Central task session writes; the V166 / V172 in-migration repairs key days on CURRENT_DATE().
--  EXPECT on the Security page the next day: no DROP and no CREATE OR REPLACE TABLE here, so no DESTRUCTIVE /
--  HIGH / CRITICAL CHANGE RISK row from the apply; at most LOW / MEDIUM CREATE and ALTER rows (the 19 CREATE OR
--  REPLACE PROCEDUREs, the V170 view, V167's ADD COLUMN, the two ALTER SESSION lines). OWNER_REPAIRS PART 1.1,
--  run right after, adds one MEDIUM DESTRUCTIVE row: its loader's CREATE OR REPLACE TEMPORARY TABLE
--  _OW_WH_MONITOR, a session scratch table, not a change. PART_B_V166_V172.sql ALL.2 reads it all and labels that row.
-- #####################################################################

USE ROLE SNOW_ACCOUNTADMINS;
USE SCHEMA DBA_MAINT_DB.OVERWATCH;   -- the security loader records each statement's current database (a *PROD* one scores higher)
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- the task clock: every gate and stamp is Central

-- =====================================================================
--  STAGE 2 -- MIGRATION 1 of 7 -- APPLY V166 (idempotent; GUARDS on V165; in-migration repair: FACT_STORAGE_DAILY MERGE). Source: snowflake/migrations/V166__fact_loader_window_integrity.sql
-- =====================================================================

-- V166__fact_loader_window_integrity.sql
--
-- WHY: four fact loaders could lose or understate history that no later run ever reloaded.
--   R2-007  The hourly security change reload (d <= 3) deleted from the first row of OW_QH_EXTRACT but
--           re-read only from midnight(today - d). When the extract reached further back -- a swallowed
--           extract failure across midnight keeps its old untrimmed fill, backfill_365 widens it to 90
--           days, a manual DAYS_BACK of 0/1/2 -- the rows in between were deleted, never re-read, and
--           then trimmed out of the 72h extract for good: holes in the CHANGE RISK queue, the destructive
--           breakdown and Who changed what, below the day-grain coverage check of the app.
--   R2-009  FACT_STORAGE_DAILY averaged DATABASE_STORAGE_USAGE_HISTORY per name-day. The view has one row
--           per DATABASE_ID, and a re-created or clone-refreshed database keeps its dropped IDs (Time Travel
--           and fail-safe bytes) under the same name, so the fact divided the billed bytes by the row count.
--           The live twin (storage_by_database_calendar_live) already SUMs, so mart and live disagreed.
--   R2-011  SP_LOAD_APP_COST and SP_LOAD_STORAGE_TRUTH ran DELETE then INSERT under autocommit. A failed
--           INSERT left the trailing window deleted; the next run starts a day later, so the oldest
--           deleted day was never reloaded (the v4.371.0 deferral assumed it self-healed; it does not).
--   C10     SP_LOAD_APP_COST resolved sessions only 7 days before its reload start. The task reloads each
--           day four times and the LAST reload is the narrowest, so a query in a keep-alive, pooled or
--           service session opened 7-10 days earlier was relabelled (unknown) for good.
--
--   ~ SP_LOAD_SECURITY_FACTS re-derived from V105, byte-identical except: the d <= 3 DELETE and INSERT
--     share ONE bound, lo_ts = GREATEST(MIN(extract START_TIME), midnight(today - d)). GREATEST propagates
--     NULL, so an empty extract stays a no-op. The d > 3 arm and the DAYS_BACK clamp are unchanged.
--   ~ SP_LOAD_DAILY_FACTS re-derived from V101, byte-identical except: the FACT_STORAGE_DAILY arm SUMs
--     AVERAGE_DATABASE_BYTES / AVERAGE_FAILSAFE_BYTES per name-day. The V101 task_attempts CTE stays.
--   ~ SP_LOAD_APP_COST re-derived from V077, byte-identical except: the DELETE + INSERT run in ONE
--     transaction (ROLLBACK, an APP_ERROR_LOG fact_load_failed row with PAGE AppCost, re-RAISE so the task
--     still reads FAILED), and the SESSIONS lookback is 30 days before the reload start, not 7. The
--     (unknown) label stays (SP_SCAN_SLEEP_POLLING and cloud_svc_billed_families filter on it).
--   ~ SP_LOAD_STORAGE_TRUTH re-derived from V046, byte-identical except the same one-transaction wrap
--     (PAGE StorageTruth). In both, the SOURCE_FRESHNESS_STATE MERGE stays after the COMMIT.
--   + one bounded repair: FACT_STORAGE_DAILY name-days that the view still holds as 2+ DATABASE_ID rows
--     (365-day retention) get the SUM. Older rows cannot be recomputed and stay understated.
--
-- COST: the daily TASK_LOAD_APP_COST CALL(3) now scans 33 days of SESSIONS instead of 10 (small next to
-- its QUERY_HISTORY and QUERY_ATTRIBUTION_HISTORY scans; watch its first scheduled run in TASK_HISTORY). The
-- other loaders scan what they scanned before. The repair reads about 365 days of
-- DATABASE_STORAGE_USAGE_HISTORY once (a few thousand rows) and updates only the multi-ID name-days.
-- LATENCY: unchanged; the procs swap under the running graph (no task change, no suspend).
-- FIRST RUN: the next hourly SP_LOAD_SECURITY_FACTS(3), then the daily runs: storage truth 06:30 CT,
-- SP_LOAD_DAILY_FACTS() 06:45 CT (TASK_LOAD_DAILY, then the nightly reconcile), app cost 06:55 CT. Apply
-- outside 06:30-07:15 CT so no run straddles the swap. Nothing runs at apply time except the storage repair. Owner-run
-- heals, in a Central session after V166 is applied (OWNER_REPAIRS): SP_LOAD_SECURITY_FACTS(180) with the hourly
-- graph suspended around it, the R2-011 gap grids, then SP_LOAD_STORAGE_TRUTH(N) only when they show holes and ONE
-- off-peak SP_LOAD_APP_COST of at least 30 days (relabels sessions, fills any R2-011 hole, atomic now).
-- ROLLBACK: re-run the base CREATE PROCEDURE of each proc (V105, V101, V077, V046). The repaired storage
-- rows can stay: they are what Snowflake bills, and the live twin already shows them.
-- Apply AFTER V165. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20166, 'V166 requires V165 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 165) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_SECURITY_FACTS  (from V105; d<=3 change reload DELETE + INSERT share one bound GREATEST(MIN(extract.START_TIME), midnight(today-d)), V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    d INT;
    emsg VARCHAR;
    trust_ok BOOLEAN DEFAULT TRUE;
    lo_ts TIMESTAMP_LTZ;        -- V166 (R2-007): the d<=3 change reload lower bound, DELETE and INSERT
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 3), 180))::INT;

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
     WHERE DAY < DATEADD('day', -180, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
     WHERE DAY < DATEADD('day', -180, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT
     WHERE DAY < DATEADD('day', -400, CURRENT_DATE());

    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
     WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
        (DAY, USER_NAME, COMPANY, CLIENT_IP, AUTH_FACTOR, ERROR_CATEGORY,
         LOGINS, SUCCESSES, FAILURES, FIRST_SEEN, LAST_SEEN)
    SELECT DATE(EVENT_TIMESTAMP),
           COALESCE(USER_NAME, 'UNKNOWN'),
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(COALESCE(USER_NAME, 'UNKNOWN')),
           COALESCE(CLIENT_IP, '(none)'),
           COALESCE(FIRST_AUTHENTICATION_FACTOR, 'UNKNOWN'),
           CASE
             WHEN IS_SUCCESS = 'YES' THEN 'SUCCESS'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%network%' THEN 'NETWORK POLICY'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%disabled%' THEN 'DISABLED USER'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%mfa%' THEN 'MFA'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE ANY ('%password%', '%credential%', '%authentication%')
               THEN 'CREDENTIAL'
             ELSE 'OTHER'
           END,
           COUNT(*),
           COUNT_IF(IS_SUCCESS = 'YES'),
           COUNT_IF(IS_SUCCESS = 'NO'),
           MIN(EVENT_TIMESTAMP),
           MAX(EVENT_TIMESTAMP)
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= DATEADD('day', -:d, CURRENT_DATE())
    GROUP BY 1, 2, 3, 4, 5, 6;

    IF (d <= 3) THEN
        -- V100: the d<=3 change reload reads OW_QH_EXTRACT, which retains only a rolling
        -- ~72h (SP_LOAD_QH_EXTRACT purges START_TIME < now-72h). Deleting the full calendar
        -- window (DAY >= -:d days = up to 72h + hour-of-day) while the extract can refill
        -- only the last ~72h silently dropped the earliest hours of the oldest day, for good.
        -- Delete ONLY the window the extract actually covers, so older already-loaded rows
        -- are preserved instead of erased-and-not-refilled. Empty extract -> MIN() NULL ->
        -- the delete matches nothing (safe no-op).
        -- V166 (R2-007): the DELETE and the INSERT share ONE lower bound, the later of the extract
        -- first row and midnight(today - d). V100 deleted from MIN(START_TIME) alone while the INSERT
        -- re-read only START_TIME >= today - d, so whenever the extract reached further back (a
        -- swallowed extract failure across midnight keeps the old untrimmed fill; a backfill-wide
        -- extract; a manual d of 1 or 2, DAYS_BACK 0 maps to 1) the rows in between were deleted,
        -- never re-read, then trimmed out of the extract for good. GREATEST() propagates NULL: an
        -- empty extract leaves lo_ts NULL and both statements match nothing (the V100 no-op is kept).
        lo_ts := (SELECT GREATEST(MIN(START_TIME), DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_LTZ)
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
         WHERE EVENT_TS >= :lo_ts;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
            (QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
             DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
             RISK_LEVEL, QUERY_PREVIEW)
        WITH raw AS (
            SELECT QUERY_ID, DATE(START_TIME) AS DAY, START_TIME AS EVENT_TS,
                   USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
                   CASE
                     WHEN DATABASE_NAME IS NOT NULL
                      AND DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) <> 'UNKNOWN'
                       THEN DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)
                     ELSE DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME)
                   END AS COMPANY,
                   CASE
                     WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE'
                     WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 'PRIVILEGE'
                     WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 'SECURITY POLICY'
                     WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER'
                     WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                          AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'
                     ELSE 'CREATE'
                   END AS CHANGE_KIND,
                   LEAST(100,
                     CASE
                       WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90
                       WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 80
                       WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 85
                       WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55
                       WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                            AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55
                       ELSE 30
                     END
                     + IFF(ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS'), 10, 0)
                     + IFF(COALESCE(DATABASE_NAME, '') ILIKE '%PROD%', 10, 0)
                   ) AS RISK_SCORE,
                   LEFT(QUERY_TEXT, 200) AS QUERY_PREVIEW
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= :lo_ts
              AND EXECUTION_STATUS = 'SUCCESS'
              AND (QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW',
                   'CREATE_TABLE_AS_SELECT', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN',
                   'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'RENAME',
                   'RENAME_TABLE', 'TRUNCATE_TABLE', 'ALTER_USER', 'CREATE_USER',
                   'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE')
                   OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%'
                   OR QUERY_TYPE ILIKE '%ROLE%' OR QUERY_TYPE ILIKE 'DROP%'
                   OR QUERY_TYPE ILIKE 'TRUNCATE%')
        )
        SELECT QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
               DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
               CASE WHEN RISK_SCORE >= 90 THEN 'CRITICAL'
                    WHEN RISK_SCORE >= 70 THEN 'HIGH'
                    WHEN RISK_SCORE >= 45 THEN 'MEDIUM' ELSE 'LOW' END,
               QUERY_PREVIEW
        FROM raw;
    ELSE
        -- Full backfill / manual path (d>3): reads ACCOUNT_USAGE.QUERY_HISTORY directly
        -- (full history), so delete the whole calendar window and rebuild it.
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
         WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());
        INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
            (QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
             DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
             RISK_LEVEL, QUERY_PREVIEW)
        WITH raw AS (
            SELECT QUERY_ID, DATE(START_TIME) AS DAY, START_TIME AS EVENT_TS,
                   USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
                   CASE
                     WHEN DATABASE_NAME IS NOT NULL
                      AND DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) <> 'UNKNOWN'
                       THEN DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)
                     ELSE DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME)
                   END AS COMPANY,
                   CASE
                     WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE'
                     WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 'PRIVILEGE'
                     WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 'SECURITY POLICY'
                     WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER'
                     WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                          AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'
                     ELSE 'CREATE'
                   END AS CHANGE_KIND,
                   LEAST(100,
                     CASE
                       WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90
                       WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 80
                       WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 85
                       WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55
                       WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                            AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55
                       ELSE 30
                     END
                     + IFF(ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS'), 10, 0)
                     + IFF(COALESCE(DATABASE_NAME, '') ILIKE '%PROD%', 10, 0)
                   ) AS RISK_SCORE,
                   LEFT(QUERY_TEXT, 200) AS QUERY_PREVIEW
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND (QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW',
                   'CREATE_TABLE_AS_SELECT', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN',
                   'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'RENAME',
                   'RENAME_TABLE', 'TRUNCATE_TABLE', 'ALTER_USER', 'CREATE_USER',
                   'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE')
                   OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%'
                   OR QUERY_TYPE ILIKE '%ROLE%' OR QUERY_TYPE ILIKE 'DROP%'
                   OR QUERY_TYPE ILIKE 'TRUNCATE%')
        )
        SELECT QUERY_ID, DAY, EVENT_TS, USER_NAME, ROLE_NAME, QUERY_TYPE,
               DATABASE_NAME, SCHEMA_NAME, COMPANY, CHANGE_KIND, RISK_SCORE,
               CASE WHEN RISK_SCORE >= 90 THEN 'CRITICAL'
                    WHEN RISK_SCORE >= 70 THEN 'HIGH'
                    WHEN RISK_SCORE >= 45 THEN 'MEDIUM' ELSE 'LOW' END,
               QUERY_PREVIEW
        FROM raw;
    END IF;

    BEGIN
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT t
        USING (
            WITH current_findings AS (
                SELECT CURRENT_DATE() AS DAY, SCANNER_ID::VARCHAR AS SCANNER_ID,
                       SCANNER_NAME, UPPER(SEVERITY) AS SEVERITY,
                       TOTAL_AT_RISK_COUNT, CREATED_ON AS SCANNED_AT
                FROM SNOWFLAKE.TRUST_CENTER.FINDINGS
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY SCANNER_ID ORDER BY CREATED_ON DESC
                ) = 1
            ), prior AS (
                SELECT SCANNER_ID, SCANNER_NAME, SEVERITY
                FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY SCANNER_ID ORDER BY DAY DESC, LOAD_TS DESC
                ) = 1
            )
            SELECT DAY, SCANNER_ID, SCANNER_NAME, SEVERITY,
                   TOTAL_AT_RISK_COUNT, SCANNED_AT
            FROM current_findings
            UNION ALL
            SELECT CURRENT_DATE(), p.SCANNER_ID, p.SCANNER_NAME, p.SEVERITY,
                   0, CURRENT_TIMESTAMP()
            FROM prior p
            WHERE NOT EXISTS (
                SELECT 1 FROM current_findings c WHERE c.SCANNER_ID = p.SCANNER_ID
            )
        ) s
        ON t.DAY = s.DAY AND t.SCANNER_ID = s.SCANNER_ID
        WHEN MATCHED THEN UPDATE SET
            SCANNER_NAME = s.SCANNER_NAME, SEVERITY = s.SEVERITY,
            TOTAL_AT_RISK_COUNT = s.TOTAL_AT_RISK_COUNT,
            SCANNED_AT = s.SCANNED_AT, LOAD_TS = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (DAY, SCANNER_ID, SCANNER_NAME, SEVERITY, TOTAL_AT_RISK_COUNT, SCANNED_AT)
        VALUES
            (s.DAY, s.SCANNER_ID, s.SCANNER_NAME, s.SEVERITY,
             s.TOTAL_AT_RISK_COUNT, s.SCANNED_AT);
    EXCEPTION
        WHEN OTHER THEN
            trust_ok := FALSE;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'SecurityLoader', 'trust_snapshot_unavailable', :emsg,
                   'Login/change facts loaded; grant TRUST_CENTER_VIEWER for snapshots',
                   CURRENT_ROLE();
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_SECURITY_LOGIN_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT, 'OK' AS LOAD_STATUS
          FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_LOGIN_DAILY
        UNION ALL
        SELECT 'FACT_SECURITY_CHANGE', MAX(LOAD_TS), COUNT(*), 'OK'
          FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
        UNION ALL
        SELECT 'SECURITY_TRUST_SNAPSHOT', MAX(LOAD_TS), COUNT(*),
               IFF(:trust_ok, 'OK', 'ERROR')
          FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET
        LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = s.LOAD_STATUS
    WHEN NOT MATCHED THEN INSERT
        (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, SNAPSHOT_TS, GENERATION, STATUS)
    VALUES
        (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, CURRENT_TIMESTAMP(), 1, s.LOAD_STATUS);

    RETURN 'security facts loaded ' || d || 'd';
END;
$$;

-- >>> derived:SP_LOAD_DAILY_FACTS  (from V101; FACT_STORAGE_DAILY arm AVG -> SUM per name-day, V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    wm_metering TIMESTAMP_NTZ;  -- V064 rec7: per-source watermarks (was one shared DAILY_FACTS mark)
    wm_task TIMESTAMP_NTZ;
    wm_login TIMESTAMP_NTZ;
    wm_storage TIMESTAMP_NTZ;
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_task TIMESTAMP_NTZ;      -- watermark - 1d overlap (default -3d, clamp -30d)
    lo_login TIMESTAMP_NTZ;
    lo_storage TIMESTAMP_NTZ;
    emsg VARCHAR;               -- B34 (V062): transaction-wrap error capture
    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run (return string)
BEGIN
    -- V064 rec7: each daily source keeps its OWN watermark so a per-table
    -- failure holds only THAT source's mark; siblings advance independently
    -- (no whole-group re-read of the costliest source on any one failure).
    SELECT MAX(WM_TS) INTO :wm_metering
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_METERING_DAILY';
    SELECT MAX(WM_TS) INTO :wm_task
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_TASK_DAILY';
    SELECT MAX(WM_TS) INTO :wm_login
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_LOGIN_DAILY';
    SELECT MAX(WM_TS) INTO :wm_storage
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'FACT_STORAGE_DAILY';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm_metering),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_task := GREATEST(COALESCE(DATEADD('day', -1, :wm_task),
                                 DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                        DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_login := GREATEST(COALESCE(DATEADD('day', -1, :wm_login),
                                  DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                         DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_storage := GREATEST(COALESCE(DATEADD('day', -1, :wm_storage),
                                    DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                           DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY t
    USING (
        SELECT
            USAGE_DATE AS DAY,
            UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
            SUM(COALESCE(CREDITS_USED_COMPUTE, 0)) AS CREDITS_COMPUTE,
            SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CREDITS_CLOUD_SVCS,
            SUM(COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)) AS CREDITS_ADJUSTMENT,
            SUM(COALESCE(CREDITS_USED, 0)) AS CREDITS_USED,
            SUM(COALESCE(CREDITS_BILLED,
                GREATEST(0, COALESCE(CREDITS_USED, 0) + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)))) AS CREDITS_BILLED
        FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
        WHERE USAGE_DATE >= :lo_metering::DATE
        GROUP BY 1, 2
    ) s
    ON t.DAY = s.DAY AND t.SERVICE_TYPE = s.SERVICE_TYPE
    WHEN MATCHED THEN UPDATE SET
        CREDITS_COMPUTE = s.CREDITS_COMPUTE, CREDITS_CLOUD_SVCS = s.CREDITS_CLOUD_SVCS,
        CREDITS_ADJUSTMENT = s.CREDITS_ADJUSTMENT, CREDITS_USED = s.CREDITS_USED,
        CREDITS_BILLED = s.CREDITS_BILLED, LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, SERVICE_TYPE, CREDITS_COMPUTE, CREDITS_CLOUD_SVCS, CREDITS_ADJUSTMENT, CREDITS_USED, CREDITS_BILLED)
        VALUES (s.DAY, s.SERVICE_TYPE, s.CREDITS_COMPUTE, s.CREDITS_CLOUD_SVCS, s.CREDITS_ADJUSTMENT, s.CREDITS_USED, s.CREDITS_BILLED);

    -- V064 rec7: metering has no txn wrap -- a fact-load failure aborts the proc
    -- before this line (V063's deliberate anchor-first design), so reaching here
    -- means metering loaded; advance its own mark. The mark MERGE is GUARDED
    -- (review fix): it is a NEW statement ahead of the isolated sibling blocks, so
    -- a transient OW_LOAD_WATERMARKS lock here must not abort the proc and starve
    -- task/login/storage -- log + hold the mark + fall through instead.
    BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_METERING_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_METERING_DAILY watermark advance - facts loaded, mark held, siblings unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V064 rec7: hold metering mark, keep loading siblings
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_TASK_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_task::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)
    WITH task_attempts AS (
        -- V101: collapse task auto-retries to the terminal attempt so RUNS/FAILED count
        -- scheduled runs, not attempts (a FAILED attempt that SUCCEEDED on retry is NOT a
        -- failure) — matching ops_sql.task_runs / task_recent_states, so the mart Task
        -- Health panel stops over-reporting failures the live tab collapses.
        SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, QUERY_START_TIME,
               COMPLETED_TIME, STATE, ERROR_MESSAGE
        FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
        WHERE QUERY_START_TIME >= :lo_task::DATE
        QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                   ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
    )
    SELECT
        DATE(QUERY_START_TIME),
        DATABASE_NAME,
        SCHEMA_NAME,
        NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        COUNT(*),
        SUM(IFF(STATE = 'FAILED', 1, 0)),
        AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),
        MAX_BY(STATE, QUERY_START_TIME),
        MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)
    FROM task_attempts
    GROUP BY 1, 2, 3, 4, 5;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_TASK_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_TASK_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_LOGIN_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY WHERE DAY >= :lo_login::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
        (DAY, USER_NAME, COMPANY, LOGINS, FAILED_LOGINS, PASSWORD_LOGINS)
    SELECT
        DATE(EVENT_TIMESTAMP),
        USER_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME),
        COUNT(*),
        SUM(IFF(IS_SUCCESS = 'NO', 1, 0)),
        SUM(IFF(FIRST_AUTHENTICATION_FACTOR = 'PASSWORD' AND IS_SUCCESS = 'YES', 1, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= :lo_login::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_LOGIN_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_LOGIN_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- B34 (V062): DELETE+INSERT is ONE transaction so a crash between the
    -- wipe and the refill can't leave FACT_STORAGE_DAILY half-empty; a failed
    -- INSERT rolls the DELETE back (consumers keep the previous fill).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY WHERE DAY >= :lo_storage::DATE;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
        (DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES)
    SELECT
        USAGE_DATE,
        DATABASE_NAME,
        DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
        -- V166 (R2-009): SUM, not AVG. The view has one row per DATABASE_ID per day, and a dropped
        -- (re-created / clone-refreshed) database keeps its own rows under the same name while it
        -- holds Time Travel / fail-safe bytes. Each row is already the daily average of its own
        -- DATABASE_ID, so the name-day billed bytes are the SUM (= storage_by_database_calendar_live).
        SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)),
        SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= :lo_storage::DATE
    GROUP BY 1, 2, 3;
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t
    USING (SELECT 'FACT_STORAGE_DAILY' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s
    ON t.SOURCE = s.SOURCE
    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS
    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_STORAGE_DAILY - other daily facts unaffected', CURRENT_ROLE();
            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return
    END;

    -- V064 rec7: the single shared DAILY_FACTS watermark advance is GONE -- each
    -- source advances its OWN mark in its own success path above, so one
    -- table's failure holds only that table's mark (siblings stay current).
    -- The SOURCE_FRESHNESS_STATE MERGE below stays UNGUARDED so a swallowed
    -- failure still surfaces as a stale freshness row.

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_METERING_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        UNION ALL
        SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        UNION ALL
        SELECT 'FACT_LOGIN_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
        UNION ALL
        SELECT 'FACT_STORAGE_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    IF (failed_any) THEN
        RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, that source''s watermark held';
    END IF;
    RETURN 'daily facts loaded';
END;
$$;

-- >>> derived:SP_LOAD_APP_COST  (from V077; DELETE+INSERT in one transaction, ROLLBACK + APP_ERROR_LOG + re-RAISE; SESSIONS lookback 7 -> 30 days, V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
    emsg VARCHAR;               -- V166 (R2-011): the rolled-back load error, logged then re-raised
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
    -- Reload the trailing window and self-trim beyond 400 days (advertised max
    -- window is 365; keep a buffer). No central purge touches this fact.
    -- V166 (R2-011): ONE transaction. Under autocommit a failed INSERT left the DELETE committed,
    -- and the next run starts a day later, so the oldest deleted day was never reloaded (a
    -- permanent hole per failed run). A failure now rolls back to the previous fill, is logged as
    -- fact_load_failed (the self-watch ERR leg keys on the first CONTEXT word) and is re-raised,
    -- so the task still reads FAILED.
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY
     WHERE DAY >= :lo OR DAY < DATEADD('day', -400, CURRENT_DATE());

    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY (DAY, APPLICATION, USER_NAME, COMPANY, QUERIES, CREDITS)
    WITH cred AS (
        SELECT QUERY_ID,
               SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
        WHERE START_TIME >= :lo
        GROUP BY QUERY_ID
        HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0
    ),
    q AS (
        SELECT QUERY_ID, SESSION_ID, START_TIME::DATE AS DAY,
               COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME, WAREHOUSE_NAME
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= :lo
    ),
    sess AS (
        -- One row per session; widen the lookback so a query's earlier-started
        -- session still resolves. Prefer the self-reported program, else the
        -- driver family (version stripped), else '(unknown)'.
        -- V166 (C10): 30 days before lo, not 7. The task LAST reload of a day (lo = that day)
        -- narrowed the lookback to 7 days and relabelled '(unknown)' a keep-alive / pooled session
        -- the first load had resolved. Same pad as app_cost_sql.SESSION_PAD_DAYS (the live twin).
        SELECT SESSION_ID,
               COALESCE(
                   NULLIF(GET_PATH(TRY_PARSE_JSON(CLIENT_ENVIRONMENT), 'APPLICATION')::STRING, ''),
                   NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                   '(unknown)'
               ) AS APPLICATION
        FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
        WHERE CREATED_ON >= DATEADD('day', -30, :lo)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1
    ),
    joined AS (
        SELECT q.DAY,
               COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION,
               q.USER_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(q.WAREHOUSE_NAME) AS COMPANY,
               c.CREDITS
        FROM q
        JOIN cred c ON c.QUERY_ID = q.QUERY_ID
        LEFT JOIN sess s ON s.SESSION_ID = q.SESSION_ID
    )
    SELECT DAY, APPLICATION, USER_NAME, COMPANY, COUNT(*), SUM(CREDITS)
    FROM joined
    GROUP BY DAY, APPLICATION, USER_NAME, COMPANY;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AppCost', 'fact_load_failed', LEFT(:emsg, 2000), 'FACT_APP_COST_DAILY - previous fill retained on rollback, error re-raised', CURRENT_ROLE();
            RAISE;
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_APP_COST_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT, t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT) VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    RETURN 'OK';
END;
$$;

-- >>> derived:SP_LOAD_STORAGE_TRUTH  (from V046; DELETE+INSERT in one transaction, ROLLBACK + APP_ERROR_LOG + re-RAISE, V166)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;
    emsg VARCHAR;               -- V166 (R2-011): the rolled-back load error, logged then re-raised
BEGIN
    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());
    -- V166 (R2-011): one transaction, so a failed INSERT rolls the DELETE back (see SP_LOAD_APP_COST).
    BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY WHERE DAY >= :lo;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY
        (DAY, TABLE_BYTES, STAGE_BYTES, FAILSAFE_BYTES, HYBRID_BYTES, ARCHIVE_COOL_BYTES, ARCHIVE_COLD_BYTES)
    SELECT
        USAGE_DATE,
        SUM(COALESCE(STORAGE_BYTES, 0)),
        SUM(COALESCE(STAGE_BYTES, 0)),
        SUM(COALESCE(FAILSAFE_BYTES, 0)),
        SUM(COALESCE(HYBRID_TABLE_STORAGE_BYTES, 0)),
        SUM(COALESCE(ARCHIVE_STORAGE_COOL_BYTES, 0)),
        SUM(COALESCE(ARCHIVE_STORAGE_COLD_BYTES, 0))
    FROM SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE
    WHERE USAGE_DATE >= :lo
    GROUP BY USAGE_DATE;
    COMMIT;
    EXCEPTION
        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'StorageTruth', 'fact_load_failed', LEFT(:emsg, 2000), 'FACT_STORAGE_ACCOUNT_DAILY - previous fill retained on rollback, error re-raised', CURRENT_ROLE();
            RAISE;
    END;

    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_STORAGE_ACCOUNT_DAILY' AS SOURCE_NAME,
               MAX(LOAD_TS) AS LAST_LOAD_TS, COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET
        t.LAST_LOAD_TS = s.LAST_LOAD_TS, t.ROW_COUNT = s.ROW_COUNT,
        t.SNAPSHOT_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT);

    RETURN 'OK';
END;
$$;

-- R2-009 repair (bounded, idempotent): the name-days where the view still holds 2+ DATABASE_ID rows were
-- written as the AVG; rewrite exactly those two byte columns as the SUM the loader now writes. Single-ID
-- name-days are untouched (the AVG of one row is its SUM). No DELETE, no insert, LOAD_TS unchanged.
MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
USING (
    SELECT USAGE_DATE AS DAY, DATABASE_NAME,
           SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
           SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
) s
ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME
WHEN MATCHED THEN UPDATE SET DB_BYTES = s.DB_BYTES, FAILSAFE_BYTES = s.FAILSAFE_BYTES;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 166 AS VERSION,
       'Fact loader window integrity: SP_LOAD_SECURITY_FACTS re-derived from V105, the d<=3 change reload DELETE and INSERT share one bound GREATEST(MIN(extract START_TIME), midnight(today-d)) so a swallowed extract failure, a backfill-wide extract or a manual DAYS_BACK below 3 no longer deletes rows the INSERT never re-reads; SP_LOAD_DAILY_FACTS re-derived from V101, FACT_STORAGE_DAILY SUMs the per-DATABASE_ID rows per name-day (re-created and clone-refreshed databases were averaged) plus one bounded MERGE that repairs the multi-ID name-days still in the 365-day view; SP_LOAD_APP_COST (V077) and SP_LOAD_STORAGE_TRUTH (V046) run DELETE and INSERT in one transaction with ROLLBACK, a fact_load_failed APP_ERROR_LOG row and a re-raise (a failed run lost its oldest reloaded day for good); SP_LOAD_APP_COST resolves sessions 30 days before its reload start, not 7. No task, table or DDL change; no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 166);

-- =====================================================================
--  STAGE 2 -- MIGRATION 2 of 7 -- APPLY V167 (idempotent; GUARDS on V166; in-migration repair: pattern-twin DELETE; OWNER_REPAIRS PART 1 right after this file). Source: snowflake/migrations/V167__mart_loader_edges_ai_coverage_pattern_reload.sql
-- =====================================================================

-- V167__mart_loader_edges_ai_coverage_pattern_reload.sql
--
-- Mart-loader window edges, the AI coverage watermark and the atomic pattern reload (v4.609 round-2 fixes).
--
-- WHY:
--   * R2-015: SP_LOAD_MARTS_V27 arm [1] expanded a query's spanned hours only for queries that STARTED inside
--     the window, so the hours a midnight-crossing query ran on day D-d read idle. The nightly reconcile last
--     writes a day as D-3, its left edge, so every finished day kept that error: IDLE_PCT / IDLE_CREDITS (the
--     Optimize idle and sizing panels, COST_IDLE_OPPORTUNITY) overstated idle on ELT warehouses.
--   * R2-014: arm [6] built task-graph runs only from attempts inside the window, so a run whose root started
--     before the window's first midnight was re-keyed to its first in-window child: a phantom pipeline row on
--     D-d, finalised by the reconcile, double-counting the children's credits.
--   * R2-052 (also satisfies R2-017): the Cortex Code views stamp USAGE_TIME as TIMESTAMP_TZ, and ::DATE /
--     ::TIMESTAMP_NTZ read its OWN stored offset, so arm [9] keyed days off the Central account day (and the
--     Central-midnight scan bound cut the oldest kept key short every run: a permanent under-count).
--   * R1-016: FACT_AI_USAGE_DAILY has no zero-row spine, so MIN(DAY) is the first AI use, not how far back the
--     loader reached; the app gate read it as the latter and blanked 180d / 365d / Current-year AI panels.
--   * R2-018: SP_NIGHTLY_RECONCILE DELETEd four tables up front whose reload edge (D-3 / ~now-3d) is wider
--     than the hourly task's d=2; one failed loader arm (each swallows its error) left that edge empty for good.
--     The V064 'transient gap' comment (and CHANGELOG:12389) was wrong for these four.
--   * R2-010: SP_LOAD_PATTERN_COST MERGEd on (DAY, QUERY_HASH, COMPANY, DATABASE_NAME) with COMPANY computed at
--     load time, so an Apply-mapping remap left the old-company row beside the new one (ALL scope summed both).
--   * PATTERN-RESTAMP: rows older than V120's 90-day re-stamp still carry inflated RUNS; a full-year reload is
--     only safe on the atomic loader, and the app may only read past 90 days where a reload provably reached.
--
-- WHAT (each re-derived proc is byte-identical to its base except the enumerated deltas):
--   + SOURCE_FRESHNESS_STATE.COVERAGE_FROM DATE (nullable; every writer names its columns).
--   ~ SP_LOAD_MARTS_V27 from V159: arm [1] span source START_TIME >= D-d-1 AND COALESCE(END_TIME, START_TIME)
--     >= D-d (m / q / m_idle keep D-d); arm [6] attempts from D-d-1 and WHERE DAY >= D-d before the final
--     GROUP BY (the QAH and IN-list bounds unchanged); arm [9] CONVERT_TIMEZONE('America/Chicago', ...) on DAY,
--     FIRST_TS and LAST_TS (the ai_functions arm keys an LTZ START_TIME, unchanged); the DAILY freshness MERGE
--     writes COVERAGE_FROM for FACT_AI_USAGE_DAILY = LEAST(old, today - d + 1) -- its HAVING already requires
--     BOTH AI arms. No UPDATE of SOURCE_FRESHNESS_STATE anywhere.
--   ~ SP_NIGHTLY_RECONCILE from V064: the four up-front DELETEs go; after the ('HOURLY', 3) verdict line each
--     table is swept of rows the reload did not re-stamp (LOAD_TS < recon_start) inside its reload window,
--     ONLY when that arm's :loaded token is in the marts verdict (rv). The hour bound is V159's ext_lo_hour
--     expression without its COALESCE fallback (an empty extract sweeps nothing). V159's 'SP_NIGHTLY_RECONCILE
--     DELETEs D-3..today' comment is superseded; its d > 2 gate escape is still required.
--   ~ SP_LOAD_PATTERN_COST from V120: BEGIN TRANSACTION; DELETE WHERE DAY >= :lo; INSERT (V120's aggregate,
--     unchanged); COMMIT; the V068 freshness MERGE (after the COMMIT) stamps COVERAGE_FROM = LEAST(old, :lo);
--     EXCEPTION WHEN OTHER THEN ROLLBACK; RAISE (the task still fails loudly).
--   - One-time repair: DELETE MART_PATTERN_COST_DAILY rows older than their (DAY, QUERY_HASH, DATABASE_NAME)
--     group's newest LOAD_TS by more than 10 minutes (groups of 2+ rows only). Every legitimate company row of
--     a day's last covering run shares that run's statement timestamp (V120's MERGE re-stamped LOAD_TS on
--     MATCHED), so an older row is a stale-company twin. Scan-free; recoverable by Time Travel or a reload.
--     Preview with PREFLIGHT P167.1 (rows, credits, day range) and P167.2 (same-stamp groups that are kept).
--
-- COST: arm [1]'s span leg scans one more day of QUERY_HISTORY (3 days instead of 2 on the six gated hourly
-- runs, 4 instead of 3 on the reconcile); arm [6] one more day of TASK_HISTORY; the same statement shapes, so
-- V159's compile diet stands. The reconcile adds four small DELETEs (two with a one-row extract subquery). The
-- pattern loader's statement count goes 2 -> 4 (BEGIN / DELETE / INSERT / COMMIT) on its daily 3-day window.
-- The twin DELETE reads only the mart. No new task, schedule or warehouse resume.
-- FIRST RUN: the next hourly TASK_LOAD_HOURLY graph, the 06:40-07:30 Central nightly chain and the 06:45
-- TASK_PATTERN_COST_DAILY pick up the new bodies. Nothing runs at apply time. COVERAGE_FROM starts as
-- today - 2 (AI) / today - 3 (pattern) and reaches back only after the owner-run reloads in the handoff
-- (OWNER_REPAIRS, a Central session): AI DAILY 365 reload-then-prune, then CALL SP_LOAD_PATTERN_COST(364),
-- then HOURLY N inside a TASK_LOAD_HOURLY suspend window, then the atomic arm [6] 364-day rebuild.
-- Known limits (disclosed): a query of 24h+ starting in hour 23 two days before a left edge on the
-- spring-forward night can still miss one hour; a task-graph run longer than ~1 day past the left edge can
-- still leave a phantom row; Cortex Code rows older than the views' retention stay offset-keyed.
-- ROLLBACK: re-run the base CREATEs (V159 SP_LOAD_MARTS_V27, V064 SP_NIGHTLY_RECONCILE, V120
-- SP_LOAD_PATTERN_COST); the column is inert to every older body (leave it). The twin DELETE is recoverable
-- by Time Travel (AT before the apply) or CALL SP_LOAD_PATTERN_COST(N).
-- Apply AFTER V166. Idempotent; safe to re-run. Owner applies in Snowsight; this file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20167, 'V167 requires V166 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 166) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- R1-016 / PATTERN-RESTAMP: the loaded-from watermark both re-derived loaders write inside their own freshness
-- MERGE (nullable; read only by the app, behind has_migration(167)).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS COVERAGE_FROM DATE;

-- >>> derived:SP_LOAD_MARTS_V27  (from V159; + R2-015 arm [1] span source padded a day, R2-014 arm [6] lead-in day + DAY filter, R2-052 arm [9] Central day key, R1-016 AI COVERAGE_FROM in the DAILY freshness MERGE, V167)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    emsg VARCHAR;
    loaded VARCHAR DEFAULT '';
    d INT;
    ct_hour INT;              -- V159 (D5): Central hour of this run, read once (the 4-hour gate)
    ext_lo DATE;
    ext_lo_hour TIMESTAMP_LTZ;
    req_fail INT DEFAULT 0;   -- V066 #10: REQUIRED-arm (core fact/mart) failures this run
    opt_fail INT DEFAULT 0;   -- V066 #10: OPTIONAL-arm (tag-cov, task-node, AI/Cortex) failures
    bad_scope EXCEPTION (-20661,
        'SP_LOAD_MARTS_V27: SCOPE must be HOURLY or DAILY - refusing to run as a silent no-op load.');   -- V066 #37 VALIDATE SCOPE
BEGIN
    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))::INT;

    -- V066 #37 VALIDATE SCOPE: an unrecognized SCOPE matched no arm and the terminal RETURN
    -- still claimed the marts loaded, so a typo'd scope silently loaded nothing. Fail loudly
    -- at the top instead (the outer BEGIN has no handler, so this RAISE aborts the proc).
    IF (UPPER(:SCOPE) NOT IN ('HOURLY', 'DAILY')) THEN
        RAISE bad_scope;
    END IF;

    IF (UPPER(:SCOPE) = 'HOURLY') THEN

        -- V159 compile diet (D5): the three DAY-grain arms whose ACCOUNT_USAGE MERGEs dominate this
        -- loader's compile -- [1] MART_WAREHOUSE_EFFICIENCY_DAILY, [6] MART_TASK_GRAPH_DAILY and [6b]
        -- MART_TASK_NODE_DAILY -- run every 4th Central hour (00, 04, 08, 12, 16, 20) instead of every
        -- hour, and ALWAYS when d > 2: SP_NIGHTLY_RECONCILE DELETEs D-3..today of the first two and
        -- re-loads them with ('HOURLY', 3), and backfills pass 90/365. The hourly task passes 2. A gated-off
        -- arm is not a failure (req_fail / opt_fail untouched) and appends no :loaded token, so its
        -- SOURCE_FRESHNESS_STATE row keeps its last stamp -- every name here contains DAILY, so the shared
        -- 30h cadence rule never reads it stale. Every other arm below still runs every hour.
        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;

        -- V062 B5/B10: clamp backfill lower bounds to the extract's first
        -- WHOLE day/hour so a wide :d actually loads :d days (not a silent 2),
        -- while normal ops (small :d) stay at the extract-bounded window.
        ext_lo := (SELECT COALESCE(
                       DATEADD('day', IFF(MIN(START_TIME) = DATE_TRUNC('day', MIN(START_TIME)), 0, 1), DATE(MIN(START_TIME))),
                       DATEADD('day', -:d, CURRENT_DATE()))
                   FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);
        ext_lo_hour := (SELECT COALESCE(
                       DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))),
                       DATEADD('day', -:d, CURRENT_DATE()))
                   FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);

        -- [1] warehouse efficiency ------------------------------------------
        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [1]: every 4th Central hour; always when d > 2
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY t
            USING (
                WITH m AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           SUM(CREDITS_USED) AS CREDITS_TOTAL,
                           SUM(CREDITS_USED_COMPUTE) AS CREDITS_COMPUTE,
                           COUNT_IF(CREDITS_USED > 0) AS BILLED_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                ),
                q AS (
                    SELECT DATE(START_TIME) AS DAY, WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 60000 AS QUEUED_MIN,
                           SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3) AS SPILL_GB,
                           APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000 AS P95_S,
                           SUM(COALESCE(EXECUTION_TIME, 0)) / 3600000 AS EXEC_HOURS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_NAME IS NOT NULL
                    GROUP BY 1, 2
                ),
                -- V103: ACTIVE_HOURS must count every clock hour a query was RUNNING, not just
                -- its START hour. The old COUNT(DISTINCT DATE_TRUNC('hour', START_TIME)) marked
                -- hours 11 and 12 of a 10:59->13:00 query IDLE, so IDLE_PCT (and every $ derived
                -- from it: the SUSPEND/DOWN sizing verdict, IDLE_MONTHLY_USD, the idle-$ KPI)
                -- overstated idle for any multi-hour query. Expand each query across the hours it
                -- SPANS (bounded to 25, matching insights_sql._active_hours_cte), attribute each
                -- spanned hour to its own DAY, and count distinct warehouse-day-hours.
                qh AS (
                    SELECT s.WAREHOUSE_NAME,
                           DATE(DATEADD('hour', g.SEQ, s.H0)) AS DAY,
                           DATEADD('hour', g.SEQ, s.H0) AS HOUR_TS
                    FROM (
                        SELECT WAREHOUSE_NAME,
                               DATE_TRUNC('hour', START_TIME) AS H0,
                               DATE_TRUNC('hour', COALESCE(END_TIME, START_TIME)) AS H1
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                        -- V167 (R2-015): pad the span source one day back, so a query that started before the
                        -- window's first Central midnight still marks the hours it spans on day D-d active
                        -- (the 25-row GENERATOR caps a span at H0 + 24h, so one day reaches every D-d hour but
                        -- one hour on the spring-forward night); the END_TIME floor drops prior-day queries that
                        -- never reach D-d. m / q / m_idle keep D-d, so no output row moves to an earlier day.
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND COALESCE(END_TIME, START_TIME) >= DATEADD('day', -:d, CURRENT_DATE())
                          AND WAREHOUSE_NAME IS NOT NULL
                    ) s
                    JOIN (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 25))) g
                      ON DATEADD('hour', g.SEQ, s.H0) <= s.H1
                ),
                q_active AS (
                    SELECT WAREHOUSE_NAME, DAY, COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS
                    FROM qh
                    GROUP BY 1, 2
                ),
                m_idle AS (
                    -- V127: ACTUAL credits burned in warehouse-hours with NO active (span-
                    -- expanded) query -- mirrors the live twin insights_sql.idle_warehouse_analysis
                    -- (SUM(IFF(no active query hour, CREDITS_USED, 0))). Stored so the reader
                    -- eff_idle_analysis reads accurate idle spend instead of pro-rating the day's
                    -- total credits by the hour-count IDLE_PCT (which over-states idle for scale-out
                    -- warehouses, whose idle hours cost less than their active multi-cluster hours).
                    -- Join to DISTINCT active hours (like the live query_hours CTE) so a metering
                    -- slice is never fanned out by multiple queries sharing an hour.
                    SELECT DATE(mh.START_TIME) AS DAY, mh.WAREHOUSE_NAME,
                           SUM(IFF(a.HOUR_TS IS NULL, COALESCE(mh.CREDITS_USED, 0), 0)) AS IDLE_CREDITS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY mh
                    LEFT JOIN (SELECT DISTINCT WAREHOUSE_NAME, HOUR_TS FROM qh) a
                           ON a.WAREHOUSE_NAME = mh.WAREHOUSE_NAME
                          AND a.HOUR_TS = DATE_TRUNC('hour', mh.START_TIME)
                    WHERE mh.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND mh.WAREHOUSE_ID > 0
                    GROUP BY 1, 2
                )
                SELECT COALESCE(m.DAY, q.DAY) AS DAY,
                       COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)) AS COMPANY,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0), 4) AS CREDITS_TOTAL,
                       ROUND(COALESCE(m.CREDITS_COMPUTE, 0), 4) AS CREDITS_COMPUTE,
                       COALESCE(q.QUERIES, 0) AS QUERIES,
                       COALESCE(q.FAILS, 0) AS FAILS,
                       ROUND(COALESCE(q.QUEUED_MIN, 0), 2) AS QUEUED_MIN,
                       ROUND(COALESCE(q.SPILL_GB, 0), 3) AS SPILL_GB,
                       ROUND(COALESCE(q.P95_S, 0), 1) AS P95_S,
                       ROUND(COALESCE(q.EXEC_HOURS, 0), 3) AS EXEC_HOURS,
                       COALESCE(m.BILLED_HOURS, 0) AS BILLED_HOURS,
                       COALESCE(qa.ACTIVE_HOURS, 0) AS ACTIVE_HOURS,
                       ROUND(100 * GREATEST(COALESCE(m.BILLED_HOURS, 0) - COALESCE(qa.ACTIVE_HOURS, 0), 0)
                             / NULLIF(m.BILLED_HOURS, 0), 2) AS IDLE_PCT,
                       ROUND(COALESCE(m.CREDITS_TOTAL, 0) / NULLIF(q.QUERIES, 0), 6) AS CREDITS_PER_QUERY,
                       ROUND(COALESCE(mi.IDLE_CREDITS, 0), 4) AS IDLE_CREDITS
                FROM m FULL OUTER JOIN q ON q.DAY = m.DAY AND q.WAREHOUSE_NAME = m.WAREHOUSE_NAME
                LEFT JOIN q_active qa ON qa.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)
                                     AND qa.DAY = COALESCE(m.DAY, q.DAY)
                LEFT JOIN m_idle mi ON mi.WAREHOUSE_NAME = COALESCE(m.WAREHOUSE_NAME, q.WAREHOUSE_NAME)
                                   AND mi.DAY = COALESCE(m.DAY, q.DAY)
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET
                COMPANY = s.COMPANY, CREDITS_TOTAL = s.CREDITS_TOTAL,
                CREDITS_COMPUTE = s.CREDITS_COMPUTE, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_MIN = s.QUEUED_MIN, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S,
                EXEC_HOURS = s.EXEC_HOURS, BILLED_HOURS = s.BILLED_HOURS,
                ACTIVE_HOURS = s.ACTIVE_HOURS, IDLE_PCT = s.IDLE_PCT,
                CREDITS_PER_QUERY = s.CREDITS_PER_QUERY, IDLE_CREDITS = s.IDLE_CREDITS,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE, QUERIES, FAILS,
                 QUEUED_MIN, SPILL_GB, P95_S, EXEC_HOURS, BILLED_HOURS, ACTIVE_HOURS, IDLE_PCT, CREDITS_PER_QUERY, IDLE_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.COMPANY, s.CREDITS_TOTAL, s.CREDITS_COMPUTE, s.QUERIES, s.FAILS,
                    s.QUEUED_MIN, s.SPILL_GB, s.P95_S, s.EXEC_HOURS, s.BILLED_HOURS, s.ACTIVE_HOURS, s.IDLE_PCT, s.CREDITS_PER_QUERY, s.IDLE_CREDITS);
            loaded := loaded || 'wh_eff ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;
        END IF;   -- V159 (D5) gate [1]

        -- [2] query families (top 2000/day by exec time) --------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DAY,
                       QUERY_HASH,
                       COMPANY,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM (
                    -- V082: derive COMPANY per row FIRST (UDF outside the aggregation, the
                    -- V029 shape law), so the outer GROUP BY keys on a plain column and never
                    -- on the correlated-subquery UDF directly -- grouping BY that UDF is the
                    -- exact shape that logged mart_load_failed every hour after V027 (V029).
                    SELECT DATE(START_TIME) AS DAY,
                           QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY,
                           QUERY_TEXT, EXECUTION_STATUS, USER_NAME, WAREHOUSE_NAME,
                           DATABASE_NAME, SCHEMA_NAME, EXECUTION_TIME, TOTAL_ELAPSED_TIME,
                           COMPILATION_TIME, BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                      AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                )
                GROUP BY DAY, QUERY_HASH, COMPANY
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY, COMPANY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, COMPANY, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);
            loaded := loaded || 'qfam ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_QUERY_FAMILY_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [3] role-hour fact -------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.ROLE_NAME, g.WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.EXEC_SEC
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo_hour)
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.ROLE_NAME = s.ROLE_NAME AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (HOUR_TS, ROLE_NAME, WAREHOUSE_NAME, COMPANY, QUERIES, FAILS, EXEC_SEC)
            VALUES (s.HOUR_TS, s.ROLE_NAME, s.WAREHOUSE_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.EXEC_SEC);
            loaded := loaded || 'role_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_ROLE_HOURLY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [4] schema-hour fact -----------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY t
            USING (
                SELECT g.HOUR_TS, g.DATABASE_NAME, g.SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.QUEUED_SEC, g.SPILL_GB, g.P95_S
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                           COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                           ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                           ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo_hour)
                    GROUP BY 1, 2, 3
                ) g
            ) s
            ON t.HOUR_TS = s.HOUR_TS AND t.DATABASE_NAME = s.DATABASE_NAME AND t.SCHEMA_NAME = s.SCHEMA_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES, FAILS = s.FAILS,
                QUEUED_SEC = s.QUEUED_SEC, SPILL_GB = s.SPILL_GB, P95_S = s.P95_S, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (HOUR_TS, DATABASE_NAME, SCHEMA_NAME, COMPANY, QUERIES, FAILS, QUEUED_SEC, SPILL_GB, P95_S)
            VALUES (s.HOUR_TS, s.DATABASE_NAME, s.SCHEMA_NAME, s.COMPANY, s.QUERIES, s.FAILS, s.QUEUED_SEC, s.SPILL_GB, s.P95_S);
            loaded := loaded || 'schema_hr ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_QUERY_SCHEMA_HOURLY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [4b] tag coverage by user, day grain (v4.14 tuning trio) --------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY t
            USING (
                SELECT g.DAY, g.USER_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(g.USER_NAME) AS COMPANY,
                       g.QUERIES, g.EXEC_SEC, g.UNTAGGED_EXEC_SEC
                FROM (
                    SELECT DATE(START_TIME) AS DAY,
                           COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                           COUNT(*) AS QUERIES,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC,
                           ROUND(SUM(IFF(NULLIF(QUERY_TAG, '') IS NULL,
                                         COALESCE(EXECUTION_TIME, 0), 0)) / 1000, 1) AS UNTAGGED_EXEC_SEC
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                    GROUP BY 1, 2
                ) g
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, QUERIES = s.QUERIES,
                EXEC_SEC = s.EXEC_SEC, UNTAGGED_EXEC_SEC = s.UNTAGGED_EXEC_SEC,
                LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, USER_NAME, COMPANY, QUERIES, EXEC_SEC, UNTAGGED_EXEC_SEC)
            VALUES (s.DAY, s.USER_NAME, s.COMPANY, s.QUERIES, s.EXEC_SEC, s.UNTAGGED_EXEC_SEC);
            loaded := loaded || 'tagcov ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TAG_COVERAGE_DAILY - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [5] cost allocation (exec-time share of each warehouse-hour) -------
        BEGIN
            CREATE OR REPLACE TEMPORARY TABLE _OW_ALLOC_BASE AS
            WITH wh AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       SUM(CREDITS_USED) AS HOUR_CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND WAREHOUSE_ID > 0
                GROUP BY 1, 2
            ),
            q AS (
                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, WAREHOUSE_NAME,
                       USER_NAME, COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       SUM(COALESCE(EXECUTION_TIME, 0)) AS EXEC_MS
                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND WAREHOUSE_NAME IS NOT NULL AND COALESCE(EXECUTION_TIME, 0) > 0
                GROUP BY 1, 2, 3, 4, 5, 6
            ),
            tot AS (
                SELECT HOUR_TS, WAREHOUSE_NAME, SUM(EXEC_MS) AS TOTAL_MS FROM q GROUP BY 1, 2
            )
            SELECT DATE(q.HOUR_TS) AS DAY, q.WAREHOUSE_NAME, q.USER_NAME, q.ROLE_NAME,
                   q.DATABASE_NAME, q.SCHEMA_NAME, q.EXEC_MS,
                   wh.HOUR_CREDITS * q.EXEC_MS / NULLIF(tot.TOTAL_MS, 0) AS ALLOC_CREDITS
            FROM q
            JOIN tot ON tot.HOUR_TS = q.HOUR_TS AND tot.WAREHOUSE_NAME = q.WAREHOUSE_NAME
            JOIN wh ON wh.HOUR_TS = q.HOUR_TS AND wh.WAREHOUSE_NAME = q.WAREHOUSE_NAME;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY t
            USING (
                SELECT DAY, 'USER' AS DIMENSION, USER_NAME AS KEY_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) AS COMPANY,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'DATABASE', DATABASE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
                UNION ALL
                SELECT DAY, 'SCHEMA', DATABASE_NAME || '.' || SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3, DATABASE_NAME
                UNION ALL
                SELECT DAY, 'ROLE', ROLE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_ROLE(ROLE_NAME),
                       ROUND(SUM(ALLOC_CREDITS), 6), ROUND(SUM(EXEC_MS) / 1000, 1)
                FROM _OW_ALLOC_BASE GROUP BY 1, 3
            ) s
            ON t.DAY = s.DAY AND t.DIMENSION = s.DIMENSION AND t.KEY_NAME = s.KEY_NAME
            WHEN MATCHED THEN UPDATE SET COMPANY = s.COMPANY, ALLOC_CREDITS = s.ALLOC_CREDITS,
                EXEC_SEC = s.EXEC_SEC, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, DIMENSION, KEY_NAME, COMPANY, ALLOC_CREDITS, EXEC_SEC)
            VALUES (s.DAY, s.DIMENSION, s.KEY_NAME, s.COMPANY, s.ALLOC_CREDITS, s.EXEC_SEC);
            loaded := loaded || 'alloc ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_COST_ALLOCATION_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [5b] cross-dim allocation fact (V041 R2): persist _OW_ALLOC_BASE at
        -- DAY x WAREHOUSE x DATABASE x USER before it collapses to single-dim.
        -- NO schema grain (cardinality; schema stays live-filtered). Same
        -- expressions as [5], so the day-sums reconcile by construction.
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY t
            USING (
                SELECT DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
                FROM _OW_ALLOC_BASE
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
               AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET EXEC_SEC = s.EXEC_SEC,
                ALLOC_CREDITS = s.ALLOC_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, EXEC_SEC, ALLOC_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.EXEC_SEC, s.ALLOC_CREDITS);
            loaded := loaded || 'alloc_xdim ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_COST_ALLOC_XDIM_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [6] task graphs -----------------------------------------------------
        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [6]: every 4th Central hour; always when d > 2
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY t
            USING (
                WITH attempts AS (
                    -- V126: keep EVERY attempt (do NOT collapse to the terminal attempt before
                    -- the credit join) and tag the terminal one. TASK_RUNS / FAILED_TASKS still
                    -- count scheduled tasks via TERMINAL_RN = 1 (a task auto-retried to success
                    -- is not a graph-run failure), but WH_CREDITS now SUMs the compute of EVERY
                    -- attempt -- each retry really billed compute. This mirrors the live twin
                    -- graph_sql.graph_daily_costs exactly, so the same task-graph panel's cost no
                    -- longer flips with mart warmth. V102's terminal-only credit join dropped a
                    -- failed-retry attempt's compute (documented as accepted, but it disagreed
                    -- with the live path and the "every task run" panel caption).
                    SELECT COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID) AS RUN_KEY,
                           h.NAME, h.DATABASE_NAME, h.SCHEMA_NAME,
                           h.QUERY_START_TIME, h.COMPLETED_TIME, h.STATE,
                           COALESCE(a.CREDITS, 0) AS CREDITS,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(h.GRAPH_RUN_GROUP_ID::VARCHAR, h.QUERY_ID), h.NAME, h.SCHEDULED_TIME
                               ORDER BY h.COMPLETED_TIME DESC NULLS LAST) AS TERMINAL_RN
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    LEFT JOIN (
                        SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS ROOT_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                          AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
                              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                              WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                                AND STATE IN ('SUCCEEDED', 'FAILED')
                          )
                        GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)
                    ) a ON a.ROOT_ID = h.QUERY_ID
                    -- V167 (R2-014): one lead-in day, so a run that began before the window is seen whole
                    -- here and dropped by the DAY filter below, instead of being re-keyed to its first
                    -- in-window child as a phantom pipeline row on the window's first day
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())
                      AND h.STATE IN ('SUCCEEDED', 'FAILED')
                ),
                runs AS (
                    SELECT RUN_KEY,
                           MIN_BY(NAME, QUERY_START_TIME) AS PIPELINE,
                           MIN_BY(DATABASE_NAME, QUERY_START_TIME) AS DATABASE_NAME,
                           MIN_BY(SCHEMA_NAME, QUERY_START_TIME) AS SCHEMA_NAME,
                           DATE(MIN(QUERY_START_TIME)) AS DAY,
                           COUNT_IF(TERMINAL_RN = 1) AS TASK_RUNS,
                           COUNT_IF(TERMINAL_RN = 1 AND STATE = 'FAILED') AS FAILED_TASKS,
                           DATEDIFF('second', MIN(QUERY_START_TIME), MAX(COMPLETED_TIME)) AS WALL_SEC,
                           SUM(CREDITS) AS CREDITS
                    FROM attempts
                    GROUP BY RUN_KEY
                )
                SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME,
                       COUNT(*) AS GRAPH_RUNS,
                       COUNT_IF(FAILED_TASKS > 0) AS RUNS_WITH_FAILURES,
                       SUM(TASK_RUNS) AS TASK_RUNS,
                       ROUND(AVG(WALL_SEC), 1) AS AVG_WALL_SEC,
                       ROUND(APPROX_PERCENTILE(WALL_SEC, 0.95), 1) AS P95_WALL_SEC,
                       ROUND(SUM(CREDITS), 4) AS WH_CREDITS
                FROM runs
                -- V167 (R2-014): only runs whose first attempt is inside the window; a run that started
                -- earlier was written whole on its own root day by the load that covered that day
                WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.PIPELINE = s.PIPELINE
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET GRAPH_RUNS = s.GRAPH_RUNS,
                RUNS_WITH_FAILURES = s.RUNS_WITH_FAILURES, TASK_RUNS = s.TASK_RUNS,
                AVG_WALL_SEC = s.AVG_WALL_SEC, P95_WALL_SEC = s.P95_WALL_SEC,
                WH_CREDITS = s.WH_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES,
                 TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS)
            VALUES (s.DAY, s.PIPELINE, s.DATABASE_NAME, s.SCHEMA_NAME, s.GRAPH_RUNS,
                    s.RUNS_WITH_FAILURES, s.TASK_RUNS, s.AVG_WALL_SEC, s.P95_WALL_SEC, s.WH_CREDITS);
            loaded := loaded || 'graphs ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;
        END IF;   -- V159 (D5) gate [6]

        -- [6b] per-node task timing (queue + exec delay) -> MART_TASK_NODE_DAILY
        -- Observability for the deferred reconcile-scheduling work: the
        -- SCHEDULED_TIME->QUERY_START_TIME dispatch delay (which the pipeline-grain
        -- arm [6] discards) quantifies the 06:40/06:45 XSMALL contention. Own
        -- guarded arm; touches no existing statement; one TASK_HISTORY scan at the
        -- same -:d window; MERGE on (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME).
        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [6b]: every 4th Central hour; always when d > 2
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY t
            USING (
                SELECT DATE(QUERY_START_TIME) AS DAY,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       NAME AS TASK_NAME,
                       COUNT(*) AS RUNS,
                       COUNT_IF(STATE = 'FAILED') AS FAILED,
                       ROUND(AVG(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS AVG_QUEUE_SEC,
                       ROUND(APPROX_PERCENTILE(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0), 0.95) / 1000, 2) AS P95_QUEUE_SEC,
                       ROUND(MAX(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS MAX_QUEUE_SEC,
                       ROUND(AVG(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS AVG_EXEC_SEC,
                       ROUND(APPROX_PERCENTILE(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME), 0.95) / 1000, 2) AS P95_EXEC_SEC,
                       ROUND(MAX(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS MAX_EXEC_SEC,
                       MIN(QUERY_START_TIME) AS FIRST_START,
                       MAX(COMPLETED_TIME) AS LAST_COMPLETED
                FROM (
                    -- V102: collapse task auto-retries to the terminal attempt so RUNS /
                    -- FAILED and the queue/exec percentiles count scheduled runs, not
                    -- attempts, mirroring the live ops_sql.task_runs / task_recent_states.
                    SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                           QUERY_START_TIME, COMPLETED_TIME, STATE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                    WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND STATE IN ('SUCCEEDED', 'FAILED')
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                               ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
                ) th
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.TASK_NAME = s.TASK_NAME
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET
                RUNS = s.RUNS, FAILED = s.FAILED,
                AVG_QUEUE_SEC = s.AVG_QUEUE_SEC, P95_QUEUE_SEC = s.P95_QUEUE_SEC, MAX_QUEUE_SEC = s.MAX_QUEUE_SEC,
                AVG_EXEC_SEC = s.AVG_EXEC_SEC, P95_EXEC_SEC = s.P95_EXEC_SEC, MAX_EXEC_SEC = s.MAX_EXEC_SEC,
                FIRST_START = s.FIRST_START, LAST_COMPLETED = s.LAST_COMPLETED, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, RUNS, FAILED,
                 AVG_QUEUE_SEC, P95_QUEUE_SEC, MAX_QUEUE_SEC,
                 AVG_EXEC_SEC, P95_EXEC_SEC, MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED)
            VALUES (s.DAY, s.DATABASE_NAME, s.SCHEMA_NAME, s.TASK_NAME, s.RUNS, s.FAILED,
                    s.AVG_QUEUE_SEC, s.P95_QUEUE_SEC, s.MAX_QUEUE_SEC,
                    s.AVG_EXEC_SEC, s.P95_EXEC_SEC, s.MAX_EXEC_SEC, s.FIRST_START, s.LAST_COMPLETED);
            loaded := loaded || 'task_node ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_NODE_DAILY - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;
        END IF;   -- V159 (D5) gate [6b]

        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            -- V066 #3: wrap the DELETE+INSERT in ONE transaction. Under AUTOCOMMIT the DELETE
            -- committed immediately, so a later failure in the 4-way UNION INSERT (a transient
            -- ACCOUNT_USAGE read / COMPANY_FOR_DATABASE UDF error) left the trailing 48h BLANK
            -- until the next hourly rebuild -- an incident timeline empty mid-incident. ROLLBACK
            -- on error restores the prior rows (the B34 FACT_TASK_DAILY wrap pattern).
            BEGIN TRANSACTION;
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());

            INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
                (EVENT_TS, KIND, COMPANY, SEVERITY, TITLE, REF_ID)
            SELECT RAISED_AT, 'ALERT', COMPANY, SEVERITY, LEFT(TITLE, 300), EVENT_ID
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            WHERE RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
            UNION ALL
            SELECT COMPLETED_TIME, 'TASK_FAIL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'HIGH', LEFT(DATABASE_NAME || '.' || NAME || ' failed', 300), NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE COMPLETED_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP()) AND STATE = 'FAILED'
            UNION ALL
            SELECT START_TIME, 'DDL',
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')),
                   'INFO', LEFT(QUERY_TYPE || ' by ' || USER_NAME || ' (' || COALESCE(ROLE_NAME, '?') || ')', 300), QUERY_ID
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                                 'DROP', 'RENAME', 'CREATE_VIEW', 'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
            UNION ALL
            SELECT CHANGE_SEEN_AT, 'WH_CHANGE', COMPANY, 'INFO',
                   LEFT(WAREHOUSE_NAME || ' ' || SETTING || ' ' || COALESCE(OLD_VALUE, '?') || '->' || COALESCE(NEW_VALUE, '?'), 300),
                   CHANGE_ID
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            COMMIT;
            loaded := loaded || 'timeline ';
        EXCEPTION
            WHEN OTHER THEN
                ROLLBACK;   -- V066 #3: undo the 48h DELETE if the rebuild INSERT failed
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        -- V066 #11 FRESHNESS ADVANCES ON FAILURE: stamp ONLY the sources whose arm actually
        -- loaded this run. This MERGE used to advance GENERATION and write the successful-arm
        -- list as STATUS across the whole STATIC group, so a source whose arm just failed
        -- still looked freshly loaded. Each arm appends its token to :loaded only on its
        -- success path, so gate the source set on token membership (ARRAY_CONTAINS over
        -- SPLIT(:loaded)); a failed source is left untouched -- its prior generation/snapshot
        -- stand, correctly reading as not-loaded-this-run -- and STATUS now carries that
        -- source's own outcome.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,
                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f
            JOIN (
                SELECT SOURCE_NAME, TOKEN FROM VALUES
                    ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'wh_eff'),
                    ('MART_QUERY_FAMILY_DAILY', 'qfam'),
                    ('FACT_QUERY_ROLE_HOURLY', 'role_hr'),
                    ('FACT_QUERY_SCHEMA_HOURLY', 'schema_hr'),
                    ('MART_TAG_COVERAGE_DAILY', 'tagcov'),
                    ('MART_COST_ALLOCATION_DAILY', 'alloc'),
                    ('FACT_COST_ALLOC_XDIM_DAILY', 'alloc_xdim'),
                    ('MART_TASK_GRAPH_DAILY', 'graphs'),
                    ('MART_TASK_NODE_DAILY', 'task_node'),
                    ('MART_INCIDENT_TIMELINE', 'timeline')
                    AS srcmap(SOURCE_NAME, TOKEN)
            ) m ON m.SOURCE_NAME = f.SOURCE_NAME
            WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))
            GROUP BY f.SOURCE_NAME
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = s.STATUS
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);

    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN

        -- [7] security posture ------------------------------------------------
        BEGIN
            -- V041 R11 (guarded, v4.36.1): SHOW -> RESULT_SCAN once daily
            -- (V024 precedent), so Security stops paying a SHOW + parse per
            -- render. The nested handler means a SHOW failure can never take
            -- the CORE posture metrics down with it — the monitor arms below
            -- emit no rows that day instead (HAVING; never a lying zero).
            BEGIN
                SHOW WAREHOUSES LIMIT 500;
                CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR AS
                SELECT "name"::VARCHAR AS WAREHOUSE_NAME,
                       COALESCE("resource_monitor"::VARCHAR, 'null') AS RESOURCE_MONITOR,
                       TRY_TO_NUMBER("auto_suspend"::VARCHAR) AS AUTO_SUSPEND
                FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR (
                        WAREHOUSE_NAME VARCHAR, RESOURCE_MONITOR VARCHAR, AUTO_SUSPEND NUMBER);
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'MartLoader', 'monitor_counts_skipped', :emsg, 'SHOW WAREHOUSES unavailable - core posture unaffected', CURRENT_ROLE();
            END;

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
            USING (
                -- A4: CREDENTIALS scanned ONCE; both metrics via COUNT_IF + UNPIVOT (was two scans).
                SELECT CURRENT_DATE() AS DAY, cu.METRIC AS METRIC, 'ALL' AS COMPANY, cu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(EXPIRATION_DATE IS NOT NULL
                                    AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())) AS "EXPIRING_CRED_10D",
                           COUNT_IF(EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS "EXPIRED_CRED"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                ) c
                UNPIVOT (VALUE FOR METRIC IN ("EXPIRING_CRED_10D", "EXPIRED_CRED")) cu
                UNION ALL
                SELECT CURRENT_DATE(), 'ADMIN_STMTS_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                  AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                UNION ALL
                -- A4: GRANTS_TO_USERS scanned ONCE; both grant metrics via COUNT_IF + UNPIVOT (was two
                -- scans). The outer WHERE is a superset of the rows either metric needs (created >= -30d
                -- covers the -24h change window; deleted >= -24h keeps revoked-in-24h rows), and each
                -- COUNT_IF re-applies its exact original predicate, so both counts are unchanged.
                SELECT CURRENT_DATE() AS DAY, gu.METRIC AS METRIC, 'ALL' AS COMPANY, gu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                                    OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) AS "GRANT_CHANGES_24H",
                           COUNT_IF(DELETED_ON IS NULL
                                    AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                                    AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS "BREAKGLASS_GRANTS_30D"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                    WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
                       OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                ) g
                UNPIVOT (VALUE FOR METRIC IN ("GRANT_CHANGES_24H", "BREAKGLASS_GRANTS_30D")) gu
                UNION ALL
                -- V041 R9: unused-role posture from the role-hour fact, not a
                -- 90d QUERY_HISTORY anti-join. Coverage-gated: HAVING emits NO
                -- row (never a lying zero) until the fact spans the window.
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY q
                      WHERE q.HOUR_TS >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )
                HAVING (SELECT MIN(HOUR_TS) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY)
                       <= DATEADD('day', -89, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'MFA_GAP_USERS', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
                WHERE U.DELETED_ON IS NULL AND COALESCE(U.DISABLED, FALSE) = FALSE
                  AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
                  AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                              WHERE L.USER_NAME = U.NAME
                                AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                                AND L.PASSWORD_LOGINS > 0)
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_MONITOR', 'ALL',
                       COUNT_IF(LOWER(TRIM(RESOURCE_MONITOR)) IN ('null', '', 'none'))
                FROM _OW_WH_MONITOR
                HAVING COUNT(*) > 0
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_AUTOSUSPEND', 'ALL',
                       COUNT_IF(COALESCE(AUTO_SUSPEND, 0) <= 0)
                FROM _OW_WH_MONITOR
                HAVING COUNT(*) > 0
            ) s
            ON t.DAY = s.DAY AND t.METRIC = s.METRIC AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET VALUE = s.VALUE, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (DAY, METRIC, COMPANY, VALUE)
            VALUES (s.DAY, s.METRIC, s.COMPANY, s.VALUE);
            loaded := loaded || 'posture ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_SECURITY_POSTURE_DAILY - other marts unaffected', CURRENT_ROLE();
                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm swallow unchanged)
        END;

        -- [9] AI usage (Cortex Code views bill this account; Functions guarded)
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                -- V167 (R2-052): USAGE_TIME is TIMESTAMP_TZ, and ::DATE / ::TIMESTAMP_NTZ on a TIMESTAMP_TZ
                -- read the value's OWN stored offset, not the session zone. Convert to Central first, so DAY
                -- is the account day the Central-midnight window bound below (and every sibling fact) uses,
                -- and FIRST_TS / LAST_TS hold Central wall clock (a no-op if the views stamp Central).
                SELECT CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       ANY_VALUE(u.EMAIL) AS EMAIL,
                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact
                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ
                       -- (live 2026-08-13: "expecting TIMESTAMP_NTZ(9) but got
                       -- TIMESTAMP_TZ(9) for column FIRST_TS" killed this arm on
                       -- every run, starving the AI coverage gate).
                       CONVERT_TIMEZONE('America/Chicago', MIN(c.USAGE_TIME))::TIMESTAMP_NTZ AS FIRST_TS,
                       CONVERT_TIMEZONE('America/Chicago', MAX(c.USAGE_TIME))::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(c.TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(c.TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM (
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'Snowsight' AS SOURCE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    UNION ALL
                    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS, 'CLI'
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
                    WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                ) c
                LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS u ON u.USER_ID = c.USER_ID
                GROUP BY 1, 2, 3
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, EMAIL = s.EMAIL, FIRST_TS = s.FIRST_TS,
                LAST_TS = s.LAST_TS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, USER_NAME, SOURCE, MODEL_NAME, EMAIL, FIRST_TS, LAST_TS, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.EMAIL, s.FIRST_TS, s.LAST_TS,
                    s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_code ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (code views) - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;

        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
            USING (
                -- V146: repointed off the FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY onto the canonical
                -- CORTEX_AI_FUNCTIONS_USAGE_HISTORY. Not a drop-in: TOKEN_CREDITS -> CREDITS; START_TIME
                -- is TIMESTAMP_LTZ (was NTZ) so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ (same TZ->NTZ MERGE
                -- guard as the ai_code arm, V078); and there is NO scalar TOKENS column -- token counts
                -- live in the METRICS ARRAY as {"key":{"metric":"input"|"output","unit":"tokens"},"value":N},
                -- so LATERAL FLATTEN sums value where unit='tokens'. CREDITS + REQUESTS are deduped to
                -- once per source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for
                -- empty METRICS, still counted once) so the FLATTEN fan-out cannot multiply them.
                SELECT f.START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(NULLIF(f.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(f.START_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(f.START_TIME)::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(CASE WHEN COALESCE(m.INDEX, 0) = 0 THEN 1 END) AS REQUESTS,
                       SUM(CASE WHEN m.VALUE:key:unit::STRING = 'tokens'
                                THEN m.VALUE:value::NUMBER ELSE 0 END) AS TOKENS,
                       ROUND(SUM(CASE WHEN COALESCE(m.INDEX, 0) = 0
                                      THEN COALESCE(f.CREDITS, 0) ELSE 0 END), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY f,
                     LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m
                WHERE f.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.USER_NAME = s.USER_NAME AND t.SOURCE = s.SOURCE AND t.MODEL_NAME = s.MODEL_NAME
            WHEN MATCHED THEN UPDATE SET REQUESTS = s.REQUESTS, TOKENS = s.TOKENS,
                CREDITS = s.CREDITS, EMAIL = s.EMAIL, FIRST_TS = s.FIRST_TS,
                LAST_TS = s.LAST_TS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, USER_NAME, SOURCE, MODEL_NAME, EMAIL, FIRST_TS, LAST_TS, REQUESTS, TOKENS, CREDITS)
            VALUES (s.DAY, s.USER_NAME, s.SOURCE, s.MODEL_NAME, s.EMAIL, s.FIRST_TS, s.LAST_TS,
                    s.REQUESTS, s.TOKENS, s.CREDITS);
            loaded := loaded || 'ai_functions ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected', CURRENT_ROLE();
                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm swallow unchanged)
        END;


        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        -- V066 #11 FRESHNESS ADVANCES ON FAILURE (DAILY scope): same token-gated stamp.
        -- Only posture / AI sources whose arm loaded advance; FACT_AI_USAGE_DAILY collapses
        -- its two arms (ai_code, ai_functions) to one row via GROUP BY so the MERGE matches
        -- its target exactly once.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,
                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f
            JOIN (
                SELECT SOURCE_NAME, TOKEN FROM VALUES
                    ('MART_SECURITY_POSTURE_DAILY', 'posture'),
                    ('FACT_AI_USAGE_DAILY', 'ai_code'),
                    ('FACT_AI_USAGE_DAILY', 'ai_functions')
                    AS srcmap(SOURCE_NAME, TOKEN)
            ) m ON m.SOURCE_NAME = f.SOURCE_NAME
            -- V066 #23 AI FRESHNESS PARTIAL: FACT_AI_USAGE_DAILY has TWO independent arms
            -- (ai_code + ai_functions) mapped to the ONE physical source. The #11 per-token
            -- WHERE ARRAY_CONTAINS stamped the whole source fresh as soon as a SINGLE arm's
            -- token reached :loaded, so a half-loaded AI source read green. Gate the whole
            -- group: stamp a source only when EVERY one of its tokens loaded (both AI arms,
            -- or the lone posture arm). A partial AI load leaves the prior stamp standing, so
            -- the source reads as not-loaded-this-run (same treatment #11 gives a failed arm).
            GROUP BY f.SOURCE_NAME
            HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = s.STATUS,
            -- V167 (R1-016): the AI fact's loaded-from watermark. FACT_AI_USAGE_DAILY holds only days that
            -- had usage, so MIN(DAY) is the first AI use, not how far back this loader reached, and the
            -- app gate read it as the latter (blank 180d / 365d / Current-year AI panels). The HAVING above
            -- stamps the row only when BOTH AI arms loaded. Record the earliest whole day such a run covered
            -- (today - d + 1), kept as the deepest reach ever (LEAST): the daily d=3 run never narrows it.
            COVERAGE_FROM = IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY',
                                LEAST(COALESCE(t.COVERAGE_FROM, DATEADD('day', -:d + 1, CURRENT_DATE())),
                                      DATEADD('day', -:d + 1, CURRENT_DATE())),
                                t.COVERAGE_FROM)
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS,
                IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY', DATEADD('day', -:d + 1, CURRENT_DATE()), NULL));

    END IF;

    -- V066 #10 FALSE SUCCESS: the terminal RETURN used to always claim the marts loaded,
    -- even when an arm's EXCEPTION handler swallowed a failure and continued. Return a
    -- machine-readable verdict from the REQUIRED / OPTIONAL failure counters instead.
    IF (req_fail = 0) THEN
        RETURN 'MARTS OK (' || :SCOPE || ', ' || :d || 'd): ' || :loaded
               || IFF(:opt_fail > 0, '[' || :opt_fail || ' optional failed]', '');
    END IF;
    RETURN 'MARTS WITH ERRORS: ' || :req_fail || ' required, ' || :opt_fail || ' optional ('
           || :SCOPE || ', ' || :d || 'd): ' || :loaded;
END;
$$;

-- >>> derived:SP_NIGHTLY_RECONCILE  (from V064; R2-018 token-gated mark-and-sweep of the four wide-edge tables after the marts reload instead of DELETE-first, V167)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    rv VARCHAR;             -- V064 #9: each child loader's RETURN verdict, captured per CALL
    fails INT DEFAULT 0;    -- children whose RETURN carried a WITH ERRORS/FAIL token
    recon_start TIMESTAMP_NTZ;  -- V064 #9: run start, to scope the failure-log count
    logged_fails INT DEFAULT 0; -- APP_ERROR_LOG failure rows written during this run
BEGIN
    -- V064 #9: most child loaders SWALLOW arm failures (log a '%_failed%' row to
    -- APP_ERROR_LOG, then return a benign 'loaded' string), so a per-CALL return-string
    -- check catches only the two machine-verdict children. The AUTHORITATIVE signal is
    -- the count of failure rows the children logged during this run -- captured below.
    recon_start := CURRENT_TIMESTAMP();
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
    -- V167 (R2-018): MART_WAREHOUSE_EFFICIENCY_DAILY, FACT_QUERY_ROLE_HOURLY, FACT_QUERY_SCHEMA_HOURLY and
    -- MART_TASK_GRAPH_DAILY are no longer DELETEd here: they are mark-and-swept after the marts CALL below.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_COST_ALLOCATION_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY
     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());

    -- Pull the watermarks back so the loaders re-cover the window.
    -- V064 rec7: the daily loader now keeps FOUR per-source marks, not one
    -- shared DAILY_FACTS mark -- rewind all four here or SP_LOAD_DAILY_FACTS
    -- reads a current mark and the nightly daily re-coverage silently no-ops.
    UPDATE DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS
       SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
           UPDATED_AT = CURRENT_TIMESTAMP()
     WHERE SOURCE IN ('QH_EXTRACT', 'HOURLY_FACTS',
                      'FACT_METERING_DAILY', 'FACT_TASK_DAILY',
                      'FACT_LOGIN_DAILY', 'FACT_STORAGE_DAILY');

    -- V064 #4-drop ANALYSIS (KEEP -- verified NOT redundant): TASK_LOAD_DAILY runs
    -- SP_LOAD_DAILY_FACTS first (root, ~1 day back from its live watermark), then
    -- TASK_NIGHTLY_RECONCILE runs AFTER it, rewinds the four daily marks to -3d, and
    -- DELETEs FACT_METERING_DAILY D-3 (above). The re-CALL of SP_LOAD_DAILY_FACTS below
    -- is the RE-COVER step, NOT a duplicate of the root load: it repopulates the metering
    -- rows this proc just DELETEd AND re-reads the D-2/D-3 window (rewound watermark) for
    -- late-arriving ACCOUNT_USAGE data the 1-day-back root run cannot see. Dropping it
    -- would leave a permanent metering gap. Redundant? NO -> kept.
    --
    -- TODO(#7 staging-swap): the DELETE(D-2/D-3)-then-reload here is NOT atomic -- a child
    -- failure between the DELETEs (above) and these CALLs leaves a gap that the next hourly run
    -- (extract-fed D-2 marts) or the held per-source watermark (daily facts) re-covers. V167
    -- (R2-018): that did NOT hold for the four tables whose reload edge is wider than the hourly
    -- task's ('HOURLY', 2) -- MART_WAREHOUSE_EFFICIENCY_DAILY + MART_TASK_GRAPH_DAILY (D-3) and
    -- FACT_QUERY_ROLE_HOURLY + FACT_QUERY_SCHEMA_HOURLY (~now-3d): only this proc re-loads that
    -- edge, so one failed arm lost it for good. Those four are no longer DELETEd up front; they
    -- are MARK-AND-SWEPT after the SP_LOAD_MARTS_V27 CALL below.
    -- The robust fix (build-into-staging + atomic SWAP, or one
    -- transaction per fact family wrapping delete+reload) is DEFERRED: the reloaders are
    -- separate procs that contain their OWN BEGIN TRANSACTION/COMMIT (SP_LOAD_DAILY_FACTS,
    -- SP_LOAD_QH_EXTRACT) and DDL that autocommits (SP_LOAD_MARTS_V27 does CREATE OR
    -- REPLACE TEMPORARY TABLE), so they cannot run inside a single reconcile-owned
    -- transaction without committing it early or breaking the per-source watermark rewind.
    -- A correct deferral beats a broken reconcile; the #9 verdict below makes any
    -- mid-reconcile child failure LOUD so a gap never passes silently.
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(0);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_HOURLY_FACTS();
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;
    -- V167 (R2-018) MARK-AND-SWEEP. Every SP_LOAD_MARTS_V27 MERGE arm stamps LOAD_TS = CURRENT_TIMESTAMP()
    -- on UPDATE and the column DEFAULTs to it on INSERT, so a row this reload wrote has LOAD_TS >=
    -- recon_start; an older row inside the reload window is a key the reload no longer produces (a
    -- vanished warehouse / pipeline / role / schema) and is swept. Each sweep runs ONLY when that arm's
    -- own :loaded token is in THIS call's verdict (rv, still the marts verdict here), so a failed arm keeps
    -- its previous rows (stale but present) instead of leaving a D-3 hole no later run refills. The hour
    -- bound is the [3] / [4] arms' real lower edge GREATEST(D-3, ext_lo_hour); an empty extract gives NULL
    -- and sweeps nothing.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE())
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('wh_eff'::VARIANT, SPLIT(:rv, ' '));
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE())
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('graphs'::VARIANT, SPLIT(:rv, ' '));
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY
     WHERE HOUR_TS >= (SELECT GREATEST(DATEADD('day', -3, CURRENT_DATE()),
                              DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))))
                       FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('role_hr'::VARIANT, SPLIT(:rv, ' '));
    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY
     WHERE HOUR_TS >= (SELECT GREATEST(DATEADD('day', -3, CURRENT_DATE()),
                              DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), DATE_TRUNC('hour', MIN(START_TIME))))
                       FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)
       AND LOAD_TS < :recon_start
       AND ARRAY_CONTAINS('schema_hr'::VARIANT, SPLIT(:rv, ' '));
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;

    -- V064 #9: the AUTHORITATIVE failure signal. Every child loader logs a '%_failed%'
    -- row to APP_ERROR_LOG when an arm fails (fact_load_failed, mart_load_failed,
    -- extract_load_failed, cloud_svc_mart_failed, object_cost_load_failed, ...), INCLUDING
    -- the ones that swallow the error and return a benign 'loaded' string -- so a
    -- return-string check alone would miss 4 of the 5 children. Count what was logged
    -- during THIS run; the per-CALL return check above is a secondary catch for the two
    -- machine-verdict children (SP_LOAD_DAILY_FACTS, SP_LOAD_MARTS_V27).
    -- V064 #6 RUN SCOPING: count ONLY this reconcile's OWN child-loader failures. The
    -- prior filter counted every '%_failed%' row account-wide in the run window, so a
    -- CONCURRENT AlertScan 'rule_block_failed', a NotifyWebhook 'route_send_failed', an
    -- ObjectCost load, or any app-page error logged in the same minutes was mis-attributed
    -- to the reconcile and made an OK run report WITH ERRORS. APP_ERROR_LOG has NO
    -- session/query-tag column to fence on (its columns are LOGGED_AT, PAGE, ERROR_TYPE,
    -- ERROR_MESSAGE, CONTEXT, ROLE_NAME -- see V001), and the children log without a run
    -- id, so the tightest CORRECT filter is PAGE. The five children that swallow+log write
    -- exactly three pages: ExtractLoader (SP_LOAD_QH_EXTRACT + its inner
    -- SP_LOAD_CLOUD_SVC_MART), DailyFacts (SP_LOAD_DAILY_FACTS), MartLoader
    -- (SP_LOAD_MARTS_V27). SP_LOAD_HOURLY_FACTS + SP_LOAD_OPS_DIAG do NOT swallow -- a
    -- failure there aborts THIS proc and FAILs the task run directly. (ExtractLoader can
    -- also be written by the concurrent hourly extract task; that is still a real loader
    -- failure in the window, not a cross-subsystem mis-attribution, so counting it is
    -- acceptable -- the secondary per-CALL verdict check above catches the reconcile's own
    -- extract call regardless.)
    SELECT COUNT(*) INTO :logged_fails
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
    WHERE LOGGED_AT >= :recon_start
      AND PAGE IN ('ExtractLoader', 'DailyFacts', 'MartLoader')
      AND (ERROR_TYPE ILIKE '%_failed%' OR ERROR_TYPE ILIKE '%WITH ERRORS%');
    IF (fails > 0 OR logged_fails > 0) THEN
        RETURN 'RECONCILE WITH ERRORS: ' || :logged_fails || ' loader failure(s) logged'
            || IFF(:fails > 0, ' + ' || :fails || ' child verdict(s) non-success', '')
            || ' (metering + full-retention marts 3 days, extract-fed marts 2); inspect APP_ERROR_LOG';
    END IF;
    RETURN 'RECONCILE OK - nightly reconcile complete (metering + full-retention marts 3 days, extract-fed marts 2); no child loader failures logged';
END;
$$;

-- >>> derived:SP_LOAD_PATTERN_COST  (from V120; R2-010 atomic DELETE + INSERT of the window instead of the COMPANY-keyed MERGE, + the PATTERN-RESTAMP COVERAGE_FROM stamp, V167)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(DAYS_BACK FLOAT)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    lo DATE;   -- V167 (R2-010 D1): ONE lower bound for the DELETE and both source filters
BEGIN
    lo := DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE());
    -- V167 (R2-010 D2): COMPANY is COMPANY_FOR_WAREHOUSE() at load time and part of the grain, so the
    -- V037-V120 MERGE could not match a row whose warehouse was re-mapped: it INSERTed a second row
    -- under the new company and kept the old one, and the ALL scope summed both. Replace the window
    -- atomically instead (the SP_LOAD_OBJECT_COST idiom, V139): a failed INSERT rolls the DELETE back
    -- and readers keep the previous fill. COMPANY stays in the grain (the mart has no WAREHOUSE_NAME,
    -- so one DAY / HASH / DB can legitimately span companies). No DDL inside the transaction.
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
     WHERE DAY >= :lo;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
        (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)
        SELECT m.DAY, m.QUERY_HASH, m.COMPANY, m.DATABASE_NAME,
               SUM(m.RUNS) AS RUNS,
               SUM(m.CREDITS_ATTRIBUTED) AS CREDITS_ATTRIBUTED,
               HLL_COMBINE(m.USERS_HLL) AS USERS_HLL
        FROM (
            SELECT g.DAY, g.QUERY_HASH, g.DATABASE_NAME,
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                   g.RUNS, g.CREDITS_ATTRIBUTED, g.USERS_HLL
            FROM (
                SELECT CAST(q.START_TIME AS DATE) AS DAY,
                       q.QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                       COALESCE(q.WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                       COALESCE(q.DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COUNT(*) AS RUNS,
                       SUM(a.CREDITS_ATTRIBUTED) AS CREDITS_ATTRIBUTED,
                       HLL_ACCUMULATE(q.USER_NAME) AS USERS_HLL
                -- loader-01 (round 7): pre-aggregate QUERY_ATTRIBUTION_HISTORY to ONE row per
                -- QUERY_ID before joining QUERY_HISTORY. QAH emits multiple rows for a query that
                -- spans hour boundaries, so the old direct a x q join fanned one query into N rows
                -- and COUNT(*) counted attribution rows, inflating RUNS (and halving CREDITS_PER_RUN)
                -- for exactly the long-running patterns this mart exists to surface. Matches the
                -- per-QUERY_ID pre-aggregation every sibling loader uses (V067/V077/V113).
                FROM (
                    SELECT QUERY_ID,
                           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0)
                               + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS_ATTRIBUTED
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                    WHERE START_TIME >= :lo
                    GROUP BY QUERY_ID
                ) a
                JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                  ON q.QUERY_ID = a.QUERY_ID
                 AND q.START_TIME >= :lo
                WHERE q.QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY 1, 2, 3, 4
            ) g
        ) m
        GROUP BY 1, 2, 3, 4;
    COMMIT;

    -- V068: loader-owned freshness stamp (V041-R6 pattern; this standalone-task loader
    -- was missed in the V041 handoff, freezing its SOURCE_FRESHNESS_STATE row at apply
    -- time). LAST_LOAD_TS is a RUN stamp (CURRENT_TIMESTAMP()), not MAX(LOAD_TS) of the
    -- mart, so a window with ZERO source events still reads fresh - no news is not
    -- no load.
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'MART_PATTERN_COST_DAILY' AS SOURCE_NAME, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS,
               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT,
               :lo AS COVERAGE_FROM   -- V167 (PATTERN-RESTAMP D6): the first day this run replaced
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader',
        -- V167 (PATTERN-RESTAMP D6): the deepest day an atomic reload ever replaced, NULL-safe both ways
        -- (a NULL DAYS_BACK keeps the old stamp). The app lifts its 90-day pattern cap only for a read
        -- window whose first day is on or after it. Reached only after the COMMIT (a failure re-raises).
        COVERAGE_FROM = LEAST(COALESCE(t.COVERAGE_FROM, s.COVERAGE_FROM), COALESCE(s.COVERAGE_FROM, t.COVERAGE_FROM))
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader', s.COVERAGE_FROM);
    RETURN 'OK';
EXCEPTION   -- V167 (R2-010 D5): never leave the window deleted; re-raise so the task still fails visibly
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- R2-010 one-time repair (after the atomic loader is in place): drop the stale-company twins the COMPANY-keyed
-- MERGE left behind (the V037 -> V044 -> V047 era, plus any Apply-mapping remap since). Scan-free: reads only the
-- mart. A group's legitimate company rows share their last covering run's LOAD_TS; the 10-minute slack absorbs
-- one load statement. Groups of one row are never touched.
DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
USING (
    SELECT DAY, QUERY_HASH, DATABASE_NAME, MAX(LOAD_TS) AS NEWEST_TS
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
    GROUP BY DAY, QUERY_HASH, DATABASE_NAME
    HAVING COUNT(*) > 1
) g
WHERE t.DAY = g.DAY AND t.QUERY_HASH = g.QUERY_HASH AND t.DATABASE_NAME = g.DATABASE_NAME
  AND t.LOAD_TS < DATEADD('minute', -10, g.NEWEST_TS);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 167 AS VERSION,
       'Mart-loader window edges, the AI coverage watermark and the atomic pattern reload (R2-015, R2-014, R2-052 (also R2-017), R1-016, R2-018, R2-010, PATTERN-RESTAMP). SOURCE_FRESHNESS_STATE gains a nullable COVERAGE_FROM DATE. SP_LOAD_MARTS_V27 (re-derived from V159): arm [1] pads its query-span source one day back with an END_TIME floor, so a query crossing the window edge marks its hours active (idle no longer overstated); arm [6] reads one lead-in day and keeps only runs whose first attempt is in the window (no phantom child-named pipeline rows); arm [9] keys Cortex Code DAY and FIRST_TS/LAST_TS in Central (USAGE_TIME is TIMESTAMP_TZ); the DAILY freshness MERGE stamps COVERAGE_FROM for FACT_AI_USAGE_DAILY when both AI arms loaded (deepest reach, -d+1). SP_NIGHTLY_RECONCILE (re-derived from V064) no longer DELETEs MART_WAREHOUSE_EFFICIENCY_DAILY, MART_TASK_GRAPH_DAILY, FACT_QUERY_ROLE_HOURLY and FACT_QUERY_SCHEMA_HOURLY up front; it sweeps rows the reload did not re-stamp (LOAD_TS before the run) only when that arm loaded, so a failed arm no longer leaves a permanent D-3 hole. SP_LOAD_PATTERN_COST (re-derived from V120) replaces its window atomically (DELETE + INSERT, ROLLBACK + RAISE) instead of a COMPANY-keyed MERGE that left a stale-company twin after a COMPANY_SCOPE remap, and stamps COVERAGE_FROM. One-time in-migration repair: a scan-free DELETE of MART_PATTERN_COST_DAILY rows older than their group newest LOAD_TS by more than 10 minutes. No task, rule, grant or view change; nothing is CALLed at apply time. The heavy reloads (AI DAILY 365 reload-then-prune, pattern 364, HOURLY N, the arm [6] 364-day rebuild) are owner-run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 167);

-- =====================================================================
--  STAGE 2 -- MIGRATION 3 of 7 -- APPLY V168 (idempotent; GUARDS on V167). Source: snowflake/migrations/V168__alert_scan_hourly_keys_and_sweeps.sql
-- =====================================================================

-- V168__alert_scan_hourly_keys_and_sweeps.sql
--
-- Round-2 review, alerts cluster (R2-034, R2-035, R2-036, R2-039, R2-040, R2-091): hourly alert keys and sweeps.
--
-- WHY: (R2-034) the V091 auto-clear sweep only re-checked PERF events raised in the last 48h (V096's bound), so a
-- condition that held for several days, or sat in the hysteresis band past 48h, left its older day-keyed events
-- OPEN forever. (R2-035) PIPE_COPY_FAILURES counted a rolling 24h but keyed on the SCAN day, so the files that
-- failed yesterday afternoon raised a second event after midnight. (R2-036) SEC_NEW_ADMIN_NETWORK keyed on
-- user|IP with no date, so a network quiet for 90+ days -- which the rule name, playbook and Security panel promise
-- to re-flag -- never alerted again. (R2-039) the same arm counted failed attempts as logins and always said
-- 'logged in', and a failures-only event blocked the success that followed it. (R2-091) SEC_NEW_EXPOSURE pointed
-- at Security -> Access, where no PUBLIC-grant panel exists. (R2-040) two dead prologue reads. (Holistic #4/#9)
-- the OPS_PIPELINE_DEGRADED ERR DETAIL told every logged loader failure 'its task still reads SUCCEEDED', but V166's
-- SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back and re-raise, so TASK_HISTORY shows those runs FAILED.
--
--   ~ SP_ALERT_SCAN re-derived from V162 (its current definer), byte-identical except:
--     ~ arm [14] PIPE_COPY_FAILURES: keyed by the Central FAILURE day over whole Central days (yesterday + today,
--       a -50h prune); the TITLE names the day; the |CRIT|/|WARN| band and trailing |YYYY-MM-DD stay (the V067
--       supersede and V117 carry-forward parse them), so a day's event only escalates, never re-raises.
--     ~ arm [18] SEC_NEW_ADMIN_NETWORK: SUCCESSES = COUNT_IF(IS_SUCCESS = 'YES'); the TITLE says 'logged in' only
--       when one succeeded, else 'N failed login attempt(s) from new network <IP> (0 successful)'; DETAIL says
--       'successful S of N attempt(s)'. Key = user|IP[|FAILED]|first-seen Central day (user cut to 200 chars,
--       at most 286 chars); a 48h same-episode guard on the EXACT date-stripped base (never a STARTSWITH, which
--       would let a failures-only key swallow the success); the three-role list, 90d baseline and 24h first-seen
--       window are unchanged.
--     ~ arm [20] SEC_NEW_EXPOSURE DETAIL: review in Security -> Changes (Recent grant changes).
--     ~ V067 supersede sweep: a failures-only SEC_NEW_ADMIN_NETWORK event resolves (SUPERSEDED) once the same user
--       + IP success event opens within 48h of it.
--     ~ V091 auto-clear sweep: no RAISED_AT lower bound -- every OPEN PERF_QUERY_FAIL_PCT / PERF_QUEUED_MINUTES /
--       PERF_SPILL_GB event is re-checked on its date-stripped identity (the 1h dwell, the still-firing NOT IN and
--       the three -24h windows are unchanged).
--     - the dead budget_usd / ai_credit_price prologue reads (arm [17] keeps :credit_price; the daily scan keeps
--       its own copies).
--     ~ arm [22] OPS_PIPELINE_DEGRADED ERR leg: errs carries RERAISED (a PAGE 'AppCost' / 'StorageTruth' row, the
--       V166 loaders that roll back, log and re-raise); the DETAIL says that run FAILED (a scheduled run shows
--       FAILED in TASK_HISTORY; a hand CALL raised the error to its caller), and keeps 'returned normally, so its
--       task still reads SUCCEEDED' for every other loader. Keys, sources, cadence gate and windows unchanged;
--       byte-identical to V169's daily twin.
--     ~ the RETURN label names V168; the 14-block tally is unchanged.
--   ~ ALERT_CONFIG NAME of PIPE_COPY_FAILURES and SEC_NEW_ADMIN_NETWORK, only while it still equals the seed text.
--
-- COST: unchanged in kind. Arm [14] prunes COPY_HISTORY at 50h instead of 24h; arm [18] adds one COUNT_IF over rows
-- it already reads and a 48h ALERT_EVENTS probe bounded by RULE_ID; the sweeps add predicates only.
-- LATENCY: hourly, as before.
-- FIRST RUN: the first hourly scan after the apply auto-clears every stranded OPEN PERF event whose scope is now
-- below its CLEAR threshold (PREFLIGHT P168.1 counts them; nothing is written at apply time). A failures-only or
-- success pair first seen in the last 24h does not re-raise (the guard reads its V162 undated key). The apply day
-- can raise one legitimate CRIT|<yesterday> PIPE_COPY_FAILURES event for a day whose full count first reaches 10.
-- Two apply-window suppressions, once each: (1) PIPE_COPY_FAILURES -- a table whose yesterday failures V162 re-raised
-- after midnight under today's date (title '... (24h)', the R2-035 carry-over) folds the apply day's new failures of
-- the same band into that event, so nothing pages for them that day unless they cross into the other band; PREFLIGHT
-- P168.2 flags those keys (HELD_BY_A_V162_EVENT). (2) SEC_NEW_ADMIN_NETWORK -- the guard cannot tell a V162 undated
-- event's outcome, so a success that follows a failures-only V162 event inside its 48h does not raise its own event
-- (R2-039's separate success event holds from the first V168 key on); PREFLIGHT P168.4 (second grid) lists the
-- candidates. Standing (as under V162): within one failure day, a new failure that lands after an operator resolved
-- that day's PIPE_COPY_FAILURES event stays suppressed until the next Central day, unless it crosses WARN -> CRIT.
-- ROLLBACK: re-run V162's SP_ALERT_SCAN (V162__security_takeover_admin_grant.sql, the second CREATE PROCEDURE). A
-- pair raised under V168 in the last 24h may raise once more under the undated key; the NAME text can stay.
-- Apply AFTER V167. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20168, 'V168 requires V167 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 167) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN  (from V162; [14] failure-day key, [18] outcome + first-seen-day key + 48h episode guard, [20] pointer, V067 FAILED supersede, V091 sweep any raise day, dead prologue reads, [22] ERR re-raise wording, V168)
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
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
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
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
               OR (e.RULE_ID = b.RULE_ID
                   AND e.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
                   AND (e.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')
                        OR (LENGTH(e.DEDUPE_KEY) = LENGTH(b.DEDUPE_KEY)
                            AND LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - 10)
                                = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10))))
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

-- Rule NAME text follows the new behaviour. Each refresh touches the row only while NAME still equals its seed (V011 /
-- V043), so an operator's own edit survives; a re-run is a no-op. ALERT_CONFIG has no DESCRIPTION column.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = 'Failed COPY / Snowpipe file loads per Central failure day (threshold = allowed failures)'
 WHERE RULE_ID = 'PIPE_COPY_FAILURES'
   AND NAME = 'Failed COPY / Snowpipe file loads in 24h (threshold = allowed failures)';

UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = 'Admin login attempt from a network unseen in 90 days (the title says whether any succeeded)'
 WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
   AND NAME = 'Admin login from a network unseen in 90 days';

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 168 AS VERSION,
       'Round-2 review, alerts cluster (R2-034, R2-035, R2-036, R2-039, R2-040, R2-091). SP_ALERT_SCAN re-derived from V162, byte-identical except: arm [14] PIPE_COPY_FAILURES keyed by the Central failure day over whole Central days (yesterday and today), TITLE names the day, band and trailing date kept; arm [18] SEC_NEW_ADMIN_NETWORK counts SUCCESSES, says logged in only when one succeeded (else N failed login attempts, 0 successful), keys on user, IP, FAILED for a failures-only pair, and the first-seen Central day (a network quiet 90 days alerts again), with a 48h same-episode guard on the exact date-stripped base; arm [20] SEC_NEW_EXPOSURE DETAIL points at Security, Changes; the V067 sweep supersedes a failures-only SEC_NEW_ADMIN_NETWORK event once the success event opens; the V091 auto-clear sweep re-checks every OPEN PERF event whatever its raise day; the dead budget and AI-price prologue reads are gone; the OPS_PIPELINE_DEGRADED ERR detail says a run of the V166 app-cost or storage-truth loader rolled back and FAILED instead of claiming its task still reads SUCCEEDED; RETURN names V168, tally 14 unchanged. ALERT_CONFIG NAME of PIPE_COPY_FAILURES and SEC_NEW_ADMIN_NETWORK refreshed only while it equals the seed text. No task change, no new object, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168);

-- =====================================================================
--  STAGE 2 -- MIGRATION 4 of 7 -- APPLY V169 (idempotent; GUARDS on V168). Source: snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql
-- =====================================================================

-- V169__alert_scan_daily_windows_and_keys.sql
--
-- Round-2 review, alerts cluster (R2-041, R2-042, R2-103, R2-044, R2-020 = R2-043, R2-047, R1-071, R1-233): the
-- nightly scan's windows and keys.
--
-- WHY: (R2-041) COST_BUDGET_PACE compared a month-to-date total that includes the partial current day with a
-- completed-days allowance, so an on-budget month raised a false HIGH on days 2-5; COST_FORECAST_BREACH never
-- projected the rest of today. (R2-042) COST_CONTRACT_BREACH ignored CONTRACT_END_DATE: credits after the term
-- counted, and an exhaustion projected past the end (or a term already over) paged CRITICAL while the Contract
-- tab said the term is fine or over. (R2-103) with credits set and no start date the arm fabricated a runway from
-- about 0 consumed. (R2-044) COST_STORAGE_SURGE paired a dropped or re-created database with the live one by name.
-- (R2-020 / R2-043) DQ_RECON_ERROR keyed on the scan date, so a break in the 48h window paged twice. (R2-047)
-- COST_EGRESS_SPIKE named the destination of the largest single row in 14 days, counted same-region internal
-- moves, and its rolling window never counted the ~2h before each scan. (R1-071) COST_IDLE_OPPORTUNITY skipped a
-- never-suspend warehouse (SHOW reports a NULL timer). (R1-233) SEC_TRUST_REGRESSION fired on unchanged counts at
-- a threshold of 0. (Holistic #4/#9) the OPS_PIPELINE_DEGRADED ERR DETAIL told every logged loader failure 'its task
-- still reads SUCCEEDED', but V166's SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back and re-raise, so
-- TASK_HISTORY shows those runs FAILED.
--
--   ~ SP_ALERT_SCAN_DAILY re-derived from V163 (its current definer), byte-identical except:
--     ~ both mtd CTEs + MTD_COMPLETE_USD (DAY < today, the two-partition pricing): [08] pace and [09] forecast use
--       it; [09] projects the remaining days INCLUDING today. TITLE / DETAIL say through yesterday.
--     ~ [16]: TOTAL only with a parsable CONTRACT_START_DATE; CONSUMED within [start, CONTRACT_END_DATE) (end
--       exclusive); TERM_END; no event once today >= the end or when the exhaustion falls on or after it. The
--       burn window, bands, key, severity and METRIC_VALUE (DAYS_LEFT) are unchanged.
--     ~ [12]: LAG / ROW_NUMBER per DATABASE_ID over rows with DELETED IS NULL (title, key, predicate unchanged).
--     ~ [18]: the r CTE carries MAX(LATEST_LOAD) AS NEWEST_LOAD; key = RULE|<newest error-cycle date>.
--     ~ [19]: the previous complete Central day of TRUE egress (TARGET_REGION or TARGET_CLOUD set), top
--       destination = the largest per-region total of that day; TITLE 'Egress N GB on <day> (14d avg ...)'.
--     ~ [24]: a NULL snapshot timer reads as 0 (never suspends); [29]: GREATEST(COALESCE(THRESHOLD_NUM, 1), 1).
--     ~ [22] OPS_PIPELINE_DEGRADED ERR leg: errs carries RERAISED (a PAGE 'AppCost' / 'StorageTruth' row, the V166
--       loaders that roll back, log and re-raise); the DETAIL says that run FAILED (a scheduled run shows FAILED
--       in TASK_HISTORY; a hand CALL raised the error to its caller), and keeps 'returned normally, so its task
--       still reads SUCCEEDED' for every other loader. Keys, sources and windows unchanged; byte-identical to
--       V168's hourly twin.
--     ~ the RETURN label names V169; the 14-block tally is unchanged.
--   ~ ALERT_CONFIG NAME of COST_EGRESS_SPIKE, only while it still equals the V043 seed text.
--
-- COST: unchanged in kind: two extra conditional SUMs over the month of FACT_METERING_DAILY, two SETTINGS
-- sub-selects, a 16-day DATA_TRANSFER_HISTORY read grouped by day (was 14 days ungrouped).
-- LATENCY: daily (~07:00 Central). An egress spike now pages the next morning (the previous complete day).
-- FIRST RUN: the next daily scan. Expect: no COST_BUDGET_PACE on days 2-5 of an on-budget month; COST_FORECAST_BREACH
-- slightly more sensitive (it now projects today); COST_IDLE_OPPORTUNITY may raise for never-suspend warehouses for
-- the first time (PREFLIGHT P169.6 lists them); a contract past its end or outlasting it goes quiet; one egress
-- spike confined to 00:00-07:00 of yesterday can raise once more. Nothing runs at apply time.
-- DQ_RECON_ERROR keeps two residuals of its cycle-day key. (a) Once, at the transition: a pre-V169 scan keyed the
-- SCAN date, so a failing cycle that loads later on a date the old scan already keyed (A 21:00 after the A ~07:00
-- scan wrote DQ_RECON_ERROR|A) folds into that older event and is not paged; V163 would have paged it as |A+1.
-- (b) Standing: a failing re-run that loads on a date whose event already exists (after that morning's scan)
-- folds into that date's event and does not page the next morning -- one page per failing cycle date, the
-- Reconciliation errors panel's cycle definition. PREFLIGHT P169.4 shows the newest load and its hour; PART B
-- V169.4 (the morning after the first scan) flags a newest cycle covered only by an event raised before it loaded.
-- ROLLBACK: re-run V163's SP_ALERT_SCAN_DAILY (V163__ai_runaway_trust_regression.sql, the CREATE PROCEDURE); the
-- NAME text can stay.
-- Apply AFTER V168. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20169, 'V169 requires V168 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 168) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN_DAILY  (from V163; [08]/[09] complete-day MTD, [16] contract start gate + end bound, [12] live DATABASE_ID, [18] error-cycle-day key, [19] previous-day true egress, [24] NULL timer as 0, [29] threshold floor, [22] ERR re-raise wording, V169)
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
            -- V169 (R2-041): month-to-date over COMPLETE days only (DAY < today), the numerator of
            -- DAILY_RATE_USD. MTD_USD also holds the partial UTC row of today (the ~06:45 load sees part of
            -- it), which the completed-days pace allowance ((DAY_OF_MONTH - 1) / DAYS_IN_MONTH) never budgets.
            SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_COMPLETE_USD,
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
               'MTD spend through yesterday $' || ROUND(m.MTD_COMPLETE_USD, 0) || ' is ' ||
                   ROUND(m.MTD_COMPLETE_USD / NULLIF(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 2) ||
                   'x the budget pace',
               'Budget $' || ROUND(:budget_usd, 0) || '/mo; elapsed-share allowance $' ||
                   ROUND(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0) || ' for ' || (m.DAY_OF_MONTH - 1)
                   || ' complete day(s); the partial metering of today is not counted.',
               m.MTD_COMPLETE_USD,
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_BUDGET_PACE'
         AND :budget_usd > 0
         AND m.DAY_OF_MONTH > 1
         AND m.MTD_COMPLETE_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM

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
            -- V169 (R2-041): month-to-date over COMPLETE days only (DAY < today), the numerator of
            -- DAILY_RATE_USD. MTD_USD also holds the partial UTC row of today (the ~06:45 load sees part of
            -- it), which the completed-days pace allowance ((DAY_OF_MONTH - 1) / DAYS_IN_MONTH) never budgets.
            SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%') THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_COMPLETE_USD,
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
                   ROUND(m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1), 0) ||
                   ' exceeds budget $' || ROUND(:budget_usd, 0),
               'MTD through yesterday $' || ROUND(m.MTD_COMPLETE_USD, 0) || ' + $' || ROUND(m.DAILY_RATE_USD, 0) ||
                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1) || ' remaining days incl. today.',
               m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1),
               c.RULE_ID || '|ALL|' || CURRENT_DATE()
        FROM cfg c
        JOIN mtd m
          ON c.RULE_ID = 'COST_FORECAST_BREACH'
         AND :budget_usd > 0
         AND (m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1))
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
        -- V169 (R2-042, R2-103): TOTAL counts only once CONTRACT_START_DATE parses (the app twin
        -- mart_sql.contract_exhaustion, r33); CONSUMED counts [start, CONTRACT_END_DATE) -- the end is EXCLUSIVE,
        -- the app contract_pace clock -- and nothing raises once CURRENT_DATE() >= the end or when the projected
        -- exhaustion falls on or after it. A blank end keeps the unbounded pre-V169 behaviour.
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
            SELECT TOTAL, CONSUMED, DAILY_BURN, TERM_END,
                   CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)) AS DAYS_LEFT,
                   DATEADD('day', CEIL((TOTAL - CONSUMED) / NULLIF(DAILY_BURN, 0)),
                           CURRENT_DATE()) AS EXHAUST_DATE
            FROM (
                SELECT
                    (SELECT IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,
                                COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0))
                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TOTAL,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY >= COALESCE(
                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL)))
                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), CURRENT_DATE())
                       AND DAY < COALESCE(
                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))
                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), '9999-12-31'::DATE)) AS CONSUMED,
                    (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))
                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TERM_END,
                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)
                     FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                     WHERE DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())
                                   AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN
            )
        ) p ON c.RULE_ID = 'COST_CONTRACT_BREACH'
           AND p.TOTAL > 0 AND p.DAILY_BURN > 0
           AND p.DAYS_LEFT <= c.THRESHOLD_NUM
           AND (p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))

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
            -- V169 (R2-044): one series per LIVE database id. A dropped or re-created predecessor keeps
            -- reporting rows (DELETED set) under the same DATABASE_NAME while its Time Travel and Fail-safe
            -- bytes remain, and a by-name window paired the two ids on the same day arbitrarily. A re-created
            -- database has no PREV on its first day and does not raise.
            SELECT DATABASE_NAME, USAGE_DATE,
                   AVERAGE_DATABASE_BYTES / POWER(1024, 3) AS CUR_GB,
                   LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_ID ORDER BY USAGE_DATE)
                       / POWER(1024, 3) AS PREV_GB,
                   (AVERAGE_DATABASE_BYTES
                    - LAG(AVERAGE_DATABASE_BYTES) OVER (PARTITION BY DATABASE_ID ORDER BY USAGE_DATE))
                       / POWER(1024, 3) AS GROWTH_GB
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
              AND DELETED IS NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_ID ORDER BY USAGE_DATE DESC) = 1
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
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        xfer AS (
            -- V169 (R2-047): one row per Central day and destination. TRUE egress only -- the predicate of the
            -- Security > Egress drill (security_sql.egress_baseline): a same-region internal transfer moves no
            -- data out of the account.
            SELECT CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE AS DAY,
                   COALESCE(TARGET_REGION, '(same region)') AS DEST,
                   SUM(BYTES_TRANSFERRED) AS BYTES
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -16, CURRENT_TIMESTAMP())
              AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)
            GROUP BY 1, 2
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'Egress ' || eg.GB_DAY || ' GB on ' || TO_VARCHAR(eg.SPIKE_DAY) || ' (14d avg ' || eg.GB_AVG_14D || ' GB/day)',
               'Top destination: ' || COALESCE(eg.TOP_REGION, '(same region)')
                   || '. Source: DATA_TRANSFER_HISTORY - drill in Security -> Egress.',
               eg.GB_DAY,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN (
            -- V169 (R2-047): the previous COMPLETE Central day. The scan runs ~06:50-07:30 Central, past the ~2h
            -- view latency, so consecutive daily windows tile the timeline (no blind slice before each scan, no
            -- task-jitter gap or overlap); the top destination is the largest per-region total of that day.
            SELECT MAX(DATEADD('day', -1, k.TODAY)) AS SPIKE_DAY,
                   ROUND(SUM(IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, 0)) / POWER(1024, 3), 1) AS GB_DAY,
                   ROUND(SUM(IFF(x.DAY >= DATEADD('day', -14, k.TODAY), x.BYTES, 0)) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(x.DEST, IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, NULL)) AS TOP_REGION
            FROM xfer x
            CROSS JOIN clk k
            WHERE x.DAY < k.TODAY
        ) eg
          ON c.RULE_ID = 'COST_EGRESS_SPIKE'
         AND eg.GB_DAY >= c.THRESHOLD_NUM

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
    -- [24] COST_IDLE_OPPORTUNITY (V157, Next-Fifty #13: weekly idle-waste push closing the loop alert -> one
    --      ALTER -> change scan + SP_LEDGER_AUTOBOOK. DB-side twin of the Cost Intelligence > Optimization &
    --      Savings > Idle & sizing ACTIONABLE figure (insights.idle_advisor + remediation.tighten_suspend_plan):
    --      FLAGGED = >=20% idle AND >=1 idle credit; recoverable = idle minus one 60s resume tail per active
    --      metered hour; only a settings-VERIFIED timer (the newest WAREHOUSE_CONFIG_SNAPSHOT batch, <=36h
    --      old -- a warehouse missing from it, dropped or renamed, never raises) that is
    --      disabled (<=0) or above 60s. Trailing 14 COMPLETE Central days, run-rated over the days the mart
    --      covers (at least 7). One event per warehouse per ISO week (Monday, Central); the HIGH band (>= 5x
    --      threshold) re-fires mid-week and the V067 sweep supersedes the MED one.)
    --      V169 (R1-071): a NULL snapshot timer reads as 0 = never suspends (SHOW WAREHOUSES reports a
    --      never-suspend warehouse as a NULL auto_suspend), like insights.show_auto_suspend and the mart loader.
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
                   s.RECOVERABLE_CREDITS, COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND, w.SNAPSHOT_AT,
                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,
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
    --      V169 (R1-233): a THRESHOLD_NUM below 1 reads as 1 -- a regression is a rise; 0 raised every unchanged count.
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
         AND s.CUR_N - s.PRIOR_N >= GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)
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
                   LISTAGG(MTRC, ', ') WITHIN GROUP (ORDER BY N DESC) AS TOP_METRICS,
                   MAX(LATEST_LOAD) AS NEWEST_LOAD   -- V169 (R2-020/R2-043): the newest error cycle in the window
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
               c.RULE_ID || '|' || TO_VARCHAR(COALESCE(TO_DATE(r.NEWEST_LOAD), CURRENT_DATE()))   -- V169: one event per newest error-cycle day
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

    RETURN 'alert scan daily v6 (V169: [08]/[09] complete-day MTD, [16] contract term, [12] live database id, [18] error-cycle day, [19] previous-day true egress, [24] NULL timer, [29] floor): ' || (14 - :fails) || '/14 rule blocks ok (daily)';
END;
$$;

-- Rule NAME text follows the new window. The refresh touches the row only while NAME still equals its V043 seed, so
-- an operator's own edit survives; a re-run is a no-op. ALERT_CONFIG has no DESCRIPTION column.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = 'True egress (cross-region or cross-cloud) above threshold GB on the previous complete Central day'
 WHERE RULE_ID = 'COST_EGRESS_SPIKE'
   AND NAME = 'Outbound transfer above threshold (GB / 24h)';

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 169 AS VERSION,
       'Round-2 review, alerts cluster (R2-041, R2-042, R2-103, R2-044, R2-020, R2-043, R2-047, R1-071, R1-233). SP_ALERT_SCAN_DAILY re-derived from V163, byte-identical except: both mtd CTEs gain MTD_COMPLETE_USD (complete days only), which COST_BUDGET_PACE compares with the completed-days allowance and COST_FORECAST_BREACH projects with the remaining days including today; COST_CONTRACT_BREACH counts TOTAL only with a parsable CONTRACT_START_DATE, bounds CONSUMED by CONTRACT_END_DATE (exclusive) and raises nothing once the term is over or when the exhaustion falls on or after its end; COST_STORAGE_SURGE compares per live DATABASE_ID; DQ_RECON_ERROR keys on the newest error-cycle date; COST_EGRESS_SPIKE reads the previous complete Central day of true egress with the top destination by per-region total; COST_IDLE_OPPORTUNITY reads a NULL snapshot timer as never suspends; SEC_TRUST_REGRESSION floors its threshold at 1; the OPS_PIPELINE_DEGRADED ERR detail says a run of the V166 app-cost or storage-truth loader rolled back and FAILED instead of claiming its task still reads SUCCEEDED; RETURN names V169, tally 14 unchanged. ALERT_CONFIG NAME of COST_EGRESS_SPIKE refreshed only while it equals the seed text. No task change, no new object, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169);

-- =====================================================================
--  STAGE 2 -- MIGRATION 5 of 7 -- APPLY V170 (idempotent; GUARDS on V169; adds the 5-arg SP_INCIDENT_DECLARE). Source: snowflake/migrations/V170__incident_declare_actor_and_proposals.sql
-- =====================================================================

-- V170__incident_declare_actor_and_proposals.sql
--
-- Incident declare + proposals (bug-hunt round 2: R2-028, R2-030, R2-093, R2-031).
--
-- WHY:
--   R2-028  The manual declare (Control Room > Incidents & triage > Proposed incidents) wrote DECLARED_BY and every
--           member's LINKED_BY from their V032 default, CURRENT_USER(). SP_INCIDENT_DECLARE is EXECUTE AS OWNER and
--           the app is owner-rights SiS, so that is the app owner for every DBA. Control Room shows the column as
--           "Declared by". A NEW 5-arg overload takes P_ACTOR (the app passes identity_sql()) and writes both columns.
--   R2-030  The proc committed an incident whose members INSERT linked 0 rows (the proposal list is up to 30 s
--           stale; a sweep, another operator or a concurrent auto-declare can empty the linkable set first) and
--           returned 'DECLARED: 0 member(s) linked', which the app never read. Nothing ever attaches to or
--           mitigates such an incident. Both overloads now roll that back and return a NOOP, and the success
--           verdict becomes 'OK: declared <id> with <n> member(s) linked'.
--   R2-093  INCIDENT_PROPOSALS (V072) knew only V072-era rules and band tokens. An EXH band (COST_CONTRACT_BREACH,
--           PIPE_ETL_CYCLE_LATE) read as an entity called EXH, so the declare guard looked only for an incident
--           holding an EXH member and opened a SECOND incident for the same late night; 'ALL' read as an entity
--           too. Ten more rules whose key part 2 is a bare name get their entity kind: WAREHOUSE COST_IDLE_OPPORTUNITY,
--           COST_SLEEP_POLLING; OBJECT PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH, DQ_SCHEMA_DRIFT; USER
--           SEC_FAILED_LOGINS, SEC_LOGIN_TAKEOVER, SEC_ADMIN_GRANT, COST_AI_USER_RUNAWAY. Series-prefixed keys stay
--           SCOPE, as in V072: COST_CLOUD_SVC_ANOMALY ('CLOUD SVC <WH>') and COST_ANOMALY_SWEEP ('WAREHOUSE <WH>').
--           The declare matches the raw part 2, so ENTITY_NAME keeps the prefix and those proposals never match
--           warehouse-change evidence (they cannot reach CONFIDENCE HIGH).
--   R2-031  A PIPE_TASK_FAILURES proposal counted its OWN FACT_TASK_DAILY failures as corroboration, so every one
--           read CONFIDENCE HIGH. They now reach HIGH only with a matching task change (MEDIUM on repeat days).
--
--   ~ SP_INCIDENT_DECLARE(VARCHAR x4) re-derived from V131 (its only definer): the 0-member rollback + OK verdict.
--   + SP_INCIDENT_DECLARE(VARCHAR x5) derived from V131: the same body plus P_ACTOR -> DECLARED_BY / LINKED_BY.
--     The 4-arg is KEPT so an app that is not redeployed yet still declares (it keeps crediting the owner). The
--     app CALLs the 5-arg once SCHEMA_VERSION holds 170: while its 4 h schema cache lacks 170 it re-reads the
--     table on a 30 s tier, so the first declare after the apply already does. A later migration drops the 4-arg.
--   ~ INCIDENT_PROPOSALS re-derived from V072 (its current definer): rule kinds, EXH/ALL band tokens, and the
--     PIPE_TASK_FAILURES confidence + evidence label. Same columns, same order (the app reads SELECT *).
--
-- COST: none at apply (two proc swaps and one view swap). The view is computed at read time on the same tables;
-- the declare proc gains one IF.
-- FIRST RUN: the next proposal read re-classifies every open proposal; a manual declare from a 4.609.0 app made
-- 30 s or more after the apply writes the declaring DBA (Control Room's SQL preview then ends with the viewer as a
-- 5th argument). Nothing runs at apply time. History is not rewritten: earlier manual declares
-- keep the app owner as Declared by (an optional, owner-run heuristic repair is staged separately).
-- ROLLBACK: re-run V131's CREATE PROCEDURE and V072's CREATE VIEW. Remove the 5-arg overload (its teardown.sql
-- line names the signature) only once no deployed app calls it: a 4.609.0 app on a V170 schema CALLs the 5-arg.
-- Apply AFTER V169. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20170, 'V170 requires V169 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 169) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_INCIDENT_DECLARE  (from V131; 4-arg kept for un-redeployed apps: a member-less declare rolls back with a NOOP, success verdict OK, V170)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(
    P_TITLE VARCHAR,
    P_SEVERITY VARCHAR,
    P_COMPANY VARCHAR,
    P_PROPOSAL_KEY VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    inc_id STRING;
    family STRING;
    entity_kind STRING;
    entity_name STRING;
    apply_entity BOOLEAN;
    created INT DEFAULT 0;
    members INT DEFAULT 0;
BEGIN
    IF (P_PROPOSAL_KEY IS NULL OR TRIM(P_PROPOSAL_KEY) = '') THEN
        RETURN 'INVALID: proposal key is required';
    END IF;
    inc_id := UUID_STRING();
    family := SPLIT_PART(:P_PROPOSAL_KEY, '|', 1);
    entity_kind := UPPER(SPLIT_PART(:P_PROPOSAL_KEY, '|', 3));
    -- entity_name is the 4th pipe-field. Realistic entity names (warehouse / db / task) contain no
    -- '|' -- a pipe would itself corrupt the pipe-delimited DEDUPE_KEY this is matched against
    -- (SPLIT_PART(...,2)) -- so SPLIT_PART(,4) equals the app's split('|',3) remainder for every
    -- real key, and matching a single field against the single DEDUPE_KEY entity position is correct.
    entity_name := SPLIT_PART(:P_PROPOSAL_KEY, '|', 4);
    -- Entity filter applies only for a non-ACCOUNT proposal that names an entity (matches the app's
    -- `len(parts)==4 and parts[2] != 'ACCOUNT'`); ACCOUNT proposals use the family-only scope.
    apply_entity := (ARRAY_SIZE(SPLIT(:P_PROPOSAL_KEY, '|')) >= 4 AND entity_kind <> 'ACCOUNT');

    BEGIN TRANSACTION;

    -- 1) open the incident, UNLESS an OPEN/MITIGATED incident already covers this (family, company
    --    [, entity]) — the family-already-open guard (identical predicate to the app's pre-check).
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND)
    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,
           CURRENT_TIMESTAMP(), 'UNKNOWN'
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT' AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND (i.COMPANY = :P_COMPANY OR UPPER(i.COMPANY) = 'ALL')
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = :family
          AND (NOT :apply_entity
               OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)) = UPPER(:entity_name))
    );

    -- 2) link every open/ack alert of this family (+entity) in the 48h window as members, per-alert
    --    de-duplicated, but only if the incident row was actually created above.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED)
    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND (e.COMPANY = :P_COMPANY OR UPPER(e.COMPANY) = 'ALL')
      AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = :family
      AND (NOT :apply_entity
           OR UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)) = UPPER(:entity_name))
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                      WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
                  WHERE i2.INCIDENT_ID = :inc_id);

    SELECT COUNT(*) INTO :created FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS WHERE INCIDENT_ID = :inc_id;
    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;

    -- V170 (R2-030): never commit a member-less incident. The proposal the operator declared from can be up
    -- to 30 s stale, and an auto-clear or condition-ended sweep, another operator resolving the alerts, or a
    -- concurrent auto-declare can take every proposal alert out of OPEN/ACK (or into another incident)
    -- before this CALL; the members INSERT then links 0 rows. Undo the incident row (it was never visible
    -- outside this transaction) and say so.
    IF (:created = 1 AND :members = 0) THEN
        ROLLBACK;
        RETURN 'NOOP: no open alerts left to link - nothing declared';
    END IF;

    COMMIT;

    IF (:created = 0) THEN
        RETURN 'NOOP: this family already has an open incident';
    END IF;
    RETURN 'OK: declared ' || :inc_id || ' with ' || :members || ' member(s) linked';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- >>> derived:SP_INCIDENT_DECLARE  (from V131; NEW 5-arg overload: + P_ACTOR -> DECLARED_BY / LINKED_BY, with the same rollback and OK verdict, V170)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(
    P_TITLE VARCHAR,
    P_SEVERITY VARCHAR,
    P_COMPANY VARCHAR,
    P_PROPOSAL_KEY VARCHAR,
    P_ACTOR VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    inc_id STRING;
    v_actor STRING;
    family STRING;
    entity_kind STRING;
    entity_name STRING;
    apply_entity BOOLEAN;
    created INT DEFAULT 0;
    members INT DEFAULT 0;
BEGIN
    IF (P_PROPOSAL_KEY IS NULL OR TRIM(P_PROPOSAL_KEY) = '') THEN
        RETURN 'INVALID: proposal key is required';
    END IF;
    inc_id := UUID_STRING();
    -- V170 (R2-028): the DBA who declared. The app passes identity_sql() (st.user under owner-rights SiS);
    -- CURRENT_USER() there is the app owner, so it is only the fallback. LEFT(..., 200) = the width of
    -- INCIDENTS.DECLARED_BY and INCIDENT_MEMBERS.LINKED_BY (V032).
    v_actor := LEFT(COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER()), 200);
    family := SPLIT_PART(:P_PROPOSAL_KEY, '|', 1);
    entity_kind := UPPER(SPLIT_PART(:P_PROPOSAL_KEY, '|', 3));
    -- entity_name is the 4th pipe-field. Realistic entity names (warehouse / db / task) contain no
    -- '|' -- a pipe would itself corrupt the pipe-delimited DEDUPE_KEY this is matched against
    -- (SPLIT_PART(...,2)) -- so SPLIT_PART(,4) equals the app's split('|',3) remainder for every
    -- real key, and matching a single field against the single DEDUPE_KEY entity position is correct.
    entity_name := SPLIT_PART(:P_PROPOSAL_KEY, '|', 4);
    -- Entity filter applies only for a non-ACCOUNT proposal that names an entity (matches the app's
    -- `len(parts)==4 and parts[2] != 'ACCOUNT'`); ACCOUNT proposals use the family-only scope.
    apply_entity := (ARRAY_SIZE(SPLIT(:P_PROPOSAL_KEY, '|')) >= 4 AND entity_kind <> 'ACCOUNT');

    BEGIN TRANSACTION;

    -- 1) open the incident, UNLESS an OPEN/MITIGATED incident already covers this (family, company
    --    [, entity]) — the family-already-open guard (identical predicate to the app's pre-check).
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND, DECLARED_BY)
    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,
           CURRENT_TIMESTAMP(), 'UNKNOWN', :v_actor
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT' AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND (i.COMPANY = :P_COMPANY OR UPPER(i.COMPANY) = 'ALL')
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = :family
          AND (NOT :apply_entity
               OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)) = UPPER(:entity_name))
    );

    -- 2) link every open/ack alert of this family (+entity) in the 48h window as members, per-alert
    --    de-duplicated, but only if the incident row was actually created above.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE, :v_actor
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND (e.COMPANY = :P_COMPANY OR UPPER(e.COMPANY) = 'ALL')
      AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = :family
      AND (NOT :apply_entity
           OR UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)) = UPPER(:entity_name))
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                      WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
                  WHERE i2.INCIDENT_ID = :inc_id);

    SELECT COUNT(*) INTO :created FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS WHERE INCIDENT_ID = :inc_id;
    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;

    -- V170 (R2-030): never commit a member-less incident. The proposal the operator declared from can be up
    -- to 30 s stale, and an auto-clear or condition-ended sweep, another operator resolving the alerts, or a
    -- concurrent auto-declare can take every proposal alert out of OPEN/ACK (or into another incident)
    -- before this CALL; the members INSERT then links 0 rows. Undo the incident row (it was never visible
    -- outside this transaction) and say so.
    IF (:created = 1 AND :members = 0) THEN
        ROLLBACK;
        RETURN 'NOOP: no open alerts left to link - nothing declared';
    END IF;

    COMMIT;

    IF (:created = 0) THEN
        RETURN 'NOOP: this family already has an open incident';
    END IF;
    RETURN 'OK: declared ' || :inc_id || ' with ' || :members || ' member(s) linked';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- >>> derived:INCIDENT_PROPOSALS  (from V072; EXH/ALL band tokens -> ACCOUNT, + user/warehouse/object rule kinds, PIPE_TASK_FAILURES no longer self-corroborates, V170)
CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS AS
WITH raw_alerts AS (
    SELECT e.EVENT_ID,
           e.RULE_ID,
           e.COMPANY,
           e.SEVERITY,
           e.TITLE,
           e.RAISED_AT,
           SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY,
           SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2) AS ENTITY_RAW
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND NOT EXISTS (
          SELECT 1
          FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID
      )
),
classified AS (
    SELECT r.*,
           CASE
             WHEN r.RULE_ID IN ('COST_WH_DAILY_CREDITS', 'PERF_QUEUED_MINUTES',
                                'PERF_SPILL_GB', 'COST_CLOUD_SVC_RATIO',
                                'WH_CHANGE_REGRESSION', 'COST_IDLE_OPPORTUNITY',
                                'COST_SLEEP_POLLING') THEN 'WAREHOUSE'
             WHEN r.RULE_ID IN ('PIPE_TASK_FAILURES', 'PIPE_COPY_FAILURES',
                                'PERF_CHANGE_REGRESSION', 'PIPE_DT_FAILURES',
                                'PIPE_VOLUME_DROP', 'DQ_BREACH', 'DQ_SCHEMA_DRIFT') THEN 'OBJECT'
             WHEN r.RULE_ID = 'COST_STORAGE_SURGE' THEN 'DATABASE'
             WHEN r.RULE_ID = 'COST_SERVERLESS_CREEP' THEN 'SERVICE'
             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK', 'SEC_FAILED_LOGINS',
                                'SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT',
                                'COST_AI_USER_RUNAWAY') THEN 'USER'
             WHEN r.RULE_ID = 'COST_DEPT_BUDGET_PACE' THEN 'DEPARTMENT'
             WHEN COALESCE(r.ENTITY_RAW, '') = ''
               OR TRY_TO_DATE(r.ENTITY_RAW) IS NOT NULL
               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH', 'EXH', 'ALL')
               THEN 'ACCOUNT'
             ELSE 'SCOPE'
           END AS ENTITY_KIND
    FROM raw_alerts r
),
open_alerts AS (
    SELECT c.*,
           IFF(c.ENTITY_KIND = 'ACCOUNT', 'ACCOUNT', c.ENTITY_RAW) AS ENTITY_NAME
    FROM classified c
),
proposal_groups AS (
    SELECT FAMILY,
           COMPANY,
           ENTITY_KIND,
           ENTITY_NAME,
           MAX_BY(TITLE, RAISED_AT) AS SUGGESTED_TITLE,
           DECODE(MAX(CASE UPPER(SEVERITY)
                        WHEN 'CRITICAL' THEN 3 WHEN 'HIGH' THEN 2 ELSE 1 END),
                  3, 'CRITICAL', 2, 'HIGH', 'MEDIUM') AS SEVERITY,
           MIN(RAISED_AT) AS FIRST_TS,
           MAX(RAISED_AT) AS LAST_TS,
           COUNT(*) AS ALERTS
    FROM open_alerts
    GROUP BY FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME
),
warehouse_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COUNT(w.CHANGE_ID) AS MATCHED_WH_CHANGES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY w
      ON g.ENTITY_KIND = 'WAREHOUSE'
     AND UPPER(w.WAREHOUSE_NAME) = UPPER(g.ENTITY_NAME)
     AND w.CHANGE_SEEN_AT BETWEEN DATEADD('minute', -30, g.FIRST_TS)
                              AND DATEADD('minute', 30, g.LAST_TS)
    GROUP BY 1, 2, 3, 4
),
object_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COUNT(o.CHANGE_ID) AS MATCHED_OBJECT_CHANGES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY o
      ON ((g.ENTITY_KIND = 'OBJECT' AND UPPER(o.OBJECT_NAME) = UPPER(g.ENTITY_NAME))
          OR (g.ENTITY_KIND = 'DATABASE'
              AND UPPER(o.DATABASE_NAME) = UPPER(g.ENTITY_NAME)))
     AND o.CHANGE_SEEN_AT BETWEEN DATEADD('minute', -30, g.FIRST_TS)
                              AND DATEADD('minute', 30, g.LAST_TS)
    GROUP BY 1, 2, 3, 4
),
task_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COALESCE(SUM(t.FAILED), 0) AS MATCHED_TASK_FAILURES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY t
      ON g.ENTITY_KIND = 'OBJECT'
     AND UPPER(COALESCE(t.DATABASE_NAME, '') || '.' ||
               COALESCE(t.SCHEMA_NAME, '') || '.' || t.TASK_NAME) = UPPER(g.ENTITY_NAME)
     AND t.DAY BETWEEN DATEADD('day', -1, DATE(g.FIRST_TS))
                   AND DATEADD('day', 1, DATE(g.LAST_TS))
    GROUP BY 1, 2, 3, 4
),
evidence AS (
    SELECT g.*,
           COALESCE(w.MATCHED_WH_CHANGES, 0) AS MATCHED_WH_CHANGES,
           COALESCE(o.MATCHED_OBJECT_CHANGES, 0) AS MATCHED_OBJECT_CHANGES,
           COALESCE(t.MATCHED_TASK_FAILURES, 0) AS MATCHED_TASK_FAILURES
    FROM proposal_groups g
    LEFT JOIN warehouse_evidence w
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
    LEFT JOIN object_evidence o
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
    LEFT JOIN task_evidence t
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
)
SELECT FAMILY || '|' || COMPANY || '|' || ENTITY_KIND || '|' || ENTITY_NAME AS PROPOSAL_KEY,
       SUGGESTED_TITLE,
       SEVERITY,
       COMPANY,
       ENTITY_KIND,
       ENTITY_NAME,
       FIRST_TS,
       LAST_TS,
       ALERTS,
       MATCHED_WH_CHANGES,
       MATCHED_WH_CHANGES AS NEARBY_WH_CHANGES,
       MATCHED_OBJECT_CHANGES,
       MATCHED_TASK_FAILURES,
       CASE
         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES
              + IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES) > 0 THEN 'HIGH'
         WHEN ENTITY_KIND <> 'ACCOUNT' AND ALERTS >= 2 THEN 'MEDIUM'
         ELSE 'LOW'
       END AS CONFIDENCE,
       'alerts=' || ALERTS ||
       '; matched warehouse changes=' || MATCHED_WH_CHANGES ||
       '; matched object changes=' || MATCHED_OBJECT_CHANGES ||
       IFF(FAMILY = 'PIPE_TASK_FAILURES', '; task failures (alert source, not corroboration)=',
           '; matched task failures=') || MATCHED_TASK_FAILURES AS EVIDENCE
FROM evidence;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 170 AS VERSION,
       'Incident declare + proposals (R2-028, R2-030, R2-093, R2-031): SP_INCIDENT_DECLARE re-derived from V131 (4-arg kept) plus a NEW 5-arg overload with P_ACTOR that writes INCIDENTS.DECLARED_BY and INCIDENT_MEMBERS.LINKED_BY (the app passes the viewer once V170 is applied; CURRENT_USER() under the owner-rights app is the owner). Both overloads roll back a declare whose members INSERT linked 0 rows and return NOOP: no open alerts left to link, and the success verdict is OK: declared <id> with <n> member(s) linked. INCIDENT_PROPOSALS re-derived from V072: EXH and ALL band tokens classify ACCOUNT (an EXH band no longer opens a second incident for the same family), ten more user, warehouse and object rules whose key carries a bare name get their entity kind (series-prefixed keys such as CLOUD SVC <WH> stay SCOPE), and a PIPE_TASK_FAILURES proposal no longer counts its own task failures as corroboration (HIGH only with a matching task change; its evidence labels the count as the alert source). Same view columns. No data change, nothing runs at apply.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 170);

-- =====================================================================
--  STAGE 2 -- MIGRATION 6 of 7 -- APPLY V171 (idempotent; GUARDS on V170). Source: snowflake/migrations/V171__ops_selfwatch_digest_refgaps_seed.sql
-- =====================================================================

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

-- =====================================================================
--  STAGE 2 -- MIGRATION 7 of 7 -- APPLY V172 (idempotent; GUARDS on V171; in-migration re-stamps; OWNER_REPAIRS R172.0 right after this file). Source: snowflake/migrations/V172__detection_scans_company_and_accuracy.sql
-- =====================================================================

-- V172__detection_scans_company_and_accuracy.sql
--
-- The detection scans classify company by the V044 rule and measure what they claim (V166-V172 wave, detection
-- cluster). Five scan procs re-derived, each from its current definer, plus bounded one-time repairs.
--
-- WHY:
--   (R2-023) SP_CHANGE_IMPACT_SCAN stamped COMPANY with a raw TRXS%/ALFA guess: an unmapped database read ALFA,
--     a COMPANY_SCOPE override was ignored, and the Trexis / UNKNOWN scopes never saw those changes or their
--     PERF_CHANGE_REGRESSION alerts (and an auto-declared incident copied the wrong company).
--   (R2-024) SP_ANOMALY_SWEEP (PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH) and SP_SCAN_SCHEMA_DRIFT made the
--     same guess; SP_ALERT_SCAN dropped it in V067 #22. They now call COMPANY_FOR_DATABASE inside the SP_ALERT_SCAN
--     b (...) wrapper.
--   (R2-021) the scan matched procedure calls by a bare 'NAME(' suffix, so RUN_SP_LOAD counted as SP_LOAD in the
--     frozen baseline, the after window and credits/call; the Operations drill was anchored in v4.271 and the scan
--     was not. Now 'CALL<name>(' or '.<name>(' over a whitespace-class strip, the drill's rule.
--   (R2-025) TASK runs counted every auto-retry attempt: 7 of 14 runs retried once read as 21 runs, 7 failures,
--     REGRESSED (CRITICAL at 50%) while every scheduled run succeeded. Runs, fails and p95 now count the terminal
--     attempt per scheduled run (the drill, V101, V126); retry compute still counts toward credits/call.
--   (R2-022) the AFTER credits/call divided attributed credits by ALL runs, including runs QUERY_ATTRIBUTION_HISTORY
--     had not reached yet (up to ~8h): a nightly job's +55% read +44% (no alert) and a fresh hourly change read a
--     false IMPROVED. Now settled (>8h) runs only, numerator and denominator over the same runs, per scheduled run.
--   (R1-124) VERDICT_DETAIL (and so ALERT_EVENTS.DETAIL and the Teams / email lines) printed raw seconds and
--     minutes/day ('p95 1800.0s->2400.0s'); now Hr/Min/Sec ('p95 30m -> 40m', 'queue 2h 25m -> 3h 20m/day').
--   (R1-227) SP_SCAN_CLOUD_SVC_ANOMALY's disabled-rule guard tested a COALESCEd threshold for NULL and never fired;
--     a disabled COST_CLOUD_SVC_ANOMALY still booked (at 3.5) and was delivered. Rider: a disabled
--     COST_ANOMALY_SWEEP stops booking too (the other sweep arms still run).
--   (R2-095) the COST_ORG_ACCOUNT_CREEP DETAIL pointed at a retired 'Admin > Org spend'.
--   (CORTEX-NULLIF) the sweep's Cortex pre-explain read CORTEX_MODEL raw: a blank, padded, mixed-case or invalid
--     value reached COMPLETE and the AI explanation failed. Now normalized like app.core.ai.normalize_model.
--
--   ~ SP_CHANGE_IMPACT_SCAN      from V140 (R2-023, R2-021, R2-025, R2-022, R1-124)
--   ~ SP_WAREHOUSE_CHANGE_SCAN   from V109 (R1-124)
--   ~ SP_SCAN_SCHEMA_DRIFT       from V133 (R2-024)
--   ~ SP_SCAN_CLOUD_SVC_ANOMALY  from V150 (R1-227)
--   ~ SP_ANOMALY_SWEEP           from V150 (R2-024, R2-095, R1-227 rider, CORTEX-NULLIF); RETURN stays 'v3'
--   + repairs R1-R4 below (OVERWATCH tables only; reads: one ACCOUNT_USAGE.PROCEDURES join, 30 days of
--     TASK_HISTORY).
--
-- COST: the daily change-impact scan's two TASK count legs window 21 days of TASK_HISTORY (QUALIFY) instead of a
-- raw join; the anchored match runs REGEXP_REPLACE only on rows that pass the unchanged ILIKE pre-filter. The
-- repairs read ACCOUNT_USAGE.PROCEDURES once and 30 days of TASK_HISTORY once; seconds.
-- FIRST RUN: nothing runs at apply time. The next TASK_WAREHOUSE_CHANGE_SCAN (06:40 Central) and
-- TASK_CHANGE_IMPACT_SCAN (06:50) rewrite tracking rows' VERDICT_DETAIL and re-freeze the nulled PROCEDURE
-- baselines; the next TASK_ANOMALY_SWEEP books with the new company. Verdicts on collision- or retry-affected
-- objects change on that run (intended). Already-raised alerts are not re-raised (dedupe keys unchanged).
-- DELIVERY: re-raising is not delivery. SP_NOTIFY_WEBHOOK (V164) sends an OPEN event to a route only when the
-- route's COMPANY_FILTER is ALL or the event's COMPANY, once per (EVENT_ID, ROUTE_ID) in ALERT_DELIVERIES, and
-- V034 set every existing route to 'ALFA'. (a) On a database with no COMPANY_SCOPE row that is not TRXS_ / ALFA% /
-- ADMIN, PERF_CHANGE_REGRESSION, PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH and DQ_SCHEMA_DRIFT alerts are
-- now UNKNOWN and stop posting to an ALFA-only route (the first three seed HIGH, PIPE_DT_FAILURES is CRITICAL at
-- 5+ failures), with no undelivered_expired row either (the watchdog reads the same filter). To keep them
-- posting, map the database (Cost Intelligence > Spend & Attribution > Unmapped entities) or add an ALL or
-- UNKNOWN route. (b) An OPEN event the R1b / R2 re-stamps move to a company another enabled route carries becomes
-- eligible there: the next TASK_ALERT_NOTIFY run sends it once if it is still inside the send window (24h; 7d
-- for CRITICAL). An older one raised within 7 days is not sent there: V164's watchdog logs an undelivered_expired
-- row for that route instead, then another every 24h (it skips a pair logged in the last 24h) while the event
-- stays OPEN and undelivered there, until it is 7 days old.
-- ROLLBACK (order matters): 1. Re-run the base CREATEs (V140 SP_CHANGE_IMPACT_SCAN, V109
-- SP_WAREHOUSE_CHANGE_SCAN, V133 SP_SCAN_SCHEMA_DRIFT, V150 SP_SCAN_CLOUD_SVC_ANOMALY + SP_ANOMALY_SWEEP). The
-- re-stamped COMPANY values stay (they are the corrected values). 2. Right after V140's CREATE, before the next
-- change-impact scan (06:50 Central, or Operations' Run change-impact scan now), null the still-tracking
-- TASK and PROCEDURE baselines:
--     UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
--        SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,
--            BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL
--      WHERE OBJECT_TYPE IN ('TASK', 'PROCEDURE') AND CURRENT_DATE() <= TRACKING_UNTIL AND NOT ALERTED;
-- Why: R4 and the V172 scan froze them per scheduled run (terminal attempt) and by the anchored CALL match, but
-- V140's AFTER legs count every attempt and the bare suffix match, and V140 re-freezes only a NULL baseline.
-- Kept, a task with 7 of 14 runs retried once on both sides reads 14 runs / 0 failed before vs 21 / 7 after:
-- REGRESSED, a false PERF_CHANGE_REGRESSION page; a rescaled credits/call reads a false IMPROVED. Nulled, V140's
-- next scan re-freezes them on its own basis (over its 20-day reach: a change older than 6 days gets a shorter
-- baseline). An ALERTED row keeps the baseline its alert was raised on (V140 alerts a row once). Check any
-- PERF_CHANGE_REGRESSION raised between steps 1 and 2 before acting on it.
-- Apply AFTER V171. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20172, 'V172 requires V171 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 171) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_CHANGE_IMPACT_SCAN  (from V140; COMPANY via COMPANY_FOR_DATABASE (R2-023), anchored CALL match (R2-021), TASK runs collapse retries (R2-025), settled per-run AFTER credits/call (R2-022), p95 in Hr/Min/Sec (R1-124), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    pct FLOAT;                 -- regression threshold, % increase (ALERT_CONFIG)
    min_calls FLOAT DEFAULT 5; -- both windows need this many runs for a verdict
    trk_lo TIMESTAMP_NTZ;      -- v2: oldest still-tracking change (prunes the scans)
    emsg VARCHAR;
BEGIN
    -- v2 (2026-07-10 tuning): the after-window joins used a blanket -18d
    -- bound even when only fresh changes were tracking. Bound them to the
    -- oldest ACTIVE row instead — nothing tracking means near-zero scan.
    SELECT COALESCE(MIN(CHANGE_SEEN_AT), CURRENT_TIMESTAMP()) INTO :trk_lo
    FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
    WHERE CURRENT_DATE() <= TRACKING_UNTIL;
    SELECT COALESCE(MAX(THRESHOLD_NUM), 50) INTO :pct
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PERF_CHANGE_REGRESSION';

    -- 1a) Register changed/replaced procedures. CREATE OR REPLACE resets
    --     CREATED = LAST_ALTERED, so replaced and brand-new procs both land
    --     here; never-called objects finalize as NO_BASELINE, never alerts.
    --     Overloads share one row (call matching is by name).
    MERGE INTO DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
    USING (
        SELECT g.OBJECT_TYPE, g.DATABASE_NAME, g.SCHEMA_NAME, g.OBJECT_NAME,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,  -- V172 (R2-023): the V044 classification (COMPANY_SCOPE row, then TRXS_, then ALFA%/ADMIN, else UNKNOWN), not a raw TRXS%/ALFA guess; the UDF on a plain column outside the GROUP BY (V030 shape)
               g.CHANGE_SEEN_AT
        FROM (
        SELECT 'PROCEDURE' AS OBJECT_TYPE,
               PROCEDURE_CATALOG AS DATABASE_NAME,
               PROCEDURE_SCHEMA AS SCHEMA_NAME,
               PROCEDURE_CATALOG || '.' || PROCEDURE_SCHEMA || '.' || PROCEDURE_NAME AS OBJECT_NAME,
               MAX(LAST_ALTERED) AS CHANGE_SEEN_AT
        FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES
        WHERE DELETED IS NULL
          AND PROCEDURE_CATALOG IS NOT NULL
          AND PROCEDURE_CATALOG <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own procs are self-monitored (freshness + per-loader error log), not change-impact-tracked
          AND LAST_ALTERED >= DATEADD('day', -3, CURRENT_TIMESTAMP())
        GROUP BY 1, 2, 3, 4
        ) g
    ) s
    ON t.OBJECT_TYPE = s.OBJECT_TYPE AND t.OBJECT_NAME = s.OBJECT_NAME
       AND t.CHANGE_SEEN_AT = s.CHANGE_SEEN_AT
    WHEN NOT MATCHED THEN INSERT
        (OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, COMPANY, CHANGE_SEEN_AT, TRACKING_UNTIL)
        VALUES (s.OBJECT_TYPE, s.DATABASE_NAME, s.SCHEMA_NAME, s.OBJECT_NAME, s.COMPANY,
                s.CHANGE_SEEN_AT, DATEADD('day', 14, s.CHANGE_SEEN_AT)::DATE);

    -- 1b) Register task definition changes. TASK_VERSIONS keeps every graph
    --     version; only genuine definition/schedule/warehouse diffs register,
    --     so suspend/resume churn is ignored. Guarded: an account without
    --     TASK_VERSIONS still tracks procedures.
    BEGIN
        MERGE INTO DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
        USING (
            SELECT 'TASK' AS OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME,
                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) AS COMPANY,  -- V172 (R2-023): the V044 classification, not a raw TRXS%/ALFA guess; a plain column, no aggregate
                   CHANGE_SEEN_AT
            FROM (
                SELECT DATABASE_NAME, SCHEMA_NAME,
                       DATABASE_NAME || '.' || SCHEMA_NAME || '.' || NAME AS OBJECT_NAME,
                       GRAPH_VERSION_CREATED_ON AS CHANGE_SEEN_AT,
                       DEFINITION, SCHEDULE, WAREHOUSE_NAME,
                       LAG(DEFINITION) OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                                             ORDER BY GRAPH_VERSION_CREATED_ON) AS PREV_DEFINITION,
                       LAG(SCHEDULE) OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                                           ORDER BY GRAPH_VERSION_CREATED_ON) AS PREV_SCHEDULE,
                       LAG(WAREHOUSE_NAME) OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME
                                                 ORDER BY GRAPH_VERSION_CREATED_ON) AS PREV_WAREHOUSE
                FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS
            )
            WHERE CHANGE_SEEN_AT >= DATEADD('day', -3, CURRENT_TIMESTAMP())
              AND DATABASE_NAME <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own tasks are self-monitored, not change-impact-tracked
              AND PREV_DEFINITION IS NOT NULL
              AND (NOT EQUAL_NULL(DEFINITION, PREV_DEFINITION)
                   OR NOT EQUAL_NULL(SCHEDULE, PREV_SCHEDULE)
                   OR NOT EQUAL_NULL(WAREHOUSE_NAME, PREV_WAREHOUSE))
        ) s
        ON t.OBJECT_TYPE = s.OBJECT_TYPE AND t.OBJECT_NAME = s.OBJECT_NAME
           AND t.CHANGE_SEEN_AT = s.CHANGE_SEEN_AT
        WHEN NOT MATCHED THEN INSERT
            (OBJECT_TYPE, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, COMPANY, CHANGE_SEEN_AT, TRACKING_UNTIL)
            VALUES (s.OBJECT_TYPE, s.DATABASE_NAME, s.SCHEMA_NAME, s.OBJECT_NAME, s.COMPANY,
                    s.CHANGE_SEEN_AT, DATEADD('day', 14, s.CHANGE_SEEN_AT)::DATE);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ChangeImpactScan', 'task_versions_unavailable', :emsg,
                   'TASK registration skipped; procedures still tracked', CURRENT_ROLE();
    END;

    -- 2) Best-effort DDL evidence: who ran the CREATE/ALTER near the change.
    --    Multi-match picks one arbitrarily — evidence, not lineage.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET CHANGED_BY = d.USER_NAME,
           CHANGE_DDL = LEFT(d.QUERY_TEXT, 1000)
      FROM (
          SELECT USER_NAME, QUERY_TEXT, START_TIME
          FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
          WHERE START_TIME >= DATEADD('day', -4, CURRENT_TIMESTAMP())
            AND EXECUTION_STATUS = 'SUCCESS'
            AND (QUERY_TYPE ILIKE 'CREATE%' OR QUERY_TYPE ILIKE 'ALTER%')
      ) d
     WHERE t.CHANGE_DDL IS NULL
       AND t.CHANGE_SEEN_AT >= DATEADD('day', -4, CURRENT_TIMESTAMP())
       AND d.START_TIME BETWEEN DATEADD('hour', -3, t.CHANGE_SEEN_AT)
                            AND DATEADD('hour', 3, t.CHANGE_SEEN_AT)
       AND POSITION(SPLIT_PART(t.OBJECT_NAME, '.', 3) IN UPPER(d.QUERY_TEXT)) > 0;

    -- 3) Freeze pre-change baselines (14 days before the change, once).
    --    Procedure calls are matched by 'CALLNAME(' or '.NAME(' in whitespace-stripped CALL text
    --    (V172: was a bare 'NAME(' suffix match, so RUN_NAME co-matched NAME); a
    --    same-named proc in another schema would co-match — acceptable noise,
    --    flagged here rather than hidden.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET BASELINE_FROM = DATEADD('day', -14, t.CHANGE_SEEN_AT),
           BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
           BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 MEDIAN(q.TOTAL_ELAPSED_TIME) AS MED_MS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND q.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND q.START_TIME < r.CHANGE_SEEN_AT
           AND q.QUERY_TYPE = 'CALL'
           AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
           AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                         REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                            REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
          WHERE r.OBJECT_TYPE = 'PROCEDURE' AND r.BASELINE_FROM IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET BASELINE_FROM = DATEADD('day', -14, t.CHANGE_SEEN_AT),
           BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
           BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(h.STATE = 'FAILED') AS FAILS,
                 MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
                 APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN (
              -- V172 (R2-025): one row per SCHEDULED run. Auto-retry attempts share a SCHEDULED_TIME and collapse
              -- to the terminal attempt BEFORE the join (the object_run_history drill, V101, V126), so runs, fails
              -- and p95 count scheduled runs and a retried-then-succeeded run is no failure. The step-5 credit legs
              -- still read every attempt (retry compute is real spend). The ON predicates below are unchanged.
              SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                     QUERY_START_TIME, COMPLETED_TIME, STATE
              FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                AND STATE IN ('SUCCEEDED', 'FAILED')
              QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                         ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
          ) h
            ON h.SCHEDULED_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
           AND h.STATE IN ('SUCCEEDED', 'FAILED')
           AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
          WHERE r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- Idle-before objects: freeze an explicit zero baseline (-> NO_BASELINE).
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET BASELINE_FROM = DATEADD('day', -14, CHANGE_SEEN_AT),
           BASELINE_CALLS = 0, BASELINE_FAILS = 0
     WHERE BASELINE_FROM IS NULL;

    -- 4) Refresh post-change stats while the tracking window is open.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET AFTER_CALLS = s.CALLS, AFTER_FAILS = s.FAILS,
           AFTER_MEDIAN_MS = s.MED_MS, AFTER_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 MEDIAN(q.TOTAL_ELAPSED_TIME) AS MED_MS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
           AND q.START_TIME > r.CHANGE_SEEN_AT
           AND q.QUERY_TYPE = 'CALL'
           AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
           AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                         REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                            REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
          WHERE r.OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
       SET AFTER_CALLS = s.CALLS, AFTER_FAILS = s.FAILS,
           AFTER_MEDIAN_MS = s.MED_MS, AFTER_P95_MS = s.P95_MS
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
                 COUNT_IF(h.STATE = 'FAILED') AS FAILS,
                 MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
                 APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          JOIN (
              -- V172 (R2-025): one row per SCHEDULED run. Auto-retry attempts share a SCHEDULED_TIME and collapse
              -- to the terminal attempt BEFORE the join (the object_run_history drill, V101, V126), so runs, fails
              -- and p95 count scheduled runs and a retried-then-succeeded run is no failure. The step-5 credit legs
              -- still read every attempt (retry compute is real spend). The ON predicates below are unchanged.
              SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                     QUERY_START_TIME, COMPLETED_TIME, STATE
              FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                AND STATE IN ('SUCCEEDED', 'FAILED')
              QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                         ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
          ) h
            ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
           AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT
           AND h.STATE IN ('SUCCEEDED', 'FAILED')
           AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
          WHERE r.OBJECT_TYPE = 'TASK' AND CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- 5) Measured credits/call via QUERY_ATTRIBUTION_HISTORY (~6h lag; the
    --    baseline freeze waits 8h after the change so the pre-window is
    --    complete). Guarded: without the view, runtime-only verdicts.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
           SET BASELINE_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.BASELINE_CALLS, 0)
          FROM (
              SELECT x.CHANGE_ID, SUM(a.CR) AS TOTAL_CR
              FROM (
                  SELECT r.CHANGE_ID, q.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                    ON q.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
                   AND q.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
                   AND q.START_TIME < r.CHANGE_SEEN_AT
                   AND q.QUERY_TYPE = 'CALL'
                   AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
                   AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                 REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                        OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                    REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
                  WHERE r.OBJECT_TYPE = 'PROCEDURE'
                    AND r.BASELINE_CREDITS_PER_CALL IS NULL AND r.BASELINE_CALLS > 0
                    AND r.CHANGE_SEEN_AT < DATEADD('hour', -8, CURRENT_TIMESTAMP())
                  UNION ALL
                  SELECT r.CHANGE_ID, h.QUERY_ID
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    ON h.SCHEDULED_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
                   AND h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
                   AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
                   AND h.STATE IN ('SUCCEEDED', 'FAILED')
                   AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
                  WHERE r.OBJECT_TYPE = 'TASK'
                    AND r.BASELINE_CREDITS_PER_CALL IS NULL AND r.BASELINE_CALLS > 0
                    AND r.CHANGE_SEEN_AT < DATEADD('hour', -8, CURRENT_TIMESTAMP())
              ) x
              JOIN (
                  SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
                         SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                  GROUP BY 1
              ) a ON a.RID = x.QUERY_ID
              GROUP BY x.CHANGE_ID
          ) s
         WHERE t.CHANGE_ID = s.CHANGE_ID;

        UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
           SET AFTER_CREDITS_PER_CALL = s.CR_PER_CALL
          FROM (
              -- V172 (R2-022 + R2-025): credits per SETTLED scheduled run. Only runs that started more than 8h
              -- ago count (QUERY_ATTRIBUTION_HISTORY lags up to ~8h, the wait the baseline already makes), and
              -- numerator and denominator cover the SAME runs: an unattributed settled run adds 0 credits, and
              -- a task run's retry attempts add their credits to ONE run (RUN_KEY), so the divisor counts
              -- scheduled runs like BASELINE_CALLS. A value is written only once at least one settled run is
              -- attributed (HAVING), as for the baseline -- a QAH gap never reads as a false IMPROVED.
              SELECT x.CHANGE_ID, SUM(COALESCE(a.CR, 0)) / NULLIF(COUNT(DISTINCT x.RUN_KEY), 0) AS CR_PER_CALL
              FROM (
                  SELECT r.CHANGE_ID, q.QUERY_ID, q.QUERY_ID AS RUN_KEY
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                    ON q.START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                   AND q.START_TIME > r.CHANGE_SEEN_AT
                   AND q.START_TIME < DATEADD('hour', -8, CURRENT_TIMESTAMP())
                   AND q.QUERY_TYPE = 'CALL'
                   AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'
                   AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                 REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0
                        OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN
                                    REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)
                  WHERE r.OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= r.TRACKING_UNTIL
                  UNION ALL
                  SELECT r.CHANGE_ID, h.QUERY_ID,
                         r.OBJECT_NAME || '|' || TO_VARCHAR(h.SCHEDULED_TIME) AS RUN_KEY
                  FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
                  JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
                    ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT
                   AND h.QUERY_START_TIME < DATEADD('hour', -8, CURRENT_TIMESTAMP())
                   AND h.STATE IN ('SUCCEEDED', 'FAILED')
                   AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
                  WHERE r.OBJECT_TYPE = 'TASK' AND CURRENT_DATE() <= r.TRACKING_UNTIL
              ) x
              LEFT JOIN (
                  SELECT COALESCE(ROOT_QUERY_ID, QUERY_ID) AS RID,
                         SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
                  WHERE START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)
                  GROUP BY 1
              ) a ON a.RID = x.QUERY_ID
              GROUP BY x.CHANGE_ID
              HAVING COUNT(a.RID) > 0
          ) s
         WHERE t.CHANGE_ID = s.CHANGE_ID;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ChangeImpactScan', 'attribution_unavailable', :emsg,
                   'credits/call omitted - verdicts use runtime + failure rate only', CURRENT_ROLE();
    END;

    -- 6) Verdicts (rows still inside their tracking window). Regression =
    --    credits/call up threshold% with a material absolute delta, OR p95 up
    --    threshold% and at least 30s, OR failure rate up 20 points.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET LAST_EVALUATED_AT = CURRENT_TIMESTAMP(),
           VERDICT = CASE
               WHEN BASELINE_CALLS < :min_calls THEN 'NO_BASELINE'
               WHEN COALESCE(AFTER_CALLS, 0) < :min_calls THEN 'PENDING'
               WHEN (BASELINE_CREDITS_PER_CALL > 0 AND AFTER_CREDITS_PER_CALL IS NOT NULL
                     AND AFTER_CREDITS_PER_CALL > BASELINE_CREDITS_PER_CALL * (1 + :pct / 100)
                     AND (AFTER_CREDITS_PER_CALL - BASELINE_CREDITS_PER_CALL) * AFTER_CALLS >= 0.25)
                 OR (AFTER_P95_MS > BASELINE_P95_MS * (1 + :pct / 100) AND AFTER_P95_MS >= 30000)
                 OR (AFTER_FAILS / NULLIF(AFTER_CALLS, 0)
                     >= BASELINE_FAILS / NULLIF(BASELINE_CALLS, 0) + 0.2)
                   THEN 'REGRESSED'
               WHEN (BASELINE_CREDITS_PER_CALL > 0 AND AFTER_CREDITS_PER_CALL IS NOT NULL
                     AND AFTER_CREDITS_PER_CALL < BASELINE_CREDITS_PER_CALL * 0.7)
                 OR (AFTER_P95_MS < BASELINE_P95_MS * 0.7)
                   THEN 'IMPROVED'
               ELSE 'NEUTRAL'
           END,
           VERDICT_DETAIL =
               'runs ' || COALESCE(BASELINE_CALLS::VARCHAR, '0') || '->' || COALESCE(AFTER_CALLS::VARCHAR, '0')
               || ' | fails ' || COALESCE(BASELINE_FAILS::VARCHAR, '0') || '->' || COALESCE(AFTER_FAILS::VARCHAR, '0')
               -- V172 (R1-124): durations in Hr/Min/Sec, the formulas.humanize_duration twin (HALF_TO_EVEN like
               -- Python round, on fixed-point operands). With the spaced ASCII ' -> ' arrow the app shim
               -- wh_change.humanize_verdict_detail finds nothing to rewrite in the new text.
               || ' | p95 '
               || CASE WHEN (BASELINE_P95_MS / 1000) IS NULL THEN '?'
                       WHEN ROUND((BASELINE_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (BASELINE_P95_MS / 1000) < 1 THEN ROUND((BASELINE_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (BASELINE_P95_MS / 1000) < 10 THEN TO_VARCHAR(ROUND((BASELINE_P95_MS / 1000), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((BASELINE_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' -> '
               || CASE WHEN (AFTER_P95_MS / 1000) IS NULL THEN '?'
                       WHEN ROUND((AFTER_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (AFTER_P95_MS / 1000) < 1 THEN ROUND((AFTER_P95_MS / 1000) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (AFTER_P95_MS / 1000) < 10 THEN TO_VARCHAR(ROUND((AFTER_P95_MS / 1000), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((AFTER_P95_MS / 1000), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' | credits/call ' || COALESCE(ROUND(BASELINE_CREDITS_PER_CALL, 4)::VARCHAR, 'n/a')
               || '->' || COALESCE(ROUND(AFTER_CREDITS_PER_CALL, 4)::VARCHAR, 'n/a')
     WHERE CURRENT_DATE() <= TRACKING_UNTIL;

    -- Tracking ended while still thin: close it out honestly.
    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET VERDICT = 'INSUFFICIENT_AFTER'
     WHERE CURRENT_DATE() > TRACKING_UNTIL AND VERDICT = 'PENDING';

    -- 7) One alert per confirmed regression (dedupe: object + change day).
    --    2x credits/call or a 50%+ failure rate escalates to CRITICAL.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    SELECT c.RULE_ID, r.COMPANY,
           IFF(COALESCE(r.AFTER_CREDITS_PER_CALL / NULLIF(r.BASELINE_CREDITS_PER_CALL, 0), 0) >= 2
                   OR r.AFTER_FAILS / NULLIF(r.AFTER_CALLS, 0) >= 0.5,
               'CRITICAL', c.SEVERITY),
           r.OBJECT_TYPE || ' ' || r.OBJECT_NAME || ' regressed after ' ||
               TO_VARCHAR(r.CHANGE_SEEN_AT::DATE) || ' change',
           'Schema ' || r.DATABASE_NAME || '.' || r.SCHEMA_NAME || ' | '
               || COALESCE(r.VERDICT_DETAIL, '')
               || IFF(r.CHANGED_BY IS NOT NULL, ' | changed by ' || r.CHANGED_BY, ''),
           ROUND(COALESCE(100 * (r.AFTER_CREDITS_PER_CALL / NULLIF(r.BASELINE_CREDITS_PER_CALL, 0) - 1),
                          100 * (r.AFTER_P95_MS / NULLIF(r.BASELINE_P95_MS, 0) - 1)), 1),
           c.RULE_ID || '|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
    FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
      ON c.RULE_ID = 'PERF_CHANGE_REGRESSION' AND c.ENABLED
    WHERE r.VERDICT = 'REGRESSED' AND NOT r.ALERTED
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
      );

    UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
       SET ALERTED = TRUE
     WHERE VERDICT = 'REGRESSED' AND NOT ALERTED;

    RETURN 'change impact scan complete';
END;
$$;

-- >>> derived:SP_WAREHOUSE_CHANGE_SCAN  (from V109; VERDICT_DETAIL p95 + queue in Hr/Min/Sec (R1-124), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    pct FLOAT;   -- regression threshold, % credits/day increase (ALERT_CONFIG)
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 15) INTO :pct
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'WH_CHANGE_REGRESSION';

    -- 1) Snapshot current settings (SHOW is the only source on this account:
    --    no ACCOUNT_USAGE.WAREHOUSES view — see validate.sql note).
    SHOW WAREHOUSES LIMIT 500;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
        (WAREHOUSE_NAME, COMPANY, WAREHOUSE_SIZE, AUTO_SUSPEND,
         MIN_CLUSTER_COUNT, MAX_CLUSTER_COUNT, SCALING_POLICY, AUTO_RESUME, WAREHOUSE_TYPE)
    SELECT "name",
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE("name"),
           "size",
           TRY_TO_NUMBER("auto_suspend"::VARCHAR),
           TRY_TO_NUMBER("min_cluster_count"::VARCHAR),
           TRY_TO_NUMBER("max_cluster_count"::VARCHAR),
           "scaling_policy",
           "auto_resume"::VARCHAR,
           "type"
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

    -- 2) Diff the two most recent snapshots per warehouse into the registry.
    --    One registry row per (warehouse, setting) per day; first-ever run
    --    has no prior snapshot and registers nothing.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
        (WAREHOUSE_NAME, COMPANY, SETTING, OLD_VALUE, NEW_VALUE, CHANGE_SEEN_AT, TRACKING_UNTIL)
    SELECT d.WAREHOUSE_NAME, d.COMPANY, d.SETTING, d.OLD_VALUE, d.NEW_VALUE,
           CURRENT_TIMESTAMP(), DATEADD('day', 14, CURRENT_DATE())
    FROM (
        WITH ranked AS (
            SELECT WAREHOUSE_NAME, COMPANY, WAREHOUSE_SIZE, AUTO_SUSPEND,
                   MIN_CLUSTER_COUNT, MAX_CLUSTER_COUNT, SCALING_POLICY,
                   ROW_NUMBER() OVER (PARTITION BY WAREHOUSE_NAME ORDER BY SNAPSHOT_AT DESC) AS RN
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('day', -35, CURRENT_TIMESTAMP())
        ),
        cur AS (SELECT * FROM ranked WHERE RN = 1),
        prev AS (SELECT * FROM ranked WHERE RN = 2)
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'SIZE' AS SETTING,
               prev.WAREHOUSE_SIZE AS OLD_VALUE, cur.WAREHOUSE_SIZE AS NEW_VALUE
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.WAREHOUSE_SIZE, '') <> COALESCE(prev.WAREHOUSE_SIZE, '')
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'AUTO_SUSPEND',
               prev.AUTO_SUSPEND::VARCHAR, cur.AUTO_SUSPEND::VARCHAR
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.AUTO_SUSPEND, -1) <> COALESCE(prev.AUTO_SUSPEND, -1)
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'MIN_CLUSTERS',
               prev.MIN_CLUSTER_COUNT::VARCHAR, cur.MIN_CLUSTER_COUNT::VARCHAR
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.MIN_CLUSTER_COUNT, -1) <> COALESCE(prev.MIN_CLUSTER_COUNT, -1)
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'MAX_CLUSTERS',
               prev.MAX_CLUSTER_COUNT::VARCHAR, cur.MAX_CLUSTER_COUNT::VARCHAR
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.MAX_CLUSTER_COUNT, -1) <> COALESCE(prev.MAX_CLUSTER_COUNT, -1)
        UNION ALL
        SELECT cur.WAREHOUSE_NAME, cur.COMPANY, 'SCALING_POLICY',
               prev.SCALING_POLICY, cur.SCALING_POLICY
        FROM cur JOIN prev ON prev.WAREHOUSE_NAME = cur.WAREHOUSE_NAME
        WHERE COALESCE(cur.SCALING_POLICY, '') <> COALESCE(prev.SCALING_POLICY, '')
    ) d
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
        WHERE r.WAREHOUSE_NAME = d.WAREHOUSE_NAME
          AND r.SETTING = d.SETTING
          AND r.CHANGE_SEEN_AT::DATE = CURRENT_DATE()
    );

    -- 3) Freeze pre-change baselines once. $/day is exact warehouse credits
    --    (WAREHOUSE_METERING_HISTORY); the rest comes from QUERY_HISTORY.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET BASELINE_FROM = DATEADD('day', -14, t.CHANGE_SEEN_AT),
           BASELINE_CREDITS_PER_DAY = ROUND(s.CR / 14, 4)
      FROM (
          SELECT r.CHANGE_ID, SUM(m.CREDITS_USED) AS CR
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY m
            ON m.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND m.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND m.START_TIME < r.CHANGE_SEEN_AT
           AND m.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE r.BASELINE_FROM IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET BASELINE_QUERIES = s.QRY,
           BASELINE_P95_S = ROUND(s.P95_MS / 1000, 1),
           BASELINE_QUEUED_MIN_PER_DAY = ROUND(s.QUEUED_MS / 60000 / 14, 2),
           BASELINE_SPILL_GB_PER_DAY = ROUND(s.SPILL_B / POWER(1024, 3) / 14, 3),
           BASELINE_FAIL_PCT = ROUND(100 * s.FAILS / NULLIF(s.QRY, 0), 2)
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS QRY,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS,
                 SUM(COALESCE(q.QUEUED_OVERLOAD_TIME, 0)) AS QUEUED_MS,
                 SUM(COALESCE(q.BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) AS SPILL_B
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())
           AND q.START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
           AND q.START_TIME < r.CHANGE_SEEN_AT
           AND q.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE r.BASELINE_QUERIES IS NULL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- Idle-before warehouses: freeze an explicit zero baseline (-> NO_BASELINE).
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET BASELINE_FROM = DATEADD('day', -14, CHANGE_SEEN_AT),
           BASELINE_QUERIES = COALESCE(BASELINE_QUERIES, 0),
           BASELINE_CREDITS_PER_DAY = COALESCE(BASELINE_CREDITS_PER_DAY, 0)
     WHERE BASELINE_FROM IS NULL OR BASELINE_QUERIES IS NULL;

    -- 4) Refresh post-change stats while the tracking window is open.
    --    Per-day rates divide by the exact elapsed window (min half a day)
    --    so short after-windows compare fairly.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET AFTER_DAYS = ROUND(GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 2),
           AFTER_CREDITS_PER_DAY = ROUND(s.CR / GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 4)
      FROM (
          SELECT r.CHANGE_ID, SUM(m.CREDITS_USED) AS CR
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY m
            ON m.START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
           AND m.START_TIME > r.CHANGE_SEEN_AT
           AND m.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET AFTER_QUERIES = s.QRY,
           AFTER_P95_S = ROUND(s.P95_MS / 1000, 1),
           AFTER_QUEUED_MIN_PER_DAY = ROUND(s.QUEUED_MS / 60000 / GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 2),
           AFTER_SPILL_GB_PER_DAY = ROUND(s.SPILL_B / POWER(1024, 3) / GREATEST(DATEDIFF('second', t.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) / 86400.0, 0.5), 3),
           AFTER_FAIL_PCT = ROUND(100 * s.FAILS / NULLIF(s.QRY, 0), 2)
      FROM (
          SELECT r.CHANGE_ID, COUNT(*) AS QRY,
                 COUNT_IF(q.EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                 APPROX_PERCENTILE(q.TOTAL_ELAPSED_TIME, 0.95) AS P95_MS,
                 SUM(COALESCE(q.QUEUED_OVERLOAD_TIME, 0)) AS QUEUED_MS,
                 SUM(COALESCE(q.BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) AS SPILL_B
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -18, CURRENT_TIMESTAMP())
           AND q.START_TIME > r.CHANGE_SEEN_AT
           AND q.WAREHOUSE_NAME = r.WAREHOUSE_NAME
          WHERE CURRENT_DATE() <= r.TRACKING_UNTIL
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    -- 5) Verdicts (rows still inside their tracking window). Regression =
    --    $/day up threshold% with >= 1 credit/day absolute, OR p95 up 25%
    --    and >= 30s, OR failure rate up 5 points, OR queueing up 50% and
    --    >= 10 min/day. Improvement requires the other axis not to have
    --    been traded away (cheaper but 3x slower is not IMPROVED).
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET LAST_EVALUATED_AT = CURRENT_TIMESTAMP(),
           VERDICT = CASE
               WHEN COALESCE(BASELINE_QUERIES, 0) < 20 THEN 'NO_BASELINE'
               WHEN COALESCE(AFTER_DAYS, 0) < 3 OR COALESCE(AFTER_QUERIES, 0) < 20 THEN 'PENDING'
               WHEN (AFTER_CREDITS_PER_DAY > BASELINE_CREDITS_PER_DAY * (1 + :pct / 100)
                     AND AFTER_CREDITS_PER_DAY - BASELINE_CREDITS_PER_DAY >= 1)
                 OR (AFTER_P95_S > COALESCE(BASELINE_P95_S, 0) * 1.25 AND AFTER_P95_S >= 30)
                 OR (COALESCE(AFTER_FAIL_PCT, 0) >= COALESCE(BASELINE_FAIL_PCT, 0) + 5)
                 OR (AFTER_QUEUED_MIN_PER_DAY > COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 1.5
                     AND AFTER_QUEUED_MIN_PER_DAY >= 10)
                   THEN 'REGRESSED'
               WHEN (AFTER_CREDITS_PER_DAY <= BASELINE_CREDITS_PER_DAY * 0.85
                     AND COALESCE(AFTER_P95_S, 0) <= COALESCE(BASELINE_P95_S, 0) * 1.10)
                 OR (COALESCE(AFTER_P95_S, 999999) <= COALESCE(BASELINE_P95_S, 0) * 0.75
                     AND AFTER_CREDITS_PER_DAY <= BASELINE_CREDITS_PER_DAY * 1.10)
                   THEN 'IMPROVED'
               ELSE 'NEUTRAL'
           END,
           VERDICT_DETAIL =
               'credits/day ' || COALESCE(ROUND(BASELINE_CREDITS_PER_DAY, 2)::VARCHAR, '?')
               || '->' || COALESCE(ROUND(AFTER_CREDITS_PER_DAY, 2)::VARCHAR, '?')
               -- V172 (R1-124): durations in Hr/Min/Sec, the formulas.humanize_duration twin (HALF_TO_EVEN like
               -- Python round, on fixed-point operands). With the spaced ASCII ' -> ' arrow the app shim
               -- wh_change.humanize_verdict_detail finds nothing to rewrite in the new text.
               || ' | p95 '
               || CASE WHEN (BASELINE_P95_S) IS NULL THEN '?'
                       WHEN ROUND((BASELINE_P95_S) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (BASELINE_P95_S) < 1 THEN ROUND((BASELINE_P95_S) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (BASELINE_P95_S) < 10 THEN TO_VARCHAR(ROUND((BASELINE_P95_S), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((BASELINE_P95_S), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' -> '
               || CASE WHEN (AFTER_P95_S) IS NULL THEN '?'
                       WHEN ROUND((AFTER_P95_S) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (AFTER_P95_S) < 1 THEN ROUND((AFTER_P95_S) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (AFTER_P95_S) < 10 THEN TO_VARCHAR(ROUND((AFTER_P95_S), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((AFTER_P95_S), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' | queue '
               || CASE WHEN (COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) IS NULL THEN '0s'
                       WHEN ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) < 1 THEN ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60) < 10 THEN TO_VARCHAR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || ' -> '
               || CASE WHEN (COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) IS NULL THEN '0s'
                       WHEN ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'
                       WHEN (COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) < 1 THEN ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'
                       WHEN (COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60) < 10 THEN TO_VARCHAR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'
                       ELSE TRIM(IFF(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') >= 3600, FLOOR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 3600)::INT || 'h ', '')
                                 || IFF(MOD(FLOOR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60) > 0, MOD(FLOOR(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') / 60), 60)::INT || 'm ', '')
                                 || IFF(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN') < 3600 AND MOD(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60) > 0, MOD(ROUND((COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60), 0, 'HALF_TO_EVEN'), 60)::INT || 's', ''))
                  END
               || '/day'
               || ' | fail ' || COALESCE(BASELINE_FAIL_PCT::VARCHAR, '0') || '->'
               || COALESCE(AFTER_FAIL_PCT::VARCHAR, '0') || '%'
               || ' | ' || COALESCE(BASELINE_QUERIES::VARCHAR, '0') || '->'
               || COALESCE(AFTER_QUERIES::VARCHAR, '0') || ' queries'
     WHERE CURRENT_DATE() <= TRACKING_UNTIL;

    -- Tracking ended while still thin: close it out honestly.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET VERDICT = 'INSUFFICIENT_AFTER'
     WHERE CURRENT_DATE() > TRACKING_UNTIL AND VERDICT = 'PENDING';

    -- 6) One alert per confirmed regression (dedupe: warehouse + setting +
    --    change day). 2x credits/day escalates to CRITICAL.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    SELECT c.RULE_ID, r.COMPANY,
           IFF(COALESCE(r.AFTER_CREDITS_PER_DAY / NULLIF(r.BASELINE_CREDITS_PER_DAY, 0), 0) >= 2,
               'CRITICAL', c.SEVERITY),
           'Warehouse ' || r.WAREHOUSE_NAME || ' regressed after ' || r.SETTING || ' '
               || COALESCE(r.OLD_VALUE, '?') || '->' || COALESCE(r.NEW_VALUE, '?')
               || ' on ' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE),
           COALESCE(r.VERDICT_DETAIL, ''),
           ROUND(COALESCE(100 * (r.AFTER_CREDITS_PER_DAY / NULLIF(r.BASELINE_CREDITS_PER_DAY, 0) - 1),
                          100 * (r.AFTER_P95_S / NULLIF(r.BASELINE_P95_S, 0) - 1)), 1),
           c.RULE_ID || '|' || r.WAREHOUSE_NAME || '|' || r.SETTING || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
      ON c.RULE_ID = 'WH_CHANGE_REGRESSION' AND c.ENABLED
    WHERE r.VERDICT = 'REGRESSED' AND NOT r.ALERTED
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || r.WAREHOUSE_NAME || '|' || r.SETTING || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
      );

    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
       SET ALERTED = TRUE
     WHERE VERDICT = 'REGRESSED' AND NOT ALERTED;

    RETURN 'warehouse change scan complete';
END;
$$;

-- >>> derived:SP_SCAN_SCHEMA_DRIFT  (from V133; COMPANY via COMPANY_FOR_DATABASE in a b (...) wrapper (R2-024), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Schema-drift monitor (R23). Snapshots the current column set (name + type) of each catalog-registered
-- OBJECT table from ACCOUNT_USAGE.COLUMNS, then compares today's snapshot against the latest PRIOR
-- snapshot to detect added / removed / retyped columns, booking one DQ_SCHEMA_DRIFT alert per drifted
-- table. Metadata only -- no table-data scan, no external grants. A table with no prior snapshot (first
-- scan) establishes a baseline and never alerts. Idempotent for same-day re-runs (today's snapshot is
-- replaced, the diff is always latest-prior vs today).
--
-- SCOPE (deliberate): monitors tables registered as OBJECT entities (a specific DB.SCHEMA.TABLE carrying a
-- DATA_PRODUCT). Unlike the volume / DQ_BREACH arms it does NOT expand DATABASE-level registrations --
-- per-column daily snapshots across every table in a registered database is deferred on cost grounds.
-- Register a table as an OBJECT (Decision Studio catalog) to schema-drift-monitor it.
DECLARE
    enabled_cnt INT;
BEGIN
    SELECT COUNT(*) INTO :enabled_cnt
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'DQ_SCHEMA_DRIFT' AND ENABLED;
    IF (:enabled_cnt = 0) THEN
        RETURN 'schema-drift scan skipped (rule disabled)';
    END IF;

    -- 1) refresh today's snapshot from live column metadata (idempotent for same-day re-runs).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE();
    INSERT INTO DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT (FQN, COLUMN_NAME, DATA_TYPE, SNAPSHOT_DAY)
    SELECT UPPER(c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME),
           c.COLUMN_NAME, c.DATA_TYPE, CURRENT_DATE()
    FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS c
    JOIN DBA_MAINT_DB.OVERWATCH.ENTITY_CATALOG e
      ON e.ENTITY_TYPE = 'OBJECT'
     AND UPPER(e.ENTITY_KEY) = UPPER(c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME)
     AND NULLIF(TRIM(e.DATA_PRODUCT), '') IS NOT NULL
    WHERE c.DELETED IS NULL;

    -- 2) diff today's snapshot vs the latest PRIOR snapshot per table; one alert per drifted table.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED AND RULE_ID = 'DQ_SCHEMA_DRIFT'
    ),
    cur AS (
        SELECT FQN, COLUMN_NAME, DATA_TYPE
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE()
    ),
    prior_day AS (
        SELECT FQN, MAX(SNAPSHOT_DAY) AS PD
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT
        WHERE SNAPSHOT_DAY < CURRENT_DATE() GROUP BY FQN
    ),
    prior AS (
        SELECT s.FQN, s.COLUMN_NAME, s.DATA_TYPE
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT s
        JOIN prior_day p ON p.FQN = s.FQN AND p.PD = s.SNAPSHOT_DAY
    ),
    changes AS (
        SELECT c.FQN AS FQN, 'added ' || c.COLUMN_NAME AS CH
        FROM cur c
        JOIN prior_day pd ON pd.FQN = c.FQN
        LEFT JOIN prior p ON p.FQN = c.FQN AND p.COLUMN_NAME = c.COLUMN_NAME
        WHERE p.COLUMN_NAME IS NULL
        UNION ALL
        -- removed columns -- but ONLY for a table still present in today's snapshot (the "cf" guard,
        -- symmetric with the added branch's prior_day guard). Without it, a table that was DROPPED or
        -- UNREGISTERED (cur has zero rows for it, but a prior snapshot lingers under 90-day retention)
        -- would report every column as "removed" and, because the dedup key rolls by date, re-fire that
        -- spurious alert daily until the snapshot ages out. A vanished table is a freshness/existence
        -- concern (the SLA + row-volume panels own it), not schema drift.
        SELECT p.FQN, 'removed ' || p.COLUMN_NAME
        FROM prior p
        JOIN (SELECT DISTINCT FQN FROM cur) cf ON cf.FQN = p.FQN
        LEFT JOIN cur c ON c.FQN = p.FQN AND c.COLUMN_NAME = p.COLUMN_NAME
        WHERE c.COLUMN_NAME IS NULL
        UNION ALL
        SELECT c.FQN, 'retyped ' || c.COLUMN_NAME || ' (' || p.DATA_TYPE || ' -> ' || c.DATA_TYPE || ')'
        FROM cur c JOIN prior p ON p.FQN = c.FQN AND p.COLUMN_NAME = c.COLUMN_NAME
        WHERE c.DATA_TYPE <> p.DATA_TYPE
    ),
    agg AS (
        SELECT FQN, COUNT(*) AS N,
               LISTAGG(CH, ', ') WITHIN GROUP (ORDER BY CH) AS CHANGES
        FROM changes GROUP BY FQN
    )
    SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
    FROM (
    SELECT cfg.RULE_ID,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(SPLIT_PART(a.FQN, '.', 1)),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
           cfg.SEVERITY,
           a.FQN || ' schema changed: ' || a.N || ' column change(s)',
           'Registered-table schema drift vs the prior snapshot: ' || LEFT(a.CHANGES, 1700) ||
               '. Confirm the change was intended (upstream DDL / migration) and update downstream consumers.',
           a.N,
           cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())
    FROM agg a
    CROSS JOIN cfg

    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
    );

    -- 3) retention: keep ~90 days of column snapshots.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT
    WHERE SNAPSHOT_DAY < DATEADD('day', -90, CURRENT_DATE());

    RETURN 'schema-drift scan complete';
END;
$$;

-- >>> derived:SP_SCAN_CLOUD_SVC_ANOMALY  (from V150; the disabled-rule guard counts ENABLED rows (R1-227), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Per-warehouse cloud-services anomaly scan (Phase 2). Books one COST_CLOUD_SVC_ANOMALY alert per
-- (warehouse, day) whose gross cloud-services credits are a robust-z outlier vs the warehouse's own
-- prior-28-day baseline. This is the PER-ENTITY replacement for the fixed 10/20% CS-ratio threshold:
-- a warehouse that is CHRONICALLY compile-heavy (high but STEADY cloud services) sits in-baseline and
-- does NOT alert, while a genuine step-change (a new chatty tool, a runaway metadata loop) fires.
--
-- Same robust median/MAD modified-z engine as the COST_ANOMALY_SWEEP warehouse/service arm and the
-- app twin app/logic/anomaly.robust_zscores (0.6745; mean-absolute-deviation / 0.7979 fallback when
-- MAD collapses to 0). Source is MART_CLOUD_SVC_DAILY (gross CS credits, per family x warehouse x day;
-- CS is USAGE, never billable -- the ~10% rebate is account+day and not warehouse-decomposable).
-- Materiality is a CS-CREDIT VOLUME floor, NOT the $50 compute floor: cloud-services credits are tiny,
-- so a dollar gate would suppress real CS step-changes. The last 3 complete days are (re)scored with a
-- per-(series,day) dedup key, so a day deleted mid-reconcile is picked up on the next run.
DECLARE
    zthr FLOAT;
    cs_floor FLOAT DEFAULT 1.0;   -- CS credits/day floor: below this, a spike is not worth paging
    enabled_cnt INT;
BEGIN
    -- V172 (R1-227): gate on the ENABLED row count like SP_SCAN_SCHEMA_DRIFT / SP_SCAN_RECON_ERRORS. V150
    -- tested :zthr IS NULL after COALESCE(.., 3.5), which never fired, so a disabled or deleted rule still
    -- scanned (at 3.5, not the tuned threshold) and SP_NOTIFY_WEBHOOK delivered what it booked.
    SELECT COUNT(*), COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :enabled_cnt, :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY' AND ENABLED;
    IF (:enabled_cnt = 0) THEN
        RETURN 'cloud-services anomaly scan skipped (rule disabled)';
    END IF;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'CLOUD SVC ' || COALESCE(WAREHOUSE_NAME, 'NONE') AS SERIES, COMPANY, DAY,
               SUM(CS_CREDITS) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    meanad AS (
        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1
    ),
    active AS (
        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS
        FROM series GROUP BY 1
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,
               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,
               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        JOIN meanad ma ON ma.SERIES = s.SERIES
        JOIN active a ON a.SERIES = s.SERIES
        WHERE s.DAY >= DATEADD('day', -3, CURRENT_DATE())
    )
    SELECT 'COST_CLOUD_SVC_ANOMALY', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || IFF(l.SIGNED_Z < 0, ' cloud-services collapsed to ', ' cloud-services spiked to ') ||
               ROUND(l.CREDITS, 2) || ' credits on ' || TO_VARCHAR(l.DAY) ||
               ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 2) || ' CS credits/day over the prior 28d (GROSS usage, before the ' ||
               'account-level ~10% rebate). Robust z ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. This per-warehouse baseline replaces the fixed 10/20% ratio: a chronically compile-heavy ' ||
               'warehouse stays in-baseline, so this is a real step-change. Investigate: Operations > ' ||
               'Queries > cloud-services chatter by application, or Cost > Spend cloud-services health.',
           l.ROBUST_Z,
           'COST_CLOUD_SVC_ANOMALY|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr
      AND l.ACTIVE_DAYS >= 10
      AND (
          (l.SIGNED_Z > 0 AND l.CREDITS >= :cs_floor)
          OR (l.SIGNED_Z < 0 AND l.MED >= :cs_floor)
      )
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_CLOUD_SVC_ANOMALY|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    RETURN 'cloud-services anomaly scan complete';
END;
$$;

-- >>> derived:SP_ANOMALY_SWEEP  (from V150; DT / volume / DQ_BREACH COMPANY via COMPANY_FOR_DATABASE (R2-024), COST_ORG_ACCOUNT_CREEP pointer (R2-095), COST_ANOMALY_SWEEP enabled gate (R1-227 rider), normalized CORTEX_MODEL read (CORTEX-NULLIF), V172)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    zthr FLOAT;
    credit_price FLOAT;
    ai_model VARCHAR;
    ev_id VARCHAR;
    ev_title VARCHAR;
    day_s VARCHAR;
    series_s VARCHAR;
    wh_s VARCHAR;
    evidence VARCHAR;
    ai_prompt VARCHAR;
    ai_resp VARCHAR;
    c_new CURSOR FOR
        SELECT EVENT_ID, TITLE, DEDUPE_KEY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        WHERE RULE_ID = 'COST_ANOMALY_SWEEP'
          AND RAISED_AT >= DATEADD('minute', -15, CURRENT_TIMESTAMP())
          AND DETAIL NOT LIKE '%| AI:%'
        LIMIT 5;
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED;

    -- V076: materiality floor mirrors the app-side warehouse anomaly gate
    -- (app/logic/anomaly.py): flag on real money AND a real baseline, so an
    -- idle warehouse cannot post a z+20 event on a trivial active day.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'WAREHOUSE ' || WAREHOUSE_NAME AS SERIES, COMPANY, DAY,
               SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
        UNION ALL
        SELECT 'SERVICE ' || SERVICE_TYPE, 'ALL', DAY, SUM(CREDITS_BILLED)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    -- V097: mean-absolute-deviation fallback denominator (== abs_dev.mean() in the
    -- app twin app/logic/anomaly.py robust_zscores) for series whose MAD collapses to 0.
    meanad AS (
        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1
    ),
    active AS (
        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS
        FROM series GROUP BY 1
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,
               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,
               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        JOIN meanad ma ON ma.SERIES = s.SERIES
        JOIN active a ON a.SERIES = s.SERIES
        -- task-dag-ordering (round 9): score the last 3 COMPLETE days, not just MAX(DAY).
        -- The nightly reconcile deletes+reloads FACT_METERING_DAILY / FACT_WAREHOUSE_DAILY for
        -- D-1..D-3 non-atomically; if the standalone sweep fires mid-reload, MAX(DAY) collapses
        -- to D-4 and yesterday's spike is scored against a truncated series (or skipped) and,
        -- because the sweep only ever scored the single latest day, never re-examined -- a
        -- permanently missed COST_ANOMALY_SWEEP alert. Scoring D-1..D-3 with the existing
        -- per-(series,day) DEDUPE_KEY (each day alerts at most once) self-heals: a day deleted at
        -- sweep time is picked up on the next run once reconcile has reloaded it.
        WHERE s.DAY >= DATEADD('day', -3, CURRENT_DATE())
    )
    SELECT 'COST_ANOMALY_SWEEP', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || IFF(l.SIGNED_Z < 0, ' collapsed to ', ' spiked to ') ||
               ROUND(l.CREDITS, 1) || ' credits on ' ||
               TO_VARCHAR(l.DAY) || ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 1) || ' credits/day over the prior 28d. ' ||
               'Robust z-score ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. Investigate: Cost > Spend / Attribution for that day.',
           l.ROBUST_Z,
           'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr
      AND l.ACTIVE_DAYS >= 10
      AND (
          (l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)
          OR (l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)
      )
      -- V172 (R1-227 rider): a disabled or deleted COST_ANOMALY_SWEEP rule books nothing. The threshold read
      -- above COALESCEs to 3.5 and never gated, so turning the rule off changed nothing. Not an early
      -- RETURN: the DT, drift, creep, volume, DQ and cloud-services arms below still run.
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                  WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED)
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    -- Dynamic-table refresh failures (guarded: accounts without the view
    -- keep the sweep's cost half working).
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
               IFF(d.FAILURES >= 5, 'CRITICAL', c.SEVERITY),
               d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   ': ' || d.FAILURES || ' dynamic-table refresh failure(s) (24h)',
               'Schema ' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                   ' | last state ' || d.LAST_STATE ||
                   '. Downstream tables are serving stale data until this refreshes.',
               d.FAILURES,
               c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DATABASE_NAME, SCHEMA_NAME, NAME,
                   COUNT_IF(STATE = 'FAILED') AS FAILURES,
                   MAX_BY(STATE, REFRESH_END_TIME) AS LAST_STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY
            WHERE REFRESH_END_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY 1, 2, 3
            HAVING COUNT_IF(STATE = 'FAILED') > 0
        ) d ON c.RULE_ID = 'PIPE_DT_FAILURES' AND c.ENABLED AND d.FAILURES > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dynamic_tables_unavailable', 'DT refresh view not readable',
                   'cost anomaly sweep unaffected', CURRENT_ROLE();
    END;


    -- PERF_FINGERPRINT_DRIFT (Mondays): p95 per query family, last 7d vs the
    -- prior 28d — catches regressions that arrive WITHOUT a DDL change
    -- (data growth, clustering decay, plan changes). Complements the
    -- change-anchored V010 tracker.
    IF (DAYOFWEEKISO(CURRENT_DATE()) = 1) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL',
               IFF(f.P95_RECENT_S >= f.P95_BASE_S * 3, 'HIGH', c.SEVERITY),
               'Query family p95 ' || f.P95_BASE_S || 's -> ' || f.P95_RECENT_S || 's: ' ||
                   LEFT(f.SAMPLE_TEXT, 60),
               'Hash ' || f.QUERY_PARAMETERIZED_HASH || ' | runs ' || f.RUNS_BASE || ' -> ' ||
                   f.RUNS_RECENT || ' | 7d vs prior 28d, no change event required. ' ||
                   'Drill: Operations > Queries (heaviest queries).',
               ROUND(100 * (f.P95_RECENT_S / NULLIF(f.P95_BASE_S, 0) - 1), 1),
               c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                   TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT QUERY_PARAMETERIZED_HASH,
                   ANY_VALUE(LEFT(QUERY_TEXT, 80)) AS SAMPLE_TEXT,
                   COUNT_IF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_RECENT,
                   COUNT_IF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_BASE,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_RECENT_S,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_BASE_S
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_PARAMETERIZED_HASH IS NOT NULL
            GROUP BY 1
            HAVING RUNS_RECENT >= 20 AND RUNS_BASE >= 20
        ) f ON c.RULE_ID = 'PERF_FINGERPRINT_DRIFT' AND c.ENABLED
           AND f.P95_BASE_S > 0
           AND f.P95_RECENT_S > f.P95_BASE_S * (1 + c.THRESHOLD_NUM / 100)
           AND f.P95_RECENT_S >= 10
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    END IF;


    -- COST_ORG_ACCOUNT_CREEP (guarded): any org account's currency spend up
    -- threshold% week-over-week — a sibling account can't surprise you.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               o.ACCOUNT_NAME || ' org spend up ' || ROUND(o.PCT, 0) || '% week-over-week',
               'Last 7d ' || ROUND(o.CUR, 0) || ' vs prior ' || ROUND(o.PRV, 0) || ' ' || o.CCY ||
                   '. Breakdown: Cost Intelligence > Contract & Forecast.',
               o.PCT,
               c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT ACCOUNT_NAME, CCY, CUR, PRV, (CUR / NULLIF(PRV, 0) - 1) * 100 AS PCT
            FROM (
                SELECT ACCOUNT_NAME, MAX(CURRENCY) AS CCY,
                       SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS CUR,
                       SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS PRV
                FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
                WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
                GROUP BY 1
            )
        ) o ON c.RULE_ID = 'COST_ORG_ACCOUNT_CREEP' AND c.ENABLED
           AND o.PCT > c.THRESHOLD_NUM AND o.CUR >= 100
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'org_usage_unavailable', 'ORGANIZATION_USAGE not readable',
                   'org creep check skipped', CURRENT_ROLE();
    END;

    -- PIPE_VOLUME_DROP (guarded): yesterday's rows-added collapsed vs the
    -- prior-7-day average on tables that normally move real volume.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(v.DB),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
               c.SEVERITY,
               v.DB || '.' || v.SCH || '.' || v.TBL || ' volume down ' || ROUND(v.DROP_PCT, 0) ||
                   '% (' || v.Y_ROWS || ' rows vs ~' || ROUND(v.AVG_ROWS, 0) || '/day)',
               'Yesterday vs prior-7d average. Upstream feed, failed COPY, or intentional? ' ||
                   'Check Operations > Pipeline SLA.',
               v.DROP_PCT,
               c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL || '|' ||
                   TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DB, SCH, TBL, Y_ROWS, AVG_ROWS,
                   (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 AS DROP_PCT
            FROM (
                SELECT d.DATABASE_NAME AS DB, d.SCHEMA_NAME AS SCH, d.TABLE_NAME AS TBL,
                       SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) AS Y_ROWS,
                       SUM(IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) / 7 AS AVG_ROWS
                FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
                WHERE d.START_TIME >= DATEADD('day', -8, CURRENT_DATE())
                  AND d.START_TIME < CURRENT_DATE()
                  -- PROD only, BOTH companies (owner decision 2026-07-08
                  -- after the DEV/SIT storm): ALFA_EDW_PRD + ALFA_EDW_MGM by
                  -- name, and every *_PRD database by suffix — which is what
                  -- covers Trexis PROD (TRXS_EDW_PRD, TRXS_GW_DATA_PRD,
                  -- TRXS_ABC_METADATA_PRD). DEV/SIT/SAN stay silent. Same
                  -- semantics as app environment_clause('PROD').
                  AND (UPPER(d.DATABASE_NAME) IN ('ALFA_EDW_PRD', 'ALFA_EDW_MGM')
                       OR UPPER(d.DATABASE_NAME) LIKE '%!_PRD' ESCAPE '!')
                GROUP BY 1, 2, 3
                HAVING AVG_ROWS >= 1000
            )
        ) v ON c.RULE_ID = 'PIPE_VOLUME_DROP' AND c.ENABLED
           AND v.DROP_PCT > c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'volume-drop check skipped', CURRENT_ROLE();
    END;

    -- DQ_BREACH (R24): registered-product tables whose most recent rows-added load is a robust-z
    -- outlier (spike OR drop) vs its own prior loads -- the DB-side twin of logic/dq.row_volume_anomalies
    -- (the Operations data-quality panel), now booked as alerts. Guarded so an ACCOUNT_USAGE gap can't
    -- break the sweep's cost/volume halves.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        -- 28-day load window: MUST match the panel's product_row_volume(28) so the alert and the
        -- Operations data-quality panel score the SAME series (same baseline, same >=11-load
        -- eligibility) and can never disagree on a table -- a wider window would fire on tables the
        -- panel omits or shift the baseline median/MAD. (The panel's QUALIFY DENSE_RANK<=600 render
        -- cap is deliberately NOT mirrored: the alert has no render limit, so a real anomaly on the
        -- 601st+ registered table still pages -- more complete, never wrong.)
        WITH vol AS (
            SELECT d.DATABASE_NAME AS DB,
                   d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.TABLE_NAME AS FQN,
                   DATE(d.START_TIME) AS DAY,
                   SUM(d.ROWS_ADDED) AS ROWS_ADDED
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
            WHERE d.START_TIME >= DATEADD('day', -28, CURRENT_DATE())
              AND d.START_TIME < CURRENT_DATE()
              AND UPPER(d.DATABASE_NAME) <> 'SNOWFLAKE'
              AND NOT REGEXP_LIKE(d.TABLE_NAME, '.*_[0-9]{8}(_[0-9]+)+', 'i')
            GROUP BY 1, 2, 3
            HAVING SUM(d.ROWS_ADDED) > 0
        ),
        cat AS (
            SELECT ENTITY_TYPE, UPPER(ENTITY_KEY) AS K, DATA_PRODUCT, OWNER_NAME, CRITICALITY
            FROM DBA_MAINT_DB.OVERWATCH.ENTITY_CATALOG
            WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
        ),
        reg AS (
            SELECT v.FQN, v.DB, v.DAY, v.ROWS_ADDED,
                   COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) AS DATA_PRODUCT,
                   COALESCE(om.OWNER_NAME, dm.OWNER_NAME) AS OWNER_NAME
            FROM vol v
            LEFT JOIN cat om ON om.ENTITY_TYPE = 'OBJECT' AND om.K = UPPER(v.FQN)
            LEFT JOIN cat dm ON om.K IS NULL AND dm.ENTITY_TYPE = 'DATABASE' AND dm.K = UPPER(v.DB)
            WHERE COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL
        ),
        ranked AS (
            SELECT FQN, DB, DAY, ROWS_ADDED, DATA_PRODUCT, OWNER_NAME,
                   ROW_NUMBER() OVER (PARTITION BY FQN ORDER BY DAY DESC) AS RN,
                   COUNT(*) OVER (PARTITION BY FQN) AS N_LOADS
            FROM reg
        ),
        base_med AS (
            SELECT FQN, MEDIAN(ROWS_ADDED) AS MED
            FROM ranked WHERE RN > 1 GROUP BY 1
        ),
        base_mad AS (
            SELECT r.FQN, m.MED, MEDIAN(ABS(r.ROWS_ADDED - m.MED)) AS MAD_RAW
            FROM ranked r JOIN base_med m ON m.FQN = r.FQN
            WHERE r.RN > 1 GROUP BY 1, 2
        ),
        scored AS (
            SELECT l.FQN, l.DB, l.DAY, l.ROWS_ADDED AS LATEST_ROWS, l.DATA_PRODUCT, l.OWNER_NAME,
                   b.MED,
                   (l.ROWS_ADDED - b.MED)
                       / NULLIF(GREATEST(b.MAD_RAW * 1.4826, 0.15 * b.MED), 0) AS RAW_Z
            FROM ranked l
            JOIN base_mad b ON b.FQN = l.FQN
            WHERE l.RN = 1
              AND l.N_LOADS >= 11
              AND b.MED >= 100
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(s.DB),  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); the b (...) wrapper as in SP_ALERT_SCAN
               c.SEVERITY,
               s.FQN || IFF(s.RAW_Z < 0, ' rows-added dropped to ', ' rows-added spiked to ') ||
                   s.LATEST_ROWS || ' on ' || TO_VARCHAR(s.DAY) || ' (z=' ||
                   ROUND(LEAST(99.9, GREATEST(-99.9, s.RAW_Z)), 1) || ')',
               'Registered product ' || COALESCE(s.DATA_PRODUCT, '(unknown)') || ', owner ' ||
                   COALESCE(s.OWNER_NAME, '(unassigned)') || '. Baseline median ' || ROUND(s.MED, 0) ||
                   ' rows/load over its prior loads. Robust z ' ||
                   ROUND(LEAST(99.9, GREATEST(-99.9, s.RAW_Z)), 1) || ' vs threshold ' || c.THRESHOLD_NUM ||
                   '. Investigate: Operations > Pipeline data-quality panel.',
               LEAST(99.9, GREATEST(-99.9, s.RAW_Z)),
               c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN scored s ON c.RULE_ID = 'DQ_BREACH' AND c.ENABLED
           AND ABS(s.RAW_Z) >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'DQ_BREACH check skipped', CURRENT_ROLE();
    END;

    -- DQ_SCHEMA_DRIFT (R23): schema drift on registered-product tables (columns added / removed /
    -- retyped vs the prior snapshot). The stateful snapshot + diff lives in SP_SCAN_SCHEMA_DRIFT; this
    -- arm just CALLs it inside the standard guard, so a COLUMNS/catalog issue can't break the other arms.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT();
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'schema_drift_scan_failed', SQLERRM,
                   'DQ_SCHEMA_DRIFT check skipped', CURRENT_ROLE();
    END;

    -- COST_CLOUD_SVC_ANOMALY (Phase 2): per-warehouse cloud-services robust-z step-change vs a 28d
    -- baseline, replacing the fixed 10/20% CS-ratio threshold so a chronically compile-heavy warehouse
    -- stays in-baseline while a real spike fires. The scan lives in SP_SCAN_CLOUD_SVC_ANOMALY; this arm
    -- just CALLs it inside the standard guard, so a MART_CLOUD_SVC_DAILY issue can't break the other arms.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cloud_svc_anomaly_scan_failed', SQLERRM,
                   'COST_CLOUD_SVC_ANOMALY check skipped', CURRENT_ROLE();
    END;

    -- Pre-explain fresh anomalies (guarded): grounded Cortex hypothesis is
    -- appended to the event DETAIL so the webhook message arrives explained.
    -- Capped at 5 events/run to bound AI spend.
    BEGIN
        -- V172 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a
        -- valid name else the default): a blank, padded, mixed-case or invalid stored value no longer
        -- reaches COMPLETE (the V171 digest read, same literal)
        SELECT IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')
          INTO :ai_model
        FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
              FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);
        FOR e IN c_new DO
            ev_id := e.EVENT_ID;
            ev_title := e.TITLE;
            series_s := SPLIT_PART(e.DEDUPE_KEY, '|', 2);
            day_s := SPLIT_PART(e.DEDUPE_KEY, '|', 3);
            wh_s := IFF(series_s LIKE 'WAREHOUSE %', LTRIM(SUBSTR(series_s, 10)), '');
            SELECT LISTAGG(SAMPLE_TEXT || ' day=' || H_DAY || 'h prior_avg=' || H_PRI || 'h', '; ')
              INTO :evidence
            FROM (
                SELECT ANY_VALUE(LEFT(QUERY_TEXT, 60)) AS SAMPLE_TEXT,
                       ROUND(SUM(IFF(DATE(START_TIME) = TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 3600000, 2) AS H_DAY,
                       ROUND(SUM(IFF(DATE(START_TIME) < TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 7 / 3600000, 2) AS H_PRI
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -7, TO_DATE(:day_s))
                  AND START_TIME < DATEADD('day', 1, TO_DATE(:day_s))
                  AND (:wh_s = '' OR WAREHOUSE_NAME = :wh_s)
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY QUERY_PARAMETERIZED_HASH
                ORDER BY H_DAY DESC
                LIMIT 10
            );
            ai_prompt := 'You are a Snowflake cost analyst. ALERT: ' || :ev_title ||
                         '. EVIDENCE (top query families, elapsed hours on the day vs prior-7d avg): ' ||
                         COALESCE(:evidence, 'none') ||
                         '. Using ONLY this evidence, name the 1-2 most likely drivers with their ' ||
                         'numbers, or say evidence is inconclusive. Max 80 words. Never invent data.';
            ai_resp := SNOWFLAKE.CORTEX.COMPLETE(:ai_model, :ai_prompt);
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
               SET DETAIL = LEFT(COALESCE(DETAIL, '') || ' | AI: ' || :ai_resp, 2000)
             WHERE EVENT_ID = :ev_id;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cortex_pre_explain_unavailable',
                   'CORTEX.COMPLETE failed - events remain unexplained (drawer AI still works)',
                   'model or grant issue', CURRENT_ROLE();
    END;

    RETURN 'anomaly sweep v3 complete';
END;
$$;

-- ---------------------------------------------------------------------------------------------------------------
-- One-time repairs (after the CREATEs, before the version row). Idempotent and bounded; OVERWATCH tables only.
-- ---------------------------------------------------------------------------------------------------------------

-- R1 (R2-023) OBJECT_CHANGE_REGISTRY.COMPANY by the V044 classification. The registry is append-only (MERGE WHEN
-- NOT MATCHED), so every row the old scan stamped with the raw guess kept it: an unmapped database read ALFA, a
-- COMPANY_SCOPE override was ignored. Only rows whose stamp changes are written.
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET COMPANY = m.CO
  FROM (
      SELECT d.DATABASE_NAME, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME) AS CO
      FROM (SELECT DISTINCT DATABASE_NAME FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY) d
  ) m
 WHERE t.DATABASE_NAME = m.DATABASE_NAME
   AND t.COMPANY IS DISTINCT FROM m.CO;

-- R1b live PERF_CHANGE_REGRESSION events (OPEN / ACK / SNOOZED, not linked to an incident -- an incident keeps
-- the company it was declared under) follow: from the re-stamped registry row of the same key, else the database
-- split out of the object FQN in DEDUPE_KEY part 2 ('PERF_CHANGE_REGRESSION|DB.SCHEMA.NAME|date'; part 2 is never
-- passed to the UDF whole). Step 7 dedupes on the key only, so nothing re-raises.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS t
   SET COMPANY = s.NEW_COMPANY
  FROM (
      SELECT k.EVENT_ID, COALESCE(g.COMPANY, k.FQN_COMPANY) AS NEW_COMPANY
      FROM (
          SELECT x.EVENT_ID, x.DEDUPE_KEY, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS FQN_COMPANY
          FROM (
              SELECT e.EVENT_ID, e.DEDUPE_KEY, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB
              FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.RULE_ID = 'PERF_CHANGE_REGRESSION'
                AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
          ) x
      ) k
      LEFT JOIN (
          SELECT 'PERF_CHANGE_REGRESSION|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE) AS DEDUPE_KEY,
                 MAX(r.COMPANY) AS COMPANY
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          GROUP BY 1
      ) g ON g.DEDUPE_KEY = k.DEDUPE_KEY
  ) s
 WHERE t.EVENT_ID = s.EVENT_ID
   AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;

-- R2 (R2-024) live DQ_SCHEMA_DRIFT / PIPE_DT_FAILURES / PIPE_VOLUME_DROP / DQ_BREACH events re-stamped the same way:
-- all four keys are 'RULE|DB.SCHEMA.OBJECT|day', so the database is part 2 up to its first dot.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS t
   SET COMPANY = s.NEW_COMPANY
  FROM (
      SELECT x.EVENT_ID, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS NEW_COMPANY
      FROM (
              SELECT e.EVENT_ID, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB
              FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.RULE_ID IN ('DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP', 'DQ_BREACH')
                AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      ) x
  ) s
 WHERE t.EVENT_ID = s.EVENT_ID
   AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;

-- R4 (R2-025) still-tracking TASK baselines were frozen on raw attempts. Re-freeze them on the terminal attempt per
-- scheduled run over their own [CHANGE_SEEN_AT - 14d, CHANGE_SEEN_AT) window (30 days of TASK_HISTORY covers every
-- tracking row; no now-20d clip). The frozen credits numerator summed every attempt and still does, so credits/call
-- is rescaled by OLD_CALLS / NEW_CALLS (a factor of 1 on a re-run); a NULL credits/call stays NULL.
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
       BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS,
       BASELINE_CREDITS_PER_CALL = s.OLD_CPC * s.OLD_CALLS / NULLIF(s.CALLS, 0)
  FROM (
      SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
             COUNT_IF(h.STATE = 'FAILED') AS FAILS,
             MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
             APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS,
             MAX(r.BASELINE_CALLS) AS OLD_CALLS, MAX(r.BASELINE_CREDITS_PER_CALL) AS OLD_CPC
      FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
      JOIN (SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME, STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE SCHEDULED_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
              AND STATE IN ('SUCCEEDED', 'FAILED')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                       ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1) h
        ON h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
       AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
       AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME
      WHERE r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NOT NULL AND r.BASELINE_CALLS > 0
        AND CURRENT_DATE() <= r.TRACKING_UNTIL
      GROUP BY r.CHANGE_ID
  ) s
 WHERE t.CHANGE_ID = s.CHANGE_ID;

-- R3 (R2-021) the suffix match froze baselines that blended a RUN_<name> / X_<name> wrapper's calls into <name>.
-- Frozen baselines never recompute, so null them -- only for still-tracking PROCEDURE rows whose short name is a
-- strict suffix of another procedure's name (deleted procedures included: their old calls are still in the
-- window), and only once: a re-run must not undo a scan's re-freeze (each re-null re-freezes over a shorter
-- window). R3 runs LAST, directly before the version row, so a missing 172 row means R3 has not committed: every
-- statement that can stop the file (the five CREATEs, R1-R2, R4's 30-day TASK_HISTORY read) runs before it, and a
-- retry after any of them nulls these rows for the first time, even if a scan ran in between. The next
-- TASK_CHANGE_IMPACT_SCAN re-freezes them with the anchored match (over the scan's own 20-day reach: a change
-- older than 6 days gets a shorter baseline, at least 6 days).
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,
       BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL
  FROM (
      SELECT DISTINCT r.CHANGE_ID
      FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
      JOIN SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES p
        ON ENDSWITH(UPPER(p.PROCEDURE_NAME), UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3)))
       AND UPPER(p.PROCEDURE_NAME) <> UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3))
      WHERE r.OBJECT_TYPE = 'PROCEDURE'
        AND CURRENT_DATE() <= r.TRACKING_UNTIL
        AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172)
  ) s
 WHERE t.CHANGE_ID = s.CHANGE_ID;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 172 AS VERSION,
       'Detection scans (V166-V172 wave, detection cluster). SP_CHANGE_IMPACT_SCAN re-derived from V140: COMPANY via COMPANY_FOR_DATABASE in both registration arms (R2-023; arm 1a through a grouped derived table, the V030 shape); procedure calls matched by CALL<name>( or .<name>( over a whitespace-class strip, the drill rule, so RUN_<name> no longer blends into <name> (R2-021); TASK runs, fails and p95 count the terminal attempt per scheduled run (R2-025); the AFTER credits/call counts settled runs (started more than 8h ago) only, LEFT JOIN plus HAVING, divided by distinct scheduled runs (R2-022); VERDICT_DETAIL p95 in Hr/Min/Sec (R1-124). SP_WAREHOUSE_CHANGE_SCAN re-derived from V109: VERDICT_DETAIL p95 and queue in Hr/Min/Sec (R1-124). SP_SCAN_SCHEMA_DRIFT re-derived from V133 and SP_ANOMALY_SWEEP from V150: PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH and DQ_SCHEMA_DRIFT COMPANY via COMPANY_FOR_DATABASE in a b wrapper (R2-024); the sweep also points COST_ORG_ACCOUNT_CREEP at Cost Intelligence > Contract & Forecast (R2-095), books COST_ANOMALY_SWEEP only while the rule is enabled (R1-227 rider, no early return) and reads CORTEX_MODEL normalized like the app (CORTEX-NULLIF); RETURN stays v3. SP_SCAN_CLOUD_SVC_ANOMALY re-derived from V150: the disabled-rule guard counts ENABLED rows (R1-227). One-time repairs: OBJECT_CHANGE_REGISTRY.COMPANY re-stamped, and live (OPEN, ACK, SNOOZED, not incident-linked) PERF_CHANGE_REGRESSION, DQ_SCHEMA_DRIFT, PIPE_DT_FAILURES, PIPE_VOLUME_DROP and DQ_BREACH events re-stamped from the database in their object FQN; tracking TASK baselines re-frozen on the terminal-attempt basis with credits/call rescaled; last, right before this row, a first-apply null of suffix-collided tracking PROCEDURE baselines (re-frozen by the next scan). No new object, no task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172);

-- =====================================================================
--  STAGE 2 -- QUICK CHECK (read-only, now): V162 through V172 are registered -- expect 11 rows.
-- =====================================================================
SELECT VERSION, APPLIED_AT, APPLIED_BY
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION BETWEEN 162 AND 172
ORDER BY VERSION;

-- NEXT (snowflake/run/README_V166_V172.md): OWNER_REPAIRS_V166_V172.sql PART 1 now -- its V167 AI re-key must run
-- before the next 06:45 Central DAILY marts run -- then PART_B_V166_V172.sql's 'now' grids.
ALTER SESSION UNSET TIMEZONE;
