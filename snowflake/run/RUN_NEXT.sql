-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql
--  APPLY V156, V157, V158, V159 (in order) + PART B verify. FOUR pending migrations
--  (Next-Fifty wave 2b, reworked for cloud-services cost; app v4.594.0 = main c797c75 reads all four).
--
--  BEFORE THIS FILE:
--    1. Deploy the 4.594.0 app (snow streamlit deploy --replace): playbooks, navigation, captions and the
--       backup-retention editors must be live first (until then Admin reads the new SETTINGS keys as
--       "safe to delete").
--    2. Run snowflake/run/PREFLIGHT_WAVE2B.sql (read-only): the warehouses that get COST_IDLE_OPPORTUNITY on
--       the first daily run.
--
--  Apply top-to-bottom as SNOW_ACCOUNTADMINS and STOP ON THE FIRST ERROR (do not run past a failed
--  statement: V157's SEC auto-clear opt-in must never land on V141's unscoped sweep). Each migration guards
--  on the prior (V156 needs V155 ... V159 needs V158) and all are idempotent, so an already-applied one
--  no-ops. There is NO apply-time scan CALL (a hand CALL of a scan can email). V158's tail EXECUTE TASK runs
--  the backup once, asynchronously -- run the V158 PART B grid about 2 minutes after apply.
--
--  V156 (#2):  ETL_CYCLE_TASKS + SP_SCAN_ETL_CYCLE(): overnight ETL cycle PUSH alerts (PIPE_ETL_TASK_FAILED /
--              _NOT_STARTED / _LATE); works only in its run window (ETL_SLA_TARGET_HHMM - 10h .. + 3h
--              Central, plus 15:00). Nothing calls it until V157.
--  V157:       one re-derivation of SP_ALERT_SCAN + SP_ALERT_SCAN_DAILY from V141: OPS_PIPELINE_DEGRADED
--              self-watch, weekly COST_IDLE_OPPORTUNITY, the V156 add-on, condition-ended sweep; compile
--              diet (SEC_NEW_EXPOSURE + SEC_CRED_EXPIRY every 4h, self-watch every 3h, dead break-glass arm
--              removed, COST_CLOUD_SVC_RATIO retired -- its row deleted, its open events closed EXPECTED).
--  V158 (#32): daily operator-data backups in the new TRANSIENT schema OVERWATCH_BAK (14 daily + 8 weekly),
--              task moved to 05:10 daily; Security-queue carve-out for the task's own prune DROPs.
--  V159:       loader compile diet: the three day-grain marts refresh every 4th Central hour (always on the
--              nightly reconcile); change attribution runs only when a new change is unattributed.
--
--  Requires V155 applied (V156's guard). V151-V155 confirmed applied 2026-09-26.
--  Rollback notes: RUNBOOK section 12 (V157: switch AUTO_CLEAR_ENABLED off for SEC_CRED_EXPIRY /
--  SEC_NEW_EXPOSURE FIRST, then re-run V141) and section 16 (V158).
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- the task clock: every new gate and stamp is Central

-- =====================================================================
--  MIGRATION 1 of 4 -- APPLY V156 (idempotent; GUARDS on V155; new objects, no tail CALL). Source: snowflake/migrations/V156__etl_cycle_push_alerts.sql
-- =====================================================================
-- V156__etl_cycle_push_alerts.sql
--
-- Next-Fifty rank 2: PUSH the nightly Informatica cycle trouble overnight instead of waiting for someone to
-- open the app. The pull side shipped app-only in v4.589 (Operations > Pipeline SLA > Tonight and the Brief
-- Nightly-cycle tile, via etl_control_sql.cycle_night_health_scan / cycle_finish_history_scan and
-- insights.etl_cycle_sla_forecast). Until now no database-side object read CONTROL_STATUS (V134 and V138
-- only seed its settings). Config-driven, same SETTINGS as those panels: ETL_CONTROL_STATUS_FQN plus the V138
-- keys ETL_CYCLE_START_WORKFLOW / ETL_CYCLE_END_WORKFLOW / ETL_SLA_TARGET_HHMM / ETL_SLA_BREACH_HHMM (a missing
-- row falls back to the app DEFAULT_SETTINGS value, a blank row disables the rule that needs it). Adds:
--   * ETL_CYCLE_TASKS     - transient scan cache: the last 23 WHOLE nights of CONTROL_STATUS (cut by night
--                           key, so the oldest cached night is never partial), one row per (night, workflow,
--                           task) with Informatica retries collapsed to the terminal attempt (the MAX_BY idiom;
--                           TERMINAL_START = that attempt's own start), night-keyed by DATE(TASK_START_DTTM -
--                           12h) like the app, plus FIRST_OK_END = the earliest clean finish among the attempts
--                           that STARTED at/after that night's LAST starter start (the night's last kickoff;
--                           the starter name is a bound value).
--   * SP_SCAN_ETL_CYCLE() - inside its RUN WINDOW (below) allowlist-validates the FQN, rebuilds ETL_CYCLE_TASKS
--                           with one EXECUTE IMMEDIATE, then raises three rules itself, each inside its own
--                           EXCEPTION guard:
--       PIPE_ETL_TASK_FAILED       MEDIUM (HIGH for the terminal workflow). One event per (workflow, night)
--                                  whose final-attempt failed-task count >= THRESHOLD_NUM (1; a threshold
--                                  below 1 still needs one failure). Auto-clears (AUTO_CLEARED, OPEN-only)
--                                  when a retry later succeeds: every final attempt of that workflow-night has
--                                  FINISHED and none failed (a retry still running keeps the event OPEN, so a
--                                  retry that fails again never re-raises it). The clear UPDATE runs only when
--                                  an OPEN PIPE_ETL_TASK_FAILED event exists (one EXISTS probe otherwise).
--       PIPE_ETL_CYCLE_NOT_STARTED HIGH. The app NEXT_CYCLE_OVERDUE test: the starter has been silent longer
--                                  than 24h + grace AND a night it ran on last week has come round again, past
--                                  last week kickoff + grace, with no run. THRESHOLD_NUM = grace minutes (120 =
--                                  the app 7200s). One event per missed night.
--       PIPE_ETL_CYCLE_LATE        HIGH, or CRITICAL for an unfinished miss. Tonight = the newest night the
--                                  starter ran. Banded in the dedupe key: WARN = terminal unfinished within
--                                  THRESHOLD_NUM (60) minutes of the target, or the projection lands past the
--                                  hard deadline; CRIT = past the target; EXH = past the hard deadline. The
--                                  projection is a new SQL heuristic sharing SLA_FORECAST_MIN_RUNS /
--                                  SLA_FORECAST_FIT_NIGHTS (the median start-to-finish of the newest 14 prior
--                                  clean nights, >= 4 needed, shifted by tonight's start), not the app's
--                                  Theil-Sen margin trend. A night that FINISHED late raises CRIT/EXH at the
--                                  rule severity (HIGH: email, no incident); a night still unfinished past a
--                                  deadline raises CRITICAL, which SP_INCIDENT_AUTODECLARE turns into an
--                                  incident within about an hour (while INCIDENT_AUTO_DECLARE_CRITICAL is on).
--                                  The V067 supersede sweep in SP_ALERT_SCAN resolves the lower band on
--                                  escalation. METRIC_VALUE = minutes left to the target, written only for a
--                                  lead-window WARN (NULL otherwise), so LOWER_IS_WORSE tuning reads only the
--                                  lead THRESHOLD_NUM actually sets. A terminal task is done at its first
--                                  clean finish after the night's last kickoff (FIRST_OK_END: the attempt must
--                                  START at/after it), so a re-run of it after the night finished (e.g. a
--                                  next-morning recon re-run keyed to the same night) never re-grades an
--                                  on-time night, and once the real kickoff runs an attempt that started
--                                  before it (an afternoon re-run of the whole chain, even one running across
--                                  the kickoff) is never its FIRST_OK_END;
--                                  a task without one counts only when its terminal attempt started at/after
--                                  that night's cycle start (an afternoon terminal re-run keyed to the same
--                                  night neither completes nor hides the night); a night whose terminal has
--                                  not dispatched is judged only when the terminal ran on the same night last
--                                  week (the app RAN_LAST_WEEK half of its regular-workflow test), so a
--                                  weekday-only terminal stays quiet.
--     No starter run = no LATE for that night; TASK_FAILED still reports any workflow failure keyed after the
--     last cycle start. NOT_STARTED is the one rule that speaks on a missed night, with the SAME test as the
--     app Cycle start: Overdue tile (a holiday on a night that ran last week fires it: resolve as EXPECTED).
--   * three ALERT_CONFIG rules (PIPELINE), WHEN NOT MATCHED only. AUTO_CLEAR_ENABLED is left at its default
--     (FALSE): the V091 sweep recomputes only PERF scopes, and TASK_FAILED carries its own retry auto-clear.
-- The clock is pinned to America/Chicago (CONTROL_STATUS timestamps are naive Central, like the app assumes).
-- RUN WINDOW (wave-2b rework, compile diet; no task or schedule change). The hourly alert scan (about :08-:15
-- past each hour, after the :07 root) calls this every hour, but it works only in the Central hours from
-- HOUR(target - 10h) through HOUR(target + 3h) inclusive, wrapping midnight (target = ETL_SLA_TARGET_HHMM as
-- parsed below, blank or malformed -> 07:00), plus one daytime pass in the 15:xx scan: at 07:00 the 21:xx ..
-- 10:xx scans + 15:xx = 15 of 24 calls. Out of window a call reads only the rule count and SETTINGS and returns
-- BEFORE the cache DELETE, so ETL_CYCLE_TASKS keeps the last in-window scan's nights (the playbook triage
-- queries read it). The window holds the ~22:00 kickoff + the NOT_STARTED grace, the lead-window WARN, the
-- target (CRIT) and the default hard deadline (EXH). LATENCY TRADE (by choice): a TASK_FAILED from a daytime
-- re-run, and the retry auto-clear of an OPEN one, surface at the next in-window scan (the 15:xx pass or the
-- window start), up to ~6h later than hourly, and a daytime failure a retry fixes before then is never raised;
-- an EXH crossing after the window's last scan (a hard deadline more than ~3h after the target) waits for the
-- 15:xx pass (the unfinished CRIT has paged CRITICAL by then); a starter whose usual kickoff + grace falls
-- before the window opens is judged NOT_STARTED at the window start.
-- Known edges (documented, not fixed). FIRST_OK_END must START at/after the night's LAST starter start, so a
-- starter attempt keyed to the night that starts after a terminal attempt started voids it (a next-morning
-- re-run of the STARTER, or a starter task that starts while or after the terminal runs): that night then grades each terminal
-- task on its latest attempt, as before FIRST_OK_END, so a terminal re-run alongside it re-grades a night that
-- finished on time by the re-run's own clock (a CRIT/EXH that is CRITICAL while it runs, HIGH once it ends
-- clean); a starter re-run alone stays quiet. When ETL_CYCLE_START_WORKFLOW = ETL_CYCLE_END_WORKFLOW every task
-- is a starter task, so a next-morning re-run of ANY task keyed to the night does the same. Both fail LOUD, by
-- choice: a special case for one workflow would let an afternoon re-run of it complete the night and silence a
-- hung real run. When BOTH the starter and the terminal are re-run in the afternoon, CYCLE_START = MIN(starter
-- start) is the afternoon run (the app's MIN semantics); once the real kickoff runs, an afternoon attempt that
-- started before it never counts as FIRST_OK_END, but it still reads the night complete until the real terminal
-- run starts. SILENT residual 1: a real cycle that hangs before its terminal dispatches stays quiet that night
-- (TASK_FAILED still reports a failed real run). SILENT residual 2: an afternoon-chain terminal attempt that STARTS after the real
-- kickoff (a chain started late in the afternoon, e.g. 16:00 with its terminal at 22:30) cannot be told from a
-- real early run, so its clean finish is FIRST_OK_END and a hung, failed or late real terminal that night
-- raises no LATE (TASK_FAILED still reports a failed one). A terminal task removed for good reads the first night after as
-- incomplete (the usual task count comes from the prior clean nights). Divergence from the app (deliberate): the
-- pull side (cycle_finish_history_scan, the SLA finish forecast) reads each terminal task's LATEST attempt, so a
-- next-morning terminal re-run of a night that already finished shows there as running / failed while LATE stays
-- quiet.
-- Wiring: V157 re-derives the hourly alert scan with an add-on CALL arm [23] for this proc, outside the core
-- tally (a CONTROL_STATUS grant gap logs etl_cycle_scan_failed and never trips OPS_SCAN_DEGRADED); until V157
-- is applied nothing calls it. A hand CALL outside the RUN WINDOW only returns the skip string. HIGH/CRITICAL
-- reach the OVERWATCH_EMAIL path (NATIVE_ALERT_NEW_EVENTS) and any matching Teams route. Needs the SELECT on
-- CONTROL_STATUS the app panels already use (owner role). No task change, no tail CALL. Owner applies in
-- Snowsight after V155. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20156, 'V156 requires V155 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 155) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS (
    CYCLE_DATE      DATE          NOT NULL,
    WORKFLOW_NAME   VARCHAR(500)  NOT NULL,
    TASK_NAME       VARCHAR(500)  NOT NULL,
    TERMINAL_STATUS VARCHAR(100),
    FIRST_START     TIMESTAMP_NTZ NOT NULL,
    TERMINAL_START  TIMESTAMP_NTZ,
    TERMINAL_END    TIMESTAMP_NTZ,
    FIRST_OK_END    TIMESTAMP_NTZ,
    SCANNED_AT      TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('PIPE_ETL_CYCLE_LATE', 'PIPELINE', 'Nightly ETL cycle late: unfinished THRESHOLD_NUM min before the SLA target, projected past the hard deadline, or finished late', TRUE, 'HIGH', 60, 24),
        ('PIPE_ETL_CYCLE_NOT_STARTED', 'PIPELINE', 'Nightly ETL cycle did not start: starter silent THRESHOLD_NUM min past the same night last week', TRUE, 'HIGH', 120, 24),
        ('PIPE_ETL_TASK_FAILED', 'PIPELINE', 'Nightly ETL task failures: workflow tasks whose final attempt failed tonight (count >= THRESHOLD_NUM)', TRUE, 'MEDIUM', 1, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Config-driven nightly-ETL-cycle scan (Next-Fifty rank 2, V156): the PUSH twin of Operations > Pipeline SLA >
-- Tonight. One dynamic read of the configured CONTROL_STATUS table (FQN allowlist-validated, the same RLIKE as
-- V137) into ETL_CYCLE_TASKS; every rule below is static SQL over that cache. The caller (SP_ALERT_SCAN, V157)
-- wraps the CALL in its own EXCEPTION guard, so a missing grant on CONTROL_STATUS is contained; each rule
-- block is also isolated here, so one bad rule never silences the other two. Mirrors, and is parity-tested
-- against: etl_control_sql.FAILED_TASK_STATUSES / cycle_night_health_scan (anchor, missed, RAN_LAST_WEEK) /
-- cycle_finish_history_scan (cyc_start, term, cyc_end) and insights._parse_hhmm / _deadline_after. The LATE
-- projection is a new SQL heuristic sharing SLA_FORECAST_MIN_RUNS / SLA_FORECAST_FIT_NIGHTS (not parity).
-- Called every hour, it works only inside the RUN WINDOW (target - 10h .. target + 3h Central, plus a 15:00
-- pass); an out-of-window call returns after the rule count and the SETTINGS read, cache untouched.
DECLARE
    ctl_fqn STRING;
    start_wf STRING;
    end_wf STRING;
    target_raw STRING;
    breach_raw STRING;
    target_off INT DEFAULT 420;
    breach_off INT DEFAULT 480;
    lookback_days INT DEFAULT 23;
    n_enabled INT;
    n_rows INT;
    n_start_rows INT;
    n_end_rows INT;
    now_ct TIMESTAMP_NTZ;
    ins_sql STRING;
    emsg VARCHAR;
    n_failed INT DEFAULT 0;
    n_nostart INT DEFAULT 0;
    n_late INT DEFAULT 0;
BEGIN
    -- gate: only scan when at least one of the three rules exists AND is enabled.
    SELECT COUNT(*) INTO :n_enabled
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID IN ('PIPE_ETL_CYCLE_LATE', 'PIPE_ETL_CYCLE_NOT_STARTED', 'PIPE_ETL_TASK_FAILED') AND ENABLED;

    -- a missing row falls back to DEFAULT_SETTINGS (app/config.py ETL_CYCLE_* / ETL_SLA_*); a blank row
    -- stays blank and disables the rule that needs it, exactly like the app's merged settings.
    SELECT MAX(IFF(KEY = 'ETL_CONTROL_STATUS_FQN', VALUE, NULL)),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_CYCLE_START_WORKFLOW', VALUE, NULL)), 'WF_BASE_GW_CLOSEOUT_CTL_DLY')),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_CYCLE_END_WORKFLOW', VALUE, NULL)), 'WF_BASE_RECON_MTRC_CMPSIT_DAILY')),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_SLA_TARGET_HHMM', VALUE, NULL)), '07:00')),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_SLA_BREACH_HHMM', VALUE, NULL)), '08:00'))
      INTO :ctl_fqn, :start_wf, :end_wf, :target_raw, :breach_raw
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    -- 'HH:MM' (24h) -> minutes past midnight; malformed -> 07:00 / 08:00 (insights._parse_hhmm), and a hard
    -- deadline at or before the target becomes target + 60 min (insights.etl_cycle_sla_forecast).
    target_off := IFF(RLIKE(:target_raw, '^[0-9]{1,2}:[0-9]{1,2}$')
                      AND TRY_TO_NUMBER(SPLIT_PART(:target_raw, ':', 1)) <= 23
                      AND TRY_TO_NUMBER(SPLIT_PART(:target_raw, ':', 2)) <= 59,
                      TRY_TO_NUMBER(SPLIT_PART(:target_raw, ':', 1)) * 60 + TRY_TO_NUMBER(SPLIT_PART(:target_raw, ':', 2)),
                      420);
    breach_off := IFF(RLIKE(:breach_raw, '^[0-9]{1,2}:[0-9]{1,2}$')
                      AND TRY_TO_NUMBER(SPLIT_PART(:breach_raw, ':', 1)) <= 23
                      AND TRY_TO_NUMBER(SPLIT_PART(:breach_raw, ':', 2)) <= 59,
                      TRY_TO_NUMBER(SPLIT_PART(:breach_raw, ':', 1)) * 60 + TRY_TO_NUMBER(SPLIT_PART(:breach_raw, ':', 2)),
                      480);
    IF (:breach_off <= :target_off) THEN
        breach_off := :target_off + 60;
    END IF;

    -- TIMEZONE STANDARD: the scan clock is Central wall-clock NTZ, the same basis as CONTROL_STATUS.
    now_ct := CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ;

    -- RUN WINDOW (wave-2b rework, compile diet): work only in the Central hours from HOUR(target - 10h)
    -- through HOUR(target + 3h), both ends inclusive and wrapping midnight (07:00 -> the 21:xx .. 10:xx
    -- scans; a blank or malformed ETL_SLA_TARGET_HHMM already fell back to 07:00 above), plus one daytime
    -- pass in the 15:xx scan (daytime TASK_FAILED re-runs). MOD(... + 24, 24) = hours since the window
    -- opened (the + 1440 / + 24 keep MOD's dividend non-negative). Out of window this RETURNs BEFORE the
    -- DELETE below, so ETL_CYCLE_TASKS keeps the last in-window scan's nights for the playbook triage queries.
    IF (MOD(HOUR(:now_ct) - FLOOR(MOD(:target_off - 600 + 1440, 1440) / 60) + 24, 24) > 13
        AND HOUR(:now_ct) <> 15) THEN
        RETURN 'etl cycle scan skipped (outside the run window: Central hour ' || HOUR(:now_ct)
               || ' is not in HOUR(ETL_SLA_TARGET_HHMM - 10h) .. HOUR(target + 3h) or 15; cache kept)';
    END IF;

    -- every in-window run clears last run's cache first, so a stale night never lingers after a disable or a fix.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS;
    IF (:n_enabled = 0) THEN
        RETURN 'etl cycle scan skipped (rules disabled)';
    END IF;

    IF (:ctl_fqn IS NULL OR TRIM(:ctl_fqn) = ''
        OR NOT RLIKE(TRIM(:ctl_fqn), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')) THEN
        RETURN 'etl cycle scan skipped (unconfigured or invalid ETL_CONTROL_STATUS_FQN)';
    END IF;

    -- The ONLY dynamic statement. The FQN is validated above (a bare, well-formed identifier), so it is safe
    -- to concatenate; lookback_days is an INT; the starter name is BOUND as data (USING), never concatenated.
    -- WHOLE nights only: a row is kept when its night key is one of the newest lookback_days nights (the
    -- timestamp prefilter, one day wider, only prunes), so the oldest cached night is never a partial night
    -- (a partial one would let the retry auto-clear below resolve a failure that was never re-run). One row per
    -- (night, workflow, task): the terminal attempt's status / start / end by COALESCE(end, start) (a
    -- FAILED-then-retried-SUCCESS task reads SUCCESS), the first start of any attempt, and FIRST_OK_END = the
    -- earliest clean finish (ended, not a failed status) among the attempts that STARTED at/after that night's
    -- LAST starter start (CYC_LAST_START, the night's last kickoff), so a re-run after the night already finished
    -- (a next-morning recon re-run keyed to the same night) never re-opens it, while an attempt that started
    -- before the real kickoff (an afternoon re-run of the terminal, or of the whole chain, even one still running
    -- across the kickoff) is never the night's first clean finish. No special case when the starter IS the
    -- terminal workflow (the header's known edges: it fails loud, never silent).
    ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS '
               || '(CYCLE_DATE, WORKFLOW_NAME, TASK_NAME, TERMINAL_STATUS, FIRST_START, TERMINAL_START, TERMINAL_END, FIRST_OK_END) '
               || 'WITH src AS ('
               || 'SELECT DATE(DATEADD(''hour'', -12, TASK_START_DTTM::TIMESTAMP_NTZ)) AS CD, '
               || 'LEFT(TO_VARCHAR(WORKFLOW_NAME), 500) AS WF, LEFT(TO_VARCHAR(TASK_NAME), 500) AS TN, '
               || 'TASK_STATUS, TASK_START_DTTM, TASK_END_DTTM '
               || 'FROM ' || TRIM(:ctl_fqn) || ' '
               || 'WHERE TASK_START_DTTM IS NOT NULL AND WORKFLOW_NAME IS NOT NULL AND TASK_NAME IS NOT NULL '
               || 'AND TASK_START_DTTM >= DATEADD(''day'', -' || (:lookback_days + 1) || ', '
               || 'CONVERT_TIMEZONE(''America/Chicago'', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) '
               || 'AND DATE(DATEADD(''hour'', -12, TASK_START_DTTM::TIMESTAMP_NTZ)) > DATEADD(''day'', -' || :lookback_days || ', '
               || 'DATE(DATEADD(''hour'', -12, CONVERT_TIMEZONE(''America/Chicago'', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)))), '
               || 'cs AS (SELECT CD AS CS_NIGHT, '
               || 'MAX(TASK_START_DTTM) AS CYC_LAST_START FROM src WHERE WF = ? GROUP BY CD) '
               || 'SELECT CD, WF, TN, '
               || 'LEFT(TO_VARCHAR(MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))), 100), '
               || 'MIN(TASK_START_DTTM)::TIMESTAMP_NTZ, '
               || 'MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ, '
               || 'MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ, '
               || 'MIN(IFF(TASK_END_DTTM IS NOT NULL '
               || 'AND COALESCE(UPPER(TASK_STATUS), '''') NOT IN (''ABORTED'', ''ERROR'', ''ERRORED'', ''FAILED'', ''KILLED'', ''STOPPED'', ''TERMINATED'') '
               || 'AND TASK_START_DTTM >= CYC_LAST_START, TASK_END_DTTM, NULL))::TIMESTAMP_NTZ '
               || 'FROM src LEFT JOIN cs ON cs.CS_NIGHT = src.CD '
               || 'GROUP BY CD, WF, TN';
    EXECUTE IMMEDIATE :ins_sql USING (start_wf);

    SELECT COUNT(*), COUNT_IF(WORKFLOW_NAME = :start_wf), COUNT_IF(WORKFLOW_NAME = :end_wf)
      INTO :n_rows, :n_start_rows, :n_end_rows
    FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS;
    IF (:n_rows = 0) THEN
        RETURN 'etl cycle scan: no CONTROL_STATUS rows in the last ' || :lookback_days || ' days (nothing to judge)';
    END IF;

    -- A renamed anchor workflow: a missing STARTER silences LATE / NOT_STARTED, a missing TERMINAL leaves LATE
    -- judging nothing. Record it once per Central day (logged, not paged).
    IF ((:start_wf <> '' AND :n_start_rows = 0) OR (:end_wf <> '' AND :n_end_rows = 0)) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'AlertScan', 'etl_cycle_scan_misconfig',
               LEFT('No CONTROL_STATUS run in ' || :lookback_days || ' days for '
                    || IFF(:start_wf <> '' AND :n_start_rows = 0, 'ETL_CYCLE_START_WORKFLOW ' || :start_wf || ' ', '')
                    || IFF(:end_wf <> '' AND :n_end_rows = 0, 'ETL_CYCLE_END_WORKFLOW ' || :end_wf, '')
                    || ' - PIPE_ETL_CYCLE_LATE / PIPE_ETL_CYCLE_NOT_STARTED cannot judge the cycle', 2000),
               'SP_SCAN_ETL_CYCLE - check Admin > SETTINGS', CURRENT_ROLE()
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            WHERE ERROR_TYPE = 'etl_cycle_scan_misconfig'
              AND LOGGED_AT >= DATE_TRUNC('day', :now_ct)
        );
    END IF;

    -- [A] PIPE_ETL_TASK_FAILED: every workflow of tonight (nights >= the newest night the starter ran; any
    --     workflow when no starter is set) with a final-attempt failure. Terminal workflow -> at least HIGH.
    --     GREATEST(..., 1): a THRESHOLD_NUM of 0 would raise every workflow and the auto-clear below would
    --     resolve them all again, hourly.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT RULE_ID, SEVERITY, COALESCE(THRESHOLD_NUM, 1) AS MIN_FAILED
            FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
            WHERE RULE_ID = 'PIPE_ETL_TASK_FAILED' AND ENABLED
        ),
        anchor AS (
            SELECT COALESCE(MAX(IFF(WORKFLOW_NAME = :start_wf, CYCLE_DATE, NULL)), MAX(CYCLE_DATE)) AS CYCLE_DATE
            FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS
        ),
        wf AS (
            SELECT t.CYCLE_DATE, t.WORKFLOW_NAME,
                   COUNT(*) AS TASK_COUNT,
                   COUNT_IF(UPPER(t.TERMINAL_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED')) AS FAILED_TASK_COUNT,
                   LISTAGG(IFF(UPPER(t.TERMINAL_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED'),
                               t.TASK_NAME || ' (' || t.TERMINAL_STATUS || ')', NULL), ', ')
                       WITHIN GROUP (ORDER BY t.TASK_NAME) AS FAILED_TASKS
            FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t
            JOIN anchor a ON t.CYCLE_DATE >= a.CYCLE_DATE
            GROUP BY t.CYCLE_DATE, t.WORKFLOW_NAME
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL',
               IFF(w.WORKFLOW_NAME = :end_wf AND c.SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', c.SEVERITY),
               LEFT(w.WORKFLOW_NAME || ': ' || w.FAILED_TASK_COUNT || ' of ' || w.TASK_COUNT
                    || ' task(s) failed in the ' || TO_VARCHAR(w.CYCLE_DATE) || ' nightly cycle', 300),
               LEFT('Final attempt failed (Informatica retries collapse to the last attempt): '
                    || LEFT(w.FAILED_TASKS, 1500) || '. '
                    || IFF(w.WORKFLOW_NAME = :end_wf,
                           'This is the cycle TERMINAL workflow - the cycle cannot finish until it is re-run. ', '')
                    || 'Informatica may also have notified. Triage: Operations > Pipeline SLA > Tonight at a glance. '
                    || 'Auto-clears if a retry succeeds.', 2000),
               w.FAILED_TASK_COUNT,
               'PIPE_ETL_TASK_FAILED|' || LEFT(w.WORKFLOW_NAME, 200) || '|' || TO_VARCHAR(w.CYCLE_DATE)
        FROM cfg c
        JOIN wf w ON w.FAILED_TASK_COUNT >= GREATEST(c.MIN_FAILED, 1)
        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- a re-failure after a retry recovery re-alerts (V091 idiom)
        );
        n_failed := SQLROWCOUNT;

        -- retry recovered: resolve the still-OPEN event whose (workflow, night) is in the cache with zero
        -- final-attempt failures AND every final attempt finished: a retry that has only STARTED (no end yet)
        -- keeps the event OPEN, so a retry that then fails again never mints a second event and email.
        -- OPEN-only (an ACK or SNOOZE is a human decision, left alone). The cache holds whole nights only, so
        -- its oldest night is never a partial one whose failed tasks were cut away. Skipped (one EXISTS probe,
        -- no GROUP BY over the cache) when no PIPE_ETL_TASK_FAILED event is OPEN: the probe is the UPDATE's own
        -- first two predicates, so a skipped run is exactly a run that would have resolved nothing.
        IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                    WHERE RULE_ID = 'PIPE_ETL_TASK_FAILED' AND STATUS = 'OPEN')) THEN
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
               SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'
             WHERE RULE_ID = 'PIPE_ETL_TASK_FAILED'
               AND STATUS = 'OPEN'
               AND DEDUPE_KEY IN (
                   SELECT 'PIPE_ETL_TASK_FAILED|' || LEFT(t.WORKFLOW_NAME, 200) || '|' || TO_VARCHAR(t.CYCLE_DATE)
                   FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t
                   GROUP BY t.WORKFLOW_NAME, t.CYCLE_DATE
                   HAVING COUNT_IF(UPPER(t.TERMINAL_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED')) = 0
                      AND COUNT_IF(t.TERMINAL_END IS NULL) = 0
               );
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'etl_cycle_scan_failed', LEFT(:emsg, 2000),
                   'rule PIPE_ETL_TASK_FAILED - other ETL cycle rules unaffected', CURRENT_ROLE();
    END;

    -- [B] PIPE_ETL_CYCLE_NOT_STARTED: the app NEXT_CYCLE_OVERDUE test (etl_control_sql.cycle_night_health_scan
    --     'missed' + 'cyc_now'), with the grace (+2h there) read from THRESHOLD_NUM. The dedupe key is the
    --     MISSED night itself, so each expected-but-silent night alerts once.
    IF (:start_wf <> '') THEN
        BEGIN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
            WITH cfg AS (
                SELECT RULE_ID, SEVERITY, GREATEST(ROUND(COALESCE(THRESHOLD_NUM, 120)), 0) AS GRACE_MIN
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                WHERE RULE_ID = 'PIPE_ETL_CYCLE_NOT_STARTED' AND ENABLED
            ),
            cyc AS (
                SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START_AT
                FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS
                WHERE WORKFLOW_NAME = :start_wf
                GROUP BY CYCLE_DATE
            ),
            anchor AS (
                SELECT CYCLE_DATE, CYCLE_START_AT
                FROM cyc
                QUALIFY ROW_NUMBER() OVER (ORDER BY CYCLE_DATE DESC) = 1
            ),
            missed AS (
                SELECT c.RULE_ID, c.SEVERITY, c.GRACE_MIN, a.CYCLE_START_AT AS LAST_START,
                       MAX(IFF(DATEADD('minute', 7 * 1440 + c.GRACE_MIN, p.CYCLE_START_AT) < :now_ct,
                               p.CYCLE_DATE, NULL)) AS REF_NIGHT,
                       MAX(IFF(DATEADD('minute', 7 * 1440 + c.GRACE_MIN, p.CYCLE_START_AT) < :now_ct,
                               p.CYCLE_START_AT, NULL)) AS REF_START
                FROM cfg c
                CROSS JOIN anchor a
                JOIN cyc p
                  ON p.CYCLE_DATE BETWEEN DATEADD('day', -6, a.CYCLE_DATE)
                                      AND DATEADD('day', -7, DATE(DATEADD('hour', -12, :now_ct)))
                GROUP BY c.RULE_ID, c.SEVERITY, c.GRACE_MIN, a.CYCLE_START_AT
            )
            SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
            FROM (
            SELECT m.RULE_ID, 'ALL', m.SEVERITY,
                   LEFT('Nightly ETL cycle did not start: ' || :start_wf || ' has no run for the '
                        || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT)) || ' night', 300),
                   LEFT('It kicked off at ' || TO_VARCHAR(m.REF_START, 'HH24:MI') || ' on the same night last week; '
                        || 'now ' || FLOOR(DATEDIFF('minute', DATEADD('day', 7, m.REF_START), :now_ct) / 60) || 'h '
                        || MOD(DATEDIFF('minute', DATEADD('day', 7, m.REF_START), :now_ct), 60) || 'm past that (grace '
                        || m.GRACE_MIN || ' min). Last cycle start ' || TO_VARCHAR(m.LAST_START, 'YYYY-MM-DD HH24:MI')
                        || ' (Central). Nothing downstream loads until the starter runs - check the Informatica '
                        || 'scheduler / integration service. A planned no-run night (holiday, freeze) = resolve as '
                        || 'EXPECTED. Operations > Pipeline SLA > Tonight at a glance shows the same Cycle start: Overdue.', 2000),
                   DATEDIFF('minute', DATEADD('day', 7, m.REF_START), :now_ct),
                   'PIPE_ETL_CYCLE_NOT_STARTED|' || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT))
            FROM missed m
            WHERE m.REF_NIGHT IS NOT NULL
              AND DATEDIFF('second', m.LAST_START, :now_ct) > 86400 + m.GRACE_MIN * 60
            ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
            WHERE NOT EXISTS (
                SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
            );
            n_nostart := SQLROWCOUNT;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'AlertScan', 'etl_cycle_scan_failed', LEFT(:emsg, 2000),
                       'rule PIPE_ETL_CYCLE_NOT_STARTED - other ETL cycle rules unaffected', CURRENT_ROLE();
        END;
    END IF;

    -- [C] PIPE_ETL_CYCLE_LATE: tonight = the newest night the starter ran (cycle_finish_history_scan
    --     cyc_start / term / cyc_end). Deadline = the first target clock time STRICTLY after the cycle start
    --     (insights._deadline_after, cross-midnight); hard = target + (breach - target). Complete = the terminal
    --     has run at least its usual task count, none still running, none failed (retries collapsed; a task
    --     with a clean finish from an attempt that STARTED at/after the night's last kickoff is done at
    --     FIRST_OK_END, whatever a later re-run of it is doing).
    --     Severity: WARN and a night that FINISHED late take the rule severity (HIGH: email, no incident); an
    --     unfinished CRIT / EXH is CRITICAL (SP_INCIDENT_AUTODECLARE opens an incident). METRIC_VALUE is the
    --     minutes left to the target for a lead-window WARN only (NULL for a projection WARN, CRIT and EXH).
    IF (:start_wf <> '' AND :end_wf <> '') THEN
        BEGIN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
            WITH cfg AS (
                SELECT RULE_ID, SEVERITY, GREATEST(ROUND(COALESCE(THRESHOLD_NUM, 60)), 0) AS LEAD_MIN
                FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                WHERE RULE_ID = 'PIPE_ETL_CYCLE_LATE' AND ENABLED
            ),
            starts AS (
                SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START
                FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS
                WHERE WORKFLOW_NAME = :start_wf
                GROUP BY CYCLE_DATE
            ),
            ends AS (
                -- terminal tasks of the SAME night. A task with a clean finish from an attempt that STARTED
                -- at/after the night's last kickoff (FIRST_OK_END) is DONE at that first clean finish: a later
                -- re-run of it (the next morning,
                -- after the night already finished) neither re-opens nor re-grades the night. A task without
                -- one counts only when its TERMINAL attempt started at/after that night's cycle start: an
                -- afternoon re-run keyed to the night (before the ~22:00 kickoff) is not the night's
                -- completion, and once the real in-cycle run supersedes it the task counts again.
                SELECT t.CYCLE_DATE,
                       COUNT(*) AS TERM_TASKS,
                       MAX(COALESCE(t.FIRST_OK_END, t.TERMINAL_END)) AS CYCLE_FINISH,
                       COUNT_IF(t.FIRST_OK_END IS NULL
                                AND UPPER(t.TERMINAL_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED')) AS N_FAILED,
                       COUNT_IF(t.FIRST_OK_END IS NULL
                                AND t.TERMINAL_END IS NULL
                                AND (t.TERMINAL_STATUS IS NULL
                                     OR UPPER(t.TERMINAL_STATUS) NOT IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED'))) AS N_RUNNING
                FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t
                JOIN starts s ON s.CYCLE_DATE = t.CYCLE_DATE
                             AND (t.FIRST_OK_END IS NOT NULL OR t.TERMINAL_START >= s.CYCLE_START)
                WHERE t.WORKFLOW_NAME = :end_wf
                GROUP BY t.CYCLE_DATE
            ),
            nights AS (
                SELECT s.CYCLE_DATE, s.CYCLE_START, e.CYCLE_FINISH,
                       COALESCE(e.TERM_TASKS, 0) AS TERM_TASKS,
                       COALESCE(e.N_FAILED, 0) AS N_FAILED,
                       COALESCE(e.N_RUNNING, 0) AS N_RUNNING,
                       IFF(DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)) > s.CYCLE_START,
                           DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)),
                           DATEADD('minute', :target_off + 1440, DATE_TRUNC('day', s.CYCLE_START))) AS DL_T
                FROM starts s
                LEFT JOIN ends e ON e.CYCLE_DATE = s.CYCLE_DATE
            ),
            latest AS (
                SELECT MAX(CYCLE_DATE) AS CYCLE_DATE FROM nights
            ),
            term_lw AS (
                -- the app RAN_LAST_WEEK test for the terminal: did it dispatch on the same night last week?
                SELECT COUNT(*) AS N
                FROM nights n
                JOIN latest l ON n.CYCLE_DATE = DATEADD('day', -7, l.CYCLE_DATE)
                WHERE n.TERM_TASKS > 0
            ),
            hist AS (
                -- the newest 14 prior CLEAN nights (a failed / still-running night's finish is crash-short or
                -- partial, the forecaster's fit rule); >= 4 needed to project (SLA_FORECAST_MIN_RUNS).
                SELECT MEDIAN(DATEDIFF('second', n.CYCLE_START, n.CYCLE_FINISH)) AS MED_CYCLE_SEC,
                       MIN(n.TERM_TASKS) AS MIN_TERM_TASKS,
                       COUNT(*) AS N_HIST
                FROM (
                    SELECT n.*
                    FROM nights n
                    JOIN latest l ON n.CYCLE_DATE < l.CYCLE_DATE
                    WHERE n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0
                    QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14
                ) n
            ),
            tonight AS (
                SELECT n.CYCLE_DATE, n.CYCLE_START, n.CYCLE_FINISH, n.TERM_TASKS, n.N_FAILED, n.N_RUNNING,
                       n.DL_T,
                       DATEADD('minute', :breach_off - :target_off, n.DL_T) AS DL_H,
                       h.MED_CYCLE_SEC, h.N_HIST,
                       (n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0
                        AND n.TERM_TASKS >= COALESCE(h.MIN_TERM_TASKS, 1)) AS IS_COMPLETE,
                       IFF(h.N_HIST >= 4,
                           GREATEST(:now_ct, DATEADD('second', ROUND(h.MED_CYCLE_SEC), n.CYCLE_START)),
                           NULL) AS PROJECTED_FINISH
                FROM nights n
                JOIN latest l ON n.CYCLE_DATE = l.CYCLE_DATE
                CROSS JOIN hist h
            ),
            graded AS (
                SELECT t.CYCLE_DATE, t.CYCLE_START, t.CYCLE_FINISH, t.TERM_TASKS, t.N_FAILED, t.N_RUNNING,
                       t.DL_T, t.DL_H, t.MED_CYCLE_SEC, t.N_HIST, t.IS_COMPLETE, t.PROJECTED_FINISH,
                       c.RULE_ID, c.SEVERITY,
                       CASE
                           WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_H THEN 'EXH'
                           WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_T THEN 'CRIT'
                           WHEN NOT t.IS_COMPLETE
                                AND (:now_ct >= DATEADD('minute', -c.LEAD_MIN, t.DL_T)
                                     OR t.PROJECTED_FINISH > t.DL_H) THEN 'WARN'
                       END AS BAND
                FROM tonight t
                CROSS JOIN cfg c
                CROSS JOIN term_lw tl
                WHERE :now_ct < DATEADD('hour', 12, t.DL_H)   -- only the current business day; an old night never re-raises
                  AND NOT (t.TERM_TASKS = 0 AND COALESCE(tl.N, 0) = 0)   -- terminal not due tonight (did not run last week)
            )
            SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
            FROM (
            SELECT g.RULE_ID, 'ALL',
                   IFF(g.BAND = 'WARN' OR g.IS_COMPLETE, g.SEVERITY, 'CRITICAL'),
                   LEFT(CASE g.BAND
                            WHEN 'EXH' THEN IFF(g.IS_COMPLETE,
                                'Nightly ETL cycle finished ' || TO_VARCHAR(g.CYCLE_FINISH, 'HH24:MI')
                                    || ', past the ' || TO_VARCHAR(g.DL_H, 'HH24:MI') || ' hard deadline',
                                'Nightly ETL cycle past the ' || TO_VARCHAR(g.DL_H, 'HH24:MI')
                                    || ' hard deadline and still not finished')
                            WHEN 'CRIT' THEN IFF(g.IS_COMPLETE,
                                'Nightly ETL cycle finished ' || TO_VARCHAR(g.CYCLE_FINISH, 'HH24:MI')
                                    || ', after the ' || TO_VARCHAR(g.DL_T, 'HH24:MI') || ' target',
                                'Nightly ETL cycle missed the ' || TO_VARCHAR(g.DL_T, 'HH24:MI')
                                    || ' target and is still not finished')
                            ELSE IFF(g.PROJECTED_FINISH > g.DL_H,
                                'Nightly ETL cycle projected to finish ~' || TO_VARCHAR(g.PROJECTED_FINISH, 'HH24:MI')
                                    || ', past the ' || TO_VARCHAR(g.DL_H, 'HH24:MI') || ' hard deadline',
                                'Nightly ETL cycle not finished ' || DATEDIFF('minute', :now_ct, g.DL_T)
                                    || ' min before the ' || TO_VARCHAR(g.DL_T, 'HH24:MI') || ' target')
                        END || ' (' || TO_VARCHAR(g.CYCLE_DATE) || ' night)', 300),
                   LEFT('Cycle started ' || TO_VARCHAR(g.CYCLE_START, 'YYYY-MM-DD HH24:MI') || ' (' || :start_wf
                        || '). Terminal ' || :end_wf || ': '
                        || CASE WHEN g.TERM_TASKS = 0 THEN 'not dispatched yet'
                                WHEN g.N_FAILED > 0 THEN g.N_FAILED || ' task(s) FAILED - re-run it'
                                WHEN g.N_RUNNING > 0 THEN g.N_RUNNING || ' task(s) still running'
                                WHEN g.IS_COMPLETE THEN 'finished ' || TO_VARCHAR(g.CYCLE_FINISH, 'YYYY-MM-DD HH24:MI')
                                ELSE 'only ' || g.TERM_TASKS || ' task(s) dispatched so far' END
                        || '. Typical cycle '
                        || COALESCE(FLOOR(g.MED_CYCLE_SEC / 3600) || 'h ' || MOD(FLOOR(g.MED_CYCLE_SEC / 60), 60)
                                    || 'm over ' || g.N_HIST || ' clean night(s)', 'unknown (short history)')
                        || '. Target ' || TO_VARCHAR(g.DL_T, 'HH24:MI') || ', hard deadline '
                        || TO_VARCHAR(g.DL_H, 'HH24:MI') || ' (Central). Operations > Pipeline SLA > Tonight: '
                        || 'SLA finish forecast + Tonight at a glance.', 2000),
                   IFF(g.BAND = 'WARN' AND NOT COALESCE(g.PROJECTED_FINISH > g.DL_H, FALSE), DATEDIFF('minute', :now_ct, g.DL_T), NULL),
                   'PIPE_ETL_CYCLE_LATE|' || g.BAND || '|' || TO_VARCHAR(g.CYCLE_DATE)
            FROM graded g
            WHERE g.BAND IS NOT NULL
            ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
            WHERE NOT EXISTS (
                SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
            );
            n_late := SQLROWCOUNT;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'AlertScan', 'etl_cycle_scan_failed', LEFT(:emsg, 2000),
                       'rule PIPE_ETL_CYCLE_LATE - other ETL cycle rules unaffected', CURRENT_ROLE();
        END;
    END IF;

    RETURN 'etl cycle scan complete: ' || :n_rows || ' task-night rows; raised late=' || :n_late
           || ' not_started=' || :n_nostart || ' task_failed=' || :n_failed
           || ' (target ' || :target_off || ' / hard ' || :breach_off || ' min past midnight, Central)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 156 AS VERSION,
       'Nightly ETL cycle PUSH alerts (Next-Fifty rank 2): ETL_CYCLE_TASKS transient cache + SP_SCAN_ETL_CYCLE() (isolated config-driven read of ETL_CONTROL_STATUS_FQN, FQN-allowlisted, the 23 newest WHOLE nights cut by night key, retries collapsed to the terminal attempt via MAX_BY with that attempt''s start kept as TERMINAL_START, plus FIRST_OK_END = the first clean finish among attempts STARTED at/after the night''s last kickoff (MAX starter start; starter name bound via USING), night key DATE(TASK_START_DTTM - 12h), clock pinned America/Chicago) raising PIPE_ETL_TASK_FAILED (MEDIUM, HIGH for the terminal workflow, threshold never below 1, auto-clears on retry success: every final attempt finished and none failed, so a retry still running keeps it OPEN), PIPE_ETL_CYCLE_NOT_STARTED (HIGH, the app NEXT_CYCLE_OVERDUE test, grace = THRESHOLD_NUM min) and PIPE_ETL_CYCLE_LATE (WARN/CRIT/EXH bands superseded by the V067 sweep; lead = THRESHOLD_NUM min before ETL_SLA_TARGET_HHMM, or the start-shifted median of the newest 14 prior clean nights projects past ETL_SLA_BREACH_HHMM; a night that finished late is HIGH, an unfinished miss CRITICAL, which auto-declares an incident; a terminal task is done at its first clean finish from an attempt STARTED at/after the night''s last kickoff (FIRST_OK_END), so a next-morning terminal re-run never re-grades a finished night and an afternoon chain attempt that started before the real kickoff is never FIRST_OK_END (a next-morning starter re-run, or any re-run when the starter IS the terminal workflow, voids it: the night re-grades loud); two documented SILENT residuals after an afternoon chain re-run: a real cycle that hangs before its terminal dispatches, and a chain whose terminal starts after the real kickoff; a task with no FIRST_OK_END counts only when its terminal attempt started at/after the night''s cycle start; an undispatched terminal is judged only when it ran the same night last week; METRIC_VALUE only on a lead-window WARN), each in its own EXCEPTION guard. Three PIPELINE ALERT_CONFIG rules, WHEN NOT MATCHED, AUTO_CLEAR left at its default. Called every hour by the SP_ALERT_SCAN add-on CALL arm V157 adds (not counted toward OPS_SCAN_DEGRADED), it works only in the Central hours HOUR(ETL_SLA_TARGET_HHMM - 10h) through HOUR(target + 3h), wrapping midnight, plus a 15:00 pass (15 of 24 calls at the 07:00 default; a blank or malformed target falls back to 07:00): an out-of-window call returns after the rule count and the SETTINGS read and leaves ETL_CYCLE_TASKS untouched, so a daytime TASK_FAILED raise or retry clear lands up to ~6h later; the retry auto-clear UPDATE runs only when an OPEN PIPE_ETL_TASK_FAILED event exists. Needs SELECT on CONTROL_STATUS (already granted for the app panels).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 156);

-- =====================================================================
--  MIGRATION 2 of 4 -- APPLY V157 (idempotent; GUARDS on V156; STOP here on any error). Source: snowflake/migrations/V157__alert_scan_self_watch_idle_push.sql
-- =====================================================================
-- V157__alert_scan_self_watch_idle_push.sql
--
-- Next-Fifty wave 2b, ranks 10a/b/d + 13 + 2 (ETL-cycle add-on arm) + 12c: the SINGLE wave-2 re-derivation
-- of both alert scans, each from its CURRENT definition V141 (tests/test_proc_lineage.py).
--
--   SP_ALERT_SCAN (hourly):
--     - arm [15] removed: it keyed on a rule whose ALERT_CONFIG row was deleted at V034, so it joined no
--       row, yet it was the hourly scan's only ACCOUNT_USAGE.QUERY_HISTORY read;
--     - arm [11] removed and COST_CLOUD_SVC_RATIO RETIRED (owner decision, wave-2b rework): V150's
--       per-warehouse robust-z COST_CLOUD_SVC_ANOMALY (daily, SP_ANOMALY_SWEEP) supersedes the fixed
--       10/20% ratio; [11] was the hourly scan's only WAREHOUSE_METERING_HISTORY read. The file retires the
--       rule the V034 way (below the procs): its row goes, its OPEN/ACK/SNOOZED events close as EXPECTED;
--     ~ cadence gates (compile diet): the Central hour is read ONCE per run into ct_hour. Arms [10]
--       SEC_CRED_EXPIRY (3.9 s) and [20] SEC_NEW_EXPOSURE (19.4 s, the scan's heaviest compile; [14]
--       PIPE_COPY_FAILURES at 5.8 s stays hourly) -- run
--       only when MOD(ct_hour, 4) = 1 (01,05,09,13,17,21 Central); [22] only when MOD(ct_hour, 3) = 2
--       (02,05,08,11,14,17,20,23). Each gate wraps an UNCHANGED arm; a gated-off arm counts as ok. A failed
--       hour read keeps the DEFAULT 5 (inside both slots): every gated block runs, like before V157;
--     + [22] OPS_PIPELINE_DEGRADED (counting): pipeline self-watch -- a stale SOURCE_FRESHNESS_STATE row, a
--       loader failure that was logged and swallowed, or an idle alert notifier -- every 3rd hour here and
--       every morning in the daily scan;
--     + [23] PIPE_ETL_CYCLE add-on (NOT counting, ungated): runs SP_SCAN_ETL_CYCLE (V156), which applies its
--       own ETL-window gate; a CONTROL_STATUS grant gap logs etl_cycle_scan_failed and never trips
--       OPS_SCAN_DEGRADED;
--     + condition-ended sweep (#12c): an OPEN SEC_CRED_EXPIRY / SEC_NEW_EXPOSURE event resolves as
--       CONDITION_ENDED once ACCOUNT_USAGE shows the credential rotated/removed or the PUBLIC grant batch
--       fully revoked (OPEN only, 1h dwell, positive evidence only). Each rule's clear runs only in its raise
--       arm's 4-hourly slot;
--     ~ the V091 auto-clear sweep is scoped to its 3 PERF rules (the only rules whose still-firing set it
--       recomputes), so opting another rule into AUTO_CLEAR_ENABLED never blanket-clears it after 1h;
--     ~ arm [10]: a prior event closed for an EARLIER expiry (by anyone -- a human ACTIONED/NOISE/EXPECTED
--       resolve included, however late) or machine-closed (CONDITION_ENDED / SUPERSEDED) no longer blocks
--       the credential's key, so a rotated credential's next expiry cycle re-alerts. The key itself is
--       unchanged: the cycle id is the expiry date every arm [10] since V009 writes at the head of DETAIL
--       ('Rotate before YYYY-MM-DD', both bands), now pinned to Central on both the write and the match.
--       A live (OPEN/ACK/SNOOZED) event, or a close for this same expiry, still blocks; EXPIRING is never
--       minted while that credential's EXPIRED event is live;
--     + [hb] heartbeat: stamps SOURCE_FRESHNESS_STATE 'ALERT_SCAN_HOURLY' last in every run, as ONE point
--       UPDATE (an INSERT only when the row is missing: the first run, or after a delete).
--     Counting arms 13 -> 12 (13 - [15] - [11] + [22]); the self-alert and the RETURN say 12.
--   SP_ALERT_SCAN_DAILY:
--     + [22] OPS_PIPELINE_DEGRADED (byte-identical to the hourly copy; shared dedupe keys, so whichever
--       graph is alive raises each finding once);
--     + [24] COST_IDLE_OPPORTUNITY (counting): weekly idle-waste push, the DB-side twin of the Optimize
--       ACTIONABLE figure (net of the 60s resume tail, settings-verified timer from the newest SHOW
--       WAREHOUSES snapshot batch -- a dropped/renamed warehouse never raises -- 14 complete Central days);
--     + [hb] heartbeat 'ALERT_SCAN_DAILY' (the same point-UPDATE shape). Counting arms 9 -> 11. No cadence
--       gate: the daily scan runs once a day.
--   ALERT_CONFIG: OPS_PIPELINE_DEGRADED (PLATFORM, HIGH) and COST_IDLE_OPPORTUNITY (COST, MEDIUM, 100 USD/month,
--   HIGH band at 5x) are seeded WHEN NOT MATCHED only. SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE are opted into
--   auto-clear AFTER both procs are replaced (never before: V141's unscoped sweep would blanket-clear them).
--
-- Everything else in both V141 bodies is byte-identical (tests/migrations/test_v157_* normalizes each back
-- to V141): DECLARE (+ ct_hour), the SETTINGS read, [wake], the 11 surviving hourly arms ([10] carries its
-- recurrence fix; [10] and [20] sit unchanged inside their gates), both self-alerts (hourly literal now 12),
-- the V067/V115 supersede sweep, the V091 body except its one scope line, the V117 carry-forward, the daily
-- arms [06]-[19], the [17]/[18] add-ons and the V064 trailing-30-complete-day burn.
--
-- LATENCY TRADE (owner decisions D1/D2/D8/D9; the compile saving is the point): a new PUBLIC grant
-- (SEC_NEW_EXPOSURE) and a credential entering its window or expiring (SEC_CRED_EXPIRY -- the EXPIRED band is
-- CRITICAL and auto-declares an incident) surface up to ~4h later than an hourly check would raise them, on
-- top of ACCOUNT_USAGE's own lag; a CONDITION_ENDED clear lands up to ~4h after the evidence; the hourly [22]
-- self-watch reports a stale source or an idle notifier up to ~3h later (the daily scan's copy still runs
-- every morning). Every other hourly arm and sweep still runs every hour.
-- A condition that starts and ends between two checks is never raised at all: a PUBLIC grant revoked before
-- the next 4-hourly check (an exposure shorter than ~4h, after ACCOUNT_USAGE lag; V141 already missed ones
-- under ~1h) and a stale-source or idle-notifier episode that clears between two [22] slots (the ERR leg's
-- 24h lookback still catches every logged loader failure). GRANTS_TO_ROLES history and Security > Changes
-- still show such a grant.
--
-- FIRST RUN: at the first [22] slot (hourly scan) or daily run, every SOURCE_FRESHNESS_STATE row already past
-- its cadence raises one HIGH OPS_PIPELINE_DEGRADED event, and the first daily run raises this ISO week's
-- COST_IDLE_OPPORTUNITY events (preview with the
-- separate read-only PREFLIGHT_WAVE2B.sql). A credential already inside its expiry window whose only prior
-- SEC_CRED_EXPIRY event for that key was closed for an EARLIER expiry date (an earlier cycle, e.g.
-- human-resolved, however late) raises its previously suppressed event once (CRITICAL, and an auto-declared
-- incident, if already expired). A close for the SAME expiry date, or a still-live event, still suppresses
-- it. Known edge: a row written before V157 by a hand-run scan from a session in another timezone carries
-- that zone's date; if the expiry fell on a different calendar date there, the row reads as an earlier cycle
-- and the event re-raises once (a duplicate, not a missed alert). Deploy the app build that excludes
-- CONDITION_ENDED from the human-resolution metrics BEFORE applying (it shipped in wave 2a). No procedure runs
-- at apply time: the scans pick this up on their next scheduled run. Applying also closes every OPEN, ACK'd
-- or SNOOZED COST_CLOUD_SVC_RATIO event as EXPECTED and deletes that rule's ALERT_CONFIG row (history in
-- ALERT_EVENTS is kept).
-- ROLLBACK (order matters): FIRST, by hand (never inside a migration), switch AUTO_CLEAR_ENABLED off for
-- SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE; only THEN re-run V141's two procs (RUNBOOK section 12, "Rolling back
-- V157"). Reversed, an hourly scan landing between the two steps runs V141's unscoped V091 sweep, which
-- AUTO_CLEARs their OPEN events 1h after raise -- and V141's arms [10]/[20] never re-raise an auto-cleared key.
-- The retired COST_CLOUD_SVC_RATIO row stays deleted after a rollback (V141's arm [11] then joins no row, like
-- the old [15]); re-seed it by hand only if the fixed ratio is wanted back.
-- Apply AFTER V156 (SP_SCAN_ETL_CYCLE must exist for [23]; before it, the arm only logs
-- etl_cycle_scan_failed). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20157, 'V157 requires V156 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 156) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Next-Fifty #10 + #13: the two new rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered,
-- and AUTO_CLEAR_ENABLED keeps its default.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('OPS_PIPELINE_DEGRADED', 'PLATFORM', 'OVERWATCH pipeline self-watch: a telemetry source past its load cadence, a swallowed loader failure, or an idle alert notifier', TRUE, 'HIGH', 0, 24),
        ('COST_IDLE_OPPORTUNITY', 'COST', 'Weekly idle-waste opportunity: a settings-verified AUTO_SUSPEND tightening recovers at least the threshold in USD per month', TRUE, 'MEDIUM', 100, 336)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- >>> derived:SP_ALERT_SCAN  (from V141; - dead break-glass arm [15], - retired COST_CLOUD_SVC_RATIO arm [11], [10]/[20] + their condition-ended clears every 4h, + [22] OPS_PIPELINE_DEGRADED every 3h, + [23] PIPE_ETL_CYCLE add-on, + condition-ended sweep, V091 sweep scoped to PERF, arm [10] recurrence fix, + [hb], V157)
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
               :fails || ' of 12 alert rule block(s) failed this run',
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
               ROW_COUNT = (12 - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = 'alert scan ' || (12 - :fails) || '/12 rule blocks ok'
         WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT 'ALERT_SCAN_HOURLY', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   (12 - :fails), 1, 'alert scan ' || (12 - :fails) || '/12 rule blocks ok';
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   'ALERT_SCAN_HOURLY heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan v12 (V157: + OPS_PIPELINE_DEGRADED self-watch + ETL-cycle add-on + condition-ended sweep + heartbeat, - dead break-glass arm, - retired cloud-services ratio arm, security arms every 4h, self-watch every 3h): ' || (12 - :fails) || '/12 rule blocks ok';
END;
$$;

-- >>> derived:SP_ALERT_SCAN_DAILY  (from V141; + [22] OPS_PIPELINE_DEGRADED, + [24] COST_IDLE_OPPORTUNITY, + [hb], V157)
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
               :fails || ' of 11 daily alert rule block(s) failed this run',
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
               ROW_COUNT = (11 - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = 'alert scan daily ' || (11 - :fails) || '/11 rule blocks ok (daily)'
         WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT 'ALERT_SCAN_DAILY', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   (11 - :fails), 1, 'alert scan daily ' || (11 - :fails) || '/11 rule blocks ok (daily)';
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   'ALERT_SCAN_DAILY heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

    RETURN 'alert scan daily v3 (V157: + OPS_PIPELINE_DEGRADED self-watch + COST_IDLE_OPPORTUNITY + heartbeat): ' || (11 - :fails) || '/11 rule blocks ok (daily)';
END;
$$;

-- Next-Fifty #12c: opt the two state-verified SECURITY rules into the condition-ended sweep. Placed AFTER
-- both procs on purpose: under V141's body the unscoped V091 sweep would resolve these rules' OPEN events
-- 1h after raise with no condition check. TRUE-only; this file never switches a flag off.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET AUTO_CLEAR_ENABLED = TRUE
 WHERE RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_EXPOSURE');

-- Owner decision (wave-2b rework): retire COST_CLOUD_SVC_RATIO. V150's per-warehouse robust-z
-- COST_CLOUD_SVC_ANOMALY (daily, SP_ANOMALY_SWEEP) supersedes the fixed 10/20% ratio, and arm [11] -- an hourly
-- WAREHOUSE_METERING_HISTORY read -- is gone from SP_ALERT_SCAN above. The house retire pattern (V034,
-- SEC_BREAK_GLASS_USE): the rule row goes and lingering events close as EXPECTED -- SNOOZED ones too, since the
-- hourly wake step would otherwise reopen an event no scan can ever close again. ALERT_EVENTS history is kept.
-- Row FIRST, then the events: once the row is gone no arm [11] joins a config row -- not even a scan that
-- started before this file and still runs V141's body -- so nothing can re-raise the rule after the close.
DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
 WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO';

UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',
       RESOLVED_AT = CURRENT_TIMESTAMP()
 WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO' AND STATUS IN ('OPEN', 'ACK', 'SNOOZED');

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 157 AS VERSION,
       'Next-Fifty wave 2b (ranks 10a/b/d, 13, 2, 12c) plus the wave-2b compile-diet rework: the single wave-2 re-derivation of SP_ALERT_SCAN and SP_ALERT_SCAN_DAILY from V141, byte-identical otherwise. Hourly: dead break-glass arm [15] removed (its rule was deleted at V034; it was the scan''s only ACCOUNT_USAGE.QUERY_HISTORY read); arm [11] removed and COST_CLOUD_SVC_RATIO retired (V150 COST_CLOUD_SVC_ANOMALY supersedes the fixed ratio; its OPEN, ACK and SNOOZED events close as EXPECTED and its ALERT_CONFIG row is deleted, the V034 pattern); cadence gates from one Central-hour read per run (fail-open default 5): arms [10] SEC_CRED_EXPIRY and [20] SEC_NEW_EXPOSURE and their condition-ended clears run when MOD(hour, 4) = 1 (01,05,09,13,17,21 Central), [22] when MOD(hour, 3) = 2, so those alerts and clears can land up to about 4h (3h for [22]) later; + [22] OPS_PIPELINE_DEGRADED self-watch (a SOURCE_FRESHNESS_STATE row past the shared DAILY/METERING 30h else 3h name rule, at most one event per source per last-load day; a swallowed loader failure of the five NATIVE_ALERT_STALE_FACTS error types, one per type, source and Central day, the three optional mart arms left to the stale leg; an idle alert notifier via OW_SENDER_LEASE while a route is enabled); + [23] add-on arm running SP_SCAN_ETL_CYCLE (V156, ungated here; not counted toward OPS_SCAN_DEGRADED, logs etl_cycle_scan_failed); + #12c condition-ended sweep (an OPEN SEC_CRED_EXPIRY or SEC_NEW_EXPOSURE event resolves CONDITION_ENDED once CREDENTIALS or GRANTS_TO_ROLES show the condition ended; 1h dwell, positive evidence only, opt-in via AUTO_CLEAR_ENABLED); the V091 auto-clear sweep scoped to its 3 PERF rules; arm [10] (key unchanged) ignores CONDITION_ENDED and SUPERSEDED rows and any row closed for an earlier expiry, human resolves included however late (the cycle id is the expiry date every arm [10] since V009 writes at the head of DETAIL, now pinned to Central on write and match; a live row always blocks), so a rotated credential''s next expiry re-alerts, and never mints EXPIRING while the EXPIRED event is live; + [hb] ALERT_SCAN_HOURLY heartbeat (one point UPDATE, an INSERT only when the row is missing). Tally 13 -> 12. Daily: + [22] (byte-identical, shared keys, ungated) + [24] COST_IDLE_OPPORTUNITY (weekly per warehouse, net recoverable USD/month after the 60s resume tail, settings-verified timer (newest SHOW WAREHOUSES snapshot batch) disabled or above 60s, 14 complete Central days with at least 7 covered; MEDIUM at 100 USD/month, HIGH band at 5x) + [hb] ALERT_SCAN_DAILY heartbeat. Tally 9 -> 11. Seeds OPS_PIPELINE_DEGRADED (PLATFORM, HIGH) and COST_IDLE_OPPORTUNITY (COST, MEDIUM, 100, 336h) WHEN NOT MATCHED only; opts SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE into auto-clear after both procs are replaced. No task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 157);

-- =====================================================================
--  MIGRATION 3 of 4 -- APPLY V158 (idempotent; GUARDS on V157; tail EXECUTE TASK (async)). Source: snowflake/migrations/V158__operator_backup_generations.sql
-- =====================================================================
-- V158__operator_backup_generations.sql
--
-- Next-Fifty #32: operator-data backups rotate DAILY into dated generations instead of one weekly
-- *_BAK_LAST overwrite (a bad bulk edit noticed a day later could lose up to a week; after the next
-- Sunday the only backup already held the corruption).
--
-- * New TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK (owner-only). roles.sql grants FUTURE TABLES
--   only in OVERWATCH, so generations here add no grant/revoke churn to OVERWATCH (recent grant
--   changes, the RCA feed, unused-grant candidates, tag coverage) and survive a drop of OVERWATCH.
-- * SP_BACKUP_OPERATOR_TABLES re-derived from V089 (its current definer): every day at 05:10 Central
--   each of the 25 operator tables is cloned to an immutable TRANSIENT
--   OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd> (Central day). Sundays also clone <T>_OWBAK_W<yyyymmdd> from
--   that D generation and run the V089 <T>_BAK_LAST statement unchanged (still weekly). A table
--   missing on this install is a logged skip, not a failure.
-- * Backup-vs-source row counts go to the new OPERATOR_BACKUP_LOG, one CLONED row per generation (the
--   daily D; Sundays also the weekly W). The proc trims the log at 400 days.
-- * Prune: newest SETTINGS BACKUP_KEEP_DAILY (14) / BACKUP_KEEP_WEEKLY (8) generations per table and
--   kind (floors 7 / 4, ceilings 60 / 52). Only TRANSIENT base tables in OVERWATCH_BAK whose whole
--   name is one of the 25 + _OWBAK_[DW] + 8 digits, dated before today, re-checked before each DROP.
--   The manual <T>_BAK_<yyyymmdd> DR clones (teardown.sql B0, rebuild/00) use a different token.
-- * Statement budget (wave-2b rework D11): ONE set-based INFORMATION_SCHEMA probe per run (the 25
--   sources present + which already hold today's generation; fails open to V089's clone-everything)
--   replaces the per-table probes, and ONE set-based PRUNED log insert replaces the per-DROP inserts.
--   A steady-state weekday is 59 statements and a Sunday 135, 489 a week (the untrimmed per-table
--   design: 107 / 208 / 850; V089 ran 26 a week). A same-day re-run issues no no-op CLONE.
-- * SOURCE_FRESHNESS_STATE 'OPERATOR_BACKUP_DAILY' (30h cadence by name) advances only on a run with
--   zero clone failures, so a failing or suspended backup goes stale and the dead-man paths fire.
-- * TASK_BACKUP_OPERATOR was created IF NOT EXISTS (V015), so its schedule moves in place
--   (SUSPEND / SET SCHEDULE / RESUME) from Sunday 05:40 to daily 05:10 Central.
-- * V_SECURITY_EXCEPTION_QUEUE re-derived from V151 (its current definer) with ONE carve-out: the
--   prune DROPs score DESTRUCTIVE 100 / CRITICAL, so a DROP run by the task (USER_NAME SYSTEM) whose
--   statement is exactly the generated OVERWATCH_BAK generation DROP leaves the CHANGE RISK queue.
--   The first prune happens 15 days after apply, so the carve-out is always in place first.
--
-- Restore = INSERT OVERWRITE as the table-owner role (RUNBOOK section 16): a TRANSIENT backup cannot
-- CLONE back into a permanent table, and a CLONE restore would re-apply the schema FUTURE grants.
-- The tail starts the first generation through the TASK (asynchronous; it runs as SYSTEM, inside the
-- carve-out, keyed on the Central day whatever the worksheet zone). Nothing is pruned on day 1.
-- DR replay (schema gone, or a factory reset): apply V001..V157, restore the operator tables
-- (SETTINGS first), THEN this file. Its tail backs up whatever the tables hold and prunes with the
-- retention SETTINGS holds (RUNBOOK section 16 step 3).
-- Owner applies in Snowsight after V157. This file never runs from the app.
-- Rollback: V089:27-71 proc, ALTER TASK ... SET SCHEDULE = 'USING CRON 40 5 * * 0 America/Chicago',
-- and the V151 view.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20158, 'V158 requires V157 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 157) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Dedicated backup schema (decision O-12). TRANSIENT: generations need no Fail-safe. No grants: the
-- proc owner (the role applying this file) owns every generation and runs every restore.
CREATE TRANSIENT SCHEMA IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK COMMENT = 'OVERWATCH operator-data backup generations (V158). Owner-only; never dropped by teardown.';

-- Retention (decision O-13), Admin-editable within 7-60 / 4-52. WHEN NOT MATCHED only: an
-- operator's edited value is never overwritten.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('BACKUP_KEEP_DAILY', '14'),
        ('BACKUP_KEEP_WEEKLY', '8')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- Backup ledger (operator history: teardown lists it commented, never a live drop).
-- ACTION: CLONED / SKIPPED_MISSING / CLONE_FAILED / PRUNED / PRUNE_FAILED.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG (
    RUN_ID           VARCHAR(64)   NOT NULL,
    GENERATION       VARCHAR(16)   NOT NULL,
    SOURCE_TABLE     VARCHAR(256)  NOT NULL,
    BACKUP_TABLE     VARCHAR(256),
    ACTION           VARCHAR(20)   NOT NULL,
    ROW_COUNT        NUMBER(38,0),
    SOURCE_ROW_COUNT NUMBER(38,0),
    BYTES            NUMBER(38,0),
    DETAIL           VARCHAR(1000),
    LOGGED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- >>> derived:SP_BACKUP_OPERATOR_TABLES  (from V089; daily dated generations in OVERWATCH_BAK + keep-count prune + row-count log + freshness stamp, V158)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    tables ARRAY DEFAULT [
        'SETTINGS', 'COMPANY_SCOPE', 'ALERT_CONFIG', 'ALERT_EVENTS',
        'ALERT_AUDIT', 'ACTION_QUEUE', 'SAVINGS_LEDGER', 'DEPARTMENT_MAP',
        'ALERT_ROUTES', 'REMEDIATION_LOG', 'USER_PREFS',
        'OBJECT_CHANGE_REGISTRY', 'WAREHOUSE_CHANGE_REGISTRY',
        'WAREHOUSE_CONFIG_SNAPSHOT', 'PIPELINE_SLA_CONFIG', 'DAILY_DIGEST',
        'DEPT_BUDGETS', 'INCIDENTS', 'INCIDENT_MEMBERS', 'ACTION_ACTIVITY',
        'EVIDENCE_LINKS', 'ENTITY_CATALOG', 'USER_WATCHLIST',
        'OPTIMIZATION_EXPERIMENTS', 'SLO_OBJECTIVES'
    ];
    tname VARCHAR;
    emsg VARCHAR;
    done INT DEFAULT 0;
    i INT;
    -- V158: dated generations in DBA_MAINT_DB.OVERWATCH_BAK, keep-count prune, row-count log,
    -- freshness stamp.
    failed INT DEFAULT 0;          -- clone failures: they hold the freshness stamp
    missing INT DEFAULT 0;         -- source table absent on this install: a skip, never a failure
    pruned INT DEFAULT 0;
    prune_failed INT DEFAULT 0;
    probe_ok BOOLEAN DEFAULT FALSE;  -- the one metadata probe answered (else every clone is attempted)
    src_present ARRAY;             -- the probe: the 25 sources that exist in OVERWATCH
    have_d ARRAY;                  -- the probe: sources whose daily generation for today exists
    have_w ARRAY;                  -- the probe: sources whose weekly generation for today exists
    pruned_list VARCHAR DEFAULT '';  -- dropped generation names, logged by ONE insert after the prune
    keep_d FLOAT DEFAULT 14;
    keep_w FLOAT DEFAULT 8;
    day_ct DATE;
    gen_d VARCHAR;
    gen_w VARCHAR;
    is_sunday BOOLEAN DEFAULT FALSE;
    run_id VARCHAR;
    prune_re VARCHAR;
    pname VARCHAR;
    total_rows NUMBER(38,0) DEFAULT 0;
    fstatus VARCHAR;
    res RESULTSET;
BEGIN
    run_id := UUID_STRING();
    -- Retention from SETTINGS (Admin-editable; seeded 14 / 8 above). A missing or non-numeric value
    -- falls back to the default; the floors (7 daily / 4 weekly) mean no value can prune the history
    -- away, and the ceilings (60 / 52) bound storage.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_DAILY', VALUE, NULL))), 14),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_WEEKLY', VALUE, NULL))), 8)
      INTO :keep_d, :keep_w
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
    keep_d := LEAST(GREATEST(ROUND(keep_d), 7), 60);
    keep_w := LEAST(GREATEST(ROUND(keep_w), 4), 52);

    -- Generation key = the America/Chicago calendar day (TIMEZONE STANDARD), never the session zone.
    day_ct := CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE;
    gen_d := 'D' || TO_CHAR(day_ct, 'YYYYMMDD');
    gen_w := 'W' || TO_CHAR(day_ct, 'YYYYMMDD');
    is_sunday := (DAYOFWEEKISO(day_ct) = 7);
    -- The ONLY names the prune may ever touch: one of the 25 + _OWBAK_ + D/W + 8 digits. REGEXP_LIKE
    -- anchors the whole name, so a manual <T>_BAK_<yyyymmdd> DR clone can never match.
    prune_re := '(' || ARRAY_TO_STRING(:tables, '|') || ')_OWBAK_[DW][0-9]{8}';

    -- ONE set-based metadata probe for the whole run (wave-2b rework D11; it was one probe per table):
    -- which of the 25 sources exist in OVERWATCH, and which of them already have today's daily / weekly
    -- generation in OVERWATCH_BAK (a same-day re-run then skips the no-op CLONE). Fails OPEN: if the
    -- probe itself errors, probe_ok stays FALSE, so nothing counts as missing or already taken and every
    -- clone is attempted exactly as V089 did (a missing source then lands as clone_failed).
    BEGIN
        SELECT COALESCE(ARRAY_AGG(IFF(TABLE_SCHEMA = 'OVERWATCH', TABLE_NAME, NULL)), ARRAY_CONSTRUCT()),
               COALESCE(ARRAY_AGG(IFF(TABLE_SCHEMA = 'OVERWATCH_BAK' AND RIGHT(TABLE_NAME, 9) = :gen_d,
                                      LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16), NULL)), ARRAY_CONSTRUCT()),
               COALESCE(ARRAY_AGG(IFF(TABLE_SCHEMA = 'OVERWATCH_BAK' AND RIGHT(TABLE_NAME, 9) = :gen_w,
                                      LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16), NULL)), ARRAY_CONSTRUCT())
          INTO :src_present, :have_d, :have_w
          FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
         WHERE TABLE_SCHEMA IN ('OVERWATCH', 'OVERWATCH_BAK')
           AND TABLE_TYPE = 'BASE TABLE'
           AND ((TABLE_SCHEMA = 'OVERWATCH' AND ARRAY_CONTAINS(TABLE_NAME::VARIANT, :tables))
             OR (TABLE_SCHEMA = 'OVERWATCH_BAK' AND REGEXP_LIKE(TABLE_NAME, :prune_re)
                 AND RIGHT(TABLE_NAME, 9) IN (:gen_d, :gen_w)));
        probe_ok := TRUE;
    EXCEPTION
        WHEN OTHER THEN
            probe_ok := FALSE;
    END;

    FOR i IN 0 TO ARRAY_SIZE(:tables) - 1 DO
        tname := GET(:tables, i)::VARCHAR;
        -- Missing only on a definite answer: the probe ran and did not list the source (fails OPEN).
        IF (COALESCE(probe_ok AND NOT ARRAY_CONTAINS(tname::VARIANT, :src_present), FALSE)) THEN
            missing := missing + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                (RUN_ID, GENERATION, SOURCE_TABLE, ACTION, DETAIL)
            SELECT :run_id, :gen_d, :tname, 'SKIPPED_MISSING', 'source table absent on this install';
        ELSE
            BEGIN
                -- V158: the daily generation, immutable once taken (IF NOT EXISTS), in the dedicated
                -- TRANSIENT schema OVERWATCH_BAK (no FUTURE grants there, so no grant churn). One the
                -- probe already saw today is not re-issued: the CLONE would be a no-op.
                IF (NOT COALESCE(probe_ok AND ARRAY_CONTAINS(tname::VARIANT, :have_d), FALSE)) THEN
                    EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                      '_OWBAK_' || :gen_d || ' CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;
                END IF;
                IF (is_sunday) THEN
                    -- Sundays: the weekly generation, cloned from today's daily one inside OVERWATCH_BAK,
                    -- and the V089 *_BAK_LAST pointer in OVERWATCH, still weekly (statement unchanged).
                    IF (NOT COALESCE(probe_ok AND ARRAY_CONTAINS(tname::VARIANT, :have_w), FALSE)) THEN
                        EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                          '_OWBAK_' || :gen_w || ' CLONE DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                          '_OWBAK_' || :gen_d;
                    END IF;
                    -- V089: TRANSIENT target -- a transient source (ALERT_EVENTS,
                    -- ACTION_QUEUE, ...) cannot clone into a PERMANENT table
                    -- ("Transient object cannot be cloned to a permanent object"),
                    -- which failed those backups every run. TRANSIENT works for both
                    -- transient and permanent sources and needs no Fail-safe.
                    EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||
                                      '_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;
                END IF;
                done := done + 1;          -- this source holds today's generation (taken now or earlier today)
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    failed := failed + 1;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'BackupOperatorTables', 'clone_failed', LEFT(:emsg, 2000),
                           'table ' || :tname || ' generation ' || :gen_d || ' (OPERATOR_BACKUP_DAILY)', CURRENT_ROLE();
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                        (RUN_ID, GENERATION, SOURCE_TABLE, ACTION, DETAIL)
                    SELECT :run_id, :gen_d, :tname, 'CLONE_FAILED', LEFT(:emsg, 1000);
            END;
        END IF;
    END FOR;

    -- Row counts: INFORMATION_SCHEMA metadata only, no table scan. One CLONED row per OVERWATCH_BAK
    -- generation taken today (the daily D; on Sundays also the weekly W), backup vs source, so a
    -- restore can pick any kept generation on evidence (the Sunday *_BAK_LAST pointer is not logged).
    -- Isolated: a failure here never blocks the prune, the freshness stamp or the RETURN.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
        SELECT :run_id, :gen_d, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
        FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
        JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
          ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
         AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_d
        WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
          AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
        IF (is_sunday) THEN
            -- The weekly generation outlives its D twin (kept in weeks, not days), so it gets its own
            -- CLONED row: once the D generation is pruned, the log still names a table that exists.
            INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
            SELECT :run_id, :gen_w, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
            FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
            JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
              ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
             AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_w
            WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
              AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
        END IF;
        -- The freshness ROW_COUNT is the daily generation's rows only (never doubled on a Sunday).
        SELECT COALESCE(SUM(ROW_COUNT), 0) INTO :total_rows
          FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
         WHERE RUN_ID = :run_id AND ACTION = 'CLONED' AND GENERATION = :gen_d;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_log_failed', LEFT(:emsg, 2000),
                   'row-count log ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): generations taken, counts not logged', CURRENT_ROLE();
    END;

    -- Prune: keep the newest keep_d daily / keep_w weekly generations per table and kind (rank-based,
    -- so a paused task never empties the history; today's generation is never a candidate). Only
    -- TRANSIENT base tables in DBA_MAINT_DB.OVERWATCH_BAK whose whole name matches prune_re, and the
    -- regex is re-checked right before each DROP. Isolated like the log above. Each dropped name is
    -- collected for the ONE PRUNED insert after this block (it was one INSERT per DROP).
    BEGIN
        res := (
            SELECT g.TABLE_NAME
            FROM (
                SELECT TABLE_NAME,
                       LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16) AS BASE_NAME,
                       SUBSTR(TABLE_NAME, LENGTH(TABLE_NAME) - 8, 1) AS GEN_KIND,
                       TRY_TO_DATE(RIGHT(TABLE_NAME, 8), 'YYYYMMDD') AS GEN_DAY
                FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_CATALOG = 'DBA_MAINT_DB'
                  AND TABLE_SCHEMA = 'OVERWATCH_BAK'
                  AND TABLE_TYPE = 'BASE TABLE'
                  AND IS_TRANSIENT = 'YES'
                  AND REGEXP_LIKE(TABLE_NAME, :prune_re)
            ) g
            WHERE g.GEN_DAY IS NOT NULL
            QUALIFY g.GEN_DAY < :day_ct
                AND ROW_NUMBER() OVER (PARTITION BY g.BASE_NAME, g.GEN_KIND ORDER BY g.GEN_DAY DESC)
                    > IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)
            ORDER BY g.TABLE_NAME
        );
        LET c_prune CURSOR FOR res;
        FOR r IN c_prune DO
            pname := r.TABLE_NAME;
            IF (REGEXP_LIKE(pname, prune_re)) THEN   -- second check right before the DROP
                BEGIN
                    EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;
                    pruned := pruned + 1;
                    pruned_list := pruned_list || pname || ' ';   -- whole-name regex match: no space inside
                EXCEPTION
                    WHEN OTHER THEN
                        emsg := SQLERRM;
                        prune_failed := prune_failed + 1;
                        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                        SELECT 'BackupOperatorTables', 'backup_prune_failed', LEFT(:emsg, 2000),
                               'table ' || :pname || ' (OPERATOR_BACKUP_DAILY)', CURRENT_ROLE();
                        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, DETAIL)
                        SELECT :run_id, RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname,
                               'PRUNE_FAILED', LEFT(:emsg, 1000);
                END;
            END IF;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            prune_failed := prune_failed + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_prune_failed', LEFT(:emsg, 2000),
                   'prune scan ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): scan stopped, generations not yet dropped are kept', CURRENT_ROLE();
    END;

    -- The PRUNED rows: ONE set-based insert per run (wave-2b rework D11; it was one INSERT per DROP),
    -- the same row per dropped generation as before. It sits after the prune block, so every DROP that
    -- ran is logged even when the scan stopped part-way. Isolated: a failure is logged, never blocking.
    IF (pruned > 0) THEN
        BEGIN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION)
            SELECT :run_id, RIGHT(p.VALUE::VARCHAR, 9), LEFT(p.VALUE::VARCHAR, LENGTH(p.VALUE::VARCHAR) - 16),
                   p.VALUE::VARCHAR, 'PRUNED'
            FROM TABLE(FLATTEN(INPUT => SPLIT(TRIM(:pruned_list), ' '))) p;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'BackupOperatorTables', 'backup_log_failed', LEFT(:emsg, 2000),
                       'prune log ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): ' || :pruned || ' generation(s) dropped, not logged', CURRENT_ROLE();
        END;
    END IF;

    -- The log trims itself (SP_PURGE_FACTS is untouched).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
     WHERE LOGGED_AT < DATEADD('day', -400, CURRENT_TIMESTAMP());

    -- Freshness (V068 idiom, Central-pinned). LAST_LOAD_TS advances only on a run with zero clone
    -- failures (V066 #11), so a failing or suspended backup goes stale and the dead-man paths fire.
    -- The name carries DAILY, so every name-based cadence rule judges it at 30h.
    fstatus := 'backup ' || gen_d || ': ' || done || ' cloned, ' || failed || ' failed, ' ||
               missing || ' missing, ' || pruned || ' pruned, ' || prune_failed || ' prune failed';
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'OPERATOR_BACKUP_DAILY' AS SOURCE_NAME,
               IFF(:failed = 0 AND :done > 0, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, NULL) AS RUN_TS,
               :total_rows AS ROW_COUNT,
               LEFT(:fstatus, 400) AS STATUS
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = COALESCE(s.RUN_TS, t.LAST_LOAD_TS),
        ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
        GENERATION = COALESCE(t.GENERATION, 0) + 1, STATUS = s.STATUS
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, SNAPSHOT_TS, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.RUN_TS, s.ROW_COUNT,
            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, 1, s.STATUS);

    IF (failed > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'BackupOperatorTables', 'backup_incomplete', LEFT(:fstatus, 2000),
               'OPERATOR_BACKUP_DAILY ' || :gen_d || ': freshness stamp held until a clean run; see OPERATOR_BACKUP_LOG',
               CURRENT_ROLE();
    END IF;

    RETURN 'cloned ' || :done || ' operator table(s) to OVERWATCH_BAK.*_OWBAK_' || :gen_d ||
           IFF(:is_sunday, ' (+ weekly ' || :gen_w || ' and *_BAK_LAST)', '') || '; ' || :failed || ' failed, ' ||
           :missing || ' missing, ' || :pruned || ' pruned, ' || :prune_failed || ' prune failed';
END;
$$;

-- Daily cadence (decision O-13): 05:10 Central rides the hourly chain's warm WH_ALFA_ADMIN
-- (TASK_LOAD_HOURLY :07) and runs ahead of the 06:30+ daily batch.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR
    SET SCHEDULE = 'USING CRON 10 5 * * * America/Chicago';
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR RESUME;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V151; + OVERWATCH_BAK backup-prune carve-out, V158)
CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE AS
WITH latest_posture AS (
    SELECT METRIC, VALUE, DAY
    FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
    QUALIFY DAY = MAX(DAY) OVER ()
), candidates AS (
    SELECT 'IDENTITY' AS DOMAIN, 'ALL' AS COMPANY,
           NULL::VARCHAR AS ACTOR_COMPANY, NULL::VARCHAR AS OBJECT_COMPANY,
           'ALERT' AS ENTITY_TYPE, METRIC AS ENTITY_KEY,
           IFF(METRIC = 'MFA_GAP_USERS', 'HIGH', 'MEDIUM') AS SEVERITY,
           CASE METRIC WHEN 'MFA_GAP_USERS' THEN 'Users with password activity and no MFA'
                       WHEN 'EXPIRED_CRED' THEN 'Expired credentials remain active'
                       ELSE 'Credentials expire within 10 days' END AS TITLE,
           VALUE || ' open exception(s)' AS DETAIL,
           VALUE AS IMPACT_COUNT, DAY::TIMESTAMP_NTZ AS DETECTED_AT, 1.0 AS CONFIDENCE
    FROM latest_posture
    WHERE METRIC IN ('MFA_GAP_USERS', 'EXPIRED_CRED', 'EXPIRING_CRED_10D') AND VALUE > 0
    UNION ALL
    SELECT 'PRIVILEGE', 'ALL', NULL, NULL, 'ALERT', METRIC,
           'HIGH', 'Recent break-glass grants', VALUE || ' grant(s) in 30 days',
           VALUE, DAY::TIMESTAMP_NTZ, 1.0
    FROM latest_posture
    WHERE METRIC = 'BREAKGLASS_GRANTS_30D' AND VALUE > 0
    UNION ALL
    SELECT 'TRUST CENTER', 'ALL', NULL, NULL, 'ALERT', SCANNER_ID,
           COALESCE(SEVERITY, 'MEDIUM'), SCANNER_NAME,
           CURRENT_COUNT || ' entity finding(s); ' || CHANGE_STATE,
           CURRENT_COUNT, SCANNED_AT::TIMESTAMP_NTZ,
           IFF(CHANGE_STATE IN ('NEW', 'REGRESSED'), 1.0, 0.9)
    FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_TRUST_DELTA
    WHERE CURRENT_COUNT > 0
    UNION ALL
    SELECT 'CHANGE RISK', COMPANY,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(COALESCE(USER_NAME, 'UNKNOWN')),
           IFF(DATABASE_NAME IS NULL, NULL,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)),
           IFF(DATABASE_NAME IS NULL, 'ALERT', 'OBJECT'),
           COALESCE(DATABASE_NAME || '.' || SCHEMA_NAME, QUERY_ID),
           RISK_LEVEL,
           CHANGE_KIND || ': ' || COALESCE(DATABASE_NAME || '.' || SCHEMA_NAME, QUERY_TYPE),
           COALESCE(USER_NAME, 'unknown') || ' via ' || COALESCE(ROLE_NAME, 'unknown'),
           1, EVENT_TS::TIMESTAMP_NTZ,
           RISK_SCORE / 100.0
    FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
    WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) AND RISK_SCORE >= 70
      -- V088 excluded DESTRUCTIVE (DROP/TRUNCATE) rows by Terraform service roles
      -- (TF_*) and on the app scratch schema DBA_MAINT_DB.PUBLIC -- the owner
      -- diagnostic (2026-08-17) put 94% of the flood on TF_* roles.
      -- V151: that exclusion also hid every TF_* DROP USER / DROP ROLE / DROP POLICY,
      -- because the loader files DROP_USER and DROP_ROLE under DESTRUCTIVE (its DROP
      -- test runs before its USER test). Identity and governance deletions are not
      -- truncate-and-reload noise, so a row is now kept whenever its QUERY_TYPE names
      -- a USER / ROLE / POLICY object or its statement opens DROP USER, DROP ROLE,
      -- DROP DATABASE ROLE, DROP APPLICATION ROLE or DROP (kind) POLICY. The preview
      -- test is keyword-anchored, never a bare POLICY substring, so a TF_* TRUNCATE of
      -- an insurance FACT_POLICY table stays excluded. Table/schema drops by TF_*
      -- roles and the DBA_MAINT_DB.PUBLIC churn stay excluded, NULL-safe as before.
      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND (
          UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'
          OR (UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'
              AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC'))
          AND NOT (COALESCE(QUERY_TYPE, '') ILIKE ANY ('%USER%', '%ROLE%', '%POLICY%')
              OR LTRIM(REGEXP_REPLACE(UPPER(COALESCE(QUERY_PREVIEW, '')), '[[:space:]]+', ' '))
                 LIKE ANY ('DROP USER %', 'DROP ROLE %', 'DROP DATABASE ROLE %',
                           'DROP APPLICATION ROLE %', 'DROP MASKING POLICY %',
                           'DROP ROW ACCESS POLICY %', 'DROP NETWORK POLICY %',
                           'DROP PASSWORD POLICY %', 'DROP SESSION POLICY %',
                           'DROP AUTHENTICATION POLICY %', 'DROP AGGREGATION POLICY %',
                           'DROP PROJECTION POLICY %', 'DROP JOIN POLICY %',
                           'DROP PACKAGES POLICY %', 'DROP PRIVACY POLICY %',
                           'DROP STORAGE LIFECYCLE POLICY %', 'DROP BACKUP POLICY %')))
      -- V158 (Next-Fifty #32): the backup-generation prune of OVERWATCH itself (task-run as SYSTEM,
      -- the exact generated DROP only). A human DROP of a backup or any other drop still surfaces.
      AND NOT (CHANGE_KIND = 'DESTRUCTIVE'
               AND UPPER(COALESCE(USER_NAME, '')) = 'SYSTEM'
               AND REGEXP_LIKE(COALESCE(QUERY_PREVIEW, ''),
                   'DROP TABLE IF EXISTS DBA_MAINT_DB[.]OVERWATCH_BAK[.][A-Z0-9_]+_OWBAK_[DW][0-9]{8}'))
), open_actions AS (
    SELECT SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY,
           MAX_BY(ACTION_ID, CREATED_AT) AS ACTION_ID,
           MAX_BY(OWNER, CREATED_AT) AS OWNER,
           MAX_BY(STATUS, CREATED_AT) AS ACTION_STATUS
    FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
    WHERE STATUS IN ('OPEN', 'IN_PROGRESS')
    GROUP BY 1, 2
)
SELECT c.DOMAIN, c.COMPANY, c.ACTOR_COMPANY, c.OBJECT_COMPANY,
       c.ENTITY_TYPE, c.ENTITY_KEY, c.SEVERITY, c.TITLE, c.DETAIL,
       c.IMPACT_COUNT,
       c.DETECTED_AT, c.CONFIDENCE, a.OWNER, a.ACTION_ID,
       COALESCE(a.ACTION_STATUS, 'UNTRACKED') AS STATUS
FROM candidates c
LEFT JOIN open_actions a
  ON a.SOURCE_ENTITY_TYPE = c.ENTITY_TYPE AND a.SOURCE_ENTITY_KEY = c.ENTITY_KEY;

-- First generation now, through the TASK: it runs as SYSTEM like every scheduled run (inside the
-- carve-out above), exercises the real task path, and keys the generation on the Central day
-- whatever the worksheet zone. Asynchronous: read TASK_HISTORY / OPERATOR_BACKUP_LOG in PART B.
EXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 158 AS VERSION,
       'Operator-data backups rotate daily (Next-Fifty #32): new TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK (owner-only, no FUTURE grants, so no grant churn in OVERWATCH). SP_BACKUP_OPERATOR_TABLES re-derived from V089 clones the 25 operator tables every day at 05:10 Central to immutable TRANSIENT OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd> generations (Sundays also _OWBAK_W, cloned from the D generation, and V089''s <T>_BAK_LAST statement, still weekly), logs backup vs source row counts to the new OPERATOR_BACKUP_LOG, prunes to SETTINGS BACKUP_KEEP_DAILY 14 / BACKUP_KEEP_WEEKLY 8 (floors 7/4, only TRANSIENT OVERWATCH_BAK tables whose whole name matches the generation pattern, dated before today) and stamps SOURCE_FRESHNESS_STATE OPERATOR_BACKUP_DAILY only on a run with zero clone failures. One set-based INFORMATION_SCHEMA probe per run (sources present + generations already taken today; fails open) and one set-based PRUNED log insert keep a steady-state day at 59 statements (Sunday 135). TASK_BACKUP_OPERATOR moved from Sunday 05:40 to daily 05:10 via SUSPEND/SET SCHEDULE/RESUME. V_SECURITY_EXCEPTION_QUEUE re-derived from V151 with one carve-out: the task''s own generation-prune DROP (USER_NAME SYSTEM, exact generated statement) leaves the CHANGE RISK queue. Restore = INSERT OVERWRITE as the table-owner role. Tail: EXECUTE TASK (first generation).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 158);

-- =====================================================================
--  MIGRATION 4 of 4 -- APPLY V159 (idempotent; GUARDS on V158; no tail CALL). Source: snowflake/migrations/V159__loader_compile_diet.sql
-- =====================================================================
-- V159__loader_compile_diet.sql
--
-- Loader compile diet (wave-2b rework, owner decisions D5 + D6). DIAG_CS_SELF_COST (2026-09-26) put the ten
-- heaviest OVERWATCH scheduled compile families at ~166 min/week on WH_ALFA_ADMIN (a floor, not the whole
-- scheduled total). After the alert scan, the next-largest
-- families are this migration's two procs, which recompile a heavy ACCOUNT_USAGE statement every hour for
-- data that changes far less often (runs/week x average compile, measured):
--   * SP_LOAD_MARTS_V27 HOURLY arm [1] MERGE MART_WAREHOUSE_EFFICIENCY_DAILY: 175 x 9.2 s = 27.0 min
--     (QUERY_HISTORY x2 + WAREHOUSE_METERING_HISTORY x2); arm [6] MERGE MART_TASK_GRAPH_DAILY: 175 x 5.1 s
--     = 14.8 min (TASK_HISTORY x2 + QUERY_ATTRIBUTION_HISTORY); arm [6b] MERGE MART_TASK_NODE_DAILY
--     (TASK_HISTORY) sits below the panel's cut, unmeasured. 175 = 24 hourly runs x 7 + the nightly
--     reconcile's 7 re-calls.
--   * SP_CHANGE_ATTRIBUTION's UPDATE WAREHOUSE_CHANGE_REGISTRY: 168 x 5.1 s = 14.4 min (an 8-day
--     QUERY_HISTORY join every hour, although registry rows only arrive when the change scan runs).
--
-- D5  SP_LOAD_MARTS_V27(VARCHAR, FLOAT), re-derived from V152 (its current definer). The HOURLY branch
--     reads the Central hour ONCE (SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP()))
--     INTO :ct_hour) and wraps arms [1], [6] and [6b] each in IF (d > 2 OR MOD(ct_hour, 4) = 0). They run in
--     the 00/04/08/12/16/20 Central cycles (6 of 24) and ALWAYS when d > 2: SP_NIGHTLY_RECONCILE (V064)
--     DELETEs D-3..today of MART_WAREHOUSE_EFFICIENCY_DAILY and MART_TASK_GRAPH_DAILY and re-loads them with
--     ('HOURLY', 3), so an hour-only gate would leave those days empty until the next gated hour; backfills
--     pass 90/365. TASK_LOAD_MARTS_V27_HOURLY passes ('HOURLY', 2), so d = 2 there. A gated-off arm counts
--     as OK (req_fail / opt_fail untouched) and appends no :loaded token, so the token-gated freshness MERGE
--     leaves its SOURCE_FRESHNESS_STATE row at its last stamp -- all three names contain DAILY, so the shared
--     30h cadence rule (health strip, freshness boards, NATIVE_ALERT_STALE_FACTS, OPS_PIPELINE_DEGRADED)
--     never reads them stale. The arms' own text is not touched (not even re-indented); every other HOURLY
--     arm, the DAILY scope, the freshness stamp and the RETURN are byte-identical to V152.
-- D6  SP_CHANGE_ATTRIBUTION(), re-derived from V033 (its only definer). An early-return guard runs the
--     unchanged UPDATE only IF EXISTS a WAREHOUSE_CHANGE_REGISTRY row with CHANGED_BY IS NULL and
--     CHANGE_SEEN_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP()). CHANGE_SEEN_AT is the scan's TIMESTAMP_LTZ
--     CURRENT_TIMESTAMP() stamp (V024/V109), so the probe uses the same clock as the proc's own 7-day filter
--     (an instant against an instant: no zone conversion). That gives ~3 hourly attempts per change, the
--     first at ~07:07 (27 min after the 06:40 scan) exactly as today; a skipped run returns
--     'attribution pass skipped (...)'.
--
-- Latency trade (disclosed):
--   * Today's partial row in the three marts is up to 4h old (5h across the November DST fall-back night,
--     when 01:00-02:00 Central repeats). Readers that see it later: the Optimize idle / sizing / remediation
--     panels and the idle headline, the Unit costs task-graph panel, the Operations node-timing board and
--     graph-roots failure counts, and SP_SLO_BREACH_SCAN (V096 reads MART_WAREHOUSE_EFFICIENCY_DAILY and
--     MART_TASK_NODE_DAILY), so a warehouse or task SLO breach can surface up to ~4h later -- SLO_OBJECTIVES
--     is empty today, so no SLO is affected yet. WAREHOUSE_METERING_HISTORY (~3h) and
--     QUERY_ATTRIBUTION_HISTORY (up to ~8h) already lag, which offsets part of it.
--   * Completed days are unaffected: the reconcile always re-loads D-3..today, and the 00 and 04 Central
--     cycles re-cover yesterday. SP_ALERT_SCAN_DAILY's COST_IDLE_OPPORTUNITY arm reads completed days only.
--   * A reconcile that fails mid-way now refills today at the next 4-hour slot, not the next hour.
--   * A manual CALL SP_LOAD_MARTS_V27('HOURLY', 2) outside those hours skips the three arms; pass 3 to force
--     them (snowflake/loader_chain_check.sql says so).
--   * Attribution: a row seen more than 3h ago is not retried unless a newer unattributed change arrives
--     inside its 7-day window (the unchanged UPDATE then retries all of them). Only an ACCOUNT_USAGE delay
--     beyond ~3h could lose an attribution that way.
--   * Pre-existing and NOT changed here: the -65/+5 min evidence window assumes an hourly snapshot, but the
--     snapshot is daily (06:40) or on demand (Operations Run scan), so only ALTERs made within ~65 min
--     before a scan are ever attributed. Fixing that changes behaviour and needs its own decision.
--
-- Estimated saving (ESTIMATES, DIAG family numbers): [1] and [6] drop from 175 to 49 runs/week (6 Central
-- cycles x 7 + the reconcile's 7): 27.0 -> 7.5 and 14.8 -> 4.2, about 30 compile-min/week, plus [6b]
-- (unmeasured). SP_CHANGE_ATTRIBUTION: 14.4 -> ~0.3-2.6 (168 small-table probes plus ~3 full UPDATEs on a
-- day with a warehouse change), about 12-14. Total ~42-44 of the ~166 the listed families total. Added: one scalar SELECT per
-- loader run (175/week, no table).
--
-- No task, schedule, rule, table or grant change and no tail CALL: the next hourly TASK_LOAD_HOURLY graph
-- runs both procs. Idempotent; safe to re-run. Owner applies in Snowsight after V158. This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20159, 'V159 requires V158 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 158) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27  (from V152; + the D5 Central 4-hour gate around HOURLY arms [1], [6] and [6b], always open when d > 2, V159)
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
                        WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
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
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
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
                FROM runs GROUP BY 1, 2, 3, 4
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
                SELECT c.USAGE_TIME::DATE AS DAY,
                       COALESCE(u.NAME, 'UNKNOWN') AS USER_NAME,
                       c.SOURCE AS SOURCE,
                       'n/a' AS MODEL_NAME,
                       ANY_VALUE(u.EMAIL) AS EMAIL,
                       -- V078: CORTEX_CODE_* USAGE_TIME is TIMESTAMP_TZ; the fact
                       -- columns are TIMESTAMP_NTZ and MERGE will not coerce TZ->NTZ
                       -- (live 2026-08-13: "expecting TIMESTAMP_NTZ(9) but got
                       -- TIMESTAMP_TZ(9) for column FIRST_TS" killed this arm on
                       -- every run, starving the AI coverage gate).
                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,
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
            STATUS = s.STATUS
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);

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

-- >>> derived:SP_CHANGE_ATTRIBUTION  (from V033; + the D6 recent-unattributed EXISTS gate, V159)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    -- V159 compile diet (D6): the UPDATE below compiles an 8-day ACCOUNT_USAGE.QUERY_HISTORY join, and it
    -- ran every hour. Registry rows only arrive when SP_WAREHOUSE_CHANGE_SCAN runs (the 06:40 daily scan,
    -- or an on-demand Run scan), and each row's evidence window is fixed around its CHANGE_SEEN_AT
    -- (-65/+5 min), so once QUERY_HISTORY has caught up (it lags ~45 min) a later retry finds nothing
    -- new. Run the pass only while a row seen in the last 3 hours is still unattributed: ~3 hourly
    -- attempts per change, the first at ~07:07 after the 06:40 scan as before; every other hour costs
    -- one small-table probe.
    -- CHANGE_SEEN_AT is the scan's TIMESTAMP_LTZ CURRENT_TIMESTAMP() stamp (V024/V109), compared on the
    -- same clock as the 7-day filter below. When the pass runs, the UPDATE is unchanged: it still retries
    -- every unattributed row of the last 7 days.
    IF (NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
                    WHERE CHANGED_BY IS NULL
                      AND CHANGE_SEEN_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP()))) THEN
        RETURN 'attribution pass skipped (no unattributed change seen in the last 3h)';
    END IF;

    -- Attribute unattributed registry rows from the last 7 days: the ALTER
    -- that ran within the 65 minutes before the hourly snapshot saw the
    -- change (5-minute forward grace for clock skew). Best effort.
    UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t
       SET CHANGED_BY = s.USER_NAME
      FROM (
          SELECT r.CHANGE_ID, MAX_BY(q.USER_NAME, q.START_TIME) AS USER_NAME
          FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
          JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
            ON q.START_TIME >= DATEADD('day', -8, CURRENT_TIMESTAMP())
           AND q.START_TIME BETWEEN DATEADD('minute', -65, r.CHANGE_SEEN_AT)
                                AND DATEADD('minute', 5, r.CHANGE_SEEN_AT)
           AND q.EXECUTION_STATUS = 'SUCCESS'
           AND q.QUERY_TYPE ILIKE 'ALTER%'
           AND q.QUERY_TEXT ILIKE '%' || r.WAREHOUSE_NAME || '%'
          WHERE r.CHANGED_BY IS NULL
            AND r.CHANGE_SEEN_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
          GROUP BY r.CHANGE_ID
      ) s
     WHERE t.CHANGE_ID = s.CHANGE_ID;

    RETURN 'attribution pass complete';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 159 AS VERSION,
       'Loader compile diet (wave-2b rework, owner decisions D5 + D6): two procs stop recompiling a heavy ACCOUNT_USAGE statement every hour. SP_LOAD_MARTS_V27 (re-derived from V152) reads the Central hour once at the top of its HOURLY branch and runs the three DAY-grain arms [1] MART_WAREHOUSE_EFFICIENCY_DAILY, [6] MART_TASK_GRAPH_DAILY and [6b] MART_TASK_NODE_DAILY only in the 00/04/08/12/16/20 Central cycles, and always when d > 2 (the nightly reconcile re-loads D-3..today with (''HOURLY'', 3); backfills pass 90/365). A gated-off arm is not a failure and appends no freshness token; the three names contain DAILY, so the 30h cadence rule never reads them stale. Every other arm, the DAILY scope, the freshness stamp and the RETURN are byte-identical. SP_CHANGE_ATTRIBUTION (re-derived from V033) runs its unchanged UPDATE only while a WAREHOUSE_CHANGE_REGISTRY row seen in the last 3 hours is unattributed (~3 attempts per change, the first at ~07:07 as before), else returns ''attribution pass skipped''. Latency trade: today''s partial row in the three marts is up to 4h old (5h across the November DST night) on the Optimize idle and sizing panels, the Unit costs task-graph panel, the Operations node-timing board and SP_SLO_BREACH_SCAN (V096; SLO_OBJECTIVES is empty today); completed days are unaffected. Estimated saving ~42-44 compile-min/week of the ~166 the listed DIAG families total. No task, schedule, rule, table or grant change; no tail CALL.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 159);

-- =====================================================================
--  PART B -- VERIFY (read-only; the commented smoke CALLs are optional). Paste every grid back;
--  an empty grid is an answer ('no rows'). Grids marked 'after ~1h' / 'after 24h' / 'after the next
--  4-hour cycle' are for later -- run them then.
-- =====================================================================
USE ROLE SNOW_ACCOUNTADMINS;

-- ---- V156 checks -----------------------------------------------------
-- (V156.1) ETL-cycle push rules seeded + SP_SCAN_ETL_CYCLE carries the plan corrections and the D7 run window (nothing calls it until V157's arm)
SELECT RULE_ID, FAMILY, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID LIKE 'PIPE_ETL_%' ORDER BY 1;
-- expect 3 rows, all PIPELINE / ENABLED TRUE / WINDOW_HOURS 24 / AUTO_CLEAR_ENABLED FALSE:
--   PIPE_ETL_CYCLE_LATE HIGH 60, PIPE_ETL_CYCLE_NOT_STARTED HIGH 120, PIPE_ETL_TASK_FAILED MEDIUM 1
WITH d AS (SELECT GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()') AS DDL)
SELECT CONTAINS(DDL, 't.TERMINAL_START >= s.CYCLE_START') AS TERMINAL_ATTEMPT_FILTER,
       CONTAINS(DDL, 'GREATEST(c.MIN_FAILED, 1)') AS THRESHOLD_FLOOR,
       CONTAINS(DDL, 'CROSS JOIN term_lw tl') AS REGULAR_TERMINAL_GUARD,
       CONTAINS(DDL, 'OR g.IS_COMPLETE, g.SEVERITY') AS SEVERITY_SPLIT,
       CONTAINS(DDL, 'NOT COALESCE(g.PROJECTED_FINISH > g.DL_H, FALSE)') AS LEAD_WINDOW_METRIC,
       CONTAINS(DDL, 'MAX(COALESCE(t.FIRST_OK_END, t.TERMINAL_END))') AS FIRST_OK_END_FINISH,
       CONTAINS(DDL, 'COUNT_IF(t.TERMINAL_END IS NULL) = 0') AS CLEAR_ONLY_FINISHED,
       CONTAINS(DDL, 'USING (start_wf)') AS STARTER_BOUND,
       CONTAINS(DDL, 'MAX(TASK_START_DTTM) AS CYC_LAST_START') AS LAST_KICKOFF,
       CONTAINS(DDL, 'TASK_START_DTTM >= CYC_LAST_START') AS START_BOUND,
       CONTAINS(DDL, 'FLOOR(MOD(:target_off - 600 + 1440, 1440) / 60) + 24, 24) > 13') AS RUN_WINDOW,
       CONTAINS(DDL, 'HOUR(:now_ct) <> 15') AS DAYTIME_PASS,
       POSITION('HOUR(:now_ct) <> 15' IN DDL) BETWEEN 1 AND POSITION('DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS' IN DDL) AS GATE_BEFORE_DELETE,
       CONTAINS(DDL, 'IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS') AS CLEAR_ONLY_WITH_OPEN
FROM d;   -- all TRUE (every fragment is quote-free, so the GET_DDL re-quoting of the body never changes the match)
SELECT COLUMN_NAME, DATA_TYPE FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ETL_CYCLE_TASKS' ORDER BY ORDINAL_POSITION;   -- 9 columns incl. TERMINAL_START, FIRST_OK_END, SCANNED_AT
SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) AS CENTRAL_HOUR;   -- at 07:00 the proc works only in hours 21-23, 0-10 and 15
-- OPTIONAL smoke (raises REAL alerts, deduped per night; HIGH/CRITICAL email). Outside the run window it only returns
-- 'etl cycle scan skipped (outside the run window: ...; cache kept)' and changes nothing:
-- CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();

-- (V156.2) after V157's arm has run ~24h (ACCOUNT_USAGE lags ~45 min): the cache is rebuilt only in the run window
SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', START_TIME)) AS CENTRAL_HOUR, COUNT(*) AS CACHE_REBUILDS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('hour', -26, CURRENT_TIMESTAMP())
  AND QUERY_TYPE = 'DELETE'
  AND QUERY_TEXT ILIKE 'DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS%'
GROUP BY 1 ORDER BY 1;   -- at the 07:00 default: only hours 21-23, 0-10 and 15, about one row each

-- ---- V157 checks -----------------------------------------------------
-- ===== V157 PART B (V157.1): right after apply; read-only =====
-- expect 8 rows: COST_CLOUD_SVC_RATIO ABSENT (retired), COST_CLOUD_SVC_ANOMALY ENABLED; AUTO_CLEAR_ENABLED TRUE only on the 2 SEC + 3 PERF rules
SELECT RULE_ID, FAMILY, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
WHERE RULE_ID IN ('OPS_PIPELINE_DEGRADED', 'COST_IDLE_OPPORTUNITY', 'SEC_CRED_EXPIRY', 'SEC_NEW_EXPOSURE',
                  'PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB',
                  'COST_CLOUD_SVC_RATIO', 'COST_CLOUD_SVC_ANOMALY')
ORDER BY 1;
-- any OTHER rule opted into auto-clear is no longer blanket-cleared by the V091 sweep (expect 0 rows; report any)
SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
WHERE AUTO_CLEAR_ENABLED
  AND RULE_ID NOT IN ('SEC_CRED_EXPIRY', 'SEC_NEW_EXPOSURE', 'PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB');
-- the retirement: expect NO OPEN / ACK / SNOOZED row (the EXPECTED rows resolved just now are the ones V157 closed)
SELECT STATUS, RESOLUTION_KIND, COUNT(*) AS N, MAX(RESOLVED_AT) AS LAST_RESOLVED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO'
GROUP BY 1, 2 ORDER BY 1, 2;
-- proc bodies (every fragment is quote-free: GET_DDL re-quotes the body). Expect TRUE except the 5 marked FALSE.
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'OPS_PIPELINE_DEGRADED') AS H_SELF_WATCH,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SP_SCAN_ETL_CYCLE') AS H_ETL_ARM,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'CONDITION_ENDED') AS H_CE_SWEEP,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'V157: only rules whose still-firing set this sweep recomputes') AS H_V091_SCOPED,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'OR e.DETAIL LIKE (') AS H_ARM10_CYCLE_ID,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'INTO :ct_hour') AS H_HOUR_READ,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'IF (MOD(ct_hour, 4) = 1) THEN') AS H_SECURITY_GATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'IF (MOD(ct_hour, 3) = 2) THEN') AS H_SELF_WATCH_GATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'ALERT_SCAN_HOURLY heartbeat stamp') AS H_HEARTBEAT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'IF (SQLROWCOUNT = 0) THEN') AS H_HB_POINT_UPDATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), '/12 rule blocks ok') AS H_TALLY_12,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'alert scan v12 (V157:') AS H_V12,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'SEC_BREAK_GLASS_USE') AS H_DEAD_ARM_LEFT,          -- FALSE
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'COST_CLOUD_SVC_RATIO') AS H_RATIO_ARM_LEFT,        -- FALSE
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'WAREHOUSE_METERING_HISTORY') AS H_WMH_READ_LEFT,   -- FALSE
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()'), 'MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE') AS H_HB_MERGE_LEFT,  -- FALSE
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'COST_IDLE_OPPORTUNITY') AS D_IDLE_ARM,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'OPS_PIPELINE_DEGRADED') AS D_SELF_WATCH,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'ALERT_SCAN_DAILY heartbeat stamp') AS D_HEARTBEAT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'IF (SQLROWCOUNT = 0) THEN') AS D_HB_POINT_UPDATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'alert scan daily v3 (V157:') AS D_V3,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'JOIN newest n ON s.SNAPSHOT_AT >=') AS D_IDLE_NEWEST_BATCH,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), 'ct_hour') AS D_GATED;                     -- FALSE
SELECT VERSION, APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 157;

-- ===== LATER grid (V157.2): after about 1 hour. Do NOT CALL the scans by hand -- that raises and emails real alerts =====
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, STATUS, GENERATION FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('ALERT_SCAN_HOURLY', 'ALERT_SCAN_DAILY');
-- expect hourly STATUS 'alert scan 12/12 rule blocks ok', GENERATION +1 per hourly run (1 on the first run: the row is inserted once)
-- ALERT_SCAN_DAILY appears after the next daily run (~07:00-07:30 CT) with 'alert scan daily 11/11 rule blocks ok (daily)'
SELECT SCHEDULED_TIME, STATE, RETURN_VALUE
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'TASK_ALERT_SCAN', RESULT_LIMIT => 10))
ORDER BY SCHEDULED_TIME DESC;
-- expect STATE SUCCEEDED and RETURN_VALUE ending '12/12 rule blocks ok'
SELECT LOGGED_AT, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 200) AS MSG FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'AlertScan'
  AND LOGGED_AT >= DATEADD('hour', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
ORDER BY 1 DESC;
-- expect no rule_block_failed, scan_heartbeat_failed, condition_ended_sweep_failed or cadence_gate_failed
-- etl_cycle_scan_failed = probe E5 (SELECT grant on CONTROL_STATUS) failed, or V156 is missing

-- ===== LATER grid (V157.3): after 24 hours -- the gates, measured on the live account =====
-- the security arms compile ONLY at CT_HOUR 1,5,9,13,17,21; the self-watch at 2,5,8,...,23 (+ the daily scan around 7); the ratio arm never
SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', START_TIME)) AS CT_HOUR,
       COUNT_IF(QUERY_TEXT ILIKE '%INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS%' AND QUERY_TEXT ILIKE '%GRANTS_TO_ROLES%') AS SEC_EXPOSURE_RUNS,
       COUNT_IF(QUERY_TEXT ILIKE '%INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS%' AND QUERY_TEXT ILIKE '%ACCOUNT_USAGE.CREDENTIALS%') AS CRED_EXPIRY_RUNS,
       COUNT_IF(QUERY_TEXT ILIKE '%INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS%' AND QUERY_TEXT ILIKE '%|STALE|%') AS SELF_WATCH_RUNS,
       COUNT_IF(QUERY_TEXT ILIKE '%COST_CLOUD_SVC_RATIO%' AND QUERY_TEXT ILIKE '%WAREHOUSE_METERING_HISTORY%') AS RATIO_RUNS,
       ROUND(SUM(COMPILATION_TIME) / 60000, 2) AS ALERT_EVENTS_COMPILE_MIN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
  AND USER_NAME = 'SYSTEM'
  AND QUERY_TEXT ILIKE '%DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS%'
GROUP BY 1 ORDER BY 1;
SELECT RULE_ID, SEVERITY, STATUS, TITLE, DEDUPE_KEY, RAISED_AT FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID IN ('OPS_PIPELINE_DEGRADED', 'COST_IDLE_OPPORTUNITY', 'OPS_SCAN_DEGRADED', 'COST_CLOUD_SVC_RATIO')
  AND RAISED_AT >= DATEADD('hour', -26, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
ORDER BY RAISED_AT DESC;
-- expect no COST_CLOUD_SVC_RATIO row
SELECT RULE_ID, RESOLUTION_KIND, COUNT(*) AS N FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RESOLUTION_KIND IN ('CONDITION_ENDED', 'AUTO_CLEARED')
  AND RESOLVED_AT >= DATEADD('hour', -26, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
GROUP BY 1, 2;
-- AUTO_CLEARED only for PERF_* (and V156's PIPE_ETL_TASK_FAILED); CONDITION_ENDED only for the two SEC rules, stamped at a :0x minute of hours 1,5,9,13,17,21

-- ---- V158 checks -----------------------------------------------------
-- (V158.1) operator backup generations -- run ~2 min after the V158 apply (its tail EXECUTE TASK is asynchronous).
-- Read-only. Runs under the RUN_NEXT prelude (USE ROLE SNOW_ACCOUNTADMINS; ALTER SESSION SET TIMEZONE = 'America/Chicago').
SHOW SCHEMAS LIKE 'OVERWATCH_BAK' IN DATABASE DBA_MAINT_DB;   -- options = TRANSIENT; comment names V158
SHOW FUTURE GRANTS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;      -- expect 0 rows (generations cause no grant churn)
SHOW FUTURE GRANTS IN DATABASE DBA_MAINT_DB;                  -- expect 0 rows; any row = generations inherit it (report back, probe F6)
SHOW TASKS LIKE 'TASK_BACKUP_OPERATOR' IN SCHEMA DBA_MAINT_DB.OVERWATCH;   -- schedule USING CRON 10 5 * * * America/Chicago, state started
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, ERROR_MESSAGE
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'TASK_BACKUP_OPERATOR', RESULT_LIMIT => 3))
ORDER BY SCHEDULED_TIME DESC;   -- newest (the EXECUTE TASK run) SUCCEEDED
SELECT ACTION, COUNT(*) AS N, SUM(ROW_COUNT) AS BACKUP_ROWS, SUM(SOURCE_ROW_COUNT) AS SOURCE_ROWS
FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
WHERE LOGGED_AT >= DATEADD('hour', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
GROUP BY 1 ORDER BY 1;   -- CLONED = 25 - SKIPPED_MISSING; no CLONE_FAILED, nothing PRUNED on day 1
SELECT SOURCE_TABLE, BACKUP_TABLE, ROW_COUNT, SOURCE_ROW_COUNT
FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
WHERE ACTION = 'CLONED' AND ROW_COUNT <> SOURCE_ROW_COUNT
  AND LOGGED_AT >= DATEADD('hour', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ);   -- expect none (a hot table may differ by a few rows)
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY';   -- LAST_LOAD_TS = now (Central); STATUS 'backup D<today>: 25 cloned, 0 failed, 0 missing, 0 pruned, 0 prune failed'
SELECT TABLE_NAME, IS_TRANSIENT, ROW_COUNT
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'OVERWATCH_BAK' ORDER BY 1;   -- 25 x <T>_OWBAK_D<today> (+ _OWBAK_W<today> on a Sunday), all IS_TRANSIENT = YES
SELECT LOGGED_AT, ERROR_TYPE, LEFT(ERROR_MESSAGE, 200) AS MSG, CONTEXT
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'BackupOperatorTables'
  AND LOGGED_AT >= DATEADD('hour', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
ORDER BY 1 DESC;   -- expect 0 rows (clone_failed / backup_log_failed / backup_prune_failed / backup_incomplete)
-- Proc + view shape. Every fragment is quote-free (GET_DDL re-quotes a SQL proc body).
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()'), 'DBA_MAINT_DB.OVERWATCH_BAK.') AS PROC_GENERATIONS,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()'), 'OPERATOR_BACKUP_DAILY') AS PROC_FRESHNESS,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()'), 'INTO :src_present, :have_d, :have_w') AS D11_ONE_PROBE,
       NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()'), 'TABLE_NAME = :tname') AS D11_NO_PER_TABLE_PROBE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES()'), 'FLATTEN(INPUT => SPLIT(TRIM(:pruned_list)') AS D11_ONE_PRUNE_LOG,
       CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), '_OWBAK_') AS PRUNE_CARVE_OUT,
       CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'DROP MASKING POLICY %') AS V151_KEEP_LIST_KEPT;   -- all TRUE
SELECT KEY, VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
WHERE KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY') ORDER BY 1;   -- 14 / 8 (or the operator's edit)
SELECT VERSION, APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 158;

-- ---------------------------------------------------------------------------------------------------
-- LATER (read-only, the day after apply, once the 05:10 run is in ACCOUNT_USAGE, ~45 min lag):
-- the D11 statement budget as measured. Statements per backup run from the task's own session:
-- expect about 33 a weekday for days 1-14, about 59 once the prune runs (day 15 on), about 135 on a
-- steady-state Sunday (the V089 weekly backup ran 26 a week). A count far above the model means
-- Snowflake Scripting expressions compile as statements: report the number back. A session where you
-- ran CALL by hand also counts your other worksheet statements; ignore that row.
SELECT TO_DATE(CONVERT_TIMEZONE('America/Chicago', q.START_TIME)) AS RUN_DAY_CT,
       q.SESSION_ID,
       COUNT(*) AS STATEMENTS,
       ROUND(SUM(q.COMPILATION_TIME) / 1000, 1) AS COMPILE_S,
       ROUND(SUM(q.TOTAL_ELAPSED_TIME) / 1000, 1) AS ELAPSED_S,
       ROUND(SUM(q.CREDITS_USED_CLOUD_SERVICES), 4) AS CS_CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
WHERE q.START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
  AND q.SESSION_ID IN (SELECT SESSION_ID FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                        WHERE START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
                          AND QUERY_TEXT ILIKE 'CALL DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES%')
GROUP BY 1, 2 ORDER BY 1, 2;

-- ---------------------------------------------------------------------------------------------------
-- LATER (optional, any day): prune + CHANGE RISK carve-out smoke without waiting 15 days. NOT read-only.
-- Uncomment and run AS THE ROLE THAT OWNS SP_BACKUP_OPERATOR_TABLES: the owner's-rights prune DROPs as
-- that role, so fakes owned by any other role end as PRUNE_FAILED. Find it first:
-- SELECT PROCEDURE_OWNER FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
--  WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_BACKUP_OPERATOR_TABLES';
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000101 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000102 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000103 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000104 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000105 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000106 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000107 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000108 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000109 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000110 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000111 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000112 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000113 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000114 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.USER_PREFS_OWBAK_D20000115 CLONE DBA_MAINT_DB.OVERWATCH.USER_PREFS;
-- EXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;   -- through the TASK, so the DROPs run as SYSTEM
-- -- ~2 min later. PRUNED = (real D generations + 15) - 14, oldest first; same day as apply: D20000101 + D20000102.
-- -- D11: those PRUNED rows arrive from ONE set-based insert, one row per dropped generation (same shape as before).
-- SELECT BACKUP_TABLE, ACTION, LOGGED_AT FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
--  WHERE ACTION LIKE 'PRUNE%' ORDER BY LOGGED_AT DESC, BACKUP_TABLE;
-- -- The other fakes age out one a day through the task (never drop them by hand: a human DROP is not carved out).
-- -- ~1h later (ACCOUNT_USAGE lag + SP_LOAD_SECURITY_FACTS):
-- SELECT USER_NAME, ROLE_NAME, CHANGE_KIND, RISK_SCORE, QUERY_PREVIEW FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
--  WHERE QUERY_PREVIEW ILIKE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.%' ORDER BY EVENT_TS DESC LIMIT 20;   -- USER_NAME = SYSTEM (probe F5)
-- SELECT * FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE
--  WHERE DOMAIN = 'CHANGE RISK' AND DETAIL LIKE 'SYSTEM via %'
--    AND DETECTED_AT >= DATEADD('day', -1, CURRENT_TIMESTAMP());   -- expect 0 rows

-- ---- V159 checks -----------------------------------------------------
-- (V159.1) loader compile diet -- read-only; run right after the V159 apply. RUN_NEXT prelude sets Central.
-- Every GET_DDL fragment is QUOTE-FREE.
SELECT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), 'INTO :ct_hour') AS MARTS_HOUR_READ,
       REGEXP_COUNT(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), 'IF [(]d > 2 OR MOD[(]ct_hour, 4[)] = 0[)] THEN') AS MARTS_GATES,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(VARCHAR, FLOAT)'), 'MART_TASK_NODE_DAILY - other marts unaffected') AS MARTS_V152_BODY_KEPT,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION()'), 'attribution pass skipped') AS ATTR_GATE,
       CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION()'), 'WHERE CHANGED_BY IS NULL') AS ATTR_PROBE;
-- expect TRUE, 3, TRUE, TRUE, TRUE
SELECT VERSION, APPLIED_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 159;

-- (V159.2) after the next 00/04/08/12/16/20 Central cycle (>= ~5h after apply). Read-only.
SELECT SOURCE_NAME, LAST_LOAD_TS, SNAPSHOT_TS, GENERATION, STATUS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'MART_TASK_GRAPH_DAILY', 'MART_TASK_NODE_DAILY',
                      'MART_QUERY_FAMILY_DAILY')
ORDER BY 1;
-- gated rows: SNAPSHOT_TS in a 00/04/08/12/16/20 Central cycle (or the ~06:46 reconcile), <= ~4h ago (5h on the
-- November DST night); MART_QUERY_FAMILY_DAILY (ungated control) within the last ~1h.

-- (V159.3) 24h+ after apply (ACCOUNT_USAGE lags ~45 min). Read-only.
SELECT CASE WHEN QUERY_TEXT ILIKE '%MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY%' THEN '[1] wh_eff'
            WHEN QUERY_TEXT ILIKE '%MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY%' THEN '[6] graphs'
            WHEN QUERY_TEXT ILIKE '%MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY%' THEN '[6b] task_node'
            WHEN QUERY_TEXT ILIKE '%UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t%SET CHANGED_BY%' THEN 'attribution UPDATE'
       END AS FAMILY,
       COUNT(*) AS RUNS_24H,
       ROUND(AVG(COMPILATION_TIME) / 1000, 2) AS AVG_COMPILE_S,
       ROUND(SUM(COMPILATION_TIME) / 60000, 2) AS COMPILE_MIN_24H,
       ROUND(SUM(CREDITS_USED_CLOUD_SERVICES), 4) AS CS_CREDITS_24H
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME = 'WH_ALFA_ADMIN'
  AND QUERY_TYPE IN ('MERGE', 'UPDATE')
GROUP BY 1
HAVING FAMILY IS NOT NULL
ORDER BY 1;
-- expect [1]/[6]/[6b] = 7 runs each (6 Central 4-hour cycles + the reconcile; was 25/day);
-- attribution UPDATE = 0 on a no-change day, ~3 on a changed day (was 24/day).

-- OPTIONAL smoke (what TASK_CHANGE_ATTRIBUTION runs hourly anyway; idempotent):
-- CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION();
-- -> 'attribution pass skipped (no unattributed change seen in the last 3h)' unless a change was seen in the last 3h.

-- (all) V156 through V159 are registered (4 rows).
SELECT VERSION, APPLIED_AT
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION BETWEEN 156 AND 159 ORDER BY VERSION;

ALTER SESSION UNSET TIMEZONE;
