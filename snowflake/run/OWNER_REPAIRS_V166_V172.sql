-- =====================================================================
--  OVERWATCH -- OWNER_REPAIRS_V166_V172.sql  (owner-run; AFTER RUN_NEXT.sql has applied V162 -> V172)
--  The ordered owner-run heals and worklists from the V166 - V172 cluster handoffs. Nothing here runs at apply
--  time and no migration CALLs any of it. Every write block is guarded (an EXECUTE IMMEDIATE that verdict-checks
--  its CALL) and bounded (an explicit day window or a verdict-gated prune). Most also log a failure to
--  APP_ERROR_LOG -- PAGE 'OwnerRepair' (V166) or 'OwnerRepairV167' (V167) -- and return 'FAILED: ...' instead of
--  raising, so a Run All still reaches the RESUME lines. TWO DO NOT: PART 1.1 (V167 step 1) returns
--  'FAILED (nothing pruned): ...' on a failed verdict but writes no OwnerRepairV167 row (a failed AI arm logs
--  itself as MartLoader 'mart_load_failed') and does not catch a raised error, so a raise stops a Run All there;
--  the V167 step-4 rebuild ROLLBACKs and re-raises by design. A timeout or a Stop is never caught: then run that
--  section's RESUME lines by hand. Everything else is read-only, or COMMENTED OUT where the handoff makes it
--  OPTIONAL or an owner decision.
--
--  RUN IT ONE BLOCK AT A TIME and read each pane before the next. Never run two heavy blocks at once.
--  FIRST, in every new worksheet: the session pin, the role and the guard below (the guard refuses with -20990
--  until V172 is registered; a bare re-run of the pattern loader BEFORE V167 would add a year of twins).
--  CENTRAL: the owner worksheet runs in UTC, and every loader below keys days on DATE() / ::DATE / CURRENT_DATE();
--  each section re-pins the session too.
--
--  ORDER
--    PART 1  RIGHT AFTER RUN_NEXT.sql, same sitting -- before the next 06:45 Central DAILY marts run:
--            1.1 V167 step 1 (Cortex Code days re-keyed to Central, AI COVERAGE_FROM stamped) -- REQUIRED;
--            1.2 V172 R172.0 (one change-impact scan now) -- RECOMMENDED, commented.
--    PART 2  OFF-PEAK, one block at a time: V166 (R166.1 - R166.6) -> V167 (steps 0, 2, 3, 4) -> V168 (R168.1,
--            OPTIONAL owner decision) -> V169 (UI; R169.1 OPTIONAL owner decision) -> V170 (UI; OPTIONAL SQL) ->
--            V171 (OPTIONAL) -> V172 (R172.1 - R172.4 worklists, after the next 06:50 / 07:00 Central runs).
--
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
-- =====================================================================

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

-- #####################################################################
--  PART 1 -- RIGHT AFTER RUN_NEXT.sql (same sitting; before the next 06:45 Central DAILY marts run)
-- #####################################################################

-- =====================================================================
--  1.1  V167 step 1 (REQUIRED; cluster marts, R2-052 + R1-016). Heavy-ish: see WAREHOUSE / TIMEOUT above.
--       Until it runs, each DAILY ('DAILY', 3) run re-keys only its last ~3 days to Central with a MERGE, so the
--       ~3 days around the apply double-count some evening Cortex Code usage (Chargeback & AI, the Spend CoCo
--       tile, AI budgets and the runaway arm read slightly high). Afterwards: PART B V167.4 (DRIFT ~0).
--       FAILURE: unlike the other CALL blocks it logs no OwnerRepairV167 row and does not catch a raised error
--       (see the header); a 'FAILED (nothing pruned)' pane changed nothing -- fix the cause and re-run it.
--       CHANGE RISK: the loader's CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR lands in the Security page's
--       change list as one MEDIUM DESTRUCTIVE row under your user: a session scratch table, not a change
--       (PART B ALL.2 labels it 'expected').
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- ---- step 1 R2-052 + R1-016: re-key Cortex Code days to Central and stamp the AI COVERAGE_FROM ---------------
-- RELOAD-THEN-PRUNE, never DELETE first (the ai_code arm swallows its own error; a failed reload after a DELETE
-- would empty up to 365 days). Rows the reload re-keyed carry LOAD_TS >= t0; an older Code row inside the
-- re-keyed range is a stale offset-keyed twin. Expect 'ok: MARTS OK (DAILY, 365d): posture ai_code
-- ai_functions | ...'; the freshness MERGE then stamps COVERAGE_FROM = today - 364. If the oldest day reads
-- truncated, re-run with 360.
EXECUTE IMMEDIATE $$
DECLARE
    t0 TIMESTAMP_NTZ;
    rv VARCHAR;
    touched NUMBER DEFAULT 0;
    lo DATE;
    pruned NUMBER DEFAULT 0;
BEGIN
    t0 := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    -- prune only when THIS reload's ai_code arm loaded (its token is in the verdict); a failed required posture
    -- arm still reads 'MARTS WITH ERRORS' in the returned verdict -- re-run the block once it is fixed
    IF (rv IS NULL OR NOT ARRAY_CONTAINS('ai_code'::VARIANT, SPLIT(:rv, ' '))) THEN
        RETURN 'FAILED (nothing pruned): SP_LOAD_MARTS_V27(DAILY, 365) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    SELECT COUNT(*), MIN(DAY) INTO :touched, :lo
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE IN ('Snowsight', 'CLI') AND LOAD_TS >= :t0;
    IF (touched > 0) THEN
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
         WHERE SOURCE IN ('Snowsight', 'CLI') AND DAY > :lo AND LOAD_TS < :t0;
        pruned := SQLROWCOUNT;
    END IF;
    RETURN 'ok: ' || rv || ' | re-keyed Code rows ' || touched || ' from ' || COALESCE(TO_VARCHAR(lo), 'n/a')
           || ', pruned stale offset-keyed rows ' || pruned;
END;
$$;

-- =====================================================================
--  1.2  V172 R172.0 (RECOMMENDED, still commented; cluster detection). One daily change-impact scan's
--       ACCOUNT_USAGE reads; it can raise PERF_CHANGE_REGRESSION events (delivered by the next hourly notify),
--       exactly as the 06:50 Central run would. If skipped, the Operations change table reads mixed until 06:50.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- R172.0 (RECOMMENDED, right after the apply, off-peak) refresh the change-impact registry now instead of at 06:50
-- Central. Until that scan runs, the tracking rows sit on mixed bases: R4 re-froze the TASK baselines per scheduled
-- run while AFTER_CALLS, AFTER_FAILS, VERDICT and VERDICT_DETAIL still hold the last V140 scan's attempt-based
-- values (a task with 7 of 14 runs retried once on both sides: BASELINE_CALLS 14 beside AFTER_CALLS 21, a +7
-- calls delta, under the old 'runs 21->21' text), and the PROCEDURE baselines R3 nulled still show their old
-- VERDICT. This CALL is the daily TASK_CHANGE_IMPACT_SCAN's own work (one scan's ACCOUNT_USAGE reads) and can
-- raise PERF_CHANGE_REGRESSION for rows that now cross the bar, as the 06:50 run would. Uncomment to run; if
-- skipped, the Operations change table reads mixed until 06:50.
-- CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN();

-- #####################################################################
--  PART 2 -- OFF-PEAK, ONE BLOCK AT A TIME (see OFF-PEAK and WAREHOUSE / TIMEOUT at the top)
-- #####################################################################

-- =====================================================================
--  V166 (cluster loaders): R166.1 OPTIONAL probe; R166.2 the 180-day security-fact heal + its RESUME pair;
--  R166.3 gap grids; R166.4 storage-truth heal; R166.5 app-cost heal (OFF-PEAK, heavy); R166.6 OPTIONAL pointers.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

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

-- R166.6 OPTIONAL heals in snowflake/backfill_365.sql (not V166 SQL; nothing here runs them):
--        * the MART_CLOUD_SVC_DAILY arm (the INSERT right after FACT_QUERY_DAILY) fills the cloud-services
--          statement mart's history from QUERY_HISTORY; on an existing install select and run only that INSERT
--          (idempotent: it writes only days before the mart's first day);
--        * the commented SP_LOAD_OBJECT_COST(365) block after the hourly-graph RESUME reloads a year of the
--          object-cost ledger; run it once, off-peak, away from the 06:45 CT daily task.

-- =====================================================================
--  V167 (cluster marts): step 0 read-only hole probes (they set step 3's N); step 1 ran in PART 1; step 2 the
--  pattern re-stamp; step 3 the hourly re-stamp inside a TASK_LOAD_HOURLY suspend window; step 4 the atomic
--  364-day task-graph rebuild (after step 3). Heavy: see WAREHOUSE / TIMEOUT at the top.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

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

-- =====================================================================
--  V168 (cluster alerts): R168.1 is OPTIONAL and fully commented (owner decision: bulk-resolve historic
--  PIPE_COPY_FAILURES carry-over duplicates from PREFLIGHT P168.3). R168.2 needs no statement.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

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
-- R168.2 SEC_NEW_ADMIN_NETWORK failures-only events (PREFLIGHT P168.4, second grid) and the R2-034 stranded PERF
-- events need no statement: the hourly scan clears the PERF ones itself; resolve the others in Alerts.

-- =====================================================================
--  V169 (cluster alerts): nothing runs as written. Resolve the listed historic events in the Alerts UI, except
--  P169.4's next-day DQ_RECON_ERROR twins: R169.1 is OPTIONAL and fully commented (owner decision: close the
--  older twin as SUPERSEDED, a machine-close kind the Alerts RESOLVE radios cannot set).
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- Run in a Central session (RAISED_AT is Central wall-clock; the RUN_NEXT file leads with the timezone pin).
-- V169 owner repairs: NONE run at apply. The facts are correct; only the arms' arithmetic changed. Resolve historic
-- events in the Alerts UI (bulk resolve; ALERT_AUDIT records who and why), from these read-only PREFLIGHT grids:
--   P169.1 COST_BUDGET_PACE events of days 2-5 the complete-day ratio would not have raised (NOISE);
--   P169.2 COST_CONTRACT_BREACH events with WOULD_RAISE_NEW = FALSE (NOISE), and their auto-declared incidents;
--   P169.3 COST_STORAGE_SURGE events on a re-created database (EXPECTED, note 're-created database pairing');
--   P169.4 DQ_RECON_ERROR next-day duplicates still OPEN / ACK: NOT in the Alerts UI -- use R169.1 below;
--   P169.7 SEC_TRUST_REGRESSION events with METRIC_VALUE < 1 (EXPECTED).
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

-- =====================================================================
--  V170 (cluster incidents): all OPTIONAL, nothing heavy, nothing in the migration. Labels R170.n added here.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

-- R170.1 R2-030 member-less open incidents: PART B V170.4 is the worklist. Close each row you confirm in the app:
--        Control Room > Incidents & triage > "Close this incident" (audited, forward-only, back-fills ACK_AT / OWNER).
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

-- R170.2 R2-093 duplicate EXH incidents: PART B V170.5 is the worklist. Where FAMILY_OPEN_INCIDENTS > 1, close the
--        duplicate from Control Room with its root cause. No SQL repair (intentional vs duplicate is a human call).

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

-- =====================================================================
--  V171 (cluster ops-dq): no repair required, nothing heavy. Never hand-CALL SP_DAILY_DIGEST (Cortex credits and a
--  Teams post) or SP_CANARY_SENTINEL (writes CANARY_RESULTS, can raise OPS_CANARY_FAIL).
-- =====================================================================
-- R171.1 OPTIONAL, safe (rewrites only ETL_REF_GAP_RESULTS; raises no alert). Uncomment both; expect
--        'ref-gap scan complete (N ok, 0 failed)', then the rows should match the Operations panel:
-- CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS();
-- SELECT CHECK_NAME, NEW_CODE FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS ORDER BY 1, 2;

-- =====================================================================
--  V172 (cluster detection): R172.1 - R172.4 worklists, read-only except R172.3's OPTIONAL commented close. Read
--  them after R172.0 (PART 1) or the next 06:50 Central scan; R172.4 after the next 07:00 Central sweep.
-- =====================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- this section's own Central pin (idempotent; for a fresh worksheet)

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
-- (OPTIONAL, only if R172.3 returned OPEN / ACK / SNOOZED rows) close them as EXPECTED -- uncomment to run:
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
--    SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED', RESOLVED_AT = CURRENT_TIMESTAMP()
--  WHERE e.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP') AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
--    AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
--                WHERE c.RULE_ID = e.RULE_ID AND NOT c.ENABLED AND e.RAISED_AT > c.UPDATED_AT);

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

ALTER SESSION UNSET TIMEZONE;
