-- =====================================================================================
--  OVERWATCH -- REMAINING_OWNER_REPAIRS_2026-10-09.sql  (owner-run; AFTER REPAIR_COCO_AI_FACT_2026-10-09.sql)
--  Every block of OWNER_REPAIRS_V166_V172.sql that is still correct and useful on 2026-10-09, in a safe order.
--  Writes happen only inside the source's own guarded, bounded blocks; every optional write stays commented out.
--
--  WHY: the owner ran NONE of OWNER_REPAIRS_V166_V172.sql (the ordered heals and worklists written 2026-10-01 for
--  the V162-V172 apply). The 2026-10-09 probe proved it for V167 step 1: FACT_AI_USAGE_DAILY is still
--  UTC-day-keyed for days before ~2026-09-29, although the daily task ran every day. That step (PART 1.1) is
--  staged on its own in REPAIR_COCO_AI_FACT_2026-10-09.sql, which the owner RAN on 2026-10-09 (read its R1 / R2
--  before starting here: every month DRIFT 0, GAP_DAYS 0). This file carries the rest. Each staged block is copied VERBATIM from OWNER_REPAIRS_V166_V172.sql: its banner names the
--  source lines, and the SQL inside is never edited. Where a comment inside a copied block is out of date a week
--  later, a "2026-10-09 NOTE" ABOVE that block's banner corrects it. A PRE-CHECK marked "new" was written for this
--  file; it is read-only and says whether the block is still needed.
--  STATE ASSUMED: V162-V172 applied ~2026-10-01/02; V173 applied 2026-10-04; V174 staged (applied: unconfirmed);
--  V175 staged, not applied. Nothing here needs V174 or V175. Since V172, only V173 / V174 (SP_ALERT_SCAN,
--  SP_ALERT_SCAN_DAILY, one ALERT_CONFIG rule NAME) and V175 (the new SP_ADMIN_ROLE_MEMBERS) changed anything: no
--  block below CALLs a procedure they changed, and every arm and sweep the alert worklists rely on is unchanged
--  (the PART B V168.2 / V169.2 has / lacks needles all hold in the V173 and V174 bodies).
--
--  HOW TO RUN
--   * As SNOW_ACCOUNTADMINS, after REPAIR_COCO_AI_FACT_2026-10-09.sql (it ends with UNSET TIMEZONE; this file pins
--     Central again). In every new worksheet, run the TOP block (pin, role, schema, guard) before any later block.
--   * ONE BLOCK AT A TIME; read each pane before the next. Never two heavy blocks at once, and not while
--     RUN_NEXT.sql (V174 / V175) runs. HEAVY: [A2] R166.2, [A5] R166.5, [B2] step 2, [B3] step 3, [B4] step 4.
--   * [A2] and [B3] SUSPEND the hourly graph (TASK_LOAD_HOURLY). ALWAYS run their RESUME lines, whatever the pane
--     says: until they run there are no hourly loads, no alert scan and no Teams delivery. A timeout or a Stop is
--     never caught: then run that block's RESUME lines by hand (each block carries them).
--   * When a PRE-CHECK says a block is not needed, skip the block ([A2] / [B3]: with its SUSPEND / RESUME lines).
--   * Sections C-G (alerts, incidents, detection) are read-only grids plus Alerts / Control Room UI work. They do
--     not depend on A or B: run them in any sitting, off-peak.
--   * OFF-PEAK and WAREHOUSE / TIMEOUT, from the source header (lines 28-48, verbatim). Both still hold on
--     2026-10-09: no task schedule and no loader changed after V172.
--  OFF-PEAK = outside the 06:30-07:30 Central nightly chain (06:30 storage truth, 06:40 warehouse change scan and
--  marts chain, 06:45 DAILY marts / pattern cost / object cost, 06:50 change impact, 06:55 app cost, 07:00 daily
--  scan and anomaly sweep, 07:20 digest) and away from the :07 hourly graph start. The V167 step-4 rebuild also
--  avoids the arm [6] :07 slots (00/04/08/12/16/20 Central). R166.2 and V167 step 3 SUSPEND TASK_LOAD_HOURLY:
--  start each well clear of :07 (for example :20-:50) and ALWAYS run its RESUME lines (no hourly loads, alert
--  scan or Teams delivery until they run).
--
--  WAREHOUSE / TIMEOUT (owner question, still open): the heavy blocks -- R166.2 SP_LOAD_SECURITY_FACTS(180),
--  R166.5 SP_LOAD_APP_COST(30+), V167 step 1 SP_LOAD_MARTS_V27('DAILY', 365), step 2 SP_LOAD_PATTERN_COST(364),
--  step 3 SP_LOAD_QH_EXTRACT(90) + SP_LOAD_MARTS_V27('HOURLY', 90), step 4 the 364-day MART_TASK_GRAPH_DAILY
--  rebuild -- run on this worksheet's warehouse. WH_ALFA_ADMIN's STATEMENT_TIMEOUT_IN_SECONDS is 300 s per V002
--  (the 2026-09-29 live probe saw 1800 s). A session-level ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS cannot
--  lift a lower warehouse-level value (the lower non-zero one wins), so pick a warehouse whose own timeout allows
--  these, for example:
--    -- USE WAREHOUSE <a warehouse with a long enough STATEMENT_TIMEOUT_IN_SECONDS>;
--    -- SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE <that warehouse>;   -- read-only check
--  A timeout is safe to re-run: R166.4, R166.5, step 2 and step 4 are one transaction (rolled back); step 1 and
--  step 3 reload with MERGEs or transactional DELETE + INSERTs that roll back on failure (SP_LOAD_QH_EXTRACT and
--  arm [8] DELETE + INSERT inside a transaction), so re-run them; step 1 prunes only after a successful reload.
--  R166.2 is the exception: its d>3 arm commits each DELETE before its INSERT, so re-run it until it reads ok
--  (its header says so).
--     2026-10-09: "V167 step 1" above now lives in REPAIR_COCO_AI_FACT_2026-10-09.sql. R0 below shows this
--     worksheet's two limits; if that file's step 1 finished on your warehouse, use the same warehouse for A and B.
--   * CHANGE RISK: [B3]'s SP_LOAD_MARTS_V27('HOURLY', 90) runs CREATE OR REPLACE TEMPORARY TABLE _OW_ALLOC_BASE
--     inside the proc (as step 1's DAILY reload runs _OW_WH_MONITOR). Security > Changes lists it as one MEDIUM
--     DESTRUCTIVE row under your user: a session scratch table, not a change.
--
--  ORDER
--    0. REPAIR_COCO_AI_FACT_2026-10-09.sql (V167 step 1, PART 1.1): DONE 2026-10-09 (confirm its R1 / R2 first).
--    1. TOP     the session pin, role, schema and -20990 guard (source lines 51-68), then R0 / R0b / R0c (read-only).
--    2. A V166  [A0] pin -> [A1] R166.1 (pre-check) -> [A2] R166.2 + its RESUME pair (HEAVY; suspends the hourly
--               graph) -> [A3] R166.3 grids (pre-check) -> [A4] R166.4 (cheap; skips itself) -> [A5] R166.5
--               (HEAVY) -> [A6] R166.6 (OPTIONAL; pointers only).
--    3. B V167  [B0] pin -> [B1] step 0 probes (pre-check; set step 3's N) -> [B2] step 2 (HEAVY) -> [B3] step 3
--               + its RESUME (HEAVY; suspends the hourly graph) -> [B4] step 4 (HEAVY; after [B3]; outside the
--               00/04/08/12/16/20 :07 Central slots).
--    4. C V168  [C0] pin -> [C1] R168.1 (OPTIONAL) -> [C2] R168.2 (Alerts UI worklist).
--    5. D V169  [D0] pin -> [D1] Alerts UI worklists (P169.1, P169.2, P169.3, P169.7) -> [D2] R169.1 (OPTIONAL).
--    6. E V170  [E0] pin -> [E1] R170.1 (Control Room worklist) -> [E2] its SQL alternative (OPTIONAL) ->
--               [E3] R170.2 (Control Room worklist) -> [E4] R170.3 (OPTIONAL; default no repair).
--    7. F V171  [F0] banner only: nothing to run.
--    8. G V172  [G0] pin -> [G1] R172.1 -> [G2] R172.2 -> [G3] R172.3 -> [G4] its close (OPTIONAL) -> [G5] R172.4.
--    9. FINAL CHECK (read-only) -> ALTER SESSION UNSET TIMEZONE -> the AFTER list.
--
--  SKIPPED (every line of OWNER_REPAIRS_V166_V172.sql is either staged below or listed here)
--    * Lines 1-49 (header) and 70-72 / 139-141 (the PART 1 / PART 2 banners): replaced by this header, which quotes
--      the OFF-PEAK and WAREHOUSE / TIMEOUT text verbatim.
--    * PART 1.1, lines 74-120 (V167 step 1, the AI DAILY 365 reload-then-prune): already staged in
--      REPAIR_COCO_AI_FACT_2026-10-09.sql, which copies lines 51-120 verbatim; run 2026-10-09. Re-running it is
--      safe (it re-keys and prunes only after a successful reload) but not needed once its R1 / R2 read clean.
--    * PART 1.2, lines 122-137 (R172.0, a hand CALL SP_CHANGE_IMPACT_SCAN "right after the apply"): superseded. It
--      only brought the first post-apply scan forward from 06:50; TASK_CHANGE_IMPACT_SCAN (V010: CRON 50 6 * * *
--      America/Chicago) has run the same CALL every morning since V172. A hand CALL now repeats a scan and can
--      raise PERF_CHANGE_REGRESSION events. PART B V172.3 (AFTER list) confirms the scans re-froze the registry.
--    * R168.2, its R2-034 stranded-PERF half (no statement; lines 570-571 are staged in [C2] for the SEC half):
--      already done by the machine. The V168 auto-clear sweep runs on every hourly scan, and V173 / V174 carry it
--      unchanged. PART B V168.4 confirms.
--    * R171.1, lines 648-651 (the commented CALL SP_SCAN_REF_GAPS): superseded. SP_ALERT_SCAN_DAILY's arm [17]
--      (V173__alert_scan_supported_subquery_and_div0.sql line 2337) CALLs SP_SCAN_REF_GAPS every morning, so
--      ETL_REF_GAP_RESULTS already holds V171's output. PART B V171.3 / V171.6 verify. Hand-run it only to debug a
--      ref_gap failure that V171.6 lists, outside 06:30-07:30 Central.
-- =====================================================================================

-- 2026-10-09 NOTE for TOP: the guard's comment expects 'ok: V172 applied ...'. With V173 applied it reads
--   'ok: V173 applied - continue' (V174 or V175 if those are applied). Any of those passes; -20990 means stop.
-- =====================================================================================
--  TOP  the session pin, the role, the schema and the -20990 V172 guard
--  source: OWNER_REPAIRS_V166_V172.sql lines 51-68 (verbatim)
-- =====================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- FIRST: the loaders below key days on the session zone
USE ROLE SNOW_ACCOUNTADMINS;
USE SCHEMA DBA_MAINT_DB.OVERWATCH;

-- GUARD (read-only): refuses until V172 is registered. Expect 'ok: V172 applied ...'.
EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20990, 'OWNER_REPAIRS_V166_V172 needs V172 applied first - finish RUN_NEXT.sql.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 172) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok: V' || v || ' applied - continue';
END;
$$;

-- R0 (read-only; new) this worksheet's statement timeout, at the session and at the warehouse. Edit the warehouse
--    name if your worksheet uses another (REPAIR_COCO_AI_FACT_2026-10-09.sql R0 checks the same one). The lower
--    non-zero value wins; the heavy blocks need a long one.
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION;
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_QUERY;

-- R0b (read-only; new) the migration registry. Expect 166-173, plus 174 / 175 only if applied. The new pre-checks
--     compare against APPLIED_AT of 166, 167, 169 and 170 (Central wall-clock, as RUN_NEXT's pinned session
--     stamped them).
SELECT VERSION, APPLIED_AT, APPLIED_BY
FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
WHERE VERSION BETWEEN 166 AND 175
ORDER BY VERSION;

-- R0c (read-only) PART_B_V166_V172.sql V167.1, verbatim. FACT_AI_USAGE_DAILY COVERAGE_FROM about today - 364 =
--     REPAIR_COCO_AI_FACT_2026-10-09.sql's reload ran; a date around the V167 apply = run that file first. The
--     MART_PATTERN_COST_DAILY row is [B2]'s question (its pre-check reads it again).
SELECT SOURCE_NAME, COVERAGE_FROM, STATUS, SNAPSHOT_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('FACT_AI_USAGE_DAILY', 'MART_PATTERN_COST_DAILY')
ORDER BY SOURCE_NAME;

-- #####################################################################################
--  A  V166 (cluster loaders)
-- #####################################################################################

-- =====================================================================================
--  [A0] V166 section opener (the source banner and this section's Central pin)
--  source: OWNER_REPAIRS_V166_V172.sql lines 143-147 (verbatim)
-- =====================================================================================
-- =====================================================================
--  V166 (cluster loaders): R166.1 OPTIONAL probe; R166.2 the 180-day security-fact heal + its RESUME pair;
--  R166.3 gap grids; R166.4 storage-truth heal; R166.5 app-cost heal (OFF-PEAK, heavy); R166.6 OPTIONAL pointers.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- =====================================================================================
--  [A1] R166.1 -- the PRE-CHECK for [A2] (read-only; the source's own probe, marked OPTIONAL there; it reads 180
--       days of QUERY_HISTORY, so it takes a while)
--  DECIDES: a row whose HOUR_TS is more than 3 days old is an R2-007 hole only [A2] heals -> run [A2]. No such row
--           -> skip [A2] and its RESUME pair (hours inside the last 3 days are in the hourly reload's own window).
--  source: OWNER_REPAIRS_V166_V172.sql lines 149-179 (verbatim)
-- =====================================================================================
-- R166.1 OPTIONAL (read-only; 180 days of QUERY_HISTORY): the hours where FACT_SECURITY_CHANGE holds fewer
--        admitted DDL/DCL rows than QUERY_HISTORY (the V166 d<=3 arm admission). Hour grain on purpose: the
--        R2-007 holes are shorter than a day. R166.2 is idempotent, so the probe only tells you it is needed.
WITH qh AS (
    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, COUNT(*) AS QH_ROWS
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
     WHERE START_TIME >= DATEADD('day', -180, CURRENT_DATE())
       AND START_TIME < DATEADD('hour', -3, CURRENT_TIMESTAMP())
       AND EXECUTION_STATUS = 'SUCCESS'
       AND (QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW',
            'CREATE_TABLE_AS_SELECT', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN',
            'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'RENAME',
            'RENAME_TABLE', 'TRUNCATE_TABLE', 'ALTER_USER', 'CREATE_USER',
            'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE')
            OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%'
            OR QUERY_TYPE ILIKE '%ROLE%' OR QUERY_TYPE ILIKE 'DROP%'
            OR QUERY_TYPE ILIKE 'TRUNCATE%')
     GROUP BY 1
),
fact AS (
    SELECT DATE_TRUNC('hour', EVENT_TS) AS HOUR_TS, COUNT(*) AS FACT_ROWS
      FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
     WHERE EVENT_TS >= DATEADD('day', -180, CURRENT_DATE())
     GROUP BY 1
)
SELECT qh.HOUR_TS, qh.QH_ROWS, COALESCE(fact.FACT_ROWS, 0) AS FACT_ROWS,
       qh.QH_ROWS - COALESCE(fact.FACT_ROWS, 0) AS MISSING
  FROM qh
  LEFT JOIN fact ON fact.HOUR_TS = qh.HOUR_TS
 WHERE COALESCE(fact.FACT_ROWS, 0) < qh.QH_ROWS
 ORDER BY qh.HOUR_TS;

-- =====================================================================================
--  [A2] R166.2 -- the R2-007 heal: SP_LOAD_SECURITY_FACTS(180) inside a TASK_LOAD_HOURLY suspend (HEAVY)
--  PRE-CHECK: [A1] above.
--  WHEN: off-peak, starting well clear of :07 (for example :20-:50 Central), on a warehouse whose
--        STATEMENT_TIMEOUT_IN_SECONDS allows a 180-day QUERY_HISTORY scan (R0). Run the EXECUTE IMMEDIATE, then the
--        RESUME pair below it whatever its pane says. Re-run the block until it reads ok: a FAILED or timed-out run
--        can leave FACT_SECURITY_CHANGE / FACT_SECURITY_LOGIN_DAILY holding only ~3 days. A WAIT pane: re-run it
--        in a few minutes.
--  source: OWNER_REPAIRS_V166_V172.sql lines 181-238 (verbatim)
-- =====================================================================================
-- R166.2 the R2-007 heal: the d>3 arm rebuilds 180 days of FACT_SECURITY_CHANGE (and FACT_SECURITY_LOGIN_DAILY)
--        from ACCOUNT_USAGE with the current classifier. The block SUSPENDs TASK_LOAD_HOURLY (its graph runs
--        TASK_LOAD_SECURITY_FACTS after the hourly marts) for the whole CALL, returns WAIT (graph resumed, nothing
--        loaded) while a graph run is still in flight, and RESUMEs the graph on every exit it can catch.
--        RISK: the d>3 arm commits each DELETE before its INSERT, so a CALL that fails or is cancelled part-way
--        leaves FACT_SECURITY_CHANGE / FACT_SECURITY_LOGIN_DAILY holding only the ~3 days the hourly run refills,
--        until this block reads ok. Run it on a warehouse whose STATEMENT_TIMEOUT_IN_SECONDS allows a 180-day
--        QUERY_HISTORY scan (WH_ALFA_ADMIN is 300 s per V002) and re-run it until it does; a FAILED pane says so.
--        expect 'ok: SP_LOAD_SECURITY_FACTS(180) -> security facts loaded 180d'.
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
    running NUMBER DEFAULT 0;
    called BOOLEAN DEFAULT FALSE;
BEGIN
    -- never alongside the hourly graph: the d>3 DELETE + INSERT is not one transaction and the fact key
    -- is not enforced, so an overlapping TASK_LOAD_SECURITY_FACTS insert doubles rows. Suspend the root
    -- for the whole CALL (no new graph run starts), then refuse while a run already in flight is not done.
    ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;
    SELECT COUNT(*) INTO :running
      FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.CURRENT_TASK_GRAPHS(ROOT_TASK_NAME => 'TASK_LOAD_HOURLY'))
     WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH' AND STATE = 'EXECUTING';
    IF (running > 0) THEN
        ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
        SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
        RETURN 'WAIT: an hourly graph run (TASK_LOAD_HOURLY) is still in flight; nothing was loaded and the graph is resumed. Re-run this block in a few minutes';
    END IF;
    called := TRUE;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(180);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
    SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
    IF (rv IS NULL OR NOT (rv = 'security facts loaded 180d')) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'V166 ' || 'SP_LOAD_SECURITY_FACTS(180)', CURRENT_ROLE();
        RETURN 'FAILED: ' || 'SP_LOAD_SECURITY_FACTS(180)' || ' -> ' || COALESCE(rv, 'no verdict returned') || ' -- FACT_SECURITY_CHANGE / FACT_SECURITY_LOGIN_DAILY may now hold only the ~3 days the hourly run refills (the d>3 arm commits each DELETE before its INSERT); re-run this block, on a warehouse whose STATEMENT_TIMEOUT_IN_SECONDS allows a 180-day QUERY_HISTORY scan, until it reads ok';
    END IF;
    RETURN 'ok: ' || 'SP_LOAD_SECURITY_FACTS(180)' || ' -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_call_failed', LEFT(:emsg, 2000), 'V166 ' || 'SP_LOAD_SECURITY_FACTS(180)', CURRENT_ROLE();
        ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
        SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');
        IF (called) THEN
            RETURN 'FAILED: ' || 'SP_LOAD_SECURITY_FACTS(180)' || ' - ' || emsg || ' -- FACT_SECURITY_CHANGE / FACT_SECURITY_LOGIN_DAILY may now hold only the ~3 days the hourly run refills (the d>3 arm commits each DELETE before its INSERT); re-run this block, on a warehouse whose STATEMENT_TIMEOUT_IN_SECONDS allows a 180-day QUERY_HISTORY scan, until it reads ok';
        END IF;
        RETURN 'FAILED: ' || 'SP_LOAD_SECURITY_FACTS(180)' || ' - ' || emsg;
END;
$$;

-- R166.2 (continued) ALWAYS run these two right after the block, whatever its pane says (idempotent). A timeout or
--        a Stop ends the block before its own RESUME, and until these run the WHOLE hourly graph stays suspended:
--        no hourly loads, no alert scan, no Teams delivery (the V041 stranding class).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');

-- POST-CHECK [A2] (read-only; new): TASK_LOAD_HOURLY and the tasks of its graph read state 'started' (other tasks
--   keep whatever state they had before).
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
-- AFTER [A2]: re-run [A1] (expect no row more than 3 days old). Then the POST-RESUME CHECK below, and
--   PART_B_V166_V172.sql V168.3 for the scan's health (14/14).
-- POST-RESUME CHECK (read-only; review 2026-10-09): run it after the next :07 Central hourly run that follows the
--   RESUME. Both LAST_LOAD_TS values must be LATER than the time you ran the RESUME: that proves the hourly graph
--   runs again. (PART_B V168.3 alone does not prove it: it reads the heartbeat STATUS text, which the last scan
--   before the SUSPEND already wrote.) An older stamp -> run the RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE lines again.
SELECT SOURCE_NAME, STATUS, LAST_LOAD_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('ALERT_SCAN_HOURLY', 'OW_QH_EXTRACT')
ORDER BY SOURCE_NAME;

-- =====================================================================================
--  [A3] R166.3 -- the R2-011 gap grids: the PRE-CHECK for [A4] and [A5] (read-only)
--  DECIDES: a FACT_STORAGE_ACCOUNT_DAILY row = a gap [A4] heals (it re-reads the depth itself and SKIPs when there
--           is none). A FACT_APP_COST_DAILY row = a gap [A5] reaches (its n goes past 30 when the gap is older).
--           The second grid is context: the failed runs that predict them.
--  source: OWNER_REPAIRS_V166_V172.sql lines 240-266 (verbatim)
-- =====================================================================================
-- R166.3 R2-011: calendar gaps a failed run already left (the same grid as PREFLIGHT P166.2), and the failed
--        runs that predict them. R166.4 / R166.5 read the gap depth themselves.
WITH
    f AS (
        SELECT 'FACT_APP_COST_DAILY' AS FACT, DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY GROUP BY DAY
        UNION ALL
        SELECT 'FACT_STORAGE_ACCOUNT_DAILY', DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY GROUP BY DAY
    ),
    b AS (SELECT FACT, MIN(DAY) AS LO, MAX(DAY) AS HI FROM f GROUP BY FACT),
    n AS (SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS I FROM TABLE(GENERATOR(ROWCOUNT => 401))),
    gaps AS (
        SELECT b.FACT, DATEADD('day', n.I, b.LO) AS MISSING_DAY,
               DATEDIFF('day', DATEADD('day', n.I, b.LO), CURRENT_DATE()) + 1 AS DAYS_BACK_TO_HEAL
        FROM b
        JOIN n ON DATEADD('day', n.I, b.LO) <= b.HI
        LEFT JOIN f ON f.FACT = b.FACT AND f.DAY = DATEADD('day', n.I, b.LO)
        WHERE f.DAY IS NULL
    )
SELECT FACT, MISSING_DAY, DAYS_BACK_TO_HEAL
  FROM gaps
 ORDER BY FACT, MISSING_DAY;

SELECT NAME, SCHEDULED_TIME, STATE, LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
   AND NAME IN ('TASK_LOAD_APP_COST', 'TASK_LOAD_STORAGE_TRUTH') AND STATE = 'FAILED'
 ORDER BY SCHEDULED_TIME;

-- =====================================================================================
--  [A4] R166.4 -- the storage-truth heal (cheap; about one STORAGE_USAGE row a day)
--  PRE-CHECK: [A3] above, and the block's own gap test ('SKIP: ...' = nothing to do).
--  source: OWNER_REPAIRS_V166_V172.sql lines 268-299 (verbatim)
-- =====================================================================================
-- R166.4 storage-truth heal (cheap; about one STORAGE_USAGE row a day): runs only when FACT_STORAGE_ACCOUNT_DAILY
--        has a gap, reaching its oldest gap (at most 360 days). expect 'SKIP: ...' or 'ok: ... -> OK'.
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
    n INT;
BEGIN
    SELECT COALESCE(MAX(DATEDIFF('day', DATEADD('day', 1, d.DAY), CURRENT_DATE()) + 1), 0) INTO :n
      FROM (SELECT DAY, LEAD(DAY) OVER (ORDER BY DAY) AS NEXT_DAY
              FROM (SELECT DISTINCT DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY)) d
     WHERE DATEDIFF('day', d.DAY, d.NEXT_DAY) > 1;
    IF (n = 0) THEN
        RETURN 'SKIP: FACT_STORAGE_ACCOUNT_DAILY has no calendar gap';
    END IF;
    n := LEAST(n, 360);
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_STORAGE_TRUTH(:n);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR NOT (rv = 'OK')) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'V166 ' || 'SP_LOAD_STORAGE_TRUTH(' || :n || ')', CURRENT_ROLE();
        RETURN 'FAILED: ' || 'SP_LOAD_STORAGE_TRUTH(' || n || ')' || ' -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: ' || 'SP_LOAD_STORAGE_TRUTH(' || n || ')' || ' -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_call_failed', LEFT(:emsg, 2000), 'V166 ' || 'SP_LOAD_STORAGE_TRUTH(' || :n || ')', CURRENT_ROLE();
        RETURN 'FAILED: ' || 'SP_LOAD_STORAGE_TRUTH(' || n || ')' || ' - ' || emsg;
END;
$$;
-- AFTER [A4]: re-run [A3] (no FACT_STORAGE_ACCOUNT_DAILY row). PART_B_V166_V172.sql V166.4 (no StorageTruth
--   fact_load_failed row).

-- 2026-10-09 NOTE for [A5]: "at least 30 days so the 30-day session lookback relabels recent days (C10)" still
--   holds, but the 30-day floor now reaches back only to ~2026-09-09 (~2026-09-01 at the apply): days
--   ~2026-09-01..09-08 keep their pre-V166 (7-day lookback) labels. Reaching them too would mean raising the 30 in
--   GREATEST(n, 30) to 38 -- an owner call for a heavier scan; the block below is unchanged.
-- =====================================================================================
--  [A5] R166.5 -- the app-cost heal: ONE SP_LOAD_APP_COST(n) of at least 30 days, one transaction (HEAVY)
--  WHEN: off-peak, away from the 06:55 Central TASK_LOAD_APP_COST. A timeout rolls back and keeps the previous fill;
--        re-run it on a warehouse whose STATEMENT_TIMEOUT_IN_SECONDS allows the join.
--  PRE-CHECK (read-only; new) below. DECIDES: PRE_V166_DAYS > 0, or an [A3] FACT_APP_COST_DAILY gap -> run [A5];
--           neither -> skip it.
--  source: OWNER_REPAIRS_V166_V172.sql lines 301-332 (verbatim)
-- =====================================================================================
-- PRE-CHECK [A5] (read-only; new): days of the last 30 whose FACT_APP_COST_DAILY rows were written before V166, i.e.
--   still labelled with the old 7-day session lookback (C10). The daily task reloads only its last few days, so
--   before [A5] this counts the days from today - 30 to a few days before the V166 apply; after [A5], 0.
SELECT COUNT(DISTINCT f.DAY) AS PRE_V166_DAYS, MIN(f.DAY) AS OLDEST_DAY, MAX(f.DAY) AS NEWEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY f
WHERE f.DAY >= DATEADD('day', -30, CURRENT_DATE())
  AND f.LOAD_TS < (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 166);

-- R166.5 app-cost heal, ONE call, OFF-PEAK (the heavy SESSIONS x QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY join):
--        at least 30 days so the 30-day session lookback relabels recent days (C10), deeper when
--        R166.3 shows an older gap (at most 360 days). Since V166 the reload is one transaction, so a timeout
--        rolls back and keeps the previous fill; re-run it on a warehouse whose STATEMENT_TIMEOUT_IN_SECONDS
--        allows the join. expect 'ok: SP_LOAD_APP_COST(<n>) -> OK'.
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
    n INT;
BEGIN
    SELECT COALESCE(MAX(DATEDIFF('day', DATEADD('day', 1, d.DAY), CURRENT_DATE()) + 1), 0) INTO :n
      FROM (SELECT DAY, LEAD(DAY) OVER (ORDER BY DAY) AS NEXT_DAY
              FROM (SELECT DISTINCT DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY)) d
     WHERE DATEDIFF('day', d.DAY, d.NEXT_DAY) > 1;
    n := LEAST(GREATEST(n, 30), 360);   -- at least 30 days: relabels the C10 sessions too
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(:n);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR NOT (rv = 'OK')) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'V166 ' || 'SP_LOAD_APP_COST(' || :n || ')', CURRENT_ROLE();
        RETURN 'FAILED: ' || 'SP_LOAD_APP_COST(' || n || ')' || ' -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: ' || 'SP_LOAD_APP_COST(' || n || ')' || ' -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_call_failed', LEFT(:emsg, 2000), 'V166 ' || 'SP_LOAD_APP_COST(' || :n || ')', CURRENT_ROLE();
        RETURN 'FAILED: ' || 'SP_LOAD_APP_COST(' || n || ')' || ' - ' || emsg;
END;
$$;
-- AFTER [A5]: re-run its pre-check (PRE_V166_DAYS 0) and [A3] (no FACT_APP_COST_DAILY row). PART_B_V166_V172.sql
--   V166.4 now (no AppCost fact_load_failed row) and V166.3 the next morning (both daily loaders SUCCEEDED).

-- =====================================================================================
--  [A6] OPTIONAL (owner decision) -- R166.6, pointers only (nothing in this file runs them)
--  DECISION: whether to run snowflake/backfill_365.sql's MART_CLOUD_SVC_DAILY INSERT and its commented
--            SP_LOAD_OBJECT_COST(365) block (both still there; off-peak, on a long-timeout warehouse).
--  source: OWNER_REPAIRS_V166_V172.sql lines 334-339 (verbatim; comments only)
-- =====================================================================================
-- R166.6 OPTIONAL heals in snowflake/backfill_365.sql (not V166 SQL; nothing here runs them):
--        * the MART_CLOUD_SVC_DAILY arm (the INSERT right after FACT_QUERY_DAILY) fills the cloud-services
--          statement mart's history from QUERY_HISTORY; on an existing install select and run only that INSERT
--          (idempotent: it writes only days before the mart's first day);
--        * the commented SP_LOAD_OBJECT_COST(365) block after the hourly-graph RESUME reloads a year of the
--          object-cost ledger; run it once, off-peak, away from the 06:45 CT daily task.

-- #####################################################################################
--  B  V167 (cluster marts)
-- #####################################################################################

-- 2026-10-09 NOTE for [B0]: "step 1 ran in PART 1" -- step 1 now lives in REPAIR_COCO_AI_FACT_2026-10-09.sql, run
--   first (R0c shows whether it did). Steps 0, 2, 3 and 4 follow here, in the source order.
-- =====================================================================================
--  [B0] V167 section opener (the source banner and this section's Central pin)
--  source: OWNER_REPAIRS_V166_V172.sql lines 341-346 (verbatim)
-- =====================================================================================
-- =====================================================================
--  V167 (cluster marts): step 0 read-only hole probes (they set step 3's N); step 1 ran in PART 1; step 2 the
--  pattern re-stamp; step 3 the hourly re-stamp inside a TASK_LOAD_HOURLY suspend window; step 4 the atomic
--  364-day task-graph rebuild (after step 3). Heavy: see WAREHOUSE / TIMEOUT at the top.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- =====================================================================================
--  [B1] V167 step 0 -- the R2-018 hole probes: the PRE-CHECK for [B3] and [B4] (read-only)
--  DECIDES: [B3]'s N = max(90, the deepest hole these show; a 0a row implies a hole at DATE(LOGGED_AT) - 3). A 0b
--           or 0d row = a hole [B3] fills; a 0c row = a hole [B4] fills.
--  source: OWNER_REPAIRS_V166_V172.sql lines 348-384 (verbatim)
-- =====================================================================================
-- ---- step 0 (READ-ONLY) R2-018 probes: does the reconcile's DELETE-first window hold holes today? -------------
-- 0a mart_load_failed history for the four wide-edge tables (each implies a hole at DATE(LOGGED_AT) - 3)
SELECT LOGGED_AT, CONTEXT, LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'MartLoader' AND ERROR_TYPE = 'mart_load_failed'
  AND SPLIT_PART(CONTEXT, ' ', 1) IN ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'MART_TASK_GRAPH_DAILY',
                                      'FACT_QUERY_ROLE_HOURLY', 'FACT_QUERY_SCHEMA_HOURLY')
ORDER BY LOGGED_AT DESC;
-- 0b warehouse-efficiency day holes vs metering (last 90 complete days)
SELECT DATE(m.START_TIME) AS DAY, COUNT(DISTINCT m.WAREHOUSE_NAME) AS METERED,
       COUNT(DISTINCT e.WAREHOUSE_NAME) AS IN_MART
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY m
LEFT JOIN DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY e
  ON e.DAY = DATE(m.START_TIME) AND e.WAREHOUSE_NAME = m.WAREHOUSE_NAME
WHERE m.START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND m.START_TIME < CURRENT_DATE()
  AND m.WAREHOUSE_ID > 0 AND m.CREDITS_USED > 0
GROUP BY 1
HAVING COUNT(DISTINCT e.WAREHOUSE_NAME) < COUNT(DISTINCT m.WAREHOUSE_NAME)
ORDER BY 1;
-- 0c task-graph day holes: days with finished TASK_HISTORY runs but no mart row
SELECT DATE(h.QUERY_START_TIME) AS DAY, COUNT(*) AS TASK_RUNS
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
WHERE h.QUERY_START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND h.QUERY_START_TIME < CURRENT_DATE()
  AND h.STATE IN ('SUCCEEDED', 'FAILED')
  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY g WHERE g.DAY = DATE(h.QUERY_START_TIME))
GROUP BY 1
ORDER BY 1;
-- 0d role-hour / schema-hour holes: hours in FACT_QUERY_HOURLY with no role-hour or schema-hour row
SELECT q.HOUR_TS,
       IFF(r.HOUR_TS IS NULL, 'missing', 'ok') AS ROLE_HOURLY,
       IFF(s.HOUR_TS IS NULL, 'missing', 'ok') AS SCHEMA_HOURLY
FROM (SELECT DISTINCT HOUR_TS FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
      WHERE HOUR_TS >= DATEADD('day', -90, CURRENT_DATE())) q
LEFT JOIN (SELECT DISTINCT HOUR_TS FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY) r ON r.HOUR_TS = q.HOUR_TS
LEFT JOIN (SELECT DISTINCT HOUR_TS FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY) s ON s.HOUR_TS = q.HOUR_TS
WHERE r.HOUR_TS IS NULL OR s.HOUR_TS IS NULL
ORDER BY 1;

-- =====================================================================================
--  [B2] V167 step 2 -- PATTERN-RESTAMP: SP_LOAD_PATTERN_COST(364), one atomic transaction (HEAVY)
--  WHEN: off-peak, away from the 06:45 Central TASK_PATTERN_COST_DAILY. A timeout rolls the window back; re-run it.
--  PRE-CHECK (read-only; new) below. DECIDES: STEP_2 = 'NEEDED' -> run [B2]; 'DONE' -> skip it.
--  source: OWNER_REPAIRS_V166_V172.sql lines 386-410 (verbatim)
-- =====================================================================================
-- PRE-CHECK [B2] (read-only; new): how far back an atomic pattern reload has ever reached (COVERAGE_FROM). The daily
--   task reaches only ~3 days, so without [B2] this reads a date around the V167 apply; [B2] stamps today - 364.
SELECT 'MART_PATTERN_COST_DAILY' AS SOURCE_NAME, MAX(s.COVERAGE_FROM) AS COVERAGE_FROM,
       MAX(s.LAST_LOAD_TS) AS LAST_LOAD_TS,
       IFF(MAX(s.COVERAGE_FROM) <= DATEADD('day', -360, CURRENT_DATE()), 'DONE', 'NEEDED') AS STEP_2
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE s
WHERE s.SOURCE_NAME = 'MART_PATTERN_COST_DAILY';

-- ---- step 2 PATTERN-RESTAMP: one atomic full-retention re-stamp of MART_PATTERN_COST_DAILY ------------------
-- Only after V167 (the atomic loader): a bare CALL on the V120 MERGE loader would add a year of twins. 364
-- avoids the partly aged-out D-365. A timeout rolls the whole window back. Stamps COVERAGE_FROM = today - 364,
-- after which the app reads pattern windows up to 365 days.
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(364);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv <> 'OK') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_PATTERN_COST(364)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_PATTERN_COST(364) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_PATTERN_COST(364) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_PATTERN_COST(364)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_PATTERN_COST(364) - ' || emsg;
END;
$$;
-- AFTER [B2]: re-run its pre-check (STEP_2 = DONE). PART_B_V166_V172.sql V167.1 (MART_PATTERN_COST_DAILY
--   COVERAGE_FROM = today - 364) and V167.3 (TWIN_ROWS_LEFT 0).

-- 2026-10-09 NOTE for [B3]: "N = max(90, the deepest hole step 0 found)" still holds, but 90 days now reach back only
--   to ~2026-07-11 (~2026-07-03 at the apply). To re-stamp the same idle days the apply-week run would have, use 98
--   in both places -- the source's own rule ("edit both 90s together") allows it; a larger N reads more
--   QUERY_HISTORY. Idle days older than N keep their pre-V167 (overstated) idle either way.
-- =====================================================================================
--  [B3] V167 step 3 -- R2-015 (+ any R2-018 hole [B1] found): the extract + HOURLY re-stamp inside a
--       TASK_LOAD_HOURLY suspend window (HEAVY)
--  WHEN: off-peak, starting well clear of :07 (for example :20-:50 Central), and early enough that the whole
--        SUSPEND-to-RESUME unit finishes before 06:30 Central (the nightly chain). Select the SUSPEND line, both
--        EXECUTE IMMEDIATE blocks and the RESUME pair, and run them together, in order. If it stops before the
--        RESUME, run the RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE lines by hand.
--  PRE-CHECKS (read-only; new) below, BOTH before the SUSPEND. DECIDES: the company-remap grid must be EMPTY (any row
--           -> STOP, do not run [B3], paste the grid back); then PRE_V167_DAYS > 0, or a [B1] 0b / 0d row -> run
--           [B3]; neither -> skip [B3] with its SUSPEND / RESUME.
--  source: OWNER_REPAIRS_V166_V172.sql lines 412-463 (verbatim)
-- =====================================================================================
-- PRE-CHECK [B3] (read-only; new): MART_WAREHOUSE_EFFICIENCY_DAILY rows of the last 90 days last written before V167,
--   i.e. still carrying the pre-V167 (overstated) IDLE_PCT / IDLE_CREDITS. The hourly and nightly runs re-stamp only
--   the last ~3 days, so before [B3] this counts nearly every day up to a few days before the V167 apply; after
--   [B3], 0 (a stray row = a warehouse-day the reload no longer produces).
SELECT COUNT(*) AS PRE_V167_ROWS, COUNT(DISTINCT e.DAY) AS PRE_V167_DAYS, MIN(e.DAY) AS OLDEST_DAY,
       MAX(e.DAY) AS NEWEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY e
WHERE e.DAY >= DATEADD('day', -90, CURRENT_DATE())
  AND e.LOAD_TS < (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 167);

-- PRE-CHECK [B3] company remap (read-only; review 2026-10-09): MUST be empty before [B3]. [B3]'s
--   SP_LOAD_MARTS_V27('HOURLY', 90) re-MERGEs MART_QUERY_FAMILY_DAILY keyed by (DAY, QUERY_HASH, COMPANY), with
--   COMPANY = today's COMPANY_FOR_WAREHOUSE (V167 arm [2]), and deletes nothing; the nightly reconcile cleans only
--   the last 2 days. So a warehouse whose company mapping changed after its days were loaded would get a SECOND,
--   new-company query-family row beside the old one on every day [B3] reloads. MART_WAREHOUSE_EFFICIENCY_DAILY is
--   keyed by (DAY, WAREHOUSE_NAME) and keeps the company each day was loaded with, so it shows any such warehouse.
--   MISMATCH = TRUE rows sort first. Any MISMATCH = TRUE row -> STOP: do not run [B3]; paste this grid back (the fix
--   is a bounded clean-up of those warehouses' old-company query-family rows, written for the rows listed).
WITH stored AS (
    SELECT e.WAREHOUSE_NAME, e.COMPANY AS STORED_COMPANY,
           MIN(e.DAY) AS FROM_DAY, MAX(e.DAY) AS TO_DAY, COUNT(*) AS DAYS
    FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY e
    WHERE e.DAY >= DATEADD('day', -90, CURRENT_DATE())
      AND e.DAY < DATEADD('day', -2, CURRENT_DATE())
    GROUP BY 1, 2
),
mapped AS (
    SELECT s.WAREHOUSE_NAME, s.STORED_COMPANY, s.FROM_DAY, s.TO_DAY, s.DAYS,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(s.WAREHOUSE_NAME) AS CURRENT_COMPANY
    FROM stored s
)
SELECT m.WAREHOUSE_NAME, m.STORED_COMPANY, m.CURRENT_COMPANY,
       m.STORED_COMPANY IS DISTINCT FROM m.CURRENT_COMPANY AS MISMATCH,
       m.FROM_DAY, m.TO_DAY, m.DAYS
FROM mapped m
ORDER BY MISMATCH DESC, m.WAREHOUSE_NAME, m.FROM_DAY;

-- ---- step 3 R2-015 (+ any R2-018 hole step 0 found): re-stamp idle / active hours ----------------------------
-- Inside a TASK_LOAD_HOURLY suspend window (backfill_365.sql B12), so the minute-7 watermark trim cannot shrink
-- the widened extract mid-run. N = max(90, the deepest hole step 0 found): edit both 90s together. Idle days
-- older than N keep their pre-V167 (overstated) idle.
-- Each CALL block catches its own error (logged to APP_ERROR_LOG, PAGE 'OwnerRepairV167', pane 'FAILED: ...'),
-- so a Run All still reaches the RESUME; a statement timeout or a Stop cannot be caught that way:
-- !! IF THIS STOPS BEFORE THE RESUME, run the RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE lines by hand !!
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(90);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE '%extract committed: false%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_QH_EXTRACT(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_QH_EXTRACT(90) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_QH_EXTRACT(90) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_QH_EXTRACT(90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_QH_EXTRACT(90) - ' || emsg;
END;
$$;
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 90);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR rv ILIKE 'MARTS WITH ERRORS%') THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'SP_LOAD_MARTS_V27(HOURLY, 90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_MARTS_V27(HOURLY, 90) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: SP_LOAD_MARTS_V27(HOURLY, 90) -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_call_failed', LEFT(:emsg, 2000), 'SP_LOAD_MARTS_V27(HOURLY, 90)', CURRENT_ROLE();
        RETURN 'FAILED: SP_LOAD_MARTS_V27(HOURLY, 90) - ' || emsg;
END;
$$;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');

-- POST-CHECK [B3] (read-only; new): TASK_LOAD_HOURLY and the tasks of its graph read state 'started'.
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
-- AFTER [B3]: re-run its pre-checks (PRE_V167_ROWS 0; the remap grid still all MISMATCH = FALSE) and [B1] 0b / 0d
--   (no rows). Then the POST-RESUME CHECK below, and PART_B_V166_V172.sql V168.3 for the scan's health (14/14).
-- POST-RESUME CHECK (read-only; review 2026-10-09): run it after the next :07 Central hourly run that follows the
--   RESUME. Both LAST_LOAD_TS values must be LATER than the time you ran the RESUME: that proves the hourly graph
--   runs again. (PART_B V168.3 alone does not prove it: it reads the heartbeat STATUS text, which the last scan
--   before the SUSPEND already wrote.) An older stamp -> run the RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE lines again.
SELECT SOURCE_NAME, STATUS, LAST_LOAD_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('ALERT_SCAN_HOURLY', 'OW_QH_EXTRACT')
ORDER BY SOURCE_NAME;

-- =====================================================================================
--  [B4] V167 step 4 -- R2-014: the atomic 364-day MART_TASK_GRAPH_DAILY rebuild with the V167 arm [6] (HEAVY;
--       after [B3])
--  WHEN: off-peak, and not overlapping the arm [6] slots at 00:07 / 04:07 / 08:07 / 12:07 / 16:07 / 20:07 Central.
--        Any error or timeout ROLLBACKs and re-raises (the pane shows the error; the mart is unchanged): fix the
--        cause or the warehouse and re-run it.
--  PRE-CHECK (read-only; new) below. DECIDES: PRE_V167_ROWS > 0 -> run [B4]; 0 -> skip it.
--  source: OWNER_REPAIRS_V166_V172.sql lines 465-546 (verbatim)
-- =====================================================================================
-- PRE-CHECK [B4] (read-only; new): MART_TASK_GRAPH_DAILY rows of the last 364 days last written before V167, i.e.
--   built by the pre-V167 arm [6], which could leave a phantom child-named pipeline row (R2-014) that no MERGE
--   deletes. After [B4], 0.
SELECT COUNT(*) AS PRE_V167_ROWS, COUNT(DISTINCT g.DAY) AS PRE_V167_DAYS, MIN(g.DAY) AS OLDEST_DAY,
       MAX(g.DAY) AS NEWEST_DAY
FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY g
WHERE g.DAY >= DATEADD('day', -364, CURRENT_DATE())
  AND g.LOAD_TS < (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 167);

-- ---- step 4 R2-014: rebuild MART_TASK_GRAPH_DAILY for 364 days with the V167 arm [6] -------------------
-- After step 3 (its HOURLY MERGE cannot delete phantom rows). One anonymous block: BEGIN TRANSACTION; DELETE;
-- INSERT (V167's arm [6] source with d = 364); COMMIT -- any error or timeout ROLLBACKs and re-raises, so
-- the mart never sits half-empty. Run outside the :07 Central slots of arm [6] (00/04/08/12/16/20).
EXECUTE IMMEDIATE $$
BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -364, CURRENT_DATE());
    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
        (DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES, TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS)
    SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES, TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS
    FROM (
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
                        WHERE START_TIME >= DATEADD('day', -364 - 1, CURRENT_DATE())
                          AND COALESCE(ROOT_QUERY_ID, QUERY_ID) IN (
                              SELECT QUERY_ID FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                              WHERE QUERY_START_TIME >= DATEADD('day', -364, CURRENT_DATE())
                                AND STATE IN ('SUCCEEDED', 'FAILED')
                          )
                        GROUP BY COALESCE(ROOT_QUERY_ID, QUERY_ID)
                    ) a ON a.ROOT_ID = h.QUERY_ID
                    -- V167 (R2-014): one lead-in day, so a run that began before the window is seen whole
                    -- here and dropped by the DAY filter below, instead of being re-keyed to its first
                    -- in-window child as a phantom pipeline row on the window's first day
                    WHERE h.QUERY_START_TIME >= DATEADD('day', -364 - 1, CURRENT_DATE())
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
                WHERE DAY >= DATEADD('day', -364, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4
    ) s;
    COMMIT;
    RETURN 'ok: MART_TASK_GRAPH_DAILY rebuilt for 364 days';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
-- AFTER [B4]: re-run its pre-check (PRE_V167_ROWS 0) and [B1] 0c (no rows).

-- #####################################################################################
--  C  V168 (cluster alerts) -- light: read-only grids + the Alerts UI
-- #####################################################################################

-- =====================================================================================
--  [C0] V168 section opener (the source banner and this section's Central pin)
--  source: OWNER_REPAIRS_V166_V172.sql lines 548-552 (verbatim)
-- =====================================================================================
-- =====================================================================
--  V168 (cluster alerts): R168.1 is OPTIONAL and fully commented (owner decision: bulk-resolve historic
--  PIPE_COPY_FAILURES carry-over duplicates from PREFLIGHT P168.3). R168.2 needs no statement.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- 2026-10-09 NOTE for [C1]: (1) line 554's "the RUN_NEXT file leads with the timezone pin" is out of date (runbox
--   RUN_NEXT.sql now stages V174 + V175); this file's TOP pin and [C0]'s pin cover it. (2) "that PREFLIGHT P168.3
--   lists": use the pre-check below (P168.3's test, widened to 30 days). (3) The UPDATE's fixed window (RAISED_AT
--   within 14 days, COPY_HISTORY within 16) now starts ~2026-09-25, a week later than at the apply. Carry-overs
--   exist only from before V168 (since V168, arm [14] keys the Central failure day, which the UPDATE's NOT EXISTS
--   always finds), so it now reaches only about their last week: rows the pre-check marks R168_1_REACHES = FALSE
--   stay open unless you resolve them in Alerts or (owner call) widen -14 and -16 together.
-- =====================================================================================
--  [C1] OPTIONAL (owner decision) -- R168.1
--  DECISION: close the still-OPEN / ACK pre-V168 PIPE_COPY_FAILURES carry-over duplicates as SUPERSEDED, or keep
--            them as history (README owner question 13 default: keep unless bulk-resolved).
--  PRE-CHECK (read-only; new) below. DECIDES: rows with R168_1_WOULD_CLOSE = TRUE are what the UPDATE would close;
--           none -> nothing to decide.
--  source: OWNER_REPAIRS_V166_V172.sql lines 554-569 (verbatim; the UPDATE stays commented out)
-- =====================================================================================
-- live PIPE_COPY_FAILURES carry-over duplicates (PREFLIGHT P168.3's test, widened to 30 days).
-- R168_1_WOULD_CLOSE = the rows R168.1's fixed 14-day window closes today; R168_1_REACHES = FALSE = older rows it no longer reaches.
WITH f AS (
    SELECT TABLE_CATALOG_NAME || '.' || TABLE_SCHEMA_NAME || '.' || TABLE_NAME AS FQN,
           TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY, COUNT(*) AS N
    FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
    WHERE LAST_LOAD_TIME >= DATEADD('day', -32, CURRENT_TIMESTAMP())
      AND STATUS IN ('Load failed', 'Partially loaded')
    GROUP BY 1, 2
)
SELECT e.EVENT_ID, e.STATUS, e.SEVERITY, e.RAISED_AT, e.DEDUPE_KEY, e.TITLE,
       e.RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP()) AS R168_1_REACHES,
       e.STATUS IN ('OPEN', 'ACK') AND e.RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP()) AS R168_1_WOULD_CLOSE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN f ON f.FQN = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
           AND f.FAIL_DAY = TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 4))
WHERE e.RULE_ID = 'PIPE_COPY_FAILURES' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND e.RAISED_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())
  AND f.FQN IS NULL
ORDER BY e.RAISED_AT;

-- Run in a Central session (RAISED_AT is Central wall-clock; the RUN_NEXT file leads with the timezone pin).
-- R168.1 OPTIONAL (owner decision): close the still-OPEN / ACK PIPE_COPY_FAILURES carry-over duplicates that
-- PREFLIGHT P168.3 lists (a scan-day key whose day had no failure for that table). SNOOZED rows are left to wake.
-- Uncomment to run; never DELETE, never rewrite DEDUPE_KEY.
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
--    SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
--  WHERE e.RULE_ID = 'PIPE_COPY_FAILURES' AND e.STATUS IN ('OPEN', 'ACK')
--    AND e.RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
--    AND NOT EXISTS (
--        SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY h
--        WHERE h.LAST_LOAD_TIME >= DATEADD('day', -16, CURRENT_TIMESTAMP())
--          AND h.STATUS IN ('Load failed', 'Partially loaded')
--          AND h.TABLE_CATALOG_NAME || '.' || h.TABLE_SCHEMA_NAME || '.' || h.TABLE_NAME
--              = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
--          AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', h.LAST_LOAD_TIME))
--              = TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 4)));
-- AFTER [C1] (only if you ran the UPDATE): re-run its pre-check (no R168_1_WOULD_CLOSE = TRUE row). No PART B id
--   covers R168.1 (PREFLIGHT_V166_V172.sql P168.3 is the original grid).

-- =====================================================================================
--  [C2] R168.2 -- SEC_NEW_ADMIN_NETWORK failures-only events: an Alerts UI worklist (no statement to run)
--  PRE-CHECK (read-only; PREFLIGHT P168.4's second grid, live rows only) below. DECIDES: a live row with
--           SAYS_LOGGED_IN = TRUE (a pre-V168 title claiming a login that never succeeded) -> resolve it in Alerts
--           as EXPECTED or NOISE (wake a SNOOZED one first); none -> nothing to do. A "(0 successful)" row is an
--           honest V168+ title: review it, do not bulk-close it. The PERF half needs nothing (SKIPPED list).
--  source: OWNER_REPAIRS_V166_V172.sql lines 570-571 (verbatim; comments only)
-- =====================================================================================
-- live SEC_NEW_ADMIN_NETWORK events whose 26h login window held no success (PREFLIGHT P168.4 second grid, live rows only).
-- SAYS_LOGGED_IN = TRUE is a pre-V168 title that claims a login that never happened: resolve it in Alerts (EXPECTED or NOISE).
SELECT e.EVENT_ID, e.RAISED_AT, e.STATUS, e.TITLE, e.METRIC_VALUE AS ATTEMPTS,
       COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,
       e.TITLE LIKE '% logged in from new network %' AS SAYS_LOGGED_IN
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
  ON L.USER_NAME = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
 AND COALESCE(L.CLIENT_IP, '(none)') = SPLIT_PART(e.DEDUPE_KEY, '|', 3)
 AND L.EVENT_TIMESTAMP BETWEEN DATEADD('hour', -26, e.RAISED_AT) AND e.RAISED_AT
WHERE e.RULE_ID = 'SEC_NEW_ADMIN_NETWORK' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
GROUP BY e.EVENT_ID, e.RAISED_AT, e.STATUS, e.TITLE, e.METRIC_VALUE
HAVING COUNT_IF(L.IS_SUCCESS = 'YES') = 0
ORDER BY e.RAISED_AT DESC;

-- R168.2 SEC_NEW_ADMIN_NETWORK failures-only events (PREFLIGHT P168.4, second grid) and the R2-034 stranded PERF
-- events need no statement: the hourly scan clears the PERF ones itself; resolve the others in Alerts.
-- AFTER [C2]: re-run its pre-check (no OPEN / ACK row with SAYS_LOGGED_IN = TRUE). PART_B_V166_V172.sql V168.3 and
--   V168.4 (OK = the PERF half is done). If V174 gets applied, its new events for SNOW_PRI_GFR_PRD_ALFA_DSA holders
--   are PART_B_V174.sql V174.3's list, not this one.

-- #####################################################################################
--  D  V169 (cluster alerts) -- light: read-only grids + the Alerts UI
-- #####################################################################################

-- =====================================================================================
--  [D0] V169 section opener (the source banner and this section's Central pin)
--  source: OWNER_REPAIRS_V166_V172.sql lines 573-578 (verbatim)
-- =====================================================================================
-- =====================================================================
--  V169 (cluster alerts): nothing runs as written. Resolve the listed historic events in the Alerts UI, except
--  P169.4's next-day DQ_RECON_ERROR twins: R169.1 is OPTIONAL and fully commented (owner decision: close the
--  older twin as SUPERSEDED, a machine-close kind the Alerts RESOLVE radios cannot set).
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- 2026-10-09 NOTE for [D1]: (1) line 580's "the RUN_NEXT file leads with the timezone pin" is out of date; this
--   file's TOP pin and [D0]'s pin cover it. (2) Line 582's "these read-only PREFLIGHT grids": PREFLIGHT P169.1
--   previews only the current month's pace and P169.3 only the last 3 days of storage, so a week later neither
--   lists the events to resolve; the pre-checks below replace them (P169.2 and P169.7 are the PREFLIGHT text plus
--   the live rows). P169.4 is [D2]'s pre-check. No scan closes any of these events, so each one still open is
--   still yours to resolve.
-- =====================================================================================
--  [D1] V169 historic events to resolve in the Alerts UI (bulk resolve; ALERT_AUDIT records who and why)
--  PRE-CHECKS (read-only) below, one per line of the list. DECIDES: an OPEN / ACK row in a pre-check is an event to
--           resolve with the kind its line names; an empty grid = nothing to do for that line.
--  source: OWNER_REPAIRS_V166_V172.sql lines 580-587 (verbatim; comments only)
-- =====================================================================================
-- PRE-CHECK [D1] P169.1 (read-only; new). DECIDES: COMPLETE_DAY_PACE_RATIO <= THRESHOLD_NUM (or NULL) -> NOISE. The
--   ratio uses today's budget and prices: if MONTHLY_BUDGET_USD changed since, judge by eye.
-- live COST_BUDGET_PACE events of days 2-5 raised before V169, with the complete-day pace ratio V169's arm [08] tests
-- (MTD through the day before the key day over the completed-days allowance), re-computed from today's facts and settings.
-- COMPLETE_DAY_PACE_RATIO <= THRESHOLD_NUM (or NULL) = V169 would not have raised it: resolve it in Alerts as NOISE.
WITH s AS (
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE,
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) AS AI_PRICE,
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0) AS BUDGET_USD
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
),
thr AS (
    SELECT MAX(THRESHOLD_NUM) AS THRESHOLD_NUM
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_BUDGET_PACE'
),
v AS (
    SELECT MAX(APPLIED_AT) AS V169_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169
),
ev AS (
    SELECT e.EVENT_ID, e.STATUS, e.RAISED_AT, e.TITLE, TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 3)) AS KEY_DAY
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    CROSS JOIN v
    WHERE e.RULE_ID = 'COST_BUDGET_PACE' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
      AND e.RAISED_AT < v.V169_AT
)
SELECT ev.EVENT_ID, ev.STATUS, ev.RAISED_AT, ev.KEY_DAY, ev.TITLE, t.THRESHOLD_NUM,
       ROUND(SUM(m.CREDITS_BILLED * IFF(m.SERVICE_TYPE ILIKE '%CORTEX%' OR m.SERVICE_TYPE ILIKE 'AI%' OR m.SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR m.SERVICE_TYPE ILIKE '%COCO%' OR m.SERVICE_TYPE ILIKE '%COWORK%', s.AI_PRICE, s.PRICE))
             / NULLIF(s.BUDGET_USD * (DAY(ev.KEY_DAY) - 1) / DAY(LAST_DAY(ev.KEY_DAY)), 0), 3) AS COMPLETE_DAY_PACE_RATIO
FROM ev
CROSS JOIN s
CROSS JOIN thr t
LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY m
       ON m.DAY >= DATE_TRUNC('month', ev.KEY_DAY) AND m.DAY < ev.KEY_DAY
WHERE DAY(ev.KEY_DAY) BETWEEN 2 AND 5
GROUP BY ev.EVENT_ID, ev.STATUS, ev.RAISED_AT, ev.KEY_DAY, ev.TITLE, t.THRESHOLD_NUM, s.BUDGET_USD
ORDER BY ev.RAISED_AT;
-- PRE-CHECK [D1] P169.2 (read-only; PREFLIGHT P169.2 verbatim, then the incidents). DECIDES: an OPEN / ACK event
--   beside WOULD_RAISE_NEW = FALSE -> NOISE. WOULD_RAISE_NEW is today's verdict (today's settings and 30-day
--   burn): for an event from an earlier week, confirm on Cost > Contract first. Resolve the alert first, then close
--   its incident from grid (2) in Control Room ([E3] may list it too).
-- (1) PREFLIGHT P169.2 verbatim (PREFLIGHT_V166_V172.sql lines 390-428): the contract settings, then today's arm [16]
--     verdict WOULD_RAISE_NEW beside every OPEN / ACK COST_CONTRACT_BREACH event.
SELECT KEY, VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY LIKE 'CONTRACT%' ORDER BY KEY;
WITH p AS (
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
        ),
thr AS (
    SELECT COALESCE(MAX(IFF(ENABLED, THRESHOLD_NUM, NULL)), 30) AS THRESHOLD_NUM
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_CONTRACT_BREACH'
)
SELECT p.TOTAL, p.CONSUMED, p.DAILY_BURN, p.TERM_END, p.DAYS_LEFT, p.EXHAUST_DATE,
       (p.TOTAL > 0 AND p.DAILY_BURN > 0 AND p.DAYS_LEFT <= t.THRESHOLD_NUM
        AND (p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))) AS WOULD_RAISE_NEW,
       e.EVENT_ID, e.STATUS, e.SEVERITY, e.RAISED_AT, e.TITLE
FROM p
CROSS JOIN thr t
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.RULE_ID = 'COST_CONTRACT_BREACH' AND e.STATUS IN ('OPEN', 'ACK');
-- (2) the open incidents those contract events joined (auto-declared CRITICALs): close them in Control Room after
--     their alerts are resolved.
SELECT i.INCIDENT_ID, i.STATUS AS INCIDENT_STATUS, i.DETECTED_AT, i.DECLARED_BY,
       e.EVENT_ID, e.STATUS AS ALERT_STATUS, e.DEDUPE_KEY, e.TITLE
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID AND m.MEMBER_KIND = 'ALERT'
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e ON e.EVENT_ID = m.REF_ID
WHERE e.RULE_ID = 'COST_CONTRACT_BREACH' AND i.STATUS IN ('OPEN', 'MITIGATED')
ORDER BY i.DETECTED_AT;
-- PRE-CHECK [D1] P169.3 (read-only; new). DECIDES: any OPEN / ACK row -> EXPECTED ('re-created database pairing').
-- live COST_STORAGE_SURGE events raised before V169 whose database had more than one DATABASE_ID on the key day or the
-- day before (a drop / re-create the by-name series paired). PREFLIGHT P169.3 reads only the last 3 days, so it no
-- longer reaches these. Resolve them in Alerts as EXPECTED ('re-created database pairing').
WITH multi AS (
    SELECT DATABASE_NAME, USAGE_DATE, COUNT(DISTINCT DATABASE_ID) AS IDS,
           COUNT(DISTINCT IFF(DELETED IS NULL, DATABASE_ID, NULL)) AS LIVE_IDS
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(DISTINCT DATABASE_ID) > 1
),
v AS (
    SELECT MAX(APPLIED_AT) AS V169_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169
)
SELECT e.EVENT_ID, e.STATUS, e.RAISED_AT, e.DEDUPE_KEY, e.TITLE,
       MAX(m.IDS) AS DATABASE_IDS, MIN(m.LIVE_IDS) AS LIVE_IDS
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
CROSS JOIN v
JOIN multi m
  ON m.DATABASE_NAME = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
 AND m.USAGE_DATE BETWEEN DATEADD('day', -1, TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 3)))
                      AND TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 3))
WHERE e.RULE_ID = 'COST_STORAGE_SURGE' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND e.RAISED_AT < v.V169_AT
GROUP BY e.EVENT_ID, e.STATUS, e.RAISED_AT, e.DEDUPE_KEY, e.TITLE
ORDER BY e.RAISED_AT;
-- PRE-CHECK [D1] P169.7 (read-only; PREFLIGHT P169.7 verbatim, then the live rows). DECIDES: a live row in the
--   second grid -> EXPECTED ('threshold-0 artifact'); a THRESHOLD_NUM <= 0 can be set to 1 in Alerts > Rules
--   (cosmetic: the arm floors it at 1).
-- PREFLIGHT P169.7 verbatim (PREFLIGHT_V166_V172.sql lines 600-603: all-time count + the rule threshold), then the
-- live rows to resolve in Alerts as EXPECTED ('threshold-0 artifact').
SELECT (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE RULE_ID = 'SEC_TRUST_REGRESSION' AND METRIC_VALUE < 1) AS NO_RISE_EVENTS,
       (SELECT MAX(THRESHOLD_NUM) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
         WHERE RULE_ID = 'SEC_TRUST_REGRESSION') AS THRESHOLD_NUM;
SELECT EVENT_ID, STATUS, RAISED_AT, DEDUPE_KEY, TITLE, METRIC_VALUE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID = 'SEC_TRUST_REGRESSION' AND METRIC_VALUE < 1 AND STATUS IN ('OPEN', 'ACK', 'SNOOZED')
ORDER BY RAISED_AT;

-- Run in a Central session (RAISED_AT is Central wall-clock; the RUN_NEXT file leads with the timezone pin).
-- V169 owner repairs: NONE run at apply. The facts are correct; only the arms' arithmetic changed. Resolve historic
-- events in the Alerts UI (bulk resolve; ALERT_AUDIT records who and why), from these read-only PREFLIGHT grids:
--   P169.1 COST_BUDGET_PACE events of days 2-5 the complete-day ratio would not have raised (NOISE);
--   P169.2 COST_CONTRACT_BREACH events with WOULD_RAISE_NEW = FALSE (NOISE), and their auto-declared incidents;
--   P169.3 COST_STORAGE_SURGE events on a re-created database (EXPECTED, note 're-created database pairing');
--   P169.4 DQ_RECON_ERROR next-day duplicates still OPEN / ACK: NOT in the Alerts UI -- use R169.1 below;
--   P169.7 SEC_TRUST_REGRESSION events with METRIC_VALUE < 1 (EXPECTED).
-- AFTER [D1]: re-run the pre-checks (no OPEN / ACK row left that you meant to resolve). PART_B_V166_V172.sql V169.3
--   (heartbeat 14/14, no changed arm failed).

-- 2026-10-09 NOTE for [D2]: lines 589-590's "that PREFLIGHT P169.4 lists (its second grid ...)": use the pre-check
--   below (P169.4's second grid, paired). Only a pair whose OLDER event predates V169 is a twin (V169 keys the
--   error-cycle day); a pair raised wholly after V169 is two different cycles: leave it. The placeholder as written
--   still matches no row.
-- =====================================================================================
--  [D2] OPTIONAL (owner decision) -- R169.1
--  DECISION: close the OLDER twin of each pre-V169 next-day DQ_RECON_ERROR duplicate as SUPERSEDED (paste its
--            EVENT_ID), or keep them as history (README owner question 13 default: keep unless bulk-resolved).
--  WHEN: outside 06:30-07:30 Central (the daily scan inserts DQ_RECON_ERROR events around 06:50).
--  PRE-CHECK (read-only; new) below. DECIDES: a row with OLDER_PRE_V169 = TRUE gives an OLDER_EVENT_ID the UPDATE
--           may take; no such row -> nothing to decide.
--  source: OWNER_REPAIRS_V166_V172.sql lines 588-601 (verbatim; the UPDATE stays commented out)
-- =====================================================================================
-- next-day DQ_RECON_ERROR twins whose OLDER event is still OPEN / ACK (PREFLIGHT P169.4's second grid, paired).
-- Close (R169.1) only a pair with OLDER_PRE_V169 = TRUE: the pre-V169 scan-date key paged the same 48h break twice.
-- A pair raised wholly after V169 is two different error-cycle days (V169 keys the cycle day), not a twin: leave it.
WITH v AS (
    SELECT MAX(APPLIED_AT) AS V169_AT FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169
)
SELECT o.EVENT_ID AS OLDER_EVENT_ID, o.STATUS AS OLDER_STATUS, o.RAISED_AT AS OLDER_RAISED_AT, o.DEDUPE_KEY AS OLDER_KEY,
       n.EVENT_ID AS NEWER_EVENT_ID, n.STATUS AS NEWER_STATUS, n.RAISED_AT AS NEWER_RAISED_AT, n.DEDUPE_KEY AS NEWER_KEY,
       o.TITLE,
       o.RAISED_AT < v.V169_AT AS OLDER_PRE_V169,
       n.RAISED_AT < v.V169_AT AS NEWER_PRE_V169
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS o
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS n
  ON n.RULE_ID = o.RULE_ID
 AND n.TITLE = o.TITLE
 AND n.EVENT_ID <> o.EVENT_ID
 AND n.RAISED_AT::DATE = DATEADD('day', 1, o.RAISED_AT::DATE)
CROSS JOIN v
WHERE o.RULE_ID = 'DQ_RECON_ERROR' AND o.STATUS IN ('OPEN', 'ACK')
  AND o.RAISED_AT >= DATEADD('day', -90, CURRENT_TIMESTAMP())
ORDER BY o.RAISED_AT;

-- R169.1 OPTIONAL (owner decision), the one exception to resolving in the Alerts UI: close the OLDER twin of each
-- next-day DQ_RECON_ERROR duplicate that PREFLIGHT P169.4 lists (its second grid: the earlier of two events with the
-- same TITLE on consecutive days) as SUPERSEDED. SUPERSEDED is a machine-close kind the Alerts RESOLVE radios do not
-- offer: they set ACTIONED / NOISE / EXPECTED, which count in the RESOLVED total and MTTR (and ACTIONED / NOISE also
-- move the rule's precision); SUPERSEDED stays out of all three, like the scans' own escalation supersede. Replace
-- the placeholder with those EVENT_IDs, each quoted, comma-separated. Only a listed row of this rule still OPEN or
-- ACK can change; a SNOOZED twin is left to wake, and the placeholder as written matches no row. No ALERT_AUDIT row
-- is written (the scans' machine closes write none either). Uncomment to run.
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
--    SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
--  WHERE EVENT_ID IN ('<older-twin EVENT_ID from P169.4>')
--    AND RULE_ID = 'DQ_RECON_ERROR'
--    AND STATUS IN ('OPEN', 'ACK');
-- Never DELETE an ALERT_EVENTS row and never rewrite DEDUPE_KEY.
-- AFTER [D2] (only if you ran the UPDATE): re-run its pre-check (the pasted ids are gone). PART_B_V166_V172.sql
--   V169.4 (the newest reconciliation cycle was paged; closing older twins does not change it).

-- #####################################################################################
--  E  V170 (cluster incidents) -- light: read-only grids + Control Room
-- #####################################################################################

-- =====================================================================================
--  [E0] V170 section opener (the source banner and this section's Central pin)
--  source: OWNER_REPAIRS_V166_V172.sql lines 603-606 (verbatim)
-- =====================================================================================
-- =====================================================================
--  V170 (cluster incidents): all OPTIONAL, nothing heavy, nothing in the migration. Labels R170.n added here.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- =====================================================================================
--  [E1] R170.1 -- R2-030 member-less open incidents: a Control Room worklist (close each with its root cause)
--  PRE-CHECK (read-only; PART B V170.4 plus NEW_SINCE_V170) below. DECIDES: each row is an incident to close in
--           Control Room; a NEW_SINCE_V170 = TRUE row would be a V170 regression -- report it before closing it.
--  source: OWNER_REPAIRS_V166_V172.sql lines 608-609 (verbatim; comments only)
-- =====================================================================================
-- PART B V170.4 (OPEN / MITIGATED incidents with no member) plus NEW_SINCE_V170: TRUE would mean V170's member-less
-- rollback (R2-030) missed one -- report it before closing it.
SELECT i.INCIDENT_ID, i.TITLE, i.COMPANY, i.STATUS, i.DETECTED_AT, i.DECLARED_BY,
       i.DETECTED_AT >= (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 170) AS NEW_SINCE_V170
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
WHERE i.STATUS IN ('OPEN', 'MITIGATED')
  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m WHERE m.INCIDENT_ID = i.INCIDENT_ID)
ORDER BY i.DETECTED_AT;

-- R170.1 R2-030 member-less open incidents: PART B V170.4 is the worklist. Close each row you confirm in the app:
--        Control Room > Incidents & triage > "Close this incident" (audited, forward-only, back-fills ACK_AT / OWNER).
-- AFTER [E1]: PART_B_V166_V172.sql V170.4 (no rows, except any you keep open on purpose).

-- =====================================================================================
--  [E2] OPTIONAL (owner decision) -- R170.1's bounded SQL alternative
--  DECISION: close the member-less incidents [E1] lists by SQL instead of in Control Room. The app close is
--            preferred: it also back-fills ACK_AT / OWNER (line 609); this UPDATE does not.
--  source: OWNER_REPAIRS_V166_V172.sql lines 610-621 (verbatim; the UPDATE stays commented out)
-- =====================================================================================
--        OPTIONAL bounded SQL alternative: only the ids V170.4 listed, never a blanket UPDATE. Replace the
--        placeholders with those ids, then uncomment:
-- UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENTS
--    SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), ROOT_CAUSE_KIND = 'UNKNOWN',
--        ROOT_CAUSE_NOTE = 'R2-030: empty declare (its alerts cleared before they were linked)',
--        UPDATED_AT = CURRENT_TIMESTAMP()
--  WHERE INCIDENT_ID IN ('<id1>', '<id2>')
--    AND STATUS IN ('OPEN', 'MITIGATED')
--    AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
--                    WHERE m.INCIDENT_ID = INCIDENTS.INCIDENT_ID);
--        DECLARED_BY is NOT NULL (V032 default CURRENT_USER()): a manual empty declare shows the app owner there,
--        an auto-declare race shows SP_INCIDENT_AUTODECLARE.
-- AFTER [E2] (only if you ran it): PART_B_V166_V172.sql V170.4 (the closed ids are gone).

-- =====================================================================================
--  [E3] R170.2 -- R2-093 duplicate EXH incidents: a Control Room worklist (no SQL repair)
--  PRE-CHECK (read-only; PART B V170.5 verbatim) below. DECIDES: rows with FAMILY_OPEN_INCIDENTS > 1 -> close the
--           duplicate in Control Room with its root cause, after [D1]'s P169.2 resolves (a COST_CONTRACT_BREACH EXH
--           incident can sit in both lists); none -> nothing to do.
--  source: OWNER_REPAIRS_V166_V172.sql lines 623-624 (verbatim; comments only)
-- =====================================================================================
-- PART B V170.5 verbatim (PART_B_V166_V172.sql lines 378-390): open incidents holding an EXH-band member alert.
SELECT i.INCIDENT_ID, i.STATUS, i.DETECTED_AT, a.RULE_ID, a.DEDUPE_KEY,
       (SELECT COUNT(DISTINCT i2.INCIDENT_ID)
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2 ON m2.INCIDENT_ID = i2.INCIDENT_ID AND m2.MEMBER_KIND = 'ALERT'
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a2 ON a2.EVENT_ID = m2.REF_ID
        WHERE i2.STATUS IN ('OPEN', 'MITIGATED')
          AND SPLIT_PART(COALESCE(a2.DEDUPE_KEY, a2.EVENT_ID), '|', 1) = a.RULE_ID) AS FAMILY_OPEN_INCIDENTS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID AND m.MEMBER_KIND = 'ALERT'
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
WHERE SPLIT_PART(a.DEDUPE_KEY, '|', 2) = 'EXH'
  AND i.STATUS IN ('OPEN', 'MITIGATED')
ORDER BY i.DETECTED_AT;

-- R170.2 R2-093 duplicate EXH incidents: PART B V170.5 is the worklist. Where FAMILY_OPEN_INCIDENTS > 1, close the
--        duplicate from Control Room with its root cause. No SQL repair (intentional vs duplicate is a human call).
-- AFTER [E3]: PART_B_V166_V172.sql V170.5 (FAMILY_OPEN_INCIDENTS 1 on every row, or no rows).

-- =====================================================================================
--  [E4] OPTIONAL (owner decision) -- R170.3, R2-028 historical 'Declared by'
--  DECISION: rewrite DECLARED_BY / LINKED_BY of pre-V170 manual declares from the preview, one approved row at a
--            time (README owner question 12 default: NO repair). The preview SELECT is read-only; the UPDATEs stay
--            commented out.
--  source: OWNER_REPAIRS_V166_V172.sql lines 626-642 (verbatim)
-- =====================================================================================
-- R170.3 R2-028 historical 'Declared by' -- OPTIONAL, owner question, DEFAULT: NO repair (the Open incidents table
--        keeps a caption instead). Read-only preview of an unambiguous APP_USAGE incident_declare match within
--        5 minutes of DETECTED_AT (the usage flush is buffered, hence +300 s):
WITH ev AS (SELECT USER_NAME, AT FROM DBA_MAINT_DB.OVERWATCH.APP_USAGE WHERE EVENT_KIND = 'incident_declare'),
m AS (
  SELECT i.INCIDENT_ID, i.DECLARED_BY AS OLD_BY, i.DETECTED_AT, ARRAY_AGG(DISTINCT ev.USER_NAME) AS CANDIDATES
  FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
  JOIN ev ON ev.AT BETWEEN DATEADD('second', -5, i.DETECTED_AT) AND DATEADD('second', 300, i.DETECTED_AT)
  WHERE i.DECLARED_BY <> 'SP_INCIDENT_AUTODECLARE'
  GROUP BY 1, 2, 3)
SELECT * FROM m WHERE ARRAY_SIZE(CANDIDATES) = 1 AND CANDIDATES[0]::STRING <> OLD_BY;
--        Only if you approve the repair, per approved row (replace the placeholders, then uncomment both):
-- UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENTS SET DECLARED_BY = '<candidate>'
--  WHERE INCIDENT_ID = '<id>' AND DECLARED_BY = '<old>';
-- UPDATE DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS SET LINKED_BY = '<candidate>'
--  WHERE INCIDENT_ID = '<id>' AND AUTO_LINKED = FALSE AND LINKED_BY = '<old>';
--        MTTA does not move either way (it filters DECLARED_BY = 'SP_INCIDENT_AUTODECLARE').
-- AFTER [E4] (only if you ran an UPDATE): re-run the preview (the approved rows are gone). PART_B_V166_V172.sql
--   V170.6 covers new manual declares only.

-- #####################################################################################
--  F  V171 (cluster ops-dq)
-- #####################################################################################

-- =====================================================================================
--  [F0] V171 -- nothing to run (R171.1 is in the SKIPPED list); the source banner is kept for its warning
--  source: OWNER_REPAIRS_V166_V172.sql lines 644-647 (verbatim; comments only)
-- =====================================================================================
-- =====================================================================
--  V171 (cluster ops-dq): no repair required, nothing heavy. Never hand-CALL SP_DAILY_DIGEST (Cortex credits and a
--  Teams post) or SP_CANARY_SENTINEL (writes CANARY_RESULTS, can raise OPS_CANARY_FAIL).
-- =====================================================================
-- AFTER [F0]: PART_B_V166_V172.sql V171.3 and V171.6 (the nightly ref-gap scan runs clean).

-- #####################################################################################
--  G  V172 (cluster detection) -- light: read-only worklists
-- #####################################################################################

-- 2026-10-09 NOTE for [G0]: "Read them after R172.0 (PART 1) or the next 06:50 Central scan; R172.4 after the next
--   07:00 Central sweep" -- R172.0 is SKIPPED, and the 06:50 change scan and the 07:00 sweep have run every morning
--   since the apply, so read R172.1 - R172.4 now (off-peak). The same holds for R172.1's "Read after R172.0 or the
--   next 06:50 scan".
-- =====================================================================================
--  [G0] V172 section opener (the source banner and this section's Central pin)
--  source: OWNER_REPAIRS_V166_V172.sql lines 653-657 (verbatim)
-- =====================================================================================
-- =====================================================================
--  V172 (cluster detection): R172.1 - R172.4 worklists, read-only except R172.3's OPTIONAL commented close. Read
--  them after R172.0 (PART 1) or the next 06:50 Central scan; R172.4 after the next 07:00 Central sweep.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- =====================================================================================
--  [G1] R172.1 -- live PERF_CHANGE_REGRESSION alerts whose verdict is no longer REGRESSED (read-only worklist)
--  PRE-CHECK: none needed; the grid is its own check (empty = nothing to resolve). Resolve rows in the app as
--           EXPECTED, never by SQL.
--  source: OWNER_REPAIRS_V166_V172.sql lines 659-670 (verbatim)
-- =====================================================================================
-- R172.1 (R2-021 / R2-025 / R2-022) live PERF_CHANGE_REGRESSION alerts -- OPEN, ACK or SNOOZED, the set the R1b
--        re-stamp covers -- whose re-computed verdict is no longer REGRESSED (a suffix-collision or retry
--        over-count raised them). Read after R172.0 or the next 06:50 scan. Resolve them in the app as EXPECTED
--        (Alerts drawer, type-to-confirm; wake a SNOOZED one first via Alerts > Snoozed > Wake selected now, or it
--        wakes back into triage still open) -- never by SQL: RESOLVE feeds per-rule precision.
SELECT e.EVENT_ID, e.STATUS, e.SEVERITY, e.TITLE, r.OBJECT_TYPE, r.VERDICT, r.VERDICT_DETAIL, r.LAST_EVALUATED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
JOIN DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
  ON e.DEDUPE_KEY = 'PERF_CHANGE_REGRESSION|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
WHERE e.RULE_ID = 'PERF_CHANGE_REGRESSION' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND r.VERDICT <> 'REGRESSED'
ORDER BY e.RAISED_AT;
-- AFTER [G1]: re-run it (no row you meant to resolve). PART_B_V166_V172.sql V172.3 (tracking rows in Hr/Min/Sec,
--   PROCEDURE baselines re-frozen).

-- =====================================================================================
--  [G2] R172.2 -- open incidents whose member alert now carries a different company (read-only worklist)
--  PRE-CHECK: none needed; the grid is its own check (empty = nothing to re-scope). Re-scope by hand only if it
--           matters.
--  source: OWNER_REPAIRS_V166_V172.sql lines 672-690 (verbatim)
-- =====================================================================================
-- R172.2 (R2-023 / R2-024) open incidents whose member alert of the five rules now carries a different company
--        (incident-linked alerts were deliberately NOT re-stamped). Re-scope by hand only if it matters.
SELECT y.INCIDENT_ID, y.INCIDENT_STATUS, y.INCIDENT_COMPANY, y.RULE_ID, y.EVENT_ID, y.ALERT_COMPANY, y.V044_COMPANY
FROM (
    SELECT x.INCIDENT_ID, x.INCIDENT_STATUS, x.INCIDENT_COMPANY, x.RULE_ID, x.EVENT_ID, x.ALERT_COMPANY,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS V044_COMPANY
    FROM (
        SELECT i.INCIDENT_ID, i.STATUS AS INCIDENT_STATUS, i.COMPANY AS INCIDENT_COMPANY, e.RULE_ID, e.EVENT_ID,
               e.COMPANY AS ALERT_COMPANY, SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1) AS DB
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID AND m.MEMBER_KIND = 'ALERT'
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e ON e.EVENT_ID = m.REF_ID
        WHERE e.RULE_ID IN ('PERF_CHANGE_REGRESSION', 'DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP',
                            'DQ_BREACH')
          AND i.STATUS IN ('OPEN', 'MITIGATED')
    ) x
) y
WHERE y.INCIDENT_COMPANY IS DISTINCT FROM y.V044_COMPANY
ORDER BY y.INCIDENT_ID;
-- AFTER [G2]: PART_B_V166_V172.sql V172.2 (the V172 re-stamps hold).

-- =====================================================================================
--  [G3] R172.3 -- anomaly events booked while their rule was disabled (read-only; usually empty)
--  PRE-CHECK: none needed; the grid is [G4]'s pre-check (OPEN / ACK / SNOOZED rows here = [G4] has work).
--  source: OWNER_REPAIRS_V166_V172.sql lines 692-701 (verbatim)
-- =====================================================================================
-- R172.3 (R1-227) cloud-services or sweep anomaly events booked while their rule was disabled (V150
--        ignored the switch). Usually empty: both rules ship enabled.
SELECT e.RULE_ID, c.ENABLED, c.UPDATED_AT, e.STATUS, COUNT(*) AS EVENTS, MIN(e.RAISED_AT) AS FIRST_RAISED,
       MAX(e.RAISED_AT) AS LAST_RAISED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
WHERE e.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP')
  AND NOT c.ENABLED AND e.RAISED_AT > c.UPDATED_AT
GROUP BY 1, 2, 3, 4
ORDER BY 1, 4;

-- =====================================================================================
--  [G4] OPTIONAL (owner decision) -- R172.3's close
--  DECISION: close the events [G3] lists (OPEN / ACK / SNOOZED, booked while their rule was disabled) as EXPECTED,
--            or leave them for the Alerts UI.
--  source: OWNER_REPAIRS_V166_V172.sql lines 702-707 (verbatim; the UPDATE stays commented out)
-- =====================================================================================
-- (OPTIONAL, only if R172.3 returned OPEN / ACK / SNOOZED rows) close them as EXPECTED -- uncomment to run:
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
--    SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED', RESOLVED_AT = CURRENT_TIMESTAMP()
--  WHERE e.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP') AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
--    AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
--                WHERE c.RULE_ID = e.RULE_ID AND NOT c.ENABLED AND e.RAISED_AT > c.UPDATED_AT);
-- AFTER [G4] (only if you ran it): re-run [G3] (no OPEN / ACK / SNOOZED row).

-- =====================================================================================
--  [G5] R172.4 -- the arms of the re-derived scans that logged since V172, beside their 14 days before (read-only)
--  PRE-CHECK: none needed; the grid is its own check (no rows = no arm logged since the apply).
--  source: OWNER_REPAIRS_V166_V172.sql lines 709-731 (verbatim)
-- =====================================================================================
-- R172.4 (every re-derived guarded arm) after the next TASK_ANOMALY_SWEEP (07:00 Central): the APP_ERROR_LOG rows
--        of each arm that logged since the apply, beside the same arm's rows in the 14 days before (the V172.4 arm
--        table). NEW since V172 = the arm was silent before: a compile surprise in a re-derived arm, swallowed by
--        its EXCEPTION -- fix it before the next run. PRE-EXISTING = the arm already logged before V172 (by design
--        on an account without TASK_VERSIONS, ORGANIZATION_USAGE or Cortex): a V172 problem only if its since-V172
--        ERROR_MESSAGE differs from the before one. No rows = no arm logged since the apply.
SELECT IFF(x.ROWS_BEFORE = 0, 'NEW since V172', 'PRE-EXISTING') AS ARM_STATUS, x.PAGE, x.ARM,
       IFF(l.LOGGED_AT >= x.T, 'since V172', 'before V172') AS SIDE, l.LOGGED_AT, l.ERROR_MESSAGE
FROM (
    SELECT l.PAGE, l.ERROR_TYPE, l.CONTEXT,
           COALESCE(l.ERROR_TYPE, '?') || ' (' || COALESCE(l.CONTEXT, '?') || ')' AS ARM, MAX(a.T) AS T,
           COUNT_IF(l.LOGGED_AT < a.T) AS ROWS_BEFORE, COUNT_IF(l.LOGGED_AT >= a.T) AS ROWS_SINCE
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
    JOIN (SELECT MAX(APPLIED_AT) AS T FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172) a
      ON l.LOGGED_AT >= DATEADD('day', -14, a.T)
    WHERE l.PAGE IN ('AnomalySweep', 'ChangeImpactScan')
    GROUP BY l.PAGE, l.ERROR_TYPE, l.CONTEXT
    HAVING COUNT_IF(l.LOGGED_AT >= a.T) > 0
) x
JOIN DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
  ON l.PAGE = x.PAGE AND EQUAL_NULL(l.ERROR_TYPE, x.ERROR_TYPE) AND EQUAL_NULL(l.CONTEXT, x.CONTEXT)
 AND l.LOGGED_AT >= DATEADD('day', -14, x.T)
ORDER BY 1, 2, 3, l.LOGGED_AT DESC;
-- AFTER [G5]: PART_B_V166_V172.sql V172.4 (OK; a FAIL names an arm that started logging at the apply).

-- =====================================================================================
--  FINAL CHECK (read-only; new) at the end of each sitting: failures the CALL blocks and the loaders logged in the
--  last day. Expect no OwnerRepair / OwnerRepairV167 row (a row names the CALL and its error: fix the cause and
--  re-run that block). A MartLoader mart_load_failed row may also be a scheduled run's; its CONTEXT names the arm.
-- =====================================================================================
SELECT LOGGED_AT, PAGE, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE (PAGE IN ('OwnerRepair', 'OwnerRepairV167') OR (PAGE = 'MartLoader' AND ERROR_TYPE = 'mart_load_failed'))
  AND LOGGED_AT >= DATEADD('day', -1, CURRENT_TIMESTAMP())
ORDER BY LOGGED_AT DESC;

ALTER SESSION UNSET TIMEZONE;

-- =====================================================================================
--  AFTER (read-only; PART_B_V166_V172.sql unless named; in a fresh worksheet run its pin and role lines first)
--    * right after A / B: V166.4 (no AppCost / StorageTruth fact_load_failed), V167.1 (both COVERAGE_FROM about
--      today - 364), V167.3 (TWIN_ROWS_LEFT 0). V167.4 belongs to REPAIR_COCO_AI_FACT_2026-10-09.sql (its R1 / R2
--      are the stronger check).
--    * after the next :07 Central hourly run that follows any RESUME: the POST-RESUME CHECK after [A2] / [B3]
--      (both LAST_LOAD_TS later than the RESUME: the graph runs again), then V168.3 (14/14) and V168.4 (OK).
--    * the next morning (after 07:30 Central): V166.3 (both daily loaders SUCCEEDED), V169.3, V169.4, V172.3, V172.4.
--    * any time: V170.4 and V170.5 (the Control Room worklists: empty, or only rows you keep), V172.2, V171.3,
--      V171.6.
--  PASTE BACK: every write block's pane (its ok / SKIP / WAIT / FAILED text), every PRE-CHECK grid you ran (an
--  empty grid is an answer), the SHOW TASKS rows after [A2] / [B3], the FINAL CHECK grid, and the PART B grids
--  above. Say which blocks you skipped on a pre-check and which OPTIONAL blocks, if any, you uncommented.
-- =====================================================================================
