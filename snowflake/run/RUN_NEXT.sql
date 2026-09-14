-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (DIAGNOSE: SP_LOAD_PATTERN_COST "regressed after
--  2026-09-02" CRITICAL -- real slowdown, or a one-time-backfill false alarm?)
--
--  This is the oldest OPEN critical (raised 2026-09-07, ~171h). It comes from the
--  change-impact scan (V010/V061): V120 did CREATE OR REPLACE PROCEDURE
--  SP_LOAD_PATTERN_COST on 2026-09-02 (a fanout CORRECTNESS fix), which registered as an
--  object change; the detector then compared the proc's runtime/cost BEFORE vs AFTER that
--  date. It escalates to CRITICAL when AFTER credits/call >= 2x baseline OR >= 50% of after
--  calls failed. VERDICT=REGRESSED when p95 up >threshold AND >= 30s, or credits/call up
--  materially, or fails +20pts.
--
--  HYPOTHESIS: V120 also ran a ONE-TIME `CALL SP_LOAD_PATTERN_COST(90)` re-stamp at apply.
--  That single 90-day call is far heavier than the daily task's small-N calls, and with only
--  a few "after" calls when the scan first fired (09-07) it would dominate AFTER_P95 (>=30s)
--  and ~double AFTER_CREDITS_PER_CALL -> CRITICAL, even though the DAILY runtime is fine
--  (MART_PATTERN_COST_DAILY loads clean at 06:47 every day). If so, this is a FALSE POSITIVE
--  to resolve + harden (V139 just used the same CREATE-OR-REPLACE + apply-time-backfill
--  pattern for SP_LOAD_OBJECT_COST, so it may raise the identical false alert in a few days).
--
--  READ-ONLY. Run each as SNOW_ACCOUNTADMINS; paste each RESULT block back.
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;
USE WAREHOUSE WH_ALFA_QUERY;

-- [1] The alert row itself. DETAIL carries the numbers: "runs X->Y | fails A->B |
--     p95 Ns->Ms | credits/call C->D". STATUS tells us if it is still OPEN/ACK.
SELECT EVENT_ID, RULE_ID, SEVERITY, STATUS, RAISED_AT, METRIC_VALUE, TITLE, DETAIL
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE TITLE ILIKE '%SP_LOAD_PATTERN_COST%regressed%'
ORDER BY RAISED_AT DESC;
--  >>> paste RESULT [1] <<<

-- [2] The change-impact registry row for this proc: baseline vs after metrics + the CURRENT
--     verdict. If AFTER_CALLS is small and AFTER_P95_MS / AFTER_CREDITS_PER_CALL are huge vs
--     baseline, the one-time 90d re-stamp skewed it. Check whether VERDICT has since flipped
--     to NEUTRAL and whether tracking is still open (CURRENT_DATE vs TRACKING_UNTIL).
SELECT OBJECT_TYPE, OBJECT_NAME, CHANGE_SEEN_AT, TRACKING_UNTIL, LAST_EVALUATED_AT, VERDICT,
       BASELINE_CALLS, AFTER_CALLS, BASELINE_FAILS, AFTER_FAILS,
       ROUND(BASELINE_P95_MS/1000,1) AS BASE_P95_S, ROUND(AFTER_P95_MS/1000,1) AS AFTER_P95_S,
       BASELINE_CREDITS_PER_CALL, AFTER_CREDITS_PER_CALL, CHANGED_BY
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
WHERE OBJECT_NAME ILIKE '%SP_LOAD_PATTERN_COST%'
ORDER BY CHANGE_SEEN_AT DESC;
--  >>> paste RESULT [2] <<<

-- [3] Ground truth: EVERY SP_LOAD_PATTERN_COST call since just before the change. This exposes
--     the one-time 90d re-stamp (a single big ELAPSED_SEC on/around 09-02, likely run
--     interactively) vs the small daily task calls. The daily calls are the recurring health.
SELECT START_TIME, ROUND(TOTAL_ELAPSED_TIME/1000,1) AS ELAPSED_SEC, EXECUTION_STATUS,
       WAREHOUSE_NAME, QUERY_TAG, LEFT(REGEXP_REPLACE(QUERY_TEXT,'\\s+',' '),80) AS CALL_TEXT
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_TYPE = 'CALL'
  AND UPPER(QUERY_TEXT) LIKE '%SP_LOAD_PATTERN_COST%'
  AND START_TIME >= '2026-08-28'
ORDER BY START_TIME;
--  >>> paste RESULT [3] <<<

-- [4] Per-day runtime shape (success only). If 09-02 shows a big MAX_SEC/P95_SEC and every
--     other day is a low flat line, the "regression" is the one-time backfill, not the daily proc.
SELECT START_TIME::DATE AS DAY, COUNT(*) AS CALLS,
       ROUND(MAX(TOTAL_ELAPSED_TIME)/1000,1) AS MAX_SEC,
       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME,0.95)/1000,1) AS P95_SEC,
       ROUND(AVG(TOTAL_ELAPSED_TIME)/1000,1) AS AVG_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_TYPE = 'CALL'
  AND UPPER(QUERY_TEXT) LIKE '%SP_LOAD_PATTERN_COST%'
  AND EXECUTION_STATUS = 'SUCCESS'
  AND START_TIME >= '2026-08-25'
GROUP BY 1
ORDER BY 1;
--  >>> paste RESULT [4] <<<
