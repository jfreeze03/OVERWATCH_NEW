-- =====================================================================================
--  REPAIR_COCO_AI_FACT_2026-10-09.sql  --  the V167 step-1 repair that was never run (a WRITE; owner-run)
--  WHY: PROBES_COCO_FACT_GAP_2026-10-09 F1 showed 23 of 41 days where FACT_AI_USAGE_DAILY (AI users, the company
--  showback, the CoCo spend tile, the runaway alert) disagrees with Snowflake, in BOTH directions, with neighbouring
--  days cancelling exactly (Sep 04 +2.1501 / Sep 05 -2.1501; Sep 21 +0.1696 / Sep 22 -0.1696), September 20.4779
--  credits short in total, and every day from Sep 29 on exact. F2: the loader ran every day; F3: no errors; the
--  Snowflake views agree with each other every day. That is the V167 day-key change (UTC day -> Central day,
--  R2-052): days loaded before V167 keep the old key, and its documented OWNER_REPAIRS step 1, which re-keys the
--  whole year, was never run. This file is that step, copied unchanged from OWNER_REPAIRS_V166_V172.sql (PART 1.1),
--  plus a check before and after.
--  WHAT IT DOES: CALL SP_LOAD_MARTS_V27('DAILY', 365) re-merges a year of the daily marts with Central day keys, then
--  deletes ONLY the old Cortex Code rows the reload did not rewrite (stale UTC-keyed twins). It prunes nothing unless
--  the reload's AI step succeeded ('FAILED (nothing pruned): ...' otherwise). Safe to re-run.
--  WHEN / WHERE: off-peak (not 06:30-07:30 Central, not near :07), as SNOW_ACCOUNTADMINS, on a warehouse whose own
--  STATEMENT_TIMEOUT_IN_SECONDS allows a long CALL (the check below shows it). Expect 'ok: MARTS OK (DAILY, 365d): ...
--  ai_code ai_functions | re-keyed Code rows N from <date>, pruned stale offset-keyed rows M'. If it times out, re-run
--  it. Note: the loader's temporary scratch table shows in Security > Changes as one MEDIUM DESTRUCTIVE row under
--  your user; that is expected, not a change.
--  ORDER: R0 (read-only) -> the repair block -> R1 and R2 (read-only). Paste back R0, the repair's result, R1, R2.
-- =====================================================================================

-- R0 (read-only) this worksheet's statement timeout, at the session and the warehouse (edit the warehouse name if
--    your worksheet uses another). The lower non-zero value wins; 3600 or more is comfortable.
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN SESSION;
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_QUERY;

-- >>> the repair: OWNER_REPAIRS_V166_V172.sql lines 51-120, unchanged (session pin, guard, PART 1.1 step 1)
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

-- R1 (read-only, after the repair) per Central month, last 12 months: Snowflake's view (Snowsight + CLI) vs the fact.
--    Expect DRIFT 0.0000 in every month. The oldest month can read short on both sides (the 365-day edge).
WITH k AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_CT
),
v AS (
    SELECT DATE_TRUNC('month', CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE) AS MONTH_CT,
           SUM(COALESCE(c.TOKEN_CREDITS, 0)) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN k
    WHERE c.USAGE_TIME >= DATEADD('day', -364, k.TODAY_CT)::TIMESTAMP_TZ
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= DATEADD('day', -364, k.TODAY_CT)
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE < k.TODAY_CT
      AND LOWER(c.INTERFACE) IN ('snowsight', 'cli')
    GROUP BY 1
),
f AS (
    SELECT DATE_TRUNC('month', t.DAY) AS MONTH_CT, SUM(COALESCE(t.CREDITS, 0)) AS CR
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
    CROSS JOIN k
    WHERE t.SOURCE IN ('Snowsight', 'CLI')
      AND t.DAY >= DATEADD('day', -364, k.TODAY_CT)
      AND t.DAY < k.TODAY_CT
    GROUP BY 1
)
SELECT COALESCE(v.MONTH_CT, f.MONTH_CT) AS MONTH_CT,
       ROUND(v.CR, 4) AS VIEW_CREDITS,
       ROUND(f.CR, 4) AS FACT_CREDITS,
       ROUND(COALESCE(v.CR, 0) - COALESCE(f.CR, 0), 4) AS DRIFT
FROM v
FULL OUTER JOIN f ON f.MONTH_CT = v.MONTH_CT
ORDER BY 1;

-- R2 (read-only, after the repair) per Central day since 2026-08-25 (the window F1 checked): how many days still
--    differ by more than 0.001 credits. Expect GAP_DAYS = 0 and DAYS = the number of days with usage.
WITH k AS (
    SELECT '2026-08-25'::DATE AS LO,
           CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_CT
),
v AS (
    SELECT CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY, SUM(COALESCE(c.TOKEN_CREDITS, 0)) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN k
    WHERE c.USAGE_TIME >= DATEADD('day', -1, k.LO)::TIMESTAMP_TZ
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= k.LO
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE < k.TODAY_CT
      AND LOWER(c.INTERFACE) IN ('snowsight', 'cli')
    GROUP BY 1
),
f AS (
    SELECT t.DAY, SUM(COALESCE(t.CREDITS, 0)) AS CR
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY t
    CROSS JOIN k
    WHERE t.SOURCE IN ('Snowsight', 'CLI') AND t.DAY >= k.LO AND t.DAY < k.TODAY_CT
    GROUP BY 1
)
SELECT COUNT(*) AS DAYS,
       COUNT_IF(ABS(COALESCE(v.CR, 0) - COALESCE(f.CR, 0)) > 0.001) AS GAP_DAYS,
       ROUND(SUM(COALESCE(v.CR, 0)) - SUM(COALESCE(f.CR, 0)), 4) AS NET_DRIFT
FROM v
FULL OUTER JOIN f ON f.DAY = v.DAY;

ALTER SESSION UNSET TIMEZONE;
