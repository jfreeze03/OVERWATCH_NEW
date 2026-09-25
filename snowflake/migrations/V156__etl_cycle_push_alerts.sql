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
--                           that started at/after that night's FIRST starter start and ended at/after its LAST
--                           starter start (the night's last kickoff; the starter name is a bound value).
--   * SP_SCAN_ETL_CYCLE() - allowlist-validates the FQN, rebuilds ETL_CYCLE_TASKS with one EXECUTE IMMEDIATE,
--                           then raises three rules itself, each inside its own EXCEPTION guard:
--       PIPE_ETL_TASK_FAILED       MEDIUM (HIGH for the terminal workflow). One event per (workflow, night)
--                                  whose final-attempt failed-task count >= THRESHOLD_NUM (1; a threshold
--                                  below 1 still needs one failure). Auto-clears (AUTO_CLEARED, OPEN-only)
--                                  when a retry later succeeds: every final attempt of that workflow-night has
--                                  FINISHED and none failed (a retry still running keeps the event OPEN, so a
--                                  retry that fails again never re-raises it).
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
--                                  clean finish after the night's last kickoff (FIRST_OK_END), so a re-run of
--                                  it after the night finished (e.g. a next-morning recon re-run keyed to the
--                                  same night) never re-grades an on-time night, and a clean finish from before
--                                  the real kickoff (an afternoon re-run of the whole chain) is never its
--                                  FIRST_OK_END, so it never hides the real terminal run once that run starts;
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
-- Known edges (documented, not fixed). FIRST_OK_END must end at/after the night's LAST starter start, so a
-- starter attempt keyed to the night that starts after the terminal's clean finish voids it (a next-morning
-- re-run of the STARTER, or a starter task that runs after the terminal): that night then grades each terminal
-- task on its latest attempt, as before FIRST_OK_END, so a terminal re-run alongside it re-grades a night that
-- finished on time by the re-run's own clock (a CRIT/EXH that is CRITICAL while it runs, HIGH once it ends
-- clean); a starter re-run alone stays quiet. When ETL_CYCLE_START_WORKFLOW = ETL_CYCLE_END_WORKFLOW every task
-- is a starter task, so a next-morning re-run of ANY task keyed to the night does the same. Both fail LOUD, by
-- choice: a special case for one workflow would let an afternoon re-run of it complete the night and silence a
-- hung real run. When BOTH the starter and the terminal are re-run in the afternoon, CYCLE_START = MIN(starter
-- start) is the afternoon run (the app's MIN semantics); the afternoon finish never counts as FIRST_OK_END once
-- the real kickoff runs, but the terminal's afternoon attempt still reads the night complete until the real
-- terminal run starts, so a real cycle that hangs before its terminal dispatches stays quiet that night
-- (TASK_FAILED still reports a failed real run). A terminal task removed for good reads the first night after as
-- incomplete (the usual task count comes from the prior clean nights). Divergence from the app (deliberate): the
-- pull side (cycle_finish_history_scan, the SLA finish forecast) reads each terminal task's LATEST attempt, so a
-- next-morning terminal re-run of a night that already finished shows there as running / failed while LATE stays
-- quiet.
-- Wiring: V157 re-derives the hourly alert scan with an add-on CALL arm [23] for this proc, outside the core
-- tally (a CONTROL_STATUS grant gap logs etl_cycle_scan_failed and never trips OPS_SCAN_DEGRADED); until V157
-- is applied nothing calls it. HIGH/CRITICAL reach the OVERWATCH_EMAIL path (NATIVE_ALERT_NEW_EVENTS) and any
-- matching Teams route. Needs the SELECT on CONTROL_STATUS the app panels already use (owner role). No task
-- change, no tail CALL. Owner applies in Snowsight after V155. This file never runs from the app.

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

    -- always clear last run's cache first, so a stale night never lingers after a disable or a fix.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS;
    IF (:n_enabled = 0) THEN
        RETURN 'etl cycle scan skipped (rules disabled)';
    END IF;

    -- a missing row falls back to DEFAULT_SETTINGS (app/config.py ETL_CYCLE_* / ETL_SLA_*); a blank row
    -- stays blank and disables the rule that needs it, exactly like the app's merged settings.
    SELECT MAX(IFF(KEY = 'ETL_CONTROL_STATUS_FQN', VALUE, NULL)),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_CYCLE_START_WORKFLOW', VALUE, NULL)), 'WF_BASE_GW_CLOSEOUT_CTL_DLY')),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_CYCLE_END_WORKFLOW', VALUE, NULL)), 'WF_BASE_RECON_MTRC_CMPSIT_DAILY')),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_SLA_TARGET_HHMM', VALUE, NULL)), '07:00')),
           TRIM(COALESCE(MAX(IFF(KEY = 'ETL_SLA_BREACH_HHMM', VALUE, NULL)), '08:00'))
      INTO :ctl_fqn, :start_wf, :end_wf, :target_raw, :breach_raw
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    IF (:ctl_fqn IS NULL OR TRIM(:ctl_fqn) = ''
        OR NOT RLIKE(TRIM(:ctl_fqn), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')) THEN
        RETURN 'etl cycle scan skipped (unconfigured or invalid ETL_CONTROL_STATUS_FQN)';
    END IF;

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

    -- The ONLY dynamic statement. The FQN is validated above (a bare, well-formed identifier), so it is safe
    -- to concatenate; lookback_days is an INT; the starter name is BOUND as data (USING), never concatenated.
    -- WHOLE nights only: a row is kept when its night key is one of the newest lookback_days nights (the
    -- timestamp prefilter, one day wider, only prunes), so the oldest cached night is never a partial night
    -- (a partial one would let the retry auto-clear below resolve a failure that was never re-run). One row per
    -- (night, workflow, task): the terminal attempt's status / start / end by COALESCE(end, start) (a
    -- FAILED-then-retried-SUCCESS task reads SUCCESS), the first start of any attempt, and FIRST_OK_END = the
    -- earliest clean finish (ended, not a failed status) among the attempts that started at/after that night's
    -- FIRST starter start (CYC_START, as [C] starts) and ended at/after its LAST starter start (CYC_LAST_START,
    -- the night's last kickoff), so a re-run after the night already finished (a next-morning recon re-run
    -- keyed to the same night) never re-opens it, while a clean finish from before the real kickoff (an
    -- afternoon re-run of the terminal, or of the whole chain) is never the night's first clean finish. No special
    -- case when the starter IS the terminal workflow (the header's known edges: it fails loud, never silent).
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
               || 'cs AS (SELECT CD AS CS_NIGHT, MIN(TASK_START_DTTM) AS CYC_START, '
               || 'MAX(TASK_START_DTTM) AS CYC_LAST_START FROM src WHERE WF = ? GROUP BY CD) '
               || 'SELECT CD, WF, TN, '
               || 'LEFT(TO_VARCHAR(MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))), 100), '
               || 'MIN(TASK_START_DTTM)::TIMESTAMP_NTZ, '
               || 'MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ, '
               || 'MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ, '
               || 'MIN(IFF(TASK_END_DTTM IS NOT NULL '
               || 'AND COALESCE(UPPER(TASK_STATUS), '''') NOT IN (''ABORTED'', ''ERROR'', ''ERRORED'', ''FAILED'', ''KILLED'', ''STOPPED'', ''TERMINATED'') '
               || 'AND TASK_START_DTTM >= CYC_START AND TASK_END_DTTM >= CYC_LAST_START, TASK_END_DTTM, NULL))::TIMESTAMP_NTZ '
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
        -- its oldest night is never a partial one whose failed tasks were cut away.
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
    --     with a clean finish after the night's last kickoff is done at FIRST_OK_END, whatever a later re-run
    --     of it is doing).
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
                -- terminal tasks of the SAME night. A task with a clean finish after the night's last kickoff
                -- (FIRST_OK_END) is DONE at that first clean finish: a later re-run of it (the next morning,
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
       'Nightly ETL cycle PUSH alerts (Next-Fifty rank 2): ETL_CYCLE_TASKS transient cache + SP_SCAN_ETL_CYCLE() (isolated config-driven read of ETL_CONTROL_STATUS_FQN, FQN-allowlisted, the 23 newest WHOLE nights cut by night key, retries collapsed to the terminal attempt via MAX_BY with that attempt''s start kept as TERMINAL_START, plus FIRST_OK_END = the first clean finish among attempts started at/after the night''s first kickoff and ended at/after its last (MIN / MAX starter start; starter name bound via USING), night key DATE(TASK_START_DTTM - 12h), clock pinned America/Chicago) raising PIPE_ETL_TASK_FAILED (MEDIUM, HIGH for the terminal workflow, threshold never below 1, auto-clears on retry success: every final attempt finished and none failed, so a retry still running keeps it OPEN), PIPE_ETL_CYCLE_NOT_STARTED (HIGH, the app NEXT_CYCLE_OVERDUE test, grace = THRESHOLD_NUM min) and PIPE_ETL_CYCLE_LATE (WARN/CRIT/EXH bands superseded by the V067 sweep; lead = THRESHOLD_NUM min before ETL_SLA_TARGET_HHMM, or the start-shifted median of the newest 14 prior clean nights projects past ETL_SLA_BREACH_HHMM; a night that finished late is HIGH, an unfinished miss CRITICAL, which auto-declares an incident; a terminal task is done at its first clean finish after the night''s last kickoff (FIRST_OK_END), so a next-morning terminal re-run never re-grades a finished night and an afternoon re-run of the whole chain never hides the real terminal run (a next-morning starter re-run, or any re-run when the starter IS the terminal workflow, voids it: the night re-grades loud, never silent), else counts only when its terminal attempt started at/after the night''s cycle start; an undispatched terminal is judged only when it ran the same night last week; METRIC_VALUE only on a lead-window WARN), each in its own EXCEPTION guard. Three PIPELINE ALERT_CONFIG rules, WHEN NOT MATCHED, AUTO_CLEAR left at its default. Called hourly once V157 adds the SP_ALERT_SCAN add-on CALL arm (not counted toward OPS_SCAN_DEGRADED). Needs SELECT on CONTROL_STATUS (already granted for the app panels).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 156);
