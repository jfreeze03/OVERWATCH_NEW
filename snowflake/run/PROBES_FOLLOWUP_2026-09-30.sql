-- =====================================================================================
--  PROBES_FOLLOWUP_2026-09-30.sql  --  Next-Fifty follow-up probes after the 2026-09-29 run (2026-09-30)
--  WHAT: the reads the 2026-09-29 answers still leave open (#41 / #37 Cortex Code Desktop and AI metering,
--  #37 quota vs fact, #40 email-only CRITICAL volume, #33 the 1800 s cancels on WH_ALFA_ADMIN, #43 masking
--  per database and per tag, #42 Part 2 the database list, #34 the client-version read as the app owner).
--  RUN AS SNOW_ACCOUNTADMINS: pick it in the worksheet's role menu before running (or run USE ROLE
--  SNOW_ACCOUNTADMINS on its own first). This file never switches role itself.
--  READ-ONLY: SELECT / WITH / SHOW / RESULT_SCAN only, plus ALTER SESSION SET / UNSET TIMEZONE (this
--  worksheet's clock). No DML, no DDL, no CALL, no GRANT: nothing in the account is created, changed or granted.
--  Paste back every grid (an empty grid is an answer: say 'no rows'). Long cells get cut off in photos, so
--  download the grid as CSV or copy the cell text instead.
--  RUN ALL STOPS AT THE FIRST ERROR. An error in a block is that block's answer: copy the error text as the
--  answer, then highlight from the NEXT block to the end of the file and run again.
--  PAIR: F8's SHOW DATABASES and the SELECT after it (it reads the SHOW through RESULT_SCAN(LAST_QUERY_ID())).
--  Run them back to back.
--  HEAVIEST: F6 (30 days of QUERY_HISTORY for one warehouse), then F7a and F8 (ACCOUNT_USAGE.TABLES) and F7b
--  (TAG_REFERENCES). Everything else takes seconds.
--
--  PART 1 -- STILL UNRUN in PROBES_NEXT_FIFTY_WAVE4.sql. Run these from THAT file, in its own worksheet: first
--  run its lines 29-30 (USE ROLE SNOW_ACCOUNTADMINS + the Central clock), then highlight each range and run it.
--  The same Run All rule applies there: on an error, copy it as that block's answer and go on from the next block.
--    S2a, S2b       l.464-478    #43 Phase 2: does DATA_CLASSIFICATION_LATEST hold rows, how fresh, which words.
--                                Rows = the sensitive-column gap list comes from classification. 0 rows or an
--                                error = it comes from a column-name list labelled HEURISTIC.
--    S3b, S3a, S3c  l.480-526    #44a: S3b = the app's own Trust Center snapshot days (enough history for #44b's
--                                regression diff?). S3a = the TRUST_CENTER_VIEWER grant and the retained scan
--                                days. S3c = is AT_RISK_ENTITIES the full list or a sample (can #44a's entity
--                                drill be built, and is it labelled 'all' or 'sample').
--    S4b            l.567-585    #37 thresholds: the users over 15 credits a day and LAST_DAY_OVER_15. Decides
--                                whether the 19 over-15 user-days predate the 15-credit AI_USER_USAGE quota or
--                                show it not holding (V163 [28]'s wording and threshold).
--    S5a-S5i        l.616-691    #41: S5a the AI metering lines (materiality), S5b AI Functions volume, S5d
--                                QUERY_ID -> user attribution (heavy, 14 days), S5c USER_ID on AI Functions,
--                                S5e-S5i Agent / Intelligence / Analyst / Search daily / Search serving row
--                                counts. Decides which #41 Phase A / Phase B arms get built. S0a showed all five
--                                S5e-S5i views exist, so an error there is a grant or subscription problem.
--    C1a, C1b       l.950-1019   #29: fail-safe + time-travel dollars on staging-named schemas, live vs dropped
--                                copies, and the table list. Decides #29's size, whether it needs a read that
--                                includes dropped rows, and where the k threshold sits.
--    C3             l.1021-1022  #45: no SQL there. Re-run snowflake/run/DIAG_CS_SELF_COST.sql from the runbox
--                                branch in its own worksheet: fresh before-numbers for the loader scan diet
--                                (the last baseline is the 2026-09-26 run).
--    P1a, P1b       l.1204-1342  #31 check: the booked savings the daily scan saw undone, and the totals.
--                                VERIFIED_ACTIVE_MONTHLY_USD and ESTIMATED_OPEN_USD must equal Proof's ROI
--                                numerator and open pipeline on the deployed app (a mismatch is a bug to chase).
--    P2a-P2e        l.1344-1423  #14: which CONTROL_STATUS the app reads (FALLBACK_USED), the newest failed rows
--                                as JSON, each column's fill rate on failed vs other rows, DESCRIBE. Decides the
--                                column to show as the Informatica error text. P2a's SET feeds P2b-P2e, and
--                                P2e reads P2d's result, so run P2d and P2e back to back.
--    P3             l.1425-1600  #14: the Tonight drill's session linkage vs QUERY_ATTRIBUTION_HISTORY. MUST BE
--                                RE-RUN: the 09-29 result is past its RESULT_SCAN window. Send ALL columns: row
--                                1's 19 totals and every row's QAH / MISS / CLIPPED columns, not the left half.
--  Ranges 464-526, 616-691, 950-1019 and 1204-1600 are each contiguous: highlight the whole range and run it.
--  Not asked for this round: that file's other never-run blocks (W4b, W4d, W4e, W2c, W5c, S4c, S6, S7a-S7d,
--  C2a-C2d, C4c).
--
--  PART 2 -- NEW BLOCKS IN THIS FILE, safest first, risky last. The ids are the ask list's, the order is by risk:
--    F5  #40  CRITICALs no enabled route delivered, per company (own tables only)
--    F3  #41  every METERING_DAILY_HISTORY service type, 90 days (S5a without the AI filter)
--    F6  #33  who / what hit the 1800 s timeout on WH_ALFA_ADMIN, 30 days (error 630)
--    F8  #42  the database list with storage, table counts, owner and comment (SHOW + SELECT pair)
--    F4  #37  quota blocks vs FACT_AI_USAGE_DAILY on the same day
--    F1  #41  the Desktop and CLI Cortex Code column lists, one row per column
--    F7a #43  masking references per database (qualified counts, via-tag count read tolerantly)
--    F7b #43  RISKY: per masking tag, the tables / columns / databases carrying it (TAG_REFERENCES)
--    F2  #41  RISKY: Cortex Code Desktop sizing, 90 days (a view the repo has never read)
--    F9  #34  comments only: SYSTEM$CLIENT_VERSION_INFO() as the app's owner role, in another worksheet
--  No session variables are used, so any block can be re-run on its own (F8 as its SHOW + SELECT pair).
-- =====================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- OVERWATCH's NTZ stamps are Central, and CURRENT_DATE() windows match the app

-- F5 (#40) CRITICAL alerts of the last 30 days that NO enabled route delivered, per COMPANY. This is the
-- "email-only" volume the V164 choice is about (A: set OVERWATCH_EMAIL's DEFAULT_RECIPIENTS, B: seed
-- ESCALATE_EMAIL_INTEGRATION ''). "Delivered" = an ALERT_DELIVERIES row (V022:27-33, one row per SUCCESSFUL
-- send) on a route that is ENABLED now, which is exactly V164's EMAIL_ONLY test (V164:338-342).
-- ALERT_EVENTS columns per V004:38-52 (EVENT_ID, RULE_ID, RAISED_AT, COMPANY, SEVERITY, STATUS, ACK_AT),
-- ALERT_ROUTES per V012:16-24 (ROUTE_ID, ENABLED). Reads OVERWATCH tables only and no V164 column, so it runs
-- the same before or after V164 is applied. RAISED_AT is TIMESTAMP_NTZ in Central (the clock pinned above).
-- DECIDES: EMAIL_ONLY_PER_WEEK is roughly how many extra emails a week choice A would send. It is an upper
-- bound: V164 also needs the event still OPEN, unacknowledged for 120+ min, not in an acknowledged incident and
-- not snoozed. TREXIS / UNKNOWN rows are the CRITICALs the ALFA-only Teams route never carries: under choice B
-- they never escalate at all. EMAIL_ONLY_NEVER_DELIVERED separates "no route matched" from "delivered only by a
-- route that is disabled now".
WITH e AS (
    SELECT ev.COMPANY, ev.RULE_ID, ev.STATUS, ev.ACK_AT,
           rd.EVENT_ID IS NULL AS EMAIL_ONLY,
           ad.EVENT_ID IS NULL AS NEVER_DELIVERED_ANY_ROUTE
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
    LEFT JOIN (SELECT DISTINCT d.EVENT_ID
               FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
               JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
                 ON r.ROUTE_ID = d.ROUTE_ID AND r.ENABLED) rd
      ON rd.EVENT_ID = ev.EVENT_ID
    LEFT JOIN (SELECT DISTINCT EVENT_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES) ad
      ON ad.EVENT_ID = ev.EVENT_ID
    WHERE ev.SEVERITY = 'CRITICAL'
      AND ev.RAISED_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)
)
SELECT IFF(GROUPING(COMPANY) = 1, '(all companies)', COMPANY) AS ALERT_COMPANY,
       COUNT(*) AS CRITICALS_30D,
       COUNT_IF(NOT EMAIL_ONLY) AS DELIVERED_BY_ENABLED_ROUTE,
       COUNT_IF(EMAIL_ONLY) AS EMAIL_ONLY_30D,
       ROUND(COUNT_IF(EMAIL_ONLY) * 7 / 30, 1) AS EMAIL_ONLY_PER_WEEK,
       COUNT_IF(EMAIL_ONLY AND NEVER_DELIVERED_ANY_ROUTE) AS EMAIL_ONLY_NEVER_DELIVERED,
       COUNT_IF(EMAIL_ONLY AND STATUS = 'OPEN') AS EMAIL_ONLY_STILL_OPEN,
       COUNT_IF(EMAIL_ONLY AND ACK_AT IS NULL) AS EMAIL_ONLY_NEVER_ACKED,
       COUNT(DISTINCT IFF(EMAIL_ONLY, RULE_ID, NULL)) AS EMAIL_ONLY_RULES,
       LEFT(LISTAGG(DISTINCT IFF(EMAIL_ONLY, RULE_ID, NULL), ', '), 300) AS EMAIL_ONLY_RULE_IDS,
       GROUPING(COMPANY) AS IS_TOTAL_ROW
FROM e
GROUP BY GROUPING SETS ((COMPANY), ())
ORDER BY IS_TOTAL_ROW DESC, CRITICALS_30D DESC;

-- F3 (#41) S5a WITHOUT the AI filter: every METERING_DAILY_HISTORY SERVICE_TYPE's billed credits over 90 days.
-- The columns and the billed expression are cost_sql._BILLED and metering_daily_by_service (cost_sql.py:30-58:
-- SERVICE_TYPE, USAGE_DATE, CREDITS_BILLED, CREDITS_USED, CREDITS_USED_COMPUTE, CREDITS_USED_CLOUD_SERVICES,
-- CREDITS_ADJUSTMENT_CLOUD_SERVICES). IS_AI_FAMILY is common.AI_SERVICE_TOKENS (common.py:185), the test that
-- decides whether the app prices a line at the AI rate or the compute rate.
-- DECIDES: (1) which SERVICE_TYPE line(s) Cortex Code Snowsight / CLI / Desktop bill under (compare with S4a's
-- 2,165.20 Snowsight + CLI credits over the same 90 days, and with F2's Desktop credits). (2) An AI-looking line
-- with IS_AI_FAMILY = FALSE is priced at the compute rate everywhere (common.py:185, V148): a pricing defect to
-- fix. (3) The materiality gate for #41 Phase A (AI Functions) and Phase B (Agent / Intelligence / Analyst / Search).
WITH m AS (
    SELECT UPPER(COALESCE(SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
           COALESCE(SERVICE_TYPE, '') ILIKE ANY ('%CORTEX%', 'AI%', '%INTELLIGENCE%', '%COCO%', '%COWORK%') AS IS_AI_FAMILY,
           USAGE_DATE,
           COALESCE(CREDITS_BILLED, GREATEST(0, COALESCE(CREDITS_USED, 0)
                    + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0))) AS BILLED,
           COALESCE(CREDITS_USED_COMPUTE, 0) AS COMPUTE_CR,
           COALESCE(CREDITS_USED_CLOUD_SERVICES, 0) AS CLOUD_SERVICES_CR,
           COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0) AS ADJUSTMENT_CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -90, CURRENT_DATE())
)
SELECT SERVICE_TYPE,
       IS_AI_FAMILY,
       ROUND(SUM(BILLED), 2) AS CREDITS_BILLED_90D,
       ROUND(100 * SUM(BILLED) / NULLIF(SUM(SUM(BILLED)) OVER (), 0), 1) AS PCT_OF_ALL_BILLED,
       ROUND(SUM(IFF(USAGE_DATE >= DATEADD('day', -30, CURRENT_DATE()), BILLED, 0)), 2) AS CREDITS_BILLED_30D,
       ROUND(SUM(COMPUTE_CR), 2) AS CREDITS_COMPUTE_90D,
       ROUND(SUM(CLOUD_SERVICES_CR), 2) AS CREDITS_CLOUD_SERVICES_90D,
       ROUND(SUM(ADJUSTMENT_CR), 2) AS CREDITS_ADJUSTMENT_90D,
       COUNT(DISTINCT USAGE_DATE) AS DAYS_WITH_USAGE,
       MIN(USAGE_DATE) AS FIRST_DAY,
       MAX(USAGE_DATE) AS LAST_DAY
FROM m
GROUP BY 1, 2
ORDER BY CREDITS_BILLED_90D DESC;

-- F6 (#33) Who / what hit the statement timeout on WH_ALFA_ADMIN in the last 30 days (W5b: 9 cancels, ceiling
-- seen 1800 s). Error 000630 'Statement reached its statement or warehouse timeout of N second(s)' is matched and
-- N parsed exactly as W5b did (TRY_TO_NUMBER(ERROR_CODE::VARCHAR) = 630). QUERY_HISTORY columns as the repo reads
-- them: ops_sql.py:42-73 and :97-101 (EXECUTION_STATUS, ERROR_CODE, ERROR_MESSAGE, QUERY_TYPE, QUERY_TEXT,
-- TOTAL_ELAPSED_TIME, QUEUED_OVERLOAD_TIME, QUEUED_PROVISIONING_TIME), insights_sql.py:536 (QUERY_ID, USER_NAME,
-- ROLE_NAME, WAREHOUSE_NAME, DATABASE_NAME, START_TIME), cost_sql.py:1134-1135 (EXECUTION_TIME, QUERY_TAG).
-- IS_OVERWATCH_APP = common.app_self_sql (common.py:158-163): the SiS app tag (config.py:38), the 'OVERWATCH'
-- tag prefix (config.py:31) or the OVERWATCH_APP| block-comment marker in the text (built with CHR() here so
-- this file holds no comment-open sequence inside a string). TOTAL_ELAPSED includes queue time, so
-- MAX_QUEUED_SEC shows how much of the ceiling was spent waiting. Durations are shown humanized (MAX_ELAPSED).
-- DECIDES: IS_OVERWATCH_APP = TRUE rows are OVERWATCH's own statements timing out (V002 says this warehouse runs
-- the app and the telemetry tasks): an app read to fix or trim. A task / service user (e.g. SYSTEM) = a loader to
-- look at. A person = ad-hoc work, leave it. TIMEOUT_SEC_SEEN is the ceiling actually enforced (the lower of the
-- warehouse, user and session values): read it next to the deployed 'Read statement-timeout posture' panel.
WITH t AS (
    SELECT USER_NAME, ROLE_NAME,
           COALESCE(NULLIF(QUERY_TAG, ''), '(none)') AS QUERY_TAG,
           QUERY_TYPE, DATABASE_NAME, QUERY_ID, START_TIME,
           COALESCE(COALESCE(QUERY_TAG, '') LIKE 'OVERWATCH%'
                    OR CONTAINS(COALESCE(QUERY_TAG, ''), '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"')
                    OR CONTAINS(COALESCE(QUERY_TEXT, ''), CHR(47) || CHR(42) || ' OVERWATCH' || '_APP|'), FALSE) AS IS_OVERWATCH_APP,
           ROUND(TOTAL_ELAPSED_TIME / 1000)::INT AS ELAPSED_SEC,
           ROUND(COALESCE(EXECUTION_TIME, 0) / 1000)::INT AS EXEC_SEC,
           ROUND((COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000)::INT AS QUEUED_SEC,
           TRY_TO_NUMBER(REPLACE(REGEXP_SUBSTR(ERROR_MESSAGE, 'timeout of ([0-9,]+) second', 1, 1, 'e', 1), ',', '')) AS TIMEOUT_SEC,
           LEFT(REGEXP_REPLACE(COALESCE(QUERY_TEXT, ''), '[[:space:]]+', ' '), 160) AS QUERY_PREVIEW
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
      AND WAREHOUSE_NAME = 'WH_ALFA_ADMIN'
      AND EXECUTION_STATUS <> 'SUCCESS'
      AND TRY_TO_NUMBER(ERROR_CODE::VARCHAR) = 630
), g AS (
    SELECT USER_NAME, ROLE_NAME, QUERY_TAG, QUERY_TYPE, IS_OVERWATCH_APP,
           COUNT(*) AS TIMEOUTS_30D,
           MAX(ELAPSED_SEC) AS MAX_ELAPSED_SEC,
           MAX(EXEC_SEC) AS MAX_EXEC_SEC,
           MAX(QUEUED_SEC) AS MAX_QUEUED_SEC,
           MIN(TIMEOUT_SEC) AS TIMEOUT_SEC_SEEN_MIN,
           MAX(TIMEOUT_SEC) AS TIMEOUT_SEC_SEEN_MAX,
           LEFT(LISTAGG(DISTINCT DATABASE_NAME, ', '), 200) AS DATABASES,
           MIN(START_TIME) AS FIRST_SEEN,
           MAX(START_TIME) AS LAST_SEEN,
           MAX_BY(QUERY_ID, START_TIME) AS NEWEST_QUERY_ID,
           MAX_BY(QUERY_PREVIEW, START_TIME) AS NEWEST_QUERY_PREVIEW
    FROM t
    GROUP BY 1, 2, 3, 4, 5
)
SELECT USER_NAME, ROLE_NAME, LEFT(QUERY_TAG, 160) AS QUERY_TAG, QUERY_TYPE, IS_OVERWATCH_APP,
       TIMEOUTS_30D,
       TRIM(IFF(MAX_ELAPSED_SEC >= 3600, FLOOR(MAX_ELAPSED_SEC / 3600)::INT || ' hr ', '')
            || IFF(MAX_ELAPSED_SEC >= 60, MOD(FLOOR(MAX_ELAPSED_SEC / 60)::INT, 60) || ' min ', '')
            || MOD(MAX_ELAPSED_SEC, 60) || ' sec') AS MAX_ELAPSED,
       MAX_ELAPSED_SEC, MAX_EXEC_SEC, MAX_QUEUED_SEC,
       TIMEOUT_SEC_SEEN_MIN, TIMEOUT_SEC_SEEN_MAX,
       DATABASES, FIRST_SEEN, LAST_SEEN, NEWEST_QUERY_ID, NEWEST_QUERY_PREVIEW
FROM g
ORDER BY TIMEOUTS_30D DESC, MAX_ELAPSED_SEC DESC
LIMIT 50;

-- F8 (#42 Part 2) The database list, so the owner can map each database to a department (DEPARTMENT_MAP rows
-- with MAP_TYPE 'DATABASE', the Part 2 editor). SHOW DATABASES is the app's own inventory read
-- (security_sql.show_databases_sql, "name" read at main.py:709). ACCOUNT_USAGE.DATABASES is absent on this
-- account (security_sql.py:816). The SHOW's "owner", "comment", "kind" and "created_on" are read TOLERANTLY
-- (OBJECT_CONSTRUCT_KEEP_NULL(*) plus the key, the W2 form that ran 2026-09-29): a missing column reads NULL,
-- never an error. Storage = the newest DATABASE_STORAGE_USAGE_HISTORY day in the last 7 (cost_sql.py:499-505:
-- AVERAGE_DATABASE_BYTES includes time travel, AVERAGE_FAILSAFE_BYTES). Table counts and base-table bytes =
-- live ACCOUNT_USAGE.TABLES rows (security_sql.py:724-733 and :770-786: TABLE_CATALOG, TABLE_SCHEMA,
-- TABLE_TYPE, DELETED, BYTES). COMPANY = COMPANY_FOR_DATABASE's rule inlined (V044:36-48: a COMPANY_SCOPE
-- 'DATABASE' row first, else TRXS_* = Trexis, ALFA* or ADMIN = ALFA, else UNKNOWN), so no UDF grant is needed.
-- ENV = companies.py:77-78 and :242-246 (ALFA_EDW_PRD, ALFA_EDW_MGM or *_PRD = PROD). Dollars use SETTINGS
-- STORAGE_USD_PER_TB_MONTH (binary TiB, config.py:56, 23.00 if absent). DEPARTMENT_NOW = an existing
-- DEPARTMENT_MAP 'DATABASE' row (V008:5-13). Expect none: the editor offers only WAREHOUSE and ROLE today.
-- Run the SHOW and the SELECT back to back.
-- DECIDES: the owner writes a department next to each database (OWNER_ROLE and DB_COMMENT are the hints) for
-- #42 Part 2. An UNKNOWN COMPANY row needs a COMPANY_SCOPE row before #42's company panel can place its storage.
-- 'NOT IN SHOW' rows are dropped (or hidden from this role) databases that still billed storage this week.
SHOW DATABASES;
WITH sd AS (
    SELECT "name" AS DATABASE_NAME, OBJECT_CONSTRUCT_KEEP_NULL(*) AS O
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
), st AS (
    SELECT DATABASE_NAME, USAGE_DATE AS STORAGE_DAY,
           SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
           SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FS_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE())
    GROUP BY 1, 2
    QUALIFY STORAGE_DAY = MAX(STORAGE_DAY) OVER (PARTITION BY DATABASE_NAME)
), tb AS (
    SELECT TABLE_CATALOG AS DATABASE_NAME,
           COUNT(DISTINCT TABLE_SCHEMA) AS SCHEMAS_WITH_OBJECTS,
           COUNT_IF(TABLE_TYPE = 'BASE TABLE') AS LIVE_BASE_TABLES,
           COUNT_IF(TABLE_TYPE <> 'BASE TABLE') AS LIVE_VIEWS_AND_OTHER,
           SUM(IFF(TABLE_TYPE = 'BASE TABLE', COALESCE(BYTES, 0), 0)) AS BASE_TABLE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
    WHERE DELETED IS NULL
    GROUP BY 1
), cs AS (
    SELECT PATTERN, MAX(COMPANY) AS COMPANY
    FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
    WHERE SCOPE_TYPE = 'DATABASE'
    GROUP BY 1
), dm AS (
    SELECT UPPER(NAME) AS NAME, MAX(DEPARTMENT) AS DEPARTMENT
    FROM DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP
    WHERE MAP_TYPE = 'DATABASE'
    GROUP BY 1
), p AS (
    SELECT COALESCE(MAX(TRY_TO_DOUBLE(VALUE)), 23.0) AS USD_PER_TIB_MONTH
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
    WHERE KEY = 'STORAGE_USD_PER_TB_MONTH'
), a AS (
    SELECT COALESCE(sd.DATABASE_NAME, st.DATABASE_NAME) AS DATABASE_NAME,
           sd.DATABASE_NAME IS NOT NULL AS IN_SHOW, sd.O,
           st.STORAGE_DAY, st.DB_BYTES, st.FS_BYTES
    FROM sd
    FULL OUTER JOIN st ON st.DATABASE_NAME = sd.DATABASE_NAME
)
SELECT a.DATABASE_NAME,
       COALESCE(cs.COMPANY,
                IFF(UPPER(a.DATABASE_NAME) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis',
                    IFF(UPPER(a.DATABASE_NAME) LIKE 'ALFA%' OR UPPER(a.DATABASE_NAME) = 'ADMIN', 'ALFA', 'UNKNOWN'))) AS COMPANY,
       IFF(cs.COMPANY IS NULL, 'name rule', 'COMPANY_SCOPE row') AS COMPANY_FROM,
       IFF(UPPER(a.DATABASE_NAME) IN ('ALFA_EDW_PRD', 'ALFA_EDW_MGM') OR UPPER(a.DATABASE_NAME) LIKE '%!_PRD' ESCAPE '!',
           'PROD', 'NONPROD') AS ENV,
       IFF(a.IN_SHOW, COALESCE(a.O:"kind"::STRING, '(not reported)'), 'NOT IN SHOW (dropped or hidden)') AS KIND,
       a.O:"owner"::STRING AS OWNER_ROLE,
       LEFT(a.O:"comment"::STRING, 120) AS DB_COMMENT,
       LEFT(a.O:"created_on"::STRING, 10) AS CREATED_DAY,
       dm.DEPARTMENT AS DEPARTMENT_NOW,
       a.STORAGE_DAY,
       ROUND(a.DB_BYTES / POWER(1024, 3), 2) AS DB_GB_INCL_TIME_TRAVEL,
       ROUND(a.FS_BYTES / POWER(1024, 3), 2) AS FAILSAFE_GB,
       ROUND((COALESCE(a.DB_BYTES, 0) + COALESCE(a.FS_BYTES, 0)) / POWER(1024, 4) * p.USD_PER_TIB_MONTH, 2) AS STORAGE_USD_PER_MONTH,
       ROUND(100 * (COALESCE(a.DB_BYTES, 0) + COALESCE(a.FS_BYTES, 0))
             / NULLIF(SUM(COALESCE(a.DB_BYTES, 0) + COALESCE(a.FS_BYTES, 0)) OVER (), 0), 1) AS PCT_OF_ACCOUNT_STORAGE,
       tb.SCHEMAS_WITH_OBJECTS,
       tb.LIVE_BASE_TABLES,
       tb.LIVE_VIEWS_AND_OTHER,
       ROUND(tb.BASE_TABLE_BYTES / POWER(1024, 3), 2) AS BASE_TABLE_GB
FROM a
CROSS JOIN p
LEFT JOIN cs ON cs.PATTERN = UPPER(a.DATABASE_NAME)
LEFT JOIN dm ON dm.NAME = UPPER(a.DATABASE_NAME)
LEFT JOIN tb ON tb.DATABASE_NAME = a.DATABASE_NAME
ORDER BY COALESCE(a.DB_BYTES, 0) + COALESCE(a.FS_BYTES, 0) DESC, a.DATABASE_NAME
LIMIT 300;

-- F4 (#37 / #41) Does the native per-user AI quota count credits OVERWATCH does not load? Each quota block row of
-- the last 90 days next to FACT_AI_USAGE_DAILY's credits for that user on that day. This is the analysis's
-- optional cross-check, widened from 30 to 90 days to cover S4a's window. QUOTA_ACCESS_BLOCK_HISTORY: the app
-- reads the view (cortex_sql.py:384-386, ACTION_AT) and binds USER_NAME, QUOTA_NAME, CYCLE, ACTION, CREDITS,
-- PER_USER_LIMIT and BLOCKED_UNTIL by name (quotas.py:167-178), with the spellings from the owner's preview pinned
-- in tests/test_quotas.py:142-148. FACT_AI_USAGE_DAILY columns (DAY, USER_NAME, SOURCE, CREDITS) per
-- V027:162-171 and the V159 [9] loader. The fact's DAY is USAGE_TIME::DATE in the stamp's own offset, and the
-- quota's DAILY cycle may run on UTC (the 09-21 block ran to BLOCKED_UNTIL 00:00 UTC), so the fact is shown for
-- both the UTC day and the Central day of each block. A MONTHLY cycle compares a month, not a day: read those rows
-- as context only.
-- DECIDES: FACT credits well below QUOTA_CREDITS on the matching day = the quota counts a source the fact misses
-- (Cortex Code Desktop, or AI Functions), which supports the V166 Desktop arm (see F2). FACT at or above QUOTA =
-- the fact already sees what the quota sees. BLOCKED_UNTIL_UTC at 00:00 = the quota day is the UTC day, so
-- FACT_CREDITS_UTC_DAY is the fair comparison. The block dates also show whether S4a's 19 over-15 user-days
-- predate the quota.
WITH b AS (
    SELECT USER_NAME, ACTION_AT, QUOTA_NAME, CYCLE, ACTION, PER_USER_LIMIT, CREDITS, BLOCKED_UNTIL,
           CONVERT_TIMEZONE('UTC', ACTION_AT)::DATE AS UTC_DAY,
           ACTION_AT::DATE AS CENTRAL_DAY
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY
    WHERE ACTION_AT >= DATEADD('day', -90, CURRENT_TIMESTAMP())
), f AS (
    SELECT USER_NAME, DAY,
           SUM(COALESCE(CREDITS, 0)) AS CREDITS,
           LISTAGG(DISTINCT SOURCE, '+') AS SOURCES
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE DAY >= DATEADD('day', -92, CURRENT_DATE())
    GROUP BY 1, 2
)
SELECT b.USER_NAME, b.ACTION_AT, b.QUOTA_NAME, b.CYCLE, b.ACTION, b.PER_USER_LIMIT,
       ROUND(b.CREDITS, 2) AS QUOTA_CREDITS,
       CONVERT_TIMEZONE('UTC', b.BLOCKED_UNTIL) AS BLOCKED_UNTIL_UTC,
       b.UTC_DAY,
       ROUND(fu.CREDITS, 2) AS FACT_CREDITS_UTC_DAY,
       fu.SOURCES AS FACT_SOURCES_UTC_DAY,
       b.CENTRAL_DAY,
       ROUND(fc.CREDITS, 2) AS FACT_CREDITS_CENTRAL_DAY,
       fc.SOURCES AS FACT_SOURCES_CENTRAL_DAY,
       ROUND(b.CREDITS - GREATEST(COALESCE(fu.CREDITS, 0), COALESCE(fc.CREDITS, 0)), 2) AS QUOTA_MINUS_BEST_FACT
FROM b
LEFT JOIN f fu ON fu.USER_NAME = b.USER_NAME AND fu.DAY = b.UTC_DAY
LEFT JOIN f fc ON fc.USER_NAME = b.USER_NAME AND fc.DAY = b.CENTRAL_DAY
ORDER BY b.ACTION_AT DESC
LIMIT 200;

-- F1 (#41 / #37) The full column list of CORTEX_CODE_DESKTOP_USAGE_HISTORY next to CORTEX_CODE_CLI_USAGE_HISTORY,
-- ONE ROW PER COLUMN (S0b's LISTAGG cell was cut off in the photo). SNOWFLAKE.INFORMATION_SCHEMA.COLUMNS is not
-- read by the repo, but S0b ran on it 2026-09-29 with these same columns (TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,
-- DATA_TYPE, ORDINAL_POSITION), so this role can read it. TABLE_SCHEMA = 'ACCOUNT_USAGE' keeps the predicate
-- pushed down. OVERWATCH_READS_ON_CLI marks the CLI columns OVERWATCH reads: USER_ID, USAGE_TIME, TOKEN_CREDITS,
-- TOKENS (the V159 [9] loader, V159:877-879) and TOKENS_GRANULAR (cortex_sql.py:313-317).
-- DECIDES: every OVERWATCH_READS_ON_CLI row reading SAME = F2 below will run, and V166's Desktop arm can reuse the
-- ai_code projection verbatim (only the view name changes). A 'CLI ONLY' row among them = F2 errors with
-- "invalid identifier" and the arm needs this grid's Desktop names. TOKENS_GRANULAR on Desktop decides whether the
-- token-type panel (cortex_sql.py:292-340) gets a Desktop leg. An empty grid = neither view is visible to this role.
WITH c AS (
    SELECT COLUMN_NAME,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_DESKTOP_USAGE_HISTORY', ORDINAL_POSITION, NULL)) AS DESKTOP_POSITION,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_DESKTOP_USAGE_HISTORY', DATA_TYPE, NULL)) AS DESKTOP_TYPE,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_CLI_USAGE_HISTORY', ORDINAL_POSITION, NULL)) AS CLI_POSITION,
           MAX(IFF(TABLE_NAME = 'CORTEX_CODE_CLI_USAGE_HISTORY', DATA_TYPE, NULL)) AS CLI_TYPE
    FROM SNOWFLAKE.INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = 'ACCOUNT_USAGE'
      AND TABLE_NAME IN ('CORTEX_CODE_DESKTOP_USAGE_HISTORY', 'CORTEX_CODE_CLI_USAGE_HISTORY')
    GROUP BY COLUMN_NAME
)
SELECT COLUMN_NAME, DESKTOP_POSITION, DESKTOP_TYPE, CLI_POSITION, CLI_TYPE,
       CASE WHEN DESKTOP_TYPE IS NULL THEN 'CLI ONLY'
            WHEN CLI_TYPE IS NULL THEN 'DESKTOP ONLY'
            WHEN DESKTOP_TYPE = CLI_TYPE THEN 'SAME'
            ELSE 'TYPE DIFFERS' END AS COMPARISON,
       COLUMN_NAME IN ('USER_ID', 'USAGE_TIME', 'TOKEN_CREDITS', 'TOKENS', 'TOKENS_GRANULAR') AS OVERWATCH_READS_ON_CLI
FROM c
ORDER BY COALESCE(DESKTOP_POSITION, 1000 + CLI_POSITION), COLUMN_NAME;

-- F7a (#43 Phase 1) Masking references per DATABASE, keyed on fully qualified names only (never S1a's unqualified
-- counts), next to each database's live base-table count, so the environment-parity gaps (PROD vs DEV / SIT / SAN
-- / PHX / SEA) show up as rows. POLICY_REFERENCES columns: POLICY_KIND, REF_ENTITY_DOMAIN, REF_ENTITY_NAME and
-- POLICY_NAME are read by security_sql.admin_network_policy_coverage (security_sql.py:466-468). REF_DATABASE_NAME,
-- REF_SCHEMA_NAME, REF_COLUMN_NAME and POLICY_STATUS are not read by the repo, but S1b ran on them 2026-09-29.
-- POLICY_DB and POLICY_SCHEMA are listed in S0b's grid for this view (answers_2026-09-29.md:214). The via-tag
-- columns TAG_NAME, TAG_DATABASE and TAG_SCHEMA are docs-only and read TOLERANTLY (OBJECT_CONSTRUCT_KEEP_NULL(*),
-- the W2 form): an absent column reads NULL with HAS_TAG_NAME_COL = FALSE, never an error. Live base tables =
-- ACCOUNT_USAGE.TABLES (security_sql.py:724-733). COMPANY and ENV as in F8. A masked entity can be a view, so
-- MASKED_TABLES is not a strict subset of LIVE_BASE_TABLES. POLICY_ON_TAG_REFS counts masking policies attached to
-- a TAG stored in this database (the tag route, sized per tag in F7b), not columns of this database.
-- DECIDES: the #43 Phase 1 inventory per database and environment. A non-PROD EDW database with MASKED_COLUMNS far
-- below its PROD twin is an environment-parity row: a gap worklist item if the owner says non-PROD should be masked,
-- information only if not. VIA_TAG_COLUMN_REFS > 0 = tag-based masking shows up here as column rows (the builder
-- can count it from this view alone). 0 with HAS_TAG_NAME_COL = TRUE = it does not, and F7b's counts are the only
-- measure of the tag route.
WITH pr AS (
    SELECT UPPER(REF_ENTITY_DOMAIN) AS REF_DOMAIN,
           REF_DATABASE_NAME, REF_SCHEMA_NAME, REF_ENTITY_NAME, REF_COLUMN_NAME, POLICY_STATUS,
           POLICY_DB || '.' || POLICY_SCHEMA || '.' || POLICY_NAME AS POLICY_FQN,
           OBJECT_CONSTRUCT_KEEP_NULL(*) AS O
    FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
    WHERE POLICY_KIND = 'MASKING_POLICY'
), m AS (
    SELECT REF_DATABASE_NAME AS DATABASE_NAME,
           COUNT(*) AS MASKING_REFS,
           COUNT(DISTINCT POLICY_FQN) AS DISTINCT_POLICIES_QUALIFIED,
           COUNT(DISTINCT IFF(REF_DOMAIN <> 'TAG',
                              REF_DATABASE_NAME || '.' || REF_SCHEMA_NAME || '.' || REF_ENTITY_NAME, NULL)) AS MASKED_TABLES,
           COUNT(DISTINCT IFF(REF_DOMAIN <> 'TAG' AND REF_COLUMN_NAME IS NOT NULL,
                              REF_DATABASE_NAME || '.' || REF_SCHEMA_NAME || '.' || REF_ENTITY_NAME || '.' || REF_COLUMN_NAME,
                              NULL)) AS MASKED_COLUMNS,
           COUNT(DISTINCT IFF(REF_DOMAIN <> 'TAG', REF_DATABASE_NAME || '.' || REF_SCHEMA_NAME, NULL)) AS SCHEMAS_WITH_MASKING,
           COUNT_IF(REF_DOMAIN = 'TAG') AS POLICY_ON_TAG_REFS,
           COUNT_IF(REF_DOMAIN <> 'TAG' AND O:"TAG_NAME"::STRING IS NOT NULL) AS VIA_TAG_COLUMN_REFS,
           COUNT(DISTINCT IFF(REF_DOMAIN <> 'TAG' AND O:"TAG_NAME"::STRING IS NOT NULL,
                              COALESCE(O:"TAG_DATABASE"::STRING, '?') || '.' || COALESCE(O:"TAG_SCHEMA"::STRING, '?')
                              || '.' || O:"TAG_NAME"::STRING, NULL)) AS VIA_TAG_DISTINCT_TAGS,
           LISTAGG(DISTINCT POLICY_STATUS, ', ') AS STATUS_WORDS,
           LEFT(LISTAGG(DISTINCT POLICY_FQN, ', '), 400) AS POLICIES
    FROM pr
    GROUP BY 1
), k AS (
    SELECT BOOLOR_AGG(ARRAY_CONTAINS('TAG_NAME'::VARIANT, OBJECT_KEYS(O))) AS HAS_TAG_NAME_COL
    FROM pr
), lt AS (
    SELECT TABLE_CATALOG AS DATABASE_NAME, COUNT(*) AS LIVE_BASE_TABLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES
    WHERE DELETED IS NULL AND TABLE_TYPE = 'BASE TABLE'
    GROUP BY 1
), cs AS (
    SELECT PATTERN, MAX(COMPANY) AS COMPANY
    FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
    WHERE SCOPE_TYPE = 'DATABASE'
    GROUP BY 1
), j AS (
    SELECT COALESCE(m.DATABASE_NAME, lt.DATABASE_NAME) AS DATABASE_NAME,
           lt.LIVE_BASE_TABLES,
           m.MASKING_REFS, m.DISTINCT_POLICIES_QUALIFIED, m.MASKED_TABLES, m.MASKED_COLUMNS,
           m.SCHEMAS_WITH_MASKING, m.POLICY_ON_TAG_REFS, m.VIA_TAG_COLUMN_REFS, m.VIA_TAG_DISTINCT_TAGS,
           m.STATUS_WORDS, m.POLICIES
    FROM m
    FULL OUTER JOIN lt ON lt.DATABASE_NAME = m.DATABASE_NAME
)
SELECT j.DATABASE_NAME,
       COALESCE(cs.COMPANY,
                IFF(UPPER(j.DATABASE_NAME) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis',
                    IFF(UPPER(j.DATABASE_NAME) LIKE 'ALFA%' OR UPPER(j.DATABASE_NAME) = 'ADMIN', 'ALFA', 'UNKNOWN'))) AS COMPANY,
       IFF(UPPER(j.DATABASE_NAME) IN ('ALFA_EDW_PRD', 'ALFA_EDW_MGM') OR UPPER(j.DATABASE_NAME) LIKE '%!_PRD' ESCAPE '!',
           'PROD', 'NONPROD') AS ENV,
       COALESCE(j.LIVE_BASE_TABLES, 0) AS LIVE_BASE_TABLES,
       COALESCE(j.MASKED_TABLES, 0) AS MASKED_TABLES,
       COALESCE(j.MASKED_COLUMNS, 0) AS MASKED_COLUMNS,
       COALESCE(j.SCHEMAS_WITH_MASKING, 0) AS SCHEMAS_WITH_MASKING,
       COALESCE(j.MASKING_REFS, 0) AS MASKING_REFS,
       COALESCE(j.DISTINCT_POLICIES_QUALIFIED, 0) AS DISTINCT_POLICIES_QUALIFIED,
       COALESCE(j.POLICY_ON_TAG_REFS, 0) AS POLICY_ON_TAG_REFS,
       COALESCE(j.VIA_TAG_COLUMN_REFS, 0) AS VIA_TAG_COLUMN_REFS,
       COALESCE(j.VIA_TAG_DISTINCT_TAGS, 0) AS VIA_TAG_DISTINCT_TAGS,
       k.HAS_TAG_NAME_COL,
       j.STATUS_WORDS,
       j.POLICIES
FROM j
CROSS JOIN k
LEFT JOIN cs ON cs.PATTERN = UPPER(j.DATABASE_NAME)
ORDER BY j.DATABASE_NAME
LIMIT 300;

-- F7b (#43 Phase 1) RISKY. For each masking TAG (the POLICY_REFERENCES rows whose domain is TAG: 15 on 09-29),
-- which databases, schemas, tables and columns carry it. ACCOUNT_USAGE.TAG_REFERENCES lists DIRECT assignments
-- only: a tag set on a database, schema or table reaches that object's columns by inheritance, and those columns
-- are not listed one by one. The repo reads TAG_REFERENCES only probe=True ("UNVERIFIED on this account",
-- security_sql.py:696-699), with TAG_NAME, DOMAIN, OBJECT_DATABASE, OBJECT_SCHEMA and OBJECT_NAME
-- (security_sql.py:699, :738-740, :776-778). TAG_DATABASE, TAG_SCHEMA, COLUMN_NAME and OBJECT_DELETED are docs-only and read
-- TOLERANTLY (absent = NULL, see the HAS_* columns). Without TAG_DATABASE / TAG_SCHEMA the join falls back to the
-- tag's bare name, which can merge same-named tags from different databases (HAS_TAG_DATABASE_COL says which).
-- AN ERROR HERE = TAG_REFERENCES is not readable for this role. F7a still stands.
-- DECIDES: the size of #43's separate "tag-based masking" line (TABLES_CARRYING / COLUMNS_CARRYING per tag), and
-- which databases the tag route covers. A tag carried only in PROD databases means the non-PROD copies are not
-- masked through it.
WITH mt AS (
    SELECT REF_DATABASE_NAME AS TAG_DB, REF_SCHEMA_NAME AS TAG_SCHEMA_NAME, REF_ENTITY_NAME AS TAG_NAME,
           LISTAGG(DISTINCT POLICY_DB || '.' || POLICY_SCHEMA || '.' || POLICY_NAME, ', ') AS MASKING_POLICIES
    FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
    WHERE POLICY_KIND = 'MASKING_POLICY' AND UPPER(REF_ENTITY_DOMAIN) = 'TAG'
    GROUP BY 1, 2, 3
), tr AS (
    SELECT r.TAG_NAME, UPPER(r.DOMAIN) AS DOMAIN, r.OBJECT_DATABASE, r.OBJECT_SCHEMA, r.OBJECT_NAME,
           OBJECT_CONSTRUCT_KEEP_NULL(*) AS O
    FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES r
    WHERE r.TAG_NAME IN (SELECT TAG_NAME FROM mt)
), j AS (
    SELECT mt.TAG_DB || '.' || mt.TAG_SCHEMA_NAME || '.' || mt.TAG_NAME AS MASKING_TAG,
           mt.MASKING_POLICIES,
           tr.DOMAIN, tr.O,
           tr.O:"OBJECT_DELETED"::STRING IS NULL AS IS_LIVE,
           COALESCE(NULLIF(tr.OBJECT_DATABASE, ''), IFF(tr.DOMAIN = 'DATABASE', tr.OBJECT_NAME, NULL)) AS CARRIER_DB,
           tr.OBJECT_DATABASE || '.' || tr.OBJECT_SCHEMA || '.' || tr.OBJECT_NAME AS CARRIER_OBJECT
    FROM mt
    LEFT JOIN tr
      ON tr.TAG_NAME = mt.TAG_NAME
     AND COALESCE(tr.O:"TAG_DATABASE"::STRING, mt.TAG_DB) = mt.TAG_DB
     AND COALESCE(tr.O:"TAG_SCHEMA"::STRING, mt.TAG_SCHEMA_NAME) = mt.TAG_SCHEMA_NAME
)
SELECT MASKING_TAG,
       MASKING_POLICIES,
       COUNT(DOMAIN) AS ASSIGNMENTS,
       COUNT_IF(IS_LIVE AND DOMAIN = 'DATABASE') AS ON_DATABASES,
       COUNT_IF(IS_LIVE AND DOMAIN = 'SCHEMA') AS ON_SCHEMAS,
       COUNT_IF(IS_LIVE AND DOMAIN = 'TABLE') AS ON_TABLES,
       COUNT_IF(IS_LIVE AND DOMAIN = 'COLUMN') AS ON_COLUMNS,
       COUNT(DISTINCT IFF(IS_LIVE AND DOMAIN IN ('TABLE', 'COLUMN'), CARRIER_OBJECT, NULL)) AS TABLES_CARRYING,
       COUNT(DISTINCT IFF(IS_LIVE AND DOMAIN = 'COLUMN', CARRIER_OBJECT || '.' || O:"COLUMN_NAME"::STRING, NULL)) AS COLUMNS_CARRYING,
       COUNT(DISTINCT IFF(IS_LIVE, CARRIER_DB, NULL)) AS DATABASES_CARRYING,
       LEFT(LISTAGG(DISTINCT IFF(IS_LIVE, CARRIER_DB, NULL), ', '), 300) AS CARRIER_DATABASES,
       COUNT_IF(DOMAIN IS NOT NULL AND NOT IS_LIVE) AS ASSIGNMENTS_ON_DROPPED,
       BOOLOR_AGG(ARRAY_CONTAINS('TAG_DATABASE'::VARIANT, OBJECT_KEYS(O))) AS HAS_TAG_DATABASE_COL,
       BOOLOR_AGG(ARRAY_CONTAINS('COLUMN_NAME'::VARIANT, OBJECT_KEYS(O))) AS HAS_COLUMN_NAME_COL,
       BOOLOR_AGG(ARRAY_CONTAINS('OBJECT_DELETED'::VARIANT, OBJECT_KEYS(O))) AS HAS_OBJECT_DELETED_COL
FROM j
GROUP BY 1, 2
ORDER BY TABLES_CARRYING DESC, MASKING_TAG
LIMIT 100;

-- F2 (#41 / #37) RISKY. Cortex Code DESKTOP sizing over 90 days. The repo never reads this view (S0a shows it
-- exists, created 2026-06-09). The columns are the CLI view's as the V159 [9] loader reads them (USER_ID,
-- USAGE_TIME, TOKEN_CREDITS, TOKENS: V159:877-879, cortex_sql.py:25-27), assumed and not verified for Desktop
-- (F1 above checks them). AN ERROR HERE IS ITSELF THE ANSWER: 002139, "does not exist or not authorized" or an
-- unknown-object error = no Desktop subscription or grant: close the Desktop item and never union this view.
-- "invalid identifier" = the columns differ: send F1's grid.
-- DECIDES: 0 rows = a label-only change ('Snowsight + CLI' stays true). CREDITS_90D > 0 = compare it with S4a's
-- 2,165.20 (Snowsight + CLI, same 90 days) and build V166's Desktop arm [9c] plus its 120-day backfill before
-- tuning V163 [28]'s threshold. USER_DAYS_OVER_15_DESKTOP_ALONE > 0 = Desktop alone crosses the 15-credit line.
WITH d AS (
    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS, TOKENS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_DESKTOP_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
), ud AS (
    SELECT USER_ID, USAGE_TIME::DATE AS DAY, SUM(COALESCE(TOKEN_CREDITS, 0)) AS CREDITS
    FROM d
    GROUP BY 1, 2
), a AS (
    SELECT COUNT(*) AS ROWS_90D,
           COUNT(DISTINCT USER_ID) AS USERS_90D,
           COUNT(DISTINCT USAGE_TIME::DATE) AS DAYS_WITH_USAGE,
           ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 4) AS CREDITS_90D,
           ROUND(SUM(IFF(USAGE_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP()), COALESCE(TOKEN_CREDITS, 0), 0)), 4) AS CREDITS_30D,
           SUM(COALESCE(TOKENS, 0)) AS TOKENS_90D,
           MIN(USAGE_TIME) AS FIRST_TS,
           MAX(USAGE_TIME) AS LAST_TS
    FROM d
), u AS (
    SELECT COUNT(*) AS USER_DAYS,
           COUNT_IF(CREDITS > 15) AS USER_DAYS_OVER_15_DESKTOP_ALONE,
           ROUND(MAX(CREDITS), 2) AS MAX_USER_DAY_CR
    FROM ud
)
SELECT a.ROWS_90D, a.USERS_90D, a.DAYS_WITH_USAGE, a.CREDITS_90D, a.CREDITS_30D, a.TOKENS_90D,
       a.FIRST_TS, a.LAST_TS, u.USER_DAYS, u.USER_DAYS_OVER_15_DESKTOP_ALONE, u.MAX_USER_DAY_CR
FROM a
CROSS JOIN u;

-- F9 (#34) NOT FOR THIS WORKSHEET: a manual check, in a NEW worksheet so this one keeps its role. Nothing below
-- runs here (every line is a comment).
--   The app runs with its OWNER's rights (DEPLOYMENT.md:463 has SNOW_SYSADMINS own it, and :504), but W3 ran
--   SYSTEM$CLIENT_VERSION_INFO() as SNOW_ACCOUNTADMINS. To see what the Clients tab will get, in the new worksheet:
--     1. SHOW STREAMLITS IN SCHEMA DBA_MAINT_DB.OVERWATCH        (the "owner" column of OVERWATCH_APP is the role)
--     2. USE ROLE <that owner role>
--     3. SELECT SYSTEM$CLIENT_VERSION_INFO()
--   Send the result (copy the JSON cell as text) or the error text, then close that worksheet.
--   DECIDES: a result = the Clients tab's support column works under the app's rights, and the JSON is the
--   parser's test fixture (the real clientId / clientAppId / deprecatedVersions spellings W4c only confirmed
--   indirectly). An error = the tab shows 'unavailable' and falls back to the in-account STATUS column.

-- ---------------- end of file.
ALTER SESSION UNSET TIMEZONE;   -- gives this worksheet back its own clock (if Run All stopped earlier, run this line on its own)
