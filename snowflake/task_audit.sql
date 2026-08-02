-- task_audit.sql — deploy-time TASK DRIFT check (Wave 3 #6).
--
-- WHY THIS EXISTS
-- Several migrations create tasks with `CREATE TASK IF NOT EXISTS`. That guard
-- makes the statement a NO-OP when the task already exists — so if a later
-- migration (or a hand-edit) changes a task's WAREHOUSE, SCHEDULE, or
-- predecessor chain, the LIVE task keeps its OLD definition and nothing tells
-- you. This snippet diffs the live SHOW TASKS output against an operator-
-- maintained EXPECTED set and prints a VERDICT per task, so stale definitions
-- surface AT DEPLOY instead of as a silent behavioral drift weeks later.
--
-- HOW TO USE
--   1. Run the whole file as a role that can see the OVERWATCH tasks
--      (SNOW_SYSADMINS / SNOW_ACCOUNTADMINS).
--   2. Read the result. Every row should say OK. Any 'DRIFT: ...' row names the
--      exact task + field that disagrees. Remediate with an explicit
--      `ALTER TASK ... SUSPEND; CREATE OR REPLACE TASK ...; ... RESUME;` (or the
--      migration that was supposed to update it), NOT another IF NOT EXISTS.
--
-- MAKING IT GATING
--   This file only PRINTS a verdict; it never fails. To turn drift into a hard
--   stop, wrap the final SELECT in an isolated EXECUTE IMMEDIATE scripting block
--   (its own smoke, kept OUT of this read-only file) that counts the non-OK rows
--   and RAISEs when the count is > 0 -- e.g. DECLARE a drift EXCEPTION, SELECT
--   COUNT(*) INTO a var FROM (this SELECT) WHERE VERDICT <> 'OK', then RAISE it
--   IF the var > 0. Day-to-day runs stay read-only; CI/deploy runs the smoke.
--
-- MAINTAINING THE EXPECTED SET
--   The `expected` CTE below is the source of truth for what SHOULD be deployed.
--   Put NULL in any *_EXPECTED column you do not want checked — the diff skips
--   NULLs, so you can start loose (name + warehouse + state) and tighten
--   SCHEDULE / PREDECESSOR on the tasks whose timing or chaining is load-bearing.
--   This is documentation + a check, not a migration — it never mutates a task.
--
--   The seed below asserts the common case: every task RUNNING on WH_ALFA_ADMIN.
--   Reconcile it to your actual deployment before trusting the OK rows — e.g. a
--   serverless task shows WAREHOUSE = NULL and should carry WAREHOUSE_EXPECTED
--   = NULL, and a task you intentionally keep suspended should carry
--   STATE_EXPECTED = 'suspended'.

SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;

WITH live AS (
    SELECT "name"         AS NAME,
           "state"        AS STATE,          -- 'started' | 'suspended'
           "warehouse"    AS WAREHOUSE,      -- NULL for serverless tasks
           "schedule"     AS SCHEDULE,       -- e.g. 'USING CRON ...' or '60 MINUTE'
           "predecessors" AS PREDECESSORS,   -- array of fully-qualified names
           -- Normalize predecessors to a VARIANT array so the exact-match diff
           -- below works whether SHOW TASKS returns an ARRAY or its JSON string.
           PARSE_JSON(TO_VARCHAR("predecessors")) AS PRED_JSON
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
),
expected AS (
    -- OPERATOR-MAINTAINED. One row per task. NULL = "do not check this field".
    --
    -- SCHEDULE_EXPECTED / PREDECESSOR_EXPECTED encode the POST-V071 topology
    -- (V071 re-points the readers behind the extract/reconcile that feed them).
    -- Only the two ROOTS carry a SCHEDULE (their own cron, verbatim from V002's
    -- CREATE TASK); every AFTER-chained task has no schedule of its own, so its
    -- SCHEDULE_EXPECTED stays NULL (skip). PREDECESSOR_EXPECTED is the bare name
    -- of the single expected parent; the diff below matches it EXACTLY (array of
    -- size 1 whose unqualified element = this), not as a substring — so a lost
    -- chain, an extra parent, or a SCAN-vs-SCAN_DAILY prefix collision all DRIFT.
    SELECT * FROM VALUES
      -- (NAME,                          STATE_EXPECTED, WAREHOUSE_EXPECTED, SCHEDULE_EXPECTED, PREDECESSOR_EXPECTED)
      -- --- HOURLY graph (root TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> readers) ---
      ('TASK_LOAD_HOURLY',               'started', 'WH_ALFA_ADMIN', 'USING CRON 7 * * * * America/Chicago', NULL),
      ('TASK_QH_EXTRACT',                'started', 'WH_ALFA_ADMIN', NULL, 'TASK_LOAD_HOURLY'),
      ('TASK_REFRESH_EXEC_BOARD',        'started', 'WH_ALFA_ADMIN', NULL, 'TASK_QH_EXTRACT'),
      ('TASK_ALERT_SCAN',                'started', 'WH_ALFA_ADMIN', NULL, 'TASK_QH_EXTRACT'),
      ('TASK_ALERT_NOTIFY',              'started', 'WH_ALFA_ADMIN', NULL, 'TASK_ALERT_SCAN'),
      ('TASK_LOAD_MARTS_V27_HOURLY',     'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_OPS_DIAG_HOURLY',           'started', 'WH_ALFA_ADMIN', NULL, NULL),
      -- --- DAILY graph (root TASK_LOAD_DAILY -> TASK_NIGHTLY_RECONCILE -> readers) ---
      ('TASK_LOAD_DAILY',                'started', 'WH_ALFA_ADMIN', 'USING CRON 45 6 * * * America/Chicago', NULL),
      ('TASK_NIGHTLY_RECONCILE',         'started', 'WH_ALFA_ADMIN', NULL, 'TASK_LOAD_DAILY'),
      ('TASK_LOAD_MARTS_V27_DAILY',      'started', 'WH_ALFA_ADMIN', NULL, 'TASK_NIGHTLY_RECONCILE'),
      ('TASK_PLATFORM_SCORE_DAILY',      'started', 'WH_ALFA_ADMIN', NULL, 'TASK_NIGHTLY_RECONCILE'),
      ('TASK_ALERT_SCAN_DAILY',          'started', 'WH_ALFA_ADMIN', NULL, 'TASK_NIGHTLY_RECONCILE'),
      -- --- other scheduled/standalone tasks (topology not yet pinned) ---
      ('TASK_LOAD_OBJECT_COST',          'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_LOAD_STORAGE_TRUTH',        'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_LOCK_WAIT_DAILY',           'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_PATTERN_COST_DAILY',        'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_SNAPSHOT_FRESHNESS',        'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_DAILY_DIGEST',              'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_ANOMALY_SWEEP',             'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_CANARY_SENTINEL',           'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_CHANGE_ATTRIBUTION',        'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_CHANGE_IMPACT_SCAN',        'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_WAREHOUSE_CHANGE_SCAN',     'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_INCIDENT_AUTODECLARE',      'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_LEDGER_AUTOBOOK',           'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_VERIFY_SAVINGS',            'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_BACKUP_OPERATOR',           'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_PURGE_FACTS',               'started', 'WH_ALFA_ADMIN', NULL, NULL),
      ('TASK_PURGE_QUERY_TELEMETRY',     'started', 'WH_ALFA_ADMIN', NULL, NULL)
    AS t(NAME, STATE_EXPECTED, WAREHOUSE_EXPECTED, SCHEDULE_EXPECTED, PREDECESSOR_EXPECTED)
)
SELECT
    COALESCE(l.NAME, e.NAME) AS TASK,
    CASE
        WHEN e.NAME IS NULL
            THEN 'DRIFT: live task not in expected set (new/renamed — add it, or it is stale)'
        WHEN l.NAME IS NULL
            THEN 'DRIFT: expected task not deployed (run its migration / resume it)'
        WHEN e.STATE_EXPECTED IS NOT NULL AND l.STATE <> e.STATE_EXPECTED
            THEN 'DRIFT: state ' || l.STATE || ' <> expected ' || e.STATE_EXPECTED
        WHEN e.WAREHOUSE_EXPECTED IS NOT NULL AND COALESCE(l.WAREHOUSE, '') <> e.WAREHOUSE_EXPECTED
            THEN 'DRIFT: warehouse ' || COALESCE(l.WAREHOUSE, '(serverless)') || ' <> expected ' || e.WAREHOUSE_EXPECTED
        WHEN e.SCHEDULE_EXPECTED IS NOT NULL AND COALESCE(l.SCHEDULE, '') <> e.SCHEDULE_EXPECTED
            THEN 'DRIFT: schedule ' || COALESCE(l.SCHEDULE, '(none)') || ' <> expected ' || e.SCHEDULE_EXPECTED
        -- EXACT predecessor match: the live task must have EXACTLY ONE parent and
        -- its unqualified name must equal PREDECESSOR_EXPECTED. This replaces the
        -- old '%name%' LIKE, which (a) missed EXTRA parents and (b) false-matched
        -- on prefixes (TASK_ALERT_SCAN vs TASK_ALERT_SCAN_DAILY). COALESCE guards
        -- the empty/absent-array case so "no parent where one is expected" DRIFTs.
        WHEN e.PREDECESSOR_EXPECTED IS NOT NULL
             AND ( COALESCE(ARRAY_SIZE(l.PRED_JSON), 0) <> 1
                   OR SPLIT_PART(COALESCE(GET(l.PRED_JSON, 0)::STRING, ''), '.', -1)
                      <> e.PREDECESSOR_EXPECTED )
            THEN 'DRIFT: predecessors ' || COALESCE(l.PREDECESSORS::STRING, '(none)')
                 || ' <> expected exactly [' || e.PREDECESSOR_EXPECTED || ']'
        ELSE 'OK'
    END AS VERDICT,
    l.STATE, l.WAREHOUSE, l.SCHEDULE, l.PREDECESSORS
FROM live l
FULL OUTER JOIN expected e ON l.NAME = e.NAME
ORDER BY IFF(VERDICT = 'OK', 1, 0), TASK;
