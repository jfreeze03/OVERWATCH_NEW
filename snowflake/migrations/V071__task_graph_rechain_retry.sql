-- V071__task_graph_rechain_retry.sql
--
-- DAG surgery on already-applied tasks (verified against the live graph, Codex #3/#4/#43/#42).
-- Snowflake runs sibling child tasks in PARALLEL; the graph was grown by hanging readers off
-- the ROOT loaders instead of chaining them behind the extract/reconcile that FEED them, so
-- readers raced their own data. V071 re-points them and hardens the two roots. It only
-- ADD/REMOVE AFTER + SETs retry params -- it NEVER re-defines a task body (no CREATE OR
-- REPLACE TASK), so every existing schedule/warehouse/body is preserved.
--
--   #3  TASK_REFRESH_EXEC_BOARD + TASK_ALERT_SCAN: AFTER TASK_LOAD_HOURLY -> AFTER
--       TASK_QH_EXTRACT (they read the query facts the extract refreshes). NOTIFY trails SCAN.
--   #4  TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY + TASK_ALERT_SCAN_DAILY:
--       AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE (they read reconciled facts,
--       not the ones reconcile is mid-reload on). Reconcile atomicity (#7) stays deferred.
--   #43 additive retry/auto-suspend policy on both roots (TASK_AUTO_RETRY_ATTEMPTS = 1,
--       SUSPEND_TASK_AFTER_NUM_FAILURES = 10), set inside the surgery window.
--   #42 SCHEMA_VERSION.DESCRIPTION widened to VARCHAR(4000) at the very top, before the guard.
--
-- IDEMPOTENT + SAFE TO RE-RUN, via a STATE CHECK (not an error-string guess): each re-point
-- snapshots the task's predecessors from SHOW TASKS and issues ADD only if the new predecessor
-- is absent + REMOVE only if the old one is present, ADD BEFORE REMOVE. A matched re-run
-- issues neither ALTER, and there is NO exception handler, so any genuine ALTER failure (busy
-- graph / privilege) propagates loudly and ABORTS before the VERSION 71 insert. On abort the
-- graph's trailing RESUME/TASK_DEPENDENTS_ENABLE does not run, so the graph is left SUSPENDED
-- -- a loud outage that self-heals on re-run, deliberately chosen over silently orphaning a
-- task (ADD-before-REMOVE also guarantees a failed re-point keeps its original predecessor,
-- never zero). SUSPEND/RESUME are IF EXISTS (idempotent) + SYSTEM$TASK_DEPENDENTS_ENABLE(root)
-- so a clean run leaves no dependent suspended. #5 finalizer DEFERRED (see gen_v071.py header +
-- the owner smoke test in DEPLOYMENT.md). Apply AFTER V070.
--
-- OWNER SMOKE TEST (read-only, REQUIRED -- the re-point + resume are runtime-only; a
-- byte-compare cannot prove the graph resumed, and a genuine abort leaves it suspended):
--   SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
--   -- TASK_REFRESH_EXEC_BOARD.predecessors + TASK_ALERT_SCAN.predecessors = TASK_QH_EXTRACT
--   -- TASK_LOAD_MARTS_V27_DAILY / TASK_PLATFORM_SCORE_DAILY / TASK_ALERT_SCAN_DAILY
--   --   .predecessors = TASK_NIGHTLY_RECONCILE ; and state = 'started' for ALL 12 tasks.
--   -- If any task is `suspended`, re-run V071 (idempotent) or SYSTEM$TASK_DEPENDENTS_ENABLE
--   -- both roots.

-- #42 (very top, before the version guard): widen SCHEMA_VERSION.DESCRIPTION unconditionally,
-- so any install advancing the chain gets the widen even if the manual preflight was skipped.
-- GROW-ONLY: widening a VARCHAR to an equal/greater length is always allowed and idempotent;
-- this is safe to re-run only because it never SHRINKS (were a later migration to widen
-- DESCRIPTION beyond 4000, re-running V071 would be a rejected shrink -- keep this <= any
-- later width).
ALTER TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
    ALTER COLUMN DESCRIPTION SET DATA TYPE VARCHAR(4000);

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20071, 'V071 requires V070 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 70) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- ===========================================================================
-- HOURLY graph re-chain (#3). TASK_REFRESH_EXEC_BOARD + TASK_ALERT_SCAN read the query
-- facts TASK_QH_EXTRACT refreshes, but were hung AFTER the root as QH_EXTRACT's SIBLINGS
-- (V003 / V004, pre-dating the V041 extract), so Snowflake ran them in PARALLEL with the
-- extract and they raced their own data. Re-point both to run AFTER TASK_QH_EXTRACT.
-- TASK_ALERT_NOTIFY stays AFTER TASK_ALERT_SCAN and simply moves with its parent.
-- ===========================================================================
-- Suspend the whole hourly graph for the surgery (SUSPEND on a suspended task is a no-op).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_OPS_DIAG_HOURLY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY SUSPEND;

-- #43: additive retry + auto-suspend policy on the HOURLY root (set while suspended).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY
    SET TASK_AUTO_RETRY_ATTEMPTS = 1, SUSPEND_TASK_AFTER_NUM_FAILURES = 10;

-- Re-point TASK_REFRESH_EXEC_BOARD: AFTER TASK_LOAD_HOURLY -> AFTER TASK_QH_EXTRACT. STATE-CHECKED, ADD-before-REMOVE, no
-- error swallowing: ADD only if TASK_QH_EXTRACT is absent, REMOVE only if TASK_LOAD_HOURLY is present, so
-- a matched re-run is a no-op and any genuine ALTER failure aborts loudly (see header).
EXECUTE IMMEDIATE
$$
DECLARE
    preds VARCHAR;
BEGIN
    SHOW TASKS LIKE 'TASK_REFRESH_EXEC_BOARD' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COALESCE(MAX(TO_VARCHAR("predecessors")), '') INTO :preds
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_REFRESH_EXEC_BOARD';
    -- ADD the new predecessor first (transient two-parent state is inert while the
    -- graph is suspended); if this fails it aborts before the REMOVE below.
    IF (preds NOT ILIKE '%TASK_QH_EXTRACT%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD ADD AFTER DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT;
    END IF;
    -- then drop the old predecessor, only if it is still attached.
    IF (preds ILIKE '%TASK_LOAD_HOURLY%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY;
    END IF;
END;
$$;

-- Re-point TASK_ALERT_SCAN: AFTER TASK_LOAD_HOURLY -> AFTER TASK_QH_EXTRACT. STATE-CHECKED, ADD-before-REMOVE, no
-- error swallowing: ADD only if TASK_QH_EXTRACT is absent, REMOVE only if TASK_LOAD_HOURLY is present, so
-- a matched re-run is a no-op and any genuine ALTER failure aborts loudly (see header).
EXECUTE IMMEDIATE
$$
DECLARE
    preds VARCHAR;
BEGIN
    SHOW TASKS LIKE 'TASK_ALERT_SCAN' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COALESCE(MAX(TO_VARCHAR("predecessors")), '') INTO :preds
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_ALERT_SCAN';
    -- ADD the new predecessor first (transient two-parent state is inert while the
    -- graph is suspended); if this fails it aborts before the REMOVE below.
    IF (preds NOT ILIKE '%TASK_QH_EXTRACT%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN ADD AFTER DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT;
    END IF;
    -- then drop the old predecessor, only if it is still attached.
    IF (preds ILIKE '%TASK_LOAD_HOURLY%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY;
    END IF;
END;
$$;

-- Resume the whole hourly tree (children first, root last, then whole-tree enable).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_EXEC_BOARD RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_OPS_DIAG_HOURLY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');

-- ===========================================================================
-- DAILY graph re-chain (#4). TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY +
-- TASK_ALERT_SCAN_DAILY read facts TASK_NIGHTLY_RECONCILE deletes+reloads for D-2/D-3, but
-- were hung AFTER the root as reconcile's SIBLINGS (V027 / V041 / V062), so they ran in
-- PARALLEL with the reconcile and could read half-reloaded facts. Re-point all three to run
-- AFTER TASK_NIGHTLY_RECONCILE. (Reconcile is not itself atomic -- Codex #7, deferred; its
-- child loaders own their transactions/DDL -- but serializing the readers AFTER it is
-- strictly better than racing it, and safe. V071 does not touch reconcile's body.)
-- ===========================================================================
-- Suspend the whole daily graph for the surgery.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_DAILY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_PLATFORM_SCORE_DAILY SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN_DAILY SUSPEND;

-- #43: additive retry + auto-suspend policy on the DAILY root (set while suspended).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY
    SET TASK_AUTO_RETRY_ATTEMPTS = 1, SUSPEND_TASK_AFTER_NUM_FAILURES = 10;

-- Re-point TASK_LOAD_MARTS_V27_DAILY: AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE. STATE-CHECKED, ADD-before-REMOVE, no
-- error swallowing: ADD only if TASK_NIGHTLY_RECONCILE is absent, REMOVE only if TASK_LOAD_DAILY is present, so
-- a matched re-run is a no-op and any genuine ALTER failure aborts loudly (see header).
EXECUTE IMMEDIATE
$$
DECLARE
    preds VARCHAR;
BEGIN
    SHOW TASKS LIKE 'TASK_LOAD_MARTS_V27_DAILY' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COALESCE(MAX(TO_VARCHAR("predecessors")), '') INTO :preds
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_LOAD_MARTS_V27_DAILY';
    -- ADD the new predecessor first (transient two-parent state is inert while the
    -- graph is suspended); if this fails it aborts before the REMOVE below.
    IF (preds NOT ILIKE '%TASK_NIGHTLY_RECONCILE%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_DAILY ADD AFTER DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE;
    END IF;
    -- then drop the old predecessor, only if it is still attached.
    IF (preds ILIKE '%TASK_LOAD_DAILY%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_DAILY REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY;
    END IF;
END;
$$;

-- Re-point TASK_PLATFORM_SCORE_DAILY: AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE. STATE-CHECKED, ADD-before-REMOVE, no
-- error swallowing: ADD only if TASK_NIGHTLY_RECONCILE is absent, REMOVE only if TASK_LOAD_DAILY is present, so
-- a matched re-run is a no-op and any genuine ALTER failure aborts loudly (see header).
EXECUTE IMMEDIATE
$$
DECLARE
    preds VARCHAR;
BEGIN
    SHOW TASKS LIKE 'TASK_PLATFORM_SCORE_DAILY' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COALESCE(MAX(TO_VARCHAR("predecessors")), '') INTO :preds
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_PLATFORM_SCORE_DAILY';
    -- ADD the new predecessor first (transient two-parent state is inert while the
    -- graph is suspended); if this fails it aborts before the REMOVE below.
    IF (preds NOT ILIKE '%TASK_NIGHTLY_RECONCILE%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_PLATFORM_SCORE_DAILY ADD AFTER DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE;
    END IF;
    -- then drop the old predecessor, only if it is still attached.
    IF (preds ILIKE '%TASK_LOAD_DAILY%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_PLATFORM_SCORE_DAILY REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY;
    END IF;
END;
$$;

-- Re-point TASK_ALERT_SCAN_DAILY: AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE. STATE-CHECKED, ADD-before-REMOVE, no
-- error swallowing: ADD only if TASK_NIGHTLY_RECONCILE is absent, REMOVE only if TASK_LOAD_DAILY is present, so
-- a matched re-run is a no-op and any genuine ALTER failure aborts loudly (see header).
EXECUTE IMMEDIATE
$$
DECLARE
    preds VARCHAR;
BEGIN
    SHOW TASKS LIKE 'TASK_ALERT_SCAN_DAILY' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COALESCE(MAX(TO_VARCHAR("predecessors")), '') INTO :preds
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_ALERT_SCAN_DAILY';
    -- ADD the new predecessor first (transient two-parent state is inert while the
    -- graph is suspended); if this fails it aborts before the REMOVE below.
    IF (preds NOT ILIKE '%TASK_NIGHTLY_RECONCILE%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN_DAILY ADD AFTER DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE;
    END IF;
    -- then drop the old predecessor, only if it is still attached.
    IF (preds ILIKE '%TASK_LOAD_DAILY%') THEN
        ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN_DAILY REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY;
    END IF;
END;
$$;

-- Resume the whole daily tree (children first, root last, then whole-tree enable).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN_DAILY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_PLATFORM_SCORE_DAILY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_DAILY RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_NIGHTLY_RECONCILE RESUME;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 71 AS VERSION,
       'Task-graph re-chain + root retry policy (DAG surgery on applied tasks): Snowflake runs sibling child tasks in parallel, and readers were hung off the roots as siblings of the extract/reconcile that feed them, so they raced their own data. TASK_REFRESH_EXEC_BOARD and TASK_ALERT_SCAN re-pointed AFTER TASK_LOAD_HOURLY -> AFTER TASK_QH_EXTRACT so they read the refreshed query facts (#3); TASK_LOAD_MARTS_V27_DAILY, TASK_PLATFORM_SCORE_DAILY and TASK_ALERT_SCAN_DAILY re-pointed AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE so they read reconciled data rather than racing the delete+reload (#4; reconcile atomicity #7 stays deferred). Both roots gain TASK_AUTO_RETRY_ATTEMPTS=1 + SUSPEND_TASK_AFTER_NUM_FAILURES=10 (#43). SCHEMA_VERSION.DESCRIPTION widened to VARCHAR(4000) at the top before the guard (#42). Only ADD/REMOVE AFTER + SET retry params -- no task body re-defined, so schedules/warehouses/bodies are preserved; each re-point is state-checked (ADD only if the new predecessor is absent, REMOVE only if the old is present, ADD before REMOVE) with no error swallowing, so a matched re-run is a no-op and any genuine ALTER failure aborts loudly leaving the graph suspended rather than silently orphaned, and a clean run ends each graph with SYSTEM$TASK_DEPENDENTS_ENABLE. Green-on-failure finalizer (#5) deferred. No new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 71);
