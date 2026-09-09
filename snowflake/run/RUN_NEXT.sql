-- =====================================================================
--  OVERWATCH -- RUN_NEXT.sql   (DIAGNOSTIC: ETL cost-attribution join key)
--
--  GOAL: decide how to tie a CONTROL_STATUS child task (an SP_* stored-proc CALL)
--  to Snowflake QUERY_HISTORY, so OVERWATCH can attribute CREDITS / $ per ETL task,
--  mapping, workflow and run. The results pick the join design:
--    * if a QUERY_TAG carries the PRCS_ID / RUN_ID  -> clean, precise join
--    * else match by the task (proc) name in QUERY_TEXT + the task's [start,end]
--      window (+ the ETL warehouse)                 -> still solid, a bit fuzzier.
--
--  READ-ONLY. Run All as SNOW_ACCOUNTADMINS (ACCOUNT_USAGE needs the admin role).
--  Anchored on a known 09-09 run of WF_SP_SEMANTIC_PC (RUN_ID dd1ecdac...,
--  PRCS_ID 874525, task SP_F_PREM_TSACTN ran 02:11 -> 02:47). Edit the RUN_ID /
--  timestamps below to point at a different run. Paste back each RESULT block.
--
--  (The prior V125->V137 owner-apply handoff you already ran is preserved in git
--   history on the runbox branch, commit f6d2857 -- nothing here re-applies it.)
-- =====================================================================

USE ROLE SNOW_ACCOUNTADMINS;      -- ACCOUNT_USAGE needs the admin role
USE WAREHOUSE WH_ALFA_QUERY;      -- or any warehouse you can use for these reads

-- ---- 1) What Snowflake queries ran during ONE task's window? --------------
-- Look at what executed while SP_F_PREM_TSACTN ran: the warehouse, the QUERY_TAG,
-- and whether the query text is (or contains) the proc CALL.
-- RESULT:
SELECT QUERY_ID, START_TIME, WAREHOUSE_NAME, ROLE_NAME, QUERY_TAG,
       EXECUTION_STATUS, ROUND(TOTAL_ELAPSED_TIME/1000.0, 1) AS ELAPSED_S,
       LEFT(QUERY_TEXT, 160) AS QUERY_TEXT
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE START_TIME BETWEEN '2026-09-09 02:11:00' AND '2026-09-09 02:47:40'
   AND (QUERY_TEXT ILIKE '%SP_F_PREM_TSACTN%'
        OR QUERY_TAG ILIKE '%874525%' OR QUERY_TAG ILIKE '%dd1ecdac%')
 ORDER BY START_TIME
 LIMIT 100;

-- ---- 2) Does ANY QUERY_TAG that night carry the PRCS_ID / RUN_ID / workflow? --
-- If rows come back, the ETL tags its queries -> the cost join is trivial + exact.
-- If empty, the tag doesn't carry it and we join by proc-name text + time window.
-- RESULT:
SELECT QUERY_TAG, COUNT(*) AS QUERIES,
       MIN(START_TIME) AS FIRST_SEEN, MAX(START_TIME) AS LAST_SEEN
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
 WHERE START_TIME BETWEEN '2026-09-09 00:00:00' AND '2026-09-09 06:00:00'
   AND NULLIF(QUERY_TAG, '') IS NOT NULL
   AND (QUERY_TAG ILIKE '%874525%' OR QUERY_TAG ILIKE '%dd1ecdac%'
        OR QUERY_TAG ILIKE '%SEMANTIC%')
 GROUP BY QUERY_TAG
 ORDER BY QUERIES DESC
 LIMIT 50;

-- ---- 3) PROOF-OF-CONCEPT JOIN: CONTROL_STATUS SP_ tasks -> QUERY_HISTORY ---
-- Match each child task to the queries that ran inside its [start,end] window
-- AND whose text mentions the task (proc) name. Non-zero MATCHED_QUERIES means the
-- text+window join works and OVERWATCH can attach a QUERY_ID (then cost) per task.
-- RESULT:
SELECT cs.WORKFLOW_NAME, cs.TASK_NAME, cs.TASK_START_DTTM, cs.TASK_END_DTTM,
       COUNT(qh.QUERY_ID)          AS MATCHED_QUERIES,
       ANY_VALUE(qh.WAREHOUSE_NAME) AS WH,
       ANY_VALUE(qh.QUERY_TAG)      AS SAMPLE_TAG
  FROM ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS cs
  LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
    ON qh.START_TIME >= '2026-09-09 02:00:00' AND qh.START_TIME < '2026-09-09 03:00:00'
   AND qh.START_TIME BETWEEN cs.TASK_START_DTTM AND cs.TASK_END_DTTM
   AND qh.QUERY_TEXT ILIKE '%' || cs.TASK_NAME || '%'
 WHERE cs.RUN_ID = 'dd1ecdac-ea2e-4ad7-bcce-df5b164c6c0a'
   AND STARTSWITH(cs.TASK_NAME, 'SP_')
 GROUP BY cs.WORKFLOW_NAME, cs.TASK_NAME, cs.TASK_START_DTTM, cs.TASK_END_DTTM
 ORDER BY cs.TASK_START_DTTM
 LIMIT 100;

-- ---- 4) Are CREDITS attributable per query? (the cost source) -------------
-- QUERY_ATTRIBUTION_HISTORY gives credits per query, with ROOT_QUERY_ID so a
-- CALL's whole query tree rolls up. Do the run-window queries carry credits?
-- RESULT:
SELECT qah.QUERY_ID, qah.ROOT_QUERY_ID,
       ROUND(qah.CREDITS_ATTRIBUTED_COMPUTE, 6) AS CREDITS,
       LEFT(qh.QUERY_TEXT, 100) AS QUERY_TEXT
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY qah
  JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh ON qh.QUERY_ID = qah.QUERY_ID
 WHERE qah.START_TIME BETWEEN '2026-09-09 02:11:00' AND '2026-09-09 02:47:40'
   AND qh.QUERY_TEXT ILIKE '%SP_F_PREM_TSACTN%'
 ORDER BY qah.CREDITS_ATTRIBUTED_COMPUTE DESC
 LIMIT 50;

-- =====================================================================
--  Paste back the RESULT blocks:
--    #2  -> is the join tag-clean (exact) or do we use text+window?
--    #3  -> proves the text+window join actually matches tasks to queries.
--    #4  -> confirms credits are attributable per query (the $ source).
--  Then I'll build the ETL cost-attribution builder: credits/$ per task ->
--  mapping -> workflow -> run, plus cost drift on the same engine as runtime drift.
-- =====================================================================
