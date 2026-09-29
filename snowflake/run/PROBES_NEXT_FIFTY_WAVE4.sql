-- =====================================================================================
--  PROBES_NEXT_FIFTY_WAVE4.sql  --  Next-Fifty remaining-backlog read-only probes (2026-09-29)
--  READ-ONLY (the ALTER SESSION only sets this worksheet's clock). Separate from RUN_NEXT.sql.
--  Only SELECT / SHOW / DESCRIBE / RESULT_SCAN / SYSTEM$ reads / SET of session variables: no DML, no DDL,
--  nothing in the account is created, changed or granted.
--  Run top to bottom in ONE worksheet; paste back every grid (an empty grid is an answer: say 'no rows').
--  What each group previews, and the backlog rank it unblocks:
--    W (#38 #48 #34 #33)      multi-cluster fleet + clusters really used / Gen1-Gen2 mix / client drivers, the Informatica
--                             fingerprint + Snowflake's support floor / account + per-warehouse statement-timeout exposure
--    S (#43 #44 #37 #41 #39)  policy + classification coverage / Trust Center retention + entity arrays / per-user AI days
--                             over 15 cr / AI Functions attribution + per-feature AI views / login bursts + admin grants
--    C (#30 #29 #45 #40)      maintenance credits on unread objects / fail-safe on staging schemas / C3 = re-run
--                             DIAG_CS_SELF_COST.sql / notification integrations + routes + a manual ?page= deep-link test
--    P (#31 #14)              reverted savings leaving Proof's ROI numerator / CONTROL_STATUS error-text column + the
--                             Tonight drill's SESSION_ID child linkage vs QUERY_ATTRIBUTION_HISTORY
--  HEAVIEST: C2c (one pass over up to 90 days of ACCESS_HISTORY -- if it runs past ~10 min, cancel it, change
--  C2_DAYS to 30 and re-run from its SET line). Next: W5b (30 days of QUERY_HISTORY, every warehouse), then W1c and
--  S5d (14 days of QUERY_HISTORY), P3 (3 days of QUERY_HISTORY + QUERY_ATTRIBUTION_HISTORY), S4c (~20-30 s, live
--  Cortex Code views), W4 / W4c (30 days of SESSIONS), S6 (30 days of LOGIN_HISTORY). Everything else takes seconds.
--  ERRORS ARE ANSWERS: a block that errors because a view, column or function does not exist in this account (or
--  this role cannot see it) is itself the answer. Copy the error text as that block's answer and KEEP GOING.
--  Snowsight 'Run All' stops at the first error: highlight from the NEXT block to the end of the file and run
--  again. Every group puts its risky blocks last, and group W's risky tail sits before group S, so expect to resume.
--  PAIRS (the second reads the first through RESULT_SCAN / LAST_QUERY_ID(), run them back to back): W1's SHOW + SET
--  (every later W block reads $W_SHOW_QID), S0a's SHOW + SELECT, C4a's SHOW + WITH, C2c then C2d, P2d then P2e.
--  Session variables ($W_SHOW_QID, $C2_DAYS, $CTL_FQN) live only in this worksheet session: after a reconnect,
--  re-run W1's SHOW + SET before any other W block, and P2a's SET before P2b-P2e (P3 sets its own).
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';   -- NTZ stamps are Central; a UTC worksheet skews every age by 5-6h

-- =====================================================================================
--  GROUP W  --  warehouses + clients (Next-Fifty #38 / #48 / #34 / #33)   2026-09-29
--  READ-ONLY: SELECT / SHOW / DESCRIBE / RESULT_SCAN / SYSTEM$ reads / one SET of a session
--  variable / ALTER SESSION SET TIMEZONE. No DML, no DDL, nothing is changed.
--  Run top to bottom, paste back every grid (an empty grid is an answer: say 'no rows').
--  The last six blocks (W1c, W3, W3b, W4c, W4e, W2c) use a column or a function this repo has never
--  read, so they are last on purpose. If one errors, the error text IS the answer. Everything above still stands.
--  HEAVIEST: W5b (30 days of ACCOUNT_USAGE.QUERY_HISTORY aggregated per warehouse), then W1c
--  (14 days of QUERY_HISTORY for the multi-cluster warehouses only) and W4 / W4c (30 days of SESSIONS).
-- =====================================================================================

-- W1 (#38) How many warehouses are multi-cluster (MAX or MIN cluster count > 1), out of how many?
-- SHOW is the only warehouse-settings source here (no ACCOUNT_USAGE.WAREHOUSES, V109 snapshot proc).
-- The SET pins THIS show's query id so every later RESULT_SCAN($W_SHOW_QID) reads it, even after
-- other statements have run (LAST_QUERY_ID() would move on). $W_SHOW_QID lives only in this worksheet
-- session: if you reopen the worksheet or run a later block in a fresh session, run the SHOW + SET first.
-- SHOW lists only the warehouses this role can see. Columns: V109:43-55 reads "name",
-- "size", "auto_suspend", "min_cluster_count", "max_cluster_count", "scaling_policy", "type".
SHOW WAREHOUSES;
SET W_SHOW_QID = LAST_QUERY_ID();
SELECT COUNT(*) AS ALL_WAREHOUSES,
       COUNT_IF((TRY_TO_NUMBER("max_cluster_count"::VARCHAR) > 1 OR TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1)) AS MULTI_CLUSTER,
       COUNT_IF(TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1) AS MIN_CLUSTERS_GT_1,
       COUNT_IF((TRY_TO_NUMBER("max_cluster_count"::VARCHAR) > 1 OR TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1) AND UPPER("scaling_policy") = 'STANDARD') AS MC_STANDARD_POLICY,
       COUNT_IF((TRY_TO_NUMBER("max_cluster_count"::VARCHAR) > 1 OR TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1) AND UPPER("scaling_policy") = 'ECONOMY') AS MC_ECONOMY_POLICY,
       SUM(IFF((TRY_TO_NUMBER("max_cluster_count"::VARCHAR) > 1 OR TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1), TRY_TO_NUMBER("max_cluster_count"::VARCHAR), 0)) AS SUM_MAX_CLUSTERS_MC,
       COUNT_IF("max_cluster_count" IS NULL) AS MAX_CLUSTER_NULL   -- non-zero = edition/type without multi-cluster
FROM TABLE(RESULT_SCAN($W_SHOW_QID));

-- W1b (#38) The multi-cluster warehouses themselves, with 30-day metered credits (the fleet the
-- scale-in advisor would act on). WAREHOUSE_METERING_HISTORY.CREDITS_USED as in change_impact_sql.py:192-193.
-- The CLOUD_SERVICES_ONLY pseudo-warehouse is excluded from the percentage base (as mart27_sql.py:253 does).
WITH mc AS (
  SELECT "name" AS WAREHOUSE_NAME, "size" AS WAREHOUSE_SIZE, "type" AS WAREHOUSE_TYPE,
         TRY_TO_NUMBER("min_cluster_count"::VARCHAR) AS MIN_CLUSTERS,
         TRY_TO_NUMBER("max_cluster_count"::VARCHAR) AS MAX_CLUSTERS,
         "scaling_policy" AS SCALING_POLICY,
         TRY_TO_NUMBER("auto_suspend"::VARCHAR) AS AUTO_SUSPEND_SEC
  FROM TABLE(RESULT_SCAN($W_SHOW_QID))
  WHERE (TRY_TO_NUMBER("max_cluster_count"::VARCHAR) > 1 OR TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1)),
m AS (
  SELECT WAREHOUSE_NAME, SUM(CREDITS_USED) AS CREDITS_30D
  FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
  WHERE START_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
  GROUP BY 1)
SELECT mc.WAREHOUSE_NAME, mc.WAREHOUSE_SIZE, mc.WAREHOUSE_TYPE, mc.MIN_CLUSTERS, mc.MAX_CLUSTERS,
       mc.SCALING_POLICY, mc.AUTO_SUSPEND_SEC, mc.MIN_CLUSTERS > 1 AS ALWAYS_ON_EXTRA_CLUSTERS,
       ROUND(m.CREDITS_30D, 1) AS CREDITS_30D,
       ROUND(100 * m.CREDITS_30D / NULLIF((SELECT SUM(CREDITS_30D) FROM m), 0), 1) AS PCT_OF_ALL_WH_CREDITS_30D
FROM mc
LEFT JOIN m ON m.WAREHOUSE_NAME = mc.WAREHOUSE_NAME
ORDER BY m.CREDITS_30D DESC NULLS LAST, mc.WAREHOUSE_NAME;

-- W2 (#48) Does SHOW WAREHOUSES carry a "resource_constraint" column here, and how many warehouses are
-- Gen2? This form NEVER errors: it turns each SHOW row into an object (keeping NULLs) and asks for the
-- key, so a missing column reads FALSE / 0 instead of failing. Snowflake's spelling (docs, unverified
-- here -- the repo has 0 reads of it): RESOURCE_CONSTRAINT = STANDARD_GEN_1 | STANDARD_GEN_2 for standard
-- warehouses (MEMORY_* for Snowpark-optimized). Also checks a "generation" column (newer docs describe
-- GENERATION = '1' | '2', unverified). SHOW_WAREHOUSES_COLUMNS lists every column this account's SHOW returns.
WITH w AS (SELECT OBJECT_CONSTRUCT_KEEP_NULL(*) AS O FROM TABLE(RESULT_SCAN($W_SHOW_QID)))
SELECT COUNT(*) AS WAREHOUSES,
       BOOLOR_AGG(ARRAY_CONTAINS('resource_constraint'::VARIANT, OBJECT_KEYS(O))) AS HAS_RESOURCE_CONSTRAINT_COL,
       BOOLOR_AGG(ARRAY_CONTAINS('generation'::VARIANT, OBJECT_KEYS(O))) AS HAS_GENERATION_COL,
       COUNT_IF(UPPER(O:"resource_constraint"::STRING) LIKE '%GEN~_2%' ESCAPE '~') AS GEN2_BY_RESOURCE_CONSTRAINT,
       COUNT_IF(UPPER(O:"resource_constraint"::STRING) LIKE '%GEN~_1%' ESCAPE '~') AS GEN1_BY_RESOURCE_CONSTRAINT,
       COUNT_IF(O:"resource_constraint"::STRING IS NULL) AS RESOURCE_CONSTRAINT_NULL_OR_ABSENT,
       COUNT_IF(O:"generation"::STRING = '2') AS GEN2_BY_GENERATION_COL,
       ANY_VALUE(ARRAY_TO_STRING(OBJECT_KEYS(O), ', ')) AS SHOW_WAREHOUSES_COLUMNS
FROM w;

-- W2b (#48) Gen1/Gen2 mix by type and size, with 30-day credits (tolerant, never errors): which sizes are
-- already Gen2, and how much spend sits on each generation.
WITH w AS (
  SELECT "name" AS WAREHOUSE_NAME, "type" AS WAREHOUSE_TYPE, "size" AS WAREHOUSE_SIZE,
         OBJECT_CONSTRUCT_KEEP_NULL(*) AS O
  FROM TABLE(RESULT_SCAN($W_SHOW_QID))),
m AS (
  SELECT WAREHOUSE_NAME, SUM(CREDITS_USED) AS CREDITS_30D
  FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
  WHERE START_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
  GROUP BY 1)
SELECT w.WAREHOUSE_TYPE,
       COALESCE(w.O:"resource_constraint"::STRING, '(null / absent)') AS RESOURCE_CONSTRAINT,
       COALESCE(w.O:"generation"::STRING, '(null / absent)') AS GENERATION,
       w.WAREHOUSE_SIZE,
       COUNT(*) AS WAREHOUSES,
       ROUND(SUM(m.CREDITS_30D), 1) AS CREDITS_30D,
       LEFT(LISTAGG(w.WAREHOUSE_NAME, ', ') WITHIN GROUP (ORDER BY w.WAREHOUSE_NAME), 200) AS SAMPLE_WAREHOUSES
FROM w
LEFT JOIN m ON m.WAREHOUSE_NAME = w.WAREHOUSE_NAME
GROUP BY 1, 2, 3, 4
ORDER BY 2, 1, 4;

-- W4 (#34) 30 days of sessions by CLIENT_APPLICATION_ID (the driver + version string, e.g. 'JDBC 3.13.30'):
-- session count, distinct users, what the client self-reports as its program, and an Informatica marker
-- (INFA / INFORMATICA anywhere in the client id, the CLIENT_ENVIRONMENT JSON text, or the user name).
-- Top 50 by sessions PLUS every Informatica-marked row even if it is outside the top 50 (capped at 200).
-- Columns grounded: CLIENT_APPLICATION_ID / CLIENT_ENVIRONMENT / USER_NAME / CREATED_ON / SESSION_ID are
-- read by app/data/security_sql.py:1181-1240 (client_drivers) and app/data/chatter_sql.py:83-87 (+ _APP_EXPR,
-- app/data/app_cost_sql.py:24-29). DRIVER / VERSION parse = security_sql.client_drivers, byte for byte.
WITH s AS (
  SELECT CLIENT_APPLICATION_ID, USER_NAME, CREATED_ON,
         COALESCE(NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                  CLIENT_APPLICATION_ID) AS DRIVER,
         COALESCE(NULLIF(TRIM(REGEXP_SUBSTR(CLIENT_APPLICATION_ID, '[0-9][0-9.]*$')), ''), '?') AS VERSION,
         COALESCE(TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING, '(not reported)') AS PROGRAM,
         COALESCE(CLIENT_APPLICATION_ID, '') ILIKE ANY ('%INFA%', '%INFORMATICA%') AS INFA_IN_CLIENT_ID,
         COALESCE(CLIENT_ENVIRONMENT, '') ILIKE ANY ('%INFA%', '%INFORMATICA%') AS INFA_IN_CLIENT_ENV,
         COALESCE(USER_NAME, '') ILIKE ANY ('%INFA%', '%INFORMATICA%') AS INFA_IN_USER
  FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
  WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())),
g AS (
  SELECT COALESCE(CLIENT_APPLICATION_ID, '(null)') AS CLIENT_APPLICATION_ID, DRIVER, VERSION,
         COUNT(*) AS SESSIONS_30D,
         COUNT(DISTINCT USER_NAME) AS USERS,
         COUNT_IF(INFA_IN_CLIENT_ID) AS INFA_BY_CLIENT_ID,
         COUNT_IF(INFA_IN_CLIENT_ENV) AS INFA_BY_CLIENT_ENV,
         COUNT_IF(INFA_IN_USER) AS INFA_BY_USER_NAME,
         MIN(CREATED_ON)::DATE AS FIRST_SEEN,
         MAX(CREATED_ON)::DATE AS LAST_SEEN,
         LEFT(LISTAGG(DISTINCT PROGRAM, ', ') WITHIN GROUP (ORDER BY PROGRAM), 160) AS PROGRAMS,
         LEFT(LISTAGG(DISTINCT USER_NAME, ', ') WITHIN GROUP (ORDER BY USER_NAME), 160) AS SAMPLE_USERS
  FROM s
  GROUP BY 1, 2, 3)
SELECT *
FROM g
QUALIFY ROW_NUMBER() OVER (ORDER BY SESSIONS_30D DESC) <= 50
     OR INFA_BY_CLIENT_ID + INFA_BY_CLIENT_ENV + INFA_BY_USER_NAME > 0
ORDER BY (INFA_BY_CLIENT_ID + INFA_BY_CLIENT_ENV + INFA_BY_USER_NAME > 0) DESC, SESSIONS_30D DESC
LIMIT 200;

-- W4b (#34) Which columns does ACCOUNT_USAGE.SESSIONS have in this account (is there a separate
-- CLIENT_APPLICATION_VERSION / CLIENT_VERSION column, as Snowflake docs list)? Cheap, never errors
-- (same form as PROBES_WAVE2's DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS).
DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.SESSIONS;

-- W4d (#34) Informatica's real fingerprint if it does not self-report: which client driver + version and
-- which user issue the stored-proc CALLs (Alfa's nightly cycle is Informatica-orchestrated proc CALLs,
-- etl_control_sql.py:172 and :755). 7 days of QUERY_TYPE = 'CALL' (the filter etl_control_sql.py:1047 and
-- ops_sql.py:970 use), task-run CALLs (USER_NAME = 'SYSTEM', no client driver) are excluded. SESSIONS is
-- scanned 30 days back so a long-lived pooled session still resolves, '(session not found)' = older still.
-- ControlM CALLs show up here too: tell them apart by USER_NAME.
WITH c AS (
  SELECT SESSION_ID, USER_NAME, DATABASE_NAME, EXECUTION_STATUS
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
  WHERE START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
    AND QUERY_TYPE = 'CALL'
    AND COALESCE(USER_NAME, '') <> 'SYSTEM'),
sess AS (
  SELECT SESSION_ID, CLIENT_APPLICATION_ID,
         COALESCE(TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING, '(not reported)') AS PROGRAM
  FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
  WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    AND SESSION_ID IN (SELECT SESSION_ID FROM c)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC) = 1)
SELECT COALESCE(s.CLIENT_APPLICATION_ID, '(session not found)') AS CLIENT_APPLICATION_ID,
       COALESCE(s.PROGRAM, '(session not found)') AS PROGRAM,
       c.USER_NAME,
       COALESCE(c.DATABASE_NAME, '(none)') AS DATABASE_NAME,
       COUNT(*) AS CALLS_7D,
       COUNT(DISTINCT c.SESSION_ID) AS SESSIONS,
       COUNT_IF(c.EXECUTION_STATUS <> 'SUCCESS') AS NON_SUCCESS_CALLS
FROM c
LEFT JOIN sess s ON s.SESSION_ID = c.SESSION_ID
GROUP BY 1, 2, 3, 4
ORDER BY CALLS_7D DESC
LIMIT 40;

-- W5 (#33) The account-level STATEMENT_TIMEOUT_IN_SECONDS: the value every warehouse without its own
-- setting inherits. level '' (blank) = nobody set it = Snowflake's 172800 s (48 h) default. Same SHOW
-- PARAMETERS form admin.py:1452 runs (it reads "value" / "level"), at ACCOUNT scope instead of the app
-- warehouse (the IN ACCOUNT form already ran in PROBES_NEXT_FIFTY_WAVE1_R2 block (d)). The repo's ONLY
-- per-warehouse timeout read is that one (APP_WAREHOUSE only), so W5b/W5c supply the fleet view #33 needs.
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT;

-- W5b (#33) HEAVIEST BLOCK. Runaway exposure per warehouse, 30 days: p99 / max elapsed, statements over
-- 1 h / 4 h / 24 h, and timeout cancels (error 000630 'Statement reached its statement or warehouse timeout
-- of N second(s)', the form tests/test_failure_advisor.py:47-49 and app/core/query.py:256 match) with the N
-- parsed from the message -- the effective ceiling actually enforced there (the lower of the warehouse and
-- session values, so evidence, not the warehouse setting itself). SUGGESTED_TIMEOUT_SEC = p99 x 3 (the rec's
-- prefill rule). TOTAL_ELAPSED_TIME includes queued time, so that base is conservative (high).
-- Columns as ops_sql.py:42,97,144-149 read them.
SELECT WAREHOUSE_NAME,
       COUNT(*) AS QUERIES_30D,
       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.99) / 1000, 0) AS P99_ELAPSED_SEC,
       ROUND(MAX(TOTAL_ELAPSED_TIME) / 1000, 0) AS MAX_ELAPSED_SEC,
       COUNT_IF(TOTAL_ELAPSED_TIME >= 3600000) AS OVER_1H,
       COUNT_IF(TOTAL_ELAPSED_TIME >= 14400000) AS OVER_4H,
       COUNT_IF(TOTAL_ELAPSED_TIME >= 86400000) AS OVER_24H,
       COUNT_IF(TRY_TO_NUMBER(ERROR_CODE::VARCHAR) = 630) AS TIMEOUT_CANCELS,
       MIN(IFF(TRY_TO_NUMBER(ERROR_CODE::VARCHAR) = 630, TRY_TO_NUMBER(REPLACE(REGEXP_SUBSTR(ERROR_MESSAGE, 'timeout of ([0-9,]+) second', 1, 1, 'e', 1), ',', '')), NULL)) AS TIMEOUT_SEC_SEEN_MIN,
       MAX(IFF(TRY_TO_NUMBER(ERROR_CODE::VARCHAR) = 630, TRY_TO_NUMBER(REPLACE(REGEXP_SUBSTR(ERROR_MESSAGE, 'timeout of ([0-9,]+) second', 1, 1, 'e', 1), ',', '')), NULL)) AS TIMEOUT_SEC_SEEN_MAX,
       ROUND(3 * APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.99) / 1000, 0) AS SUGGESTED_TIMEOUT_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
  AND WAREHOUSE_NAME IS NOT NULL
GROUP BY 1
ORDER BY MAX_ELAPSED_SEC DESC
LIMIT 100;

-- W5c (#33) OPTIONAL. There is no bulk read of a warehouse-level STATEMENT_TIMEOUT_IN_SECONDS (SHOW
-- PARAMETERS ... IN WAREHOUSE takes one name). This prints that SHOW for every warehouse, run only the
-- lines for warehouses W5b flags (OVER_4H > 0, or MAX_ELAPSED_SEC near 172800) and paste back each
-- one-row grid (level 'WAREHOUSE' = set on the warehouse, 'ACCOUNT' or blank = inherited).
-- The trailing semicolon is built with CHR(59) so no literal semicolon sits inside a string here.
SELECT "name" AS WAREHOUSE_NAME,
       'SHOW PARAMETERS LIKE ''STATEMENT_TIMEOUT_IN_SECONDS'' IN WAREHOUSE "' || REPLACE("name", '"', '""') || '"' || CHR(59) AS RUN_THIS
FROM TABLE(RESULT_SCAN($W_SHOW_QID))
ORDER BY 1;

-- ---------------- risky tail: a column or function this repo has never read (an error here is itself the answer) --------
-- Run All stops at the first error: if one of W1c / W3 / W3b / W4c / W4e / W2c errors, note the error text
-- and run the remaining ones individually (cursor in the block, Ctrl+Enter). Nothing above depends on them.
-- Note: the W3 / W3b / W4c trio all call SYSTEM$CLIENT_VERSION_INFO(). If W3 fails with an unknown-function
-- error, skip W3b and W4c (same answer) and go on to W4e.

-- W1c (#38) How many clusters do the multi-cluster warehouses actually use? 14 days of hourly
-- MAX(QUERY_HISTORY.CLUSTER_NUMBER) vs MAX_CLUSTERS (unverified column -- a documented QUERY_HISTORY column
-- the repo only names in tests/migrations/test_v041_loader_pass.py:379, never selects). HOURS_AT_MAX = 0
-- with PEAK < MAX = the cap is never reached (scale-in candidate), MIN_CLUSTERS > 1 with P95 = 1 = idle
-- always-on clusters. CLUSTER_NUMBER is a cluster label, so the hourly max approximates clusters in use.
-- An 'invalid identifier' error = no CLUSTER_NUMBER here (the scale-in advisor must stay queued).
WITH mc AS (
  SELECT "name" AS WAREHOUSE_NAME,
         TRY_TO_NUMBER("min_cluster_count"::VARCHAR) AS MIN_CLUSTERS,
         TRY_TO_NUMBER("max_cluster_count"::VARCHAR) AS MAX_CLUSTERS,
         "scaling_policy" AS SCALING_POLICY
  FROM TABLE(RESULT_SCAN($W_SHOW_QID))
  WHERE (TRY_TO_NUMBER("max_cluster_count"::VARCHAR) > 1 OR TRY_TO_NUMBER("min_cluster_count"::VARCHAR) > 1)),
h AS (
  SELECT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME) AS HR, MAX(CLUSTER_NUMBER) AS MAX_CLUSTER_USED
  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
  WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
    AND WAREHOUSE_NAME IN (SELECT WAREHOUSE_NAME FROM mc)
    AND CLUSTER_NUMBER IS NOT NULL
  GROUP BY 1, 2)
SELECT mc.WAREHOUSE_NAME, mc.MIN_CLUSTERS, mc.MAX_CLUSTERS, mc.SCALING_POLICY,
       COUNT(h.HR) AS ACTIVE_HOURS_14D,
       MAX(h.MAX_CLUSTER_USED) AS PEAK_CLUSTERS_USED,
       APPROX_PERCENTILE(h.MAX_CLUSTER_USED, 0.95) AS P95_HOURLY_PEAK,
       COUNT_IF(h.MAX_CLUSTER_USED >= mc.MAX_CLUSTERS) AS HOURS_AT_MAX,
       COUNT_IF(h.MAX_CLUSTER_USED = 1) AS HOURS_ONE_CLUSTER
FROM mc
LEFT JOIN h ON h.WAREHOUSE_NAME = mc.WAREHOUSE_NAME
GROUP BY 1, 2, 3, 4
ORDER BY HOURS_AT_MAX DESC, PEAK_CLUSTERS_USED DESC;

-- W3 (#34) What does Snowflake itself say are the minimum-supported / nearing-end-of-support /
-- recommended client versions? Raw JSON + the entry count + the key names of the first entry (so the
-- parse in W3b/W4c can be checked against the real shape). Not ACCOUNT_USAGE: a metadata call, no scan.
-- SYSTEM$CLIENT_VERSION_INFO() is from Snowflake docs (unverified function -- the repo never calls it).
-- An unknown-function error here = not available on this account (#34 falls back to W4 + W4d).
SELECT SYSTEM$CLIENT_VERSION_INFO() AS CLIENT_VERSION_INFO_RAW,
       ARRAY_SIZE(TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO())) AS CLIENTS_LISTED,
       CASE WHEN IS_OBJECT(TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO())[0])
            THEN ARRAY_TO_STRING(OBJECT_KEYS(TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO())[0]), ', ')
            ELSE '(first entry is not an object: read the raw JSON)' END AS FIRST_ENTRY_KEYS;

-- W3b (#34) The same, one row per client. Key names are from Snowflake docs (unverified -- the repo never
-- reads this function): clientId, clientAppId, minimumSupportedVersion, minimumNearingEndOfSupportVersion,
-- recommendedVersion, deprecatedVersions. A wrong key reads NULL (never an error), RAW_ENTRY shows the truth.
SELECT f.value:"clientId"::STRING AS CLIENT_ID,
       f.value:"clientAppId"::STRING AS CLIENT_APP_ID,
       f.value:"minimumSupportedVersion"::STRING AS MIN_SUPPORTED_VERSION,
       f.value:"minimumNearingEndOfSupportVersion"::STRING AS NEARING_EOS_VERSION,
       f.value:"recommendedVersion"::STRING AS RECOMMENDED_VERSION,
       LEFT(TO_JSON(f.value:"deprecatedVersions"), 200) AS DEPRECATED_VERSIONS,
       LEFT(TO_JSON(f.value), 400) AS RAW_ENTRY
FROM TABLE(FLATTEN(INPUT => TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO()))) f
ORDER BY 2, 1;

-- W4c (#34) Support status per driver version: W4's 30-day sessions joined to W3b's parse on
-- DRIVER = clientAppId (else clientId), versions compared with the Clients tab's padded VKEY
-- (security_sql.client_drivers). UNSUPPORTED = below minimumSupportedVersion, NEARING_EOS = below
-- minimumNearingEndOfSupportVersion (interpretation unverified -- check it against W3b's raw values),
-- NOT_IN_CLIENT_VERSION_INFO = the driver name did not match any entry (UI / Snowsight / unknown tools,
-- or a naming mismatch that the rec's whitelist dict must map). A wrong key reads NULL, but this calls
-- the same unverified SYSTEM$CLIENT_VERSION_INFO() as W3, so it errors exactly when W3 does.
WITH info AS (
  SELECT f.value:"clientId"::STRING AS CLIENT_ID,
         f.value:"clientAppId"::STRING AS CLIENT_APP_ID,
         f.value:"minimumSupportedVersion"::STRING AS MIN_SUPPORTED_VERSION,
         f.value:"minimumNearingEndOfSupportVersion"::STRING AS NEARING_EOS_VERSION,
         f.value:"recommendedVersion"::STRING AS RECOMMENDED_VERSION
  FROM TABLE(FLATTEN(INPUT => TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO()))) f),
s AS (
  SELECT COALESCE(NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                  CLIENT_APPLICATION_ID) AS DRIVER,
         COALESCE(NULLIF(TRIM(REGEXP_SUBSTR(CLIENT_APPLICATION_ID, '[0-9][0-9.]*$')), ''), '?') AS VERSION,
         USER_NAME, CREATED_ON,
         (COALESCE(CLIENT_APPLICATION_ID, '') ILIKE ANY ('%INFA%', '%INFORMATICA%')
          OR COALESCE(CLIENT_ENVIRONMENT, '') ILIKE ANY ('%INFA%', '%INFORMATICA%')
          OR COALESCE(USER_NAME, '') ILIKE ANY ('%INFA%', '%INFORMATICA%')) AS INFA_MARKED
  FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
  WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    AND CLIENT_APPLICATION_ID IS NOT NULL),
g AS (
  SELECT DRIVER, VERSION, COUNT(*) AS SESSIONS_30D, COUNT(DISTINCT USER_NAME) AS USERS,
         COUNT_IF(INFA_MARKED) AS INFA_MARKED_SESSIONS, MAX(CREATED_ON)::DATE AS LAST_SEEN,
         LEFT(LISTAGG(DISTINCT USER_NAME, ', ') WITHIN GROUP (ORDER BY USER_NAME), 160) AS SAMPLE_USERS
  FROM s GROUP BY 1, 2),
j AS (
  SELECT g.*, i.CLIENT_APP_ID, i.MIN_SUPPORTED_VERSION, i.NEARING_EOS_VERSION, i.RECOMMENDED_VERSION,
         IFF(g.VERSION IS NULL OR g.VERSION = '?', NULL, LPAD(COALESCE(NULLIF(SPLIT_PART(g.VERSION, '.', 1), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(g.VERSION, '.', 2), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(g.VERSION, '.', 3), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(g.VERSION, '.', 4), ''), '0'), 6, '0')) AS V_KEY,
         IFF(i.MIN_SUPPORTED_VERSION IS NULL OR i.MIN_SUPPORTED_VERSION = '?', NULL, LPAD(COALESCE(NULLIF(SPLIT_PART(i.MIN_SUPPORTED_VERSION, '.', 1), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.MIN_SUPPORTED_VERSION, '.', 2), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.MIN_SUPPORTED_VERSION, '.', 3), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.MIN_SUPPORTED_VERSION, '.', 4), ''), '0'), 6, '0')) AS MIN_KEY,
         IFF(i.NEARING_EOS_VERSION IS NULL OR i.NEARING_EOS_VERSION = '?', NULL, LPAD(COALESCE(NULLIF(SPLIT_PART(i.NEARING_EOS_VERSION, '.', 1), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.NEARING_EOS_VERSION, '.', 2), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.NEARING_EOS_VERSION, '.', 3), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.NEARING_EOS_VERSION, '.', 4), ''), '0'), 6, '0')) AS NEAR_KEY,
         IFF(i.RECOMMENDED_VERSION IS NULL OR i.RECOMMENDED_VERSION = '?', NULL, LPAD(COALESCE(NULLIF(SPLIT_PART(i.RECOMMENDED_VERSION, '.', 1), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.RECOMMENDED_VERSION, '.', 2), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.RECOMMENDED_VERSION, '.', 3), ''), '0'), 6, '0') || LPAD(COALESCE(NULLIF(SPLIT_PART(i.RECOMMENDED_VERSION, '.', 4), ''), '0'), 6, '0')) AS REC_KEY
  FROM g
  LEFT JOIN info i
    ON UPPER(g.DRIVER) = UPPER(i.CLIENT_APP_ID) OR UPPER(g.DRIVER) = UPPER(i.CLIENT_ID)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY g.DRIVER, g.VERSION
                             ORDER BY IFF(UPPER(g.DRIVER) = UPPER(i.CLIENT_APP_ID), 0, 1)) = 1),
r AS (
  SELECT DRIVER, VERSION,
         CASE WHEN CLIENT_APP_ID IS NULL AND MIN_SUPPORTED_VERSION IS NULL THEN 'NOT_IN_CLIENT_VERSION_INFO'
              WHEN V_KEY IS NULL THEN 'NO_VERSION_REPORTED'
              WHEN MIN_KEY IS NOT NULL AND V_KEY < MIN_KEY THEN 'UNSUPPORTED'
              WHEN NEAR_KEY IS NOT NULL AND V_KEY < NEAR_KEY THEN 'NEARING_EOS'
              WHEN REC_KEY IS NOT NULL AND V_KEY < REC_KEY THEN 'OK_BELOW_RECOMMENDED'
              ELSE 'OK' END AS SUPPORT_STATUS,
         MIN_SUPPORTED_VERSION, NEARING_EOS_VERSION, RECOMMENDED_VERSION,
         SESSIONS_30D, USERS, INFA_MARKED_SESSIONS, LAST_SEEN, SAMPLE_USERS
  FROM j)
SELECT *
FROM r
ORDER BY CASE SUPPORT_STATUS WHEN 'UNSUPPORTED' THEN 0 WHEN 'NEARING_EOS' THEN 1
              WHEN 'OK_BELOW_RECOMMENDED' THEN 2 WHEN 'OK' THEN 3 ELSE 4 END,
         SESSIONS_30D DESC
LIMIT 100;

-- W4e (#34) SESSIONS by CLIENT_APPLICATION_ID x CLIENT_APPLICATION_VERSION, 7 days (unverified column --
-- in Snowflake docs, never read by the repo, W4b's DESCRIBE shows whether it exists). An 'invalid
-- identifier' error here = the view has no such column, W4's version parse from CLIENT_APPLICATION_ID stands.
SELECT CLIENT_APPLICATION_ID, CLIENT_APPLICATION_VERSION,
       COUNT(*) AS SESSIONS_7D, COUNT(DISTINCT USER_NAME) AS USERS
FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
WHERE CREATED_ON >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1, 2
ORDER BY SESSIONS_7D DESC
LIMIT 50;

-- W2c (#48) Direct read of "resource_constraint" from the same SHOW (its own statement: if the column is
-- absent in this account, this errors 'invalid identifier' -- that error IS the answer, and W2 already
-- carries the counts). Gen2 = 'STANDARD_GEN_2' (docs spelling, the LIKE escapes the underscore).
-- Last on purpose: W2 already answers #48 without erroring, so this is the least needed block.
SELECT "resource_constraint" AS RESOURCE_CONSTRAINT, "type" AS WAREHOUSE_TYPE, COUNT(*) AS WAREHOUSES,
       COUNT_IF(UPPER("resource_constraint") LIKE '%GEN~_2%' ESCAPE '~') AS GEN2
FROM TABLE(RESULT_SCAN($W_SHOW_QID))
GROUP BY 1, 2
ORDER BY 3 DESC;

-- ---------------- end of group W, GROUP S follows. If Run All stopped above, copy the error as
-- ---------------- that block's answer, highlight from the block AFTER it to the end of the file, run again.

-- =====================================================================================
--  GROUP S  --  security + AI read-only probes (Next-Fifty #37 / #39 / #41 / #43 / #44)
--  READ-ONLY: SELECT / SHOW / RESULT_SCAN only, the ALTER SESSION only sets this worksheet's clock.
--  Run top to bottom, paste back every grid (an empty grid is an answer: say 'no rows').
--  Blocks marked RISKY read a view or column this repo has never read: an error there is itself the
--  answer (the view / column is absent on this account) and does not affect any other block.
--  Snowsight STOPS a Run All at the first error: when a block errors, copy the error as that block's
--  answer, then highlight from the NEXT block to the end of the file and run again. Every block is a
--  single self-contained statement (except S0a, whose SHOW + RESULT_SCAN pair must run back to back).
--  HEAVIEST blocks: S5d (CORTEX_AI_FUNCTIONS x QUERY_HISTORY join, 14d) then S4c (live Cortex Code
--  secure views, ~20-30s window-flat per the repo's own measurement in cortex_sql.py), then S6 (30d
--  LOGIN_HISTORY with window functions). Everything else is an aggregate or a LIMITed sample.
-- =====================================================================================

-- S0a (#41/#43) which of the security / AI views exist in SNOWFLAKE.ACCOUNT_USAGE on this account?
-- (SHOW output columns are lower-case, hence the quoted "name" / "created_on".) Run the two lines together.
SHOW VIEWS IN SCHEMA SNOWFLAKE.ACCOUNT_USAGE;
SELECT "name" AS VIEW_NAME, "created_on" AS CREATED_ON
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "name" ILIKE ANY ('%CORTEX%', '%INTELLIGENCE%', '%CLASSIFICATION%', '%AGENT%', '%ANALYST%', '%QUOTA%')
   OR "name" IN ('POLICY_REFERENCES', 'LOGIN_HISTORY', 'GRANTS_TO_USERS', 'GRANTS_TO_ROLES')
ORDER BY 1;

-- S0b (#41/#43/#44) RISKY: the exact column list + type of every candidate view in ONE grid, so each later
-- '(unverified column)' is settled even if its own block errors. The repo never reads the view
-- SNOWFLAKE.INFORMATION_SCHEMA.COLUMNS (its TABLE_SCHEMA / TABLE_NAME / COLUMN_NAME / DATA_TYPE /
-- ORDINAL_POSITION are standard INFORMATION_SCHEMA columns from Snowflake docs). An error here is itself the
-- answer (INFORMATION_SCHEMA not readable on the SNOWFLAKE share for this role) -- fall back to S0a + the blocks.
-- The outer TABLE_SCHEMA IN (...) keeps the predicate pushed down (INFORMATION_SCHEMA rejects broad scans).
SELECT TABLE_SCHEMA, TABLE_NAME,
       COUNT(*) AS N_COLUMNS,
       LISTAGG(COLUMN_NAME || ' ' || DATA_TYPE, ', ') WITHIN GROUP (ORDER BY ORDINAL_POSITION) AS COLUMN_LIST
FROM SNOWFLAKE.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA IN ('ACCOUNT_USAGE', 'TRUST_CENTER')
  AND ((TABLE_SCHEMA = 'ACCOUNT_USAGE'
        AND (TABLE_NAME ILIKE ANY ('%CORTEX%', '%INTELLIGENCE%', '%CLASSIFICATION%', '%AGENT%', '%ANALYST%')
             OR TABLE_NAME = 'POLICY_REFERENCES'))
       OR (TABLE_SCHEMA = 'TRUST_CENTER' AND TABLE_NAME = 'FINDINGS'))
GROUP BY 1, 2
ORDER BY 1, 2;

-- S1a (#43) POLICY_REFERENCES: references by POLICY_KIND and entity domain, with distinct policies and distinct
-- referenced entities. Kind totals ('(all domains)') and a grand total ('(all kinds)') ride GROUPING SETS
-- (the () set + GROUPING_ID are the repo's own pattern, ops_sql.queries_health_bundle). Columns POLICY_KIND /
-- REF_ENTITY_DOMAIN / REF_ENTITY_NAME / POLICY_NAME are read by security_sql.admin_network_policy_coverage,
-- which the app runs probe=True (security_sql.py calls the view UNVERIFIED on this account) -- an error here
-- means the view is unreadable for this role. Up to ~2h latency (docs). Entity identity here is DOMAIN:NAME
-- (a table name is not database-qualified -- S1b qualifies it). Sort: '(all kinds)' first, then each kind's
-- total over its domains, then that kind's domains.
SELECT IFF(GROUPING_ID(POLICY_KIND) = 1, '(all kinds)', POLICY_KIND) AS KIND,
       IFF(GROUPING_ID(D) = 1, '(all domains)', D) AS REF_DOMAIN,
       COUNT(*) AS POLICY_REFS,
       COUNT(DISTINCT POLICY_NAME) AS DISTINCT_POLICIES,
       COUNT(DISTINCT D || ':' || REF_ENTITY_NAME) AS DISTINCT_REF_ENTITIES,
       GROUPING_ID(POLICY_KIND) + GROUPING_ID(D) AS ROLLUP_LEVEL
FROM (SELECT POLICY_KIND, UPPER(REF_ENTITY_DOMAIN) AS D, REF_ENTITY_NAME, POLICY_NAME
      FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES)
GROUP BY GROUPING SETS ((), (POLICY_KIND), (POLICY_KIND, D))
ORDER BY KIND, ROLLUP_LEVEL DESC, POLICY_REFS DESC;

-- S1b (#43) RISKY (unverified columns REF_DATABASE_NAME, REF_SCHEMA_NAME, REF_COLUMN_NAME, POLICY_STATUS -- from
-- Snowflake docs, the repo never reads them): fully-qualified distinct entities / columns per kind and the
-- status words present. An error here is itself the answer (column names differ on this account -> see S0b).
SELECT POLICY_KIND, UPPER(REF_ENTITY_DOMAIN) AS REF_DOMAIN,
       COUNT(*) AS POLICY_REFS,
       COUNT(DISTINCT COALESCE(REF_DATABASE_NAME, '') || '.' || COALESCE(REF_SCHEMA_NAME, '') || '.'
                      || REF_ENTITY_NAME) AS DISTINCT_QUALIFIED_ENTITIES,
       COUNT(DISTINCT IFF(REF_COLUMN_NAME IS NULL, NULL,
                          COALESCE(REF_DATABASE_NAME, '') || '.' || COALESCE(REF_SCHEMA_NAME, '') || '.'
                          || REF_ENTITY_NAME || '.' || REF_COLUMN_NAME)) AS DISTINCT_COLUMNS,
       COUNT(DISTINCT REF_DATABASE_NAME) AS DISTINCT_DATABASES,
       LISTAGG(DISTINCT POLICY_STATUS, ', ') AS STATUS_WORDS
FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

-- S2a (#43) RISKY (view never read by the repo, docs name): does DATA_CLASSIFICATION_LATEST exist and hold
-- anything? An error here is itself the answer (view absent / automatic classification not available).
SELECT COUNT(*) AS CLASSIFICATION_ROWS FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_CLASSIFICATION_LATEST;

-- S2b (#43) RISKY (unverified columns TABLE_ID, DATABASE_NAME, STATUS, TRIGGER_TYPE, LAST_CLASSIFIED_ON -- docs):
-- distinct classified tables, databases, freshness, and the status / trigger words present.
-- An error here with S2a OK means the column names differ -> read them off S0b.
SELECT COUNT(*) AS CLASSIFICATION_ROWS,
       COUNT(DISTINCT TABLE_ID) AS DISTINCT_TABLES,
       COUNT(DISTINCT DATABASE_NAME) AS DISTINCT_DATABASES,
       MIN(LAST_CLASSIFIED_ON) AS OLDEST_CLASSIFIED,
       MAX(LAST_CLASSIFIED_ON) AS NEWEST_CLASSIFIED,
       LISTAGG(DISTINCT STATUS, ', ') AS STATUS_WORDS,
       LISTAGG(DISTINCT TRIGGER_TYPE, ', ') AS TRIGGER_TYPES
FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_CLASSIFICATION_LATEST;

-- S3b (#44) the app's OWN daily Trust Center snapshot (V075 SECURITY_TRUST_SNAPSHOT: DAY, SCANNER_ID,
-- TOTAL_AT_RISK_COUNT, SCANNED_AT, LOAD_TS): how many distinct snapshot days exist to trend from, and gaps.
-- (Placed before S3a on purpose: it reads only the app's own table, so it cannot fail on the Trust Center
-- grant. SP_LOAD_SECURITY_FACTS keeps 400 days -- V105 -- and carries a vanished scanner forward at 0.)
SELECT COUNT(*) AS SNAPSHOT_ROWS,
       COUNT(DISTINCT SCANNER_ID) AS SCANNERS,
       COUNT(DISTINCT DAY) AS DISTINCT_SNAPSHOT_DAYS,
       MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY,
       DATEDIFF('day', MIN(DAY), MAX(DAY)) + 1 - COUNT(DISTINCT DAY) AS MISSING_DAYS_IN_SPAN,
       COUNT(DISTINCT IFF(DAY >= DATEADD('day', -30, CURRENT_DATE()), DAY, NULL)) AS SNAPSHOT_DAYS_30D,
       MAX(LOAD_TS) AS NEWEST_LOAD
FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT;

-- S3a (#44) Trust Center: latest FINDINGS row per scanner + how many distinct scan days the view retains.
-- Columns SCANNER_ID / SCANNER_NAME / SEVERITY / TOTAL_AT_RISK_COUNT / CREATED_ON are read by
-- security_sql.trust_center_findings and the V075->V105 SP_LOAD_SECURITY_FACTS snapshot arm. Needs the
-- TRUST_CENTER_VIEWER application role (admin.py self-check) -- an error here means that grant is missing.
SELECT SCANNER_ID,
       MAX_BY(SCANNER_NAME, CREATED_ON) AS SCANNER_NAME,
       MAX_BY(UPPER(SEVERITY), CREATED_ON) AS LATEST_SEVERITY,
       MAX_BY(TOTAL_AT_RISK_COUNT, CREATED_ON) AS LATEST_AT_RISK,
       MAX(CREATED_ON) AS LATEST_SCAN,
       MIN(CREATED_ON) AS FIRST_SCAN_RETAINED,
       COUNT(*) AS FINDING_ROWS,
       COUNT(DISTINCT CREATED_ON::DATE) AS DISTINCT_SCAN_DAYS,
       COUNT(DISTINCT IFF(CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP()), CREATED_ON::DATE, NULL)) AS SCAN_DAYS_30D
FROM SNOWFLAKE.TRUST_CENTER.FINDINGS
GROUP BY SCANNER_ID
ORDER BY CASE LATEST_SEVERITY WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
         LATEST_AT_RISK DESC NULLS LAST
LIMIT 200;

-- S3c (#44) RISKY (unverified column AT_RISK_ENTITIES -- docs, the repo never reads it): on the latest row per
-- scanner, does the entity array hold every at-risk entity (ENTITY_ARRAY_SIZE = TOTAL_AT_RISK_COUNT) or a
-- truncated sample, and what keys does one entity carry? An error here is itself the answer (column absent
-- or not an ARRAY -> check its type in S0b).
SELECT SCANNER_ID, SCANNER_NAME, UPPER(SEVERITY) AS SEVERITY, CREATED_ON AS SCANNED_AT,
       TOTAL_AT_RISK_COUNT,
       ARRAY_SIZE(AT_RISK_ENTITIES) AS ENTITY_ARRAY_SIZE,
       CASE WHEN AT_RISK_ENTITIES IS NULL THEN 'NO ARRAY'
            WHEN ARRAY_SIZE(AT_RISK_ENTITIES) = TOTAL_AT_RISK_COUNT THEN 'FULL'
            ELSE 'DIFFERS' END AS ARRAY_VS_COUNT,
       OBJECT_KEYS(AS_OBJECT(AT_RISK_ENTITIES[0])) AS FIRST_ENTITY_KEYS
FROM SNOWFLAKE.TRUST_CENTER.FINDINGS
QUALIFY ROW_NUMBER() OVER (PARTITION BY SCANNER_ID ORDER BY CREATED_ON DESC) = 1
ORDER BY TOTAL_AT_RISK_COUNT DESC NULLS LAST
LIMIT 200;

-- S4a (#37) per-user AI user-days above 15 credits, last 90 days, from the SAME per-user source the app's
-- guardrails read: FACT_AI_USAGE_DAILY with SOURCE <> 'Functions' (mart27_sql.ai_code_user_daily /
-- ai_code_user_rollup, Security > AI guardrails and Cost > Chargeback & AI). Grain collapsed to user-day
-- across Snowsight + CLI, strictly-over test -- exactly wave2.coco_efficiency's DAYS_OVER_CAP (which counts 0
-- when the cap is <= 0). Window DAY >= today-90 is mart27_sql._ai_code_window(90). Also reports the
-- configured COCO_DAILY_CAP_CREDITS (SETTINGS.VALUE is VARCHAR, default 15, config.py / V121) and the fact's
-- coverage: the app's gate needs FACT_FIRST_DAY <= today-89 for a 90d answer (COVERS_90D). Percentiles are
-- over ACTIVE user-days. Always returns exactly ONE row (aggregates without GROUP BY), even with no usage.
WITH cap AS (
    SELECT COALESCE(MAX(TRY_TO_DOUBLE(VALUE)), 15.0) AS CAP_CR
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'COCO_DAILY_CAP_CREDITS'
), cov AS (
    SELECT MIN(DAY) AS FACT_FIRST_DAY, MAX(DAY) AS FACT_LAST_DAY
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY WHERE SOURCE <> 'Functions'
), ud AS (
    SELECT USER_NAME, DAY, SUM(COALESCE(CREDITS, 0)) AS CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE <> 'Functions' AND DAY >= DATEADD('day', -90, CURRENT_DATE())
    GROUP BY 1, 2
), agg AS (
    SELECT COUNT(ud.USER_NAME) AS ACTIVE_USER_DAYS,
           COUNT(DISTINCT ud.USER_NAME) AS ACTIVE_USERS,
           COUNT_IF(ud.CREDITS > 15) AS USER_DAYS_OVER_15,
           COUNT(DISTINCT IFF(ud.CREDITS > 15, ud.USER_NAME, NULL)) AS USERS_OVER_15,
           COUNT_IF(c.CAP_CR > 0 AND ud.CREDITS > c.CAP_CR) AS USER_DAYS_OVER_CONFIGURED_CAP,
           ROUND(MAX(ud.CREDITS), 2) AS MAX_USER_DAY_CR,
           ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ud.CREDITS), 2) AS P95_USER_DAY_CR,
           ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ud.CREDITS), 2) AS P50_USER_DAY_CR,
           ROUND(SUM(ud.CREDITS), 2) AS CREDITS_90D,
           ROUND(SUM(IFF(ud.CREDITS > 15, ud.CREDITS - 15, 0)), 2) AS CREDITS_ABOVE_15_LINE
    FROM ud CROSS JOIN cap c
)
SELECT c.CAP_CR AS CONFIGURED_CAP_CR, v.FACT_FIRST_DAY, v.FACT_LAST_DAY,
       COALESCE(v.FACT_FIRST_DAY <= DATEADD('day', -89, CURRENT_DATE()), FALSE) AS COVERS_90D,
       a.ACTIVE_USER_DAYS, a.ACTIVE_USERS, a.USER_DAYS_OVER_15, a.USERS_OVER_15,
       a.USER_DAYS_OVER_CONFIGURED_CAP, a.MAX_USER_DAY_CR, a.P95_USER_DAY_CR, a.P50_USER_DAY_CR,
       a.CREDITS_90D, a.CREDITS_ABOVE_15_LINE
FROM agg a CROSS JOIN cap c CROSS JOIN cov v;

-- S4b (#37) the top 10 users by days over 15 credits (same source and grain as S4a). Empty grid = nobody over.
WITH ud AS (
    SELECT USER_NAME, DAY, SUM(COALESCE(CREDITS, 0)) AS CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE <> 'Functions' AND DAY >= DATEADD('day', -90, CURRENT_DATE())
    GROUP BY 1, 2
)
SELECT USER_NAME,
       COUNT_IF(CREDITS > 15) AS DAYS_OVER_15,
       COUNT(*) AS ACTIVE_DAYS,
       ROUND(MAX(CREDITS), 2) AS MAX_DAY_CR,
       ROUND(SUM(CREDITS), 2) AS CREDITS_90D,
       ROUND(SUM(IFF(CREDITS > 15, CREDITS - 15, 0)), 2) AS CREDITS_ABOVE_15_LINE,
       MAX(IFF(CREDITS > 15, DAY, NULL)) AS LAST_DAY_OVER_15
FROM ud
GROUP BY USER_NAME
HAVING COUNT_IF(CREDITS > 15) > 0
ORDER BY DAYS_OVER_15 DESC, CREDITS_ABOVE_15_LINE DESC
LIMIT 10;

-- S4c (#37) RISKY + HEAVY (~20-30s, window-flat secure-view expansion): the same S4a numbers straight from the
-- live Cortex Code views the fact is loaded from (cortex_sql._COMBINED_CODE_USAGE: USER_ID, USAGE_TIME,
-- TOKEN_CREDITS), keyed USER_ID x USAGE_TIME::DATE exactly like the V146 loader (USAGE_TIME is TIMESTAMP_TZ, so
-- ::DATE is the stamp's own-offset date, same as the fact). Should match S4a within the loader's reload lag.
-- The fact (S4a) keys on USERS.NAME (unmapped ids collapse to 'UNKNOWN', a recreated login's ids merge) while
-- this block keys on USER_ID, so ACTIVE_USERS can differ slightly. An error here (002139 / no subscription) is itself the answer.
WITH c AS (
    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -91, CURRENT_TIMESTAMP())
    UNION ALL
    SELECT USER_ID, USAGE_TIME, TOKEN_CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
    WHERE USAGE_TIME >= DATEADD('day', -91, CURRENT_TIMESTAMP())
), ud AS (
    SELECT USER_ID, USAGE_TIME::DATE AS DAY, SUM(COALESCE(TOKEN_CREDITS, 0)) AS CREDITS
    FROM c
    WHERE USAGE_TIME::DATE >= DATEADD('day', -90, CURRENT_DATE())
    GROUP BY 1, 2
)
SELECT COUNT(*) AS ACTIVE_USER_DAYS,
       COUNT(DISTINCT USER_ID) AS ACTIVE_USERS,
       COUNT_IF(CREDITS > 15) AS USER_DAYS_OVER_15,
       COUNT(DISTINCT IFF(CREDITS > 15, USER_ID, NULL)) AS USERS_OVER_15,
       ROUND(MAX(CREDITS), 2) AS MAX_USER_DAY_CR,
       ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY CREDITS), 2) AS P95_USER_DAY_CR,
       ROUND(SUM(CREDITS), 2) AS CREDITS_90D
FROM ud;

-- S5a (#41) how big is each AI metering line (90d billed credits) -- sizes whether a per-user drill for AI
-- Functions / Intelligence / AI services is worth building. METERING_DAILY_HISTORY columns + the billed
-- expression are cost_sql._BILLED, the name match mirrors cost_coverage._is_ai_family.
SELECT UPPER(SERVICE_TYPE) AS SERVICE_TYPE,
       ROUND(SUM(COALESCE(CREDITS_BILLED, GREATEST(0, COALESCE(CREDITS_USED, 0)
                 + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0)))), 2) AS CREDITS_BILLED_90D,
       COUNT(DISTINCT USAGE_DATE) AS DAYS_WITH_USAGE,
       MAX(USAGE_DATE) AS LAST_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE USAGE_DATE >= DATEADD('day', -90, CURRENT_DATE())
  AND (UPPER(SERVICE_TYPE) LIKE '%CORTEX%' OR UPPER(SERVICE_TYPE) LIKE 'AI%'
       OR UPPER(SERVICE_TYPE) LIKE '%INTELLIGENCE%' OR UPPER(SERVICE_TYPE) LIKE '%COCO%'
       OR UPPER(SERVICE_TYPE) LIKE '%COWORK%')
GROUP BY 1
ORDER BY CREDITS_BILLED_90D DESC;

-- S5b (#41) RISKY (optional view per cortex_sql.py docstring): AI Functions volume, 90d, grounded columns only
-- (START_TIME / CREDITS / QUERY_ID -- cortex_sql.cortex_ai_functions_daily, mart_sql.app_cortex_self_cost).
-- An error here means CORTEX_AI_FUNCTIONS_USAGE_HISTORY is absent: skip S5d/S5c.
SELECT COUNT(*) AS ROWS_90D,
       COUNT(DISTINCT QUERY_ID) AS DISTINCT_QUERIES,
       ROUND(SUM(COALESCE(CREDITS, 0)), 4) AS CREDITS_90D,
       MIN(START_TIME) AS FIRST_TS, MAX(START_TIME) AS LAST_TS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP());

-- S5d (#41) HEAVIEST BLOCK (QUERY_HISTORY semi-join, kept to 14d): the grounded attribution path -- AI Functions
-- QUERY_ID -> QUERY_HISTORY.USER_NAME, the same join mart_sql.app_cortex_self_cost already runs (QH window = days+1).
-- How much of the AI Functions spend can be pinned to a user this way, and across how many users? Placed ahead
-- of the S5c block on purpose (grounded columns only). An error here means CORTEX_AI_FUNCTIONS_USAGE_HISTORY is absent (see S5b).
-- The newest few hours can be unmatched purely from ACCOUNT_USAGE latency, so PCT_ATTRIBUTABLE slightly understates.
WITH ai AS (
    SELECT QUERY_ID, SUM(COALESCE(CREDITS, 0)) AS CR
    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
    WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
    GROUP BY 1
), q AS (
    SELECT QUERY_ID, USER_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -15, CURRENT_TIMESTAMP())
      AND QUERY_ID IN (SELECT QUERY_ID FROM ai WHERE QUERY_ID IS NOT NULL)
)
SELECT COUNT(ai.QUERY_ID) AS AI_QUERIES_14D,
       COUNT(q.QUERY_ID) AS MATCHED_IN_QUERY_HISTORY,
       ROUND(SUM(ai.CR), 4) AS AI_CREDITS_14D,
       ROUND(SUM(IFF(ai.QUERY_ID IS NULL, ai.CR, 0)), 4) AS CREDITS_WITH_NULL_QUERY_ID,
       ROUND(SUM(IFF(q.QUERY_ID IS NOT NULL, ai.CR, 0)), 4) AS CREDITS_ATTRIBUTABLE,
       ROUND(100 * SUM(IFF(q.QUERY_ID IS NOT NULL, ai.CR, 0)) / NULLIF(SUM(ai.CR), 0), 1) AS PCT_ATTRIBUTABLE,
       COUNT(DISTINCT q.USER_NAME) AS DISTINCT_USERS
FROM ai LEFT JOIN q ON q.QUERY_ID = ai.QUERY_ID;

-- S5c (#41) RISKY (unverified column USER_ID on CORTEX_AI_FUNCTIONS_USAGE_HISTORY -- the repo never reads a user
-- column there, the loader books this arm as USER_NAME 'ACCOUNT'): do AI Functions credits carry a user id?
-- An error here with S5b OK means there is no USER_ID column -> S5d (above) is the only attribution path.
SELECT COUNT(*) AS ROWS_90D,
       COUNT_IF(USER_ID IS NOT NULL) AS ROWS_WITH_USER_ID,
       ROUND(SUM(COALESCE(CREDITS, 0)), 4) AS CREDITS_90D,
       ROUND(SUM(IFF(USER_ID IS NOT NULL, COALESCE(CREDITS, 0), 0)), 4) AS CREDITS_WITH_USER_ID,
       COUNT(DISTINCT USER_ID) AS DISTINCT_USER_IDS
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
WHERE START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP());

-- S5e (#41) RISKY (view never read by the repo, docs name): Cortex Agent usage rows. Error = view absent.
SELECT COUNT(*) AS CORTEX_AGENT_ROWS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AGENT_USAGE_HISTORY;

-- S5f (#41) RISKY (view never read by the repo, docs name): Snowflake Intelligence usage rows. Error = view absent.
SELECT COUNT(*) AS SNOWFLAKE_INTELLIGENCE_ROWS FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_INTELLIGENCE_USAGE_HISTORY;

-- S5g (#41) RISKY (view never read by the repo, docs name): Cortex Analyst usage rows. Error = view absent.
SELECT COUNT(*) AS CORTEX_ANALYST_ROWS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_ANALYST_USAGE_HISTORY;

-- S5h (#41) RISKY (view never read by the repo, docs name): Cortex Search daily (billing) usage rows. Error = view absent.
SELECT COUNT(*) AS CORTEX_SEARCH_DAILY_ROWS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_SEARCH_DAILY_USAGE_HISTORY;

-- S5i (#41) RISKY (view never read by the repo, docs name): Cortex Search serving usage rows. Error = view absent.
SELECT COUNT(*) AS CORTEX_SEARCH_SERVING_ROWS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_SEARCH_SERVING_USAGE_HISTORY;

-- S6 (#39) 30-day failed-login bursts (>= 5 failures for one user inside 15 minutes) and whether a SUCCESSFUL
-- login for that user followed within 60 minutes. LOGIN_HISTORY columns USER_NAME / EVENT_TIMESTAMP /
-- IS_SUCCESS ('YES'/'NO') / CLIENT_IP / ERROR_MESSAGE are read by security_sql (failed_login_reasons et al.),
-- REASON is that builder's exact ERROR_MESSAGE bucketing. A failure "closes a 5-in-15 window" when the 4th
-- prior failure for the same user is <= 15 min earlier, closing failures <= 60 min apart merge into one burst,
-- a burst is "followed" by the user's first success after the burst's first closing failure and within 60 min
-- of its last. FAIL_IPS counts the IPs of the CLOSING failures only. Top 10 users, the *_ALL columns are account
-- totals over EVERY burst (SUM OVER, not the top 10). Empty grid = no bursts in 30 days. NOTE: the app already
-- ships a looser version of this signal -- security_sql.login_takeover_candidates flags a success preceded by
-- >= 5 failures within 6 HOURS (max 30d) -- so a user here should also appear there, the reverse need not hold.
WITH lh AS (
    SELECT USER_NAME, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP,
           CASE
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%network%' THEN 'NETWORK POLICY'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%disabled%' THEN 'DISABLED USER'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%mfa%' THEN 'MFA'
             WHEN COALESCE(ERROR_MESSAGE, '') ILIKE ANY ('%password%', '%credential%', '%authentication%')
               THEN 'CREDENTIAL'
             ELSE 'OTHER'
           END AS REASON
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= DATEADD('day', -30, CURRENT_TIMESTAMP())
      AND USER_NAME IS NOT NULL
), f AS (
    SELECT USER_NAME, EVENT_TIMESTAMP AS FAIL_TS, CLIENT_IP, REASON,
           LAG(EVENT_TIMESTAMP, 4) OVER (PARTITION BY USER_NAME ORDER BY EVENT_TIMESTAMP) AS FOURTH_PRIOR_FAIL_TS
    FROM lh
    WHERE IS_SUCCESS = 'NO'
), closing AS (
    SELECT USER_NAME, FAIL_TS, CLIENT_IP, REASON,
           LAG(FAIL_TS) OVER (PARTITION BY USER_NAME ORDER BY FAIL_TS) AS PREV_CLOSING_TS
    FROM f
    WHERE FOURTH_PRIOR_FAIL_TS IS NOT NULL
      AND DATEDIFF('second', FOURTH_PRIOR_FAIL_TS, FAIL_TS) <= 900
), numbered AS (
    SELECT USER_NAME, FAIL_TS, CLIENT_IP, REASON,
           SUM(IFF(PREV_CLOSING_TS IS NULL OR DATEDIFF('second', PREV_CLOSING_TS, FAIL_TS) > 3600, 1, 0))
               OVER (PARTITION BY USER_NAME ORDER BY FAIL_TS ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS BURST_NO
    FROM closing
), bursts AS (
    SELECT USER_NAME, BURST_NO,
           MIN(FAIL_TS) AS BURST_START, MAX(FAIL_TS) AS BURST_END,
           COUNT(*) AS CLOSING_FAILS,
           COUNT(DISTINCT CLIENT_IP) AS FAIL_IPS,
           MAX_BY(CLIENT_IP, FAIL_TS) AS LAST_FAIL_IP,
           MODE(REASON) AS TOP_REASON
    FROM numbered
    GROUP BY 1, 2
), followed AS (
    SELECT b.USER_NAME, b.BURST_NO, b.BURST_START, b.BURST_END, b.CLOSING_FAILS, b.FAIL_IPS,
           b.LAST_FAIL_IP, b.TOP_REASON,
           MIN(s.EVENT_TIMESTAMP) AS FIRST_SUCCESS_AFTER,
           MIN_BY(s.CLIENT_IP, s.EVENT_TIMESTAMP) AS SUCCESS_IP
    FROM bursts b
    LEFT JOIN lh s
      ON s.USER_NAME = b.USER_NAME AND s.IS_SUCCESS = 'YES'
     AND s.EVENT_TIMESTAMP > b.BURST_START
     AND s.EVENT_TIMESTAMP <= DATEADD('minute', 60, b.BURST_END)
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
), per_user AS (
    SELECT USER_NAME,
           COUNT(*) AS BURSTS,
           COUNT_IF(FIRST_SUCCESS_AFTER IS NOT NULL) AS BURSTS_THEN_SUCCESS,
           COUNT_IF(FIRST_SUCCESS_AFTER IS NOT NULL AND SUCCESS_IP = LAST_FAIL_IP) AS SUCCESS_FROM_SAME_IP,
           MAX(FAIL_IPS) AS MAX_FAIL_IPS_IN_A_BURST,
           MODE(TOP_REASON) AS TOP_REASON,
           MAX(BURST_END) AS LAST_BURST_END,
           MAX(FIRST_SUCCESS_AFTER) AS LAST_SUCCESS_AFTER_BURST
    FROM followed
    GROUP BY USER_NAME
)
SELECT USER_NAME, BURSTS, BURSTS_THEN_SUCCESS, SUCCESS_FROM_SAME_IP, MAX_FAIL_IPS_IN_A_BURST, TOP_REASON,
       LAST_BURST_END, LAST_SUCCESS_AFTER_BURST,
       SUM(BURSTS) OVER () AS BURSTS_ALL,
       SUM(BURSTS_THEN_SUCCESS) OVER () AS BURSTS_THEN_SUCCESS_ALL,
       COUNT(*) OVER () AS USERS_WITH_BURSTS_ALL,
       SUM(IFF(BURSTS_THEN_SUCCESS > 0, 1, 0)) OVER () AS USERS_BURST_THEN_SUCCESS_ALL
FROM per_user
ORDER BY BURSTS_THEN_SUCCESS DESC, BURSTS DESC, USER_NAME
LIMIT 10;

-- S7a (#39) admin-role grants to users, last 30 days, by role (+ revokes and current direct holders). Role list =
-- the standard admin roles + this account's own admin roles from security_sql.ELEVATED_ROLES / ADMIN_HOLDER_ROLES
-- (SNOW_ACCOUNTADMINS, SNOW_SYSADMINS). GRANTS_TO_USERS columns ROLE / GRANTEE_NAME / GRANTED_BY / CREATED_ON /
-- DELETED_ON are read by security_sql (admin_grant_context, admin_role_holders, recent_grant_changes). USERADMIN
-- and ORGADMIN are in no repo list (standard Snowflake system role names, literals only). A role with no rows at
-- all is simply absent from the grid. Direct user grants only -- role-to-role elevation is S7d.
SELECT ROLE,
       COUNT_IF(CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS GRANTS_30D,
       COUNT_IF(CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP()) AND DELETED_ON IS NULL) AS GRANTED_30D_STILL_HELD,
       COUNT_IF(DELETED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS REVOKES_30D,
       COUNT(DISTINCT IFF(CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP()), GRANTEE_NAME, NULL)) AS USERS_GRANTED_30D,
       COUNT(DISTINCT IFF(CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP()), GRANTED_BY, NULL)) AS GRANTORS_30D,
       COUNT(DISTINCT IFF(DELETED_ON IS NULL, GRANTEE_NAME, NULL)) AS DIRECT_HOLDERS_NOW,
       MAX(CREATED_ON) AS LAST_GRANT_EVER
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
               'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
GROUP BY ROLE
ORDER BY GRANTS_30D DESC, DIRECT_HOLDERS_NOW DESC, ROLE;

-- S7b (#39) sample: the 25 newest admin grants to users in 30 days, with PRIOR_GRANTS (0 = first-ever elevation of
-- this user to this role) and the Central weekday / hour (off-hours flag) -- the same three signals as
-- security_sql.admin_grant_context. Its PRIOR_GRANTS is a correlated COUNT of rows with CREATED_ON strictly
-- earlier, and RANK() - 1 over the full retained history is exactly that count (ties share it, ROW_NUMBER would not).
-- GRANTED_BY '(system)' when blank, as security_sql.recent_grant_changes renders it.
WITH g AS (
    SELECT ROLE, GRANTEE_NAME, GRANTED_BY, CREATED_ON, DELETED_ON,
           RANK() OVER (PARTITION BY ROLE, GRANTEE_NAME ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                   'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
)
SELECT ROLE, GRANTEE_NAME AS USER_NAME,
       COALESCE(NULLIF(TRIM(GRANTED_BY), ''), '(system)') AS GRANTED_BY,
       CREATED_ON AS GRANTED_AT, DELETED_ON AS REVOKED_AT, PRIOR_GRANTS,
       DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', CREATED_ON)) AS DOW_ISO,
       HOUR(CONVERT_TIMEZONE('America/Chicago', CREATED_ON)) AS HOUR_CT
FROM g
WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
ORDER BY CREATED_ON DESC
LIMIT 25;

-- S7c (#39) any OTHER '%ADMIN%'-named role granted to users in 30 days that the lists above miss (tells whether the
-- repo's admin-role lists are complete for this account). Empty grid = the lists are complete.
SELECT ROLE,
       COUNT(*) AS GRANTS_30D,
       COUNT(DISTINCT GRANTEE_NAME) AS USERS,
       MAX(CREATED_ON) AS LAST_GRANT
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
  AND ROLE ILIKE '%ADMIN%'
  AND ROLE NOT IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                   'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
GROUP BY ROLE
ORDER BY GRANTS_30D DESC
LIMIT 25;

-- S7d (#39) admin roles granted to (or revoked from) other ROLES in 30 days -- elevation that GRANTS_TO_USERS
-- cannot see (every holder of GRANTEE_ROLE inherits the admin role). GRANTS_TO_ROLES with GRANTED_ON = 'ROLE',
-- NAME = the granted role, GRANTEE_NAME = the receiving role (security_sql.effective_access walks the same
-- edges, recent_grant_changes reads NAME / PRIVILEGE / GRANTED_ON / GRANTED_BY). PRIVILEGE is shown:
-- USAGE = inheritance, OWNERSHIP = who owns the admin role object (effective_access does not filter on it).
SELECT NAME AS ADMIN_ROLE, GRANTEE_NAME AS GRANTEE_ROLE, PRIVILEGE,
       COALESCE(NULLIF(TRIM(GRANTED_BY), ''), '(system)') AS GRANTED_BY,
       CREATED_ON AS GRANTED_AT, DELETED_ON AS REVOKED_AT
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
WHERE GRANTED_ON = 'ROLE'
  AND NAME IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
               'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
  AND (CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
       OR DELETED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP()))
ORDER BY COALESCE(DELETED_ON, CREATED_ON) DESC
LIMIT 25;

-- ---------------- end of group S, GROUP C follows. If Run All stopped above, copy the error as
-- ---------------- that block's answer, highlight from the block AFTER it to the end of the file, run again.

-- =====================================================================================
--  GROUP C  --  cost + storage + delivery probes (Next-Fifty #29 / #30 / #45 / #40), 2026-09-29
--  READ-ONLY: SELECT / SHOW / DESCRIBE / RESULT_SCAN / SET (one session variable) only; the
--  ALTER SESSION only sets this worksheet's clock. Paste back every grid (an empty grid is an answer).
--  ORDER = RISK. A Snowsight "Run All" stops at the first error, so blocks run from safest to riskiest:
--    C2a, C2b  every column already read by the repo
--    C1a, C1b  one unverified column (TABLE_STORAGE_METRICS.IS_TRANSIENT, Snowflake docs)
--    C4a       two unverified SHOW columns ("type", "enabled", Snowflake docs)
--    C4b       DESC of OVERWATCH_EMAIL (it existed and was ENABLED in the 2026-09-24 round-1 probe)
--    C2c, C2d  three unverified table-id columns, plus the only slow scan in the file
--  If any block errors, paste the error and run the blocks below it on their own.
--  HEAVIEST BLOCK: C2c (one pass over up to 90 days of ACCESS_HISTORY). Everything else takes seconds.
-- =====================================================================================

-- C2a (#30) How many maintenance credits (automatic clustering, search optimization, MV refresh) did each
--       service burn over the last 90 COMPLETE days (Central), on how many objects, and which single object
--       cost the most? Cheap: three serverless-history scans, no ACCESS_HISTORY. The ALL row is the ceiling
--       for the #30 finding. The window [today-90, today) and the COALESCE'd name match exactly how
--       SP_LOAD_OBJECT_COST (V139) builds FACT_OBJECT_COST_DAILY, so C2b's MAINT_CREDITS should agree with
--       this block to within the fact's own gaps.
--       $ uses SETTINGS.CREDIT_PRICE_USD (TRY_TO_DOUBLE: the V153 lesson, TRY_TO_NUMBER('3.68') = 4).
WITH maint AS (
    SELECT 'CLUSTERING' AS SERVICE,
           COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN') AS OBJECT_FQN,
           SUM(CREDITS_USED) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND START_TIME < CURRENT_DATE() AND CREDITS_USED > 0
    GROUP BY 1, 2
    UNION ALL
    SELECT 'SEARCH_OPT',
           COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(BASE_TABLE_NAME, 'UNKNOWN'),
           SUM(CREDITS_USED)
    FROM SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND START_TIME < CURRENT_DATE() AND CREDITS_USED > 0
    GROUP BY 1, 2
    UNION ALL
    SELECT 'MV_REFRESH',
           COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN'),
           SUM(CREDITS_USED)
    FROM SNOWFLAKE.ACCOUNT_USAGE.MATERIALIZED_VIEW_REFRESH_HISTORY
    WHERE START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND START_TIME < CURRENT_DATE() AND CREDITS_USED > 0
    GROUP BY 1, 2
), price AS (
    SELECT COALESCE(MAX(TRY_TO_DOUBLE(VALUE)), 3.68) AS USD_PER_CREDIT
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD'
)
SELECT IFF(GROUPING(SERVICE) = 1, 'ALL', SERVICE)                    AS SVC,
       COUNT(DISTINCT OBJECT_FQN)                                   AS OBJECTS,
       ROUND(SUM(CREDITS), 2)                                       AS CREDITS_90D,
       ROUND(SUM(CREDITS) * ANY_VALUE(price.USD_PER_CREDIT), 2)     AS USD_90D,
       MAX_BY(OBJECT_FQN, CREDITS)                                  AS TOP_OBJECT,
       ROUND(MAX(CREDITS), 2)                                       AS TOP_OBJECT_CREDITS
FROM maint CROSS JOIN price
GROUP BY ROLLUP (SERVICE)
ORDER BY IFF(SVC = 'ALL', 0, 1), CREDITS_90D DESC;

-- C2b (#30) Does OVERWATCH's OWN object-cost fact already answer "maintenance credits on objects no billed
--       query read"? (the #30 rec's mart-only design). Cheap: FACT_OBJECT_COST_DAILY only, same window as C2a.
--       "Read" here = the QUERY_COMPUTE_READ arm (V139: ACCESS_HISTORY BASE_OBJECTS_ACCESSED, domains Table /
--       Materialized view, joined to QUERY_ATTRIBUTION_HISTORY; a query that also WROTE the object is booked as
--       WRITE, so this is already a "pure read"). Result-cache / metadata-only reads bill nothing and are
--       invisible here, and objects are matched by UPPER(name), not id, so OBJECTS_NO_BILLED_READ is an UPPER
--       bound on "unread". Two decisions ride on it: (1) MAINT_CREDITS per service should match C2a (if
--       FACT_DAYS < 90 the fact has holes -- say so); (2) compare OBJECTS_NO_BILLED_READ with C2d's
--       UNREAD_OBJECTS to see what the cheap proxy over-flags. READ_CREDITS_ON_THESE counts each object once
--       in the ALL row even when it has two maintenance services.
WITH f AS (
    SELECT UPPER(OBJECT_FQN) AS FQN, COST_ARM, SUM(CREDITS) AS CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
    WHERE DAY >= DATEADD('day', -90, CURRENT_DATE()) AND DAY < CURRENT_DATE()
      AND COST_ARM IN ('CLUSTERING', 'SEARCH_OPT', 'MV_REFRESH', 'QUERY_COMPUTE_READ')
    GROUP BY 1, 2
), m AS (
    SELECT FQN, COST_ARM AS SERVICE, CREDITS,
           ROW_NUMBER() OVER (PARTITION BY FQN ORDER BY COST_ARM) AS SVC_RANK
    FROM f WHERE COST_ARM <> 'QUERY_COMPUTE_READ'
), r AS (
    SELECT FQN, CREDITS AS READ_CREDITS FROM f WHERE COST_ARM = 'QUERY_COMPUTE_READ'
), cov AS (
    SELECT MIN(DAY) AS FACT_COVERS_FROM, MAX(DAY) AS FACT_NEWEST_DAY, COUNT(DISTINCT DAY) AS FACT_DAYS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY
    WHERE DAY >= DATEADD('day', -90, CURRENT_DATE()) AND DAY < CURRENT_DATE()
)
SELECT IFF(GROUPING(m.SERVICE) = 1, 'ALL', m.SERVICE)                              AS SVC,
       COUNT(DISTINCT m.FQN)                                                       AS OBJECTS,
       ROUND(SUM(m.CREDITS), 2)                                                    AS MAINT_CREDITS,
       COUNT(DISTINCT IFF(r.FQN IS NULL, m.FQN, NULL))                             AS OBJECTS_NO_BILLED_READ,
       ROUND(SUM(IFF(r.FQN IS NULL, m.CREDITS, 0)), 2)                             AS MAINT_CREDITS_NO_BILLED_READ,
       ROUND(IFF(GROUPING(m.SERVICE) = 1,
                 SUM(IFF(m.SVC_RANK = 1, COALESCE(r.READ_CREDITS, 0), 0)),
                 SUM(COALESCE(r.READ_CREDITS, 0))), 2)                             AS READ_CREDITS_ON_THESE,
       ANY_VALUE(cov.FACT_COVERS_FROM)                                             AS FACT_COVERS_FROM,
       ANY_VALUE(cov.FACT_NEWEST_DAY)                                              AS FACT_NEWEST_DAY,
       ANY_VALUE(cov.FACT_DAYS)                                                    AS FACT_DAYS
FROM m LEFT JOIN r ON r.FQN = m.FQN
CROSS JOIN cov
GROUP BY ROLLUP (m.SERVICE)
ORDER BY IFF(SVC = 'ALL', 0, 1), MAINT_CREDITS DESC;

-- C1a (#29) How much fail-safe (and time-travel) do staging-named schemas carry vs their active bytes, split
--       by IS_TRANSIENT, and does it sit on LIVE tables or on DROPPED copies? Permanent tables keep 7 days of
--       fail-safe; TRANSIENT tables keep none, so FS_GB on IS_TRANSIENT = 'NO' is what a transient conversion
--       would stop billing. FS_GB_FROM_DROPPED is fail-safe on dropped incarnations (CREATE OR REPLACE each
--       load); the planned #29 host, storage_waste, filters DELETED = FALSE (app/data/insights_sql.py l.686)
--       and can NOT see those bytes -- if FS_GB_FROM_DROPPED dominates, #29 needs a different read.
--       MATCH_RULE: 'APP SUFFIX' = exactly the staging pattern volume_deltas already ships (schema ends in
--       _STAG / _STG / _STAGING, app/data/ops_sql.py l.1567-1569); 'DELIMITED WORD' = STAG/STG/STAGE/STAGING
--       as a whole _-delimited word elsewhere (e.g. STAGING, STG_X); 'SUBSTRING ONLY' = likely false positive
--       (e.g. POSTGRES). ROW_KIND: TOTAL, BY_TRANSIENT (subtotal per IS_TRANSIENT), SCHEMA (detail).
--       $ uses SETTINGS.STORAGE_USD_PER_TB_MONTH (TB = binary TiB, app/config.py l.56; 23.00 if absent).
--       AN ERROR "invalid identifier 'IS_TRANSIENT'" HERE IS ITSELF THE ANSWER (the column is from the docs).
WITH p AS (
    SELECT COALESCE(MAX(TRY_TO_DOUBLE(VALUE)), 23.0) AS USD_PER_TIB_MONTH
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'STORAGE_USD_PER_TB_MONTH'
), m AS (
    SELECT TABLE_CATALOG, TABLE_SCHEMA, IS_TRANSIENT, COALESCE(DELETED, FALSE) AS IS_DROPPED,
           ACTIVE_BYTES, TIME_TRAVEL_BYTES, FAILSAFE_BYTES, COALESCE(RETAINED_FOR_CLONE_BYTES, 0) AS CLONE_BYTES,
           CASE WHEN UPPER(TABLE_SCHEMA) LIKE '%!_STAG' ESCAPE '!'
                  OR UPPER(TABLE_SCHEMA) LIKE '%!_STG' ESCAPE '!'
                  OR UPPER(TABLE_SCHEMA) LIKE '%!_STAGING' ESCAPE '!'                   THEN 'APP SUFFIX'
                WHEN REGEXP_LIKE(UPPER(TABLE_SCHEMA), '(.*_)?(STAG|STG|STAGE|STAGING)(_.*)?') THEN 'DELIMITED WORD'
                ELSE 'SUBSTRING ONLY' END AS MATCH_RULE
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS
    WHERE (TABLE_SCHEMA ILIKE '%STAG%' OR TABLE_SCHEMA ILIKE '%STG%')
      AND ACTIVE_BYTES + TIME_TRAVEL_BYTES + FAILSAFE_BYTES + COALESCE(RETAINED_FOR_CLONE_BYTES, 0) > 0
)
SELECT CASE WHEN GROUPING(IS_TRANSIENT) = 1 THEN 'TOTAL'
            WHEN GROUPING(TABLE_SCHEMA) = 1 THEN 'BY_TRANSIENT'
            ELSE 'SCHEMA' END                                                   AS ROW_KIND,
       TABLE_CATALOG AS DATABASE_NAME, TABLE_SCHEMA AS SCHEMA_NAME, MATCH_RULE, IS_TRANSIENT,
       COUNT_IF(NOT IS_DROPPED)                                                 AS LIVE_TABLES,
       COUNT_IF(IS_DROPPED)                                                     AS DROPPED_INCARNATIONS,
       ROUND(SUM(ACTIVE_BYTES) / POWER(1024, 3), 1)                             AS ACTIVE_GB,
       ROUND(SUM(TIME_TRAVEL_BYTES) / POWER(1024, 3), 1)                        AS TT_GB,
       ROUND(SUM(FAILSAFE_BYTES) / POWER(1024, 3), 1)                           AS FS_GB,
       ROUND(SUM(IFF(IS_DROPPED, FAILSAFE_BYTES, 0)) / POWER(1024, 3), 1)      AS FS_GB_FROM_DROPPED,
       ROUND(SUM(CLONE_BYTES) / POWER(1024, 3), 1)                              AS CLONE_RETAINED_GB,
       ROUND(SUM(FAILSAFE_BYTES) / NULLIF(SUM(ACTIVE_BYTES), 0), 2)             AS FS_PER_ACTIVE_BYTE,
       ROUND(SUM(FAILSAFE_BYTES) / POWER(1024, 4) * ANY_VALUE(p.USD_PER_TIB_MONTH), 2)     AS FS_USD_PER_MONTH,
       ROUND(SUM(TIME_TRAVEL_BYTES) / POWER(1024, 4) * ANY_VALUE(p.USD_PER_TIB_MONTH), 2)  AS TT_USD_PER_MONTH
FROM m CROSS JOIN p
GROUP BY GROUPING SETS ((TABLE_CATALOG, TABLE_SCHEMA, MATCH_RULE, IS_TRANSIENT), (IS_TRANSIENT), ())
ORDER BY CASE ROW_KIND WHEN 'TOTAL' THEN 0 WHEN 'BY_TRANSIENT' THEN 1 ELSE 2 END, FS_GB DESC NULLS LAST
LIMIT 80;

-- C1b (#29) Which staging tables carry the fail-safe? Top 25 table NAMES by fail-safe, every incarnation of a
--       name (the live row + dropped copies) folded into one row. FS_GB_ON_LIVE_ROW is the part a
--       storage_waste-based verdict can see; FS_GB_ON_DROPPED (with DROPPED_INCARNATIONS > 1) is reload-by-
--       replace churn it cannot. TT_FS_PER_LIVE_ACTIVE = (all TT + FS) / the live copy's active bytes -- the
--       ratio the rec's "FAILSAFE + TIME_TRAVEL >= k x ACTIVE" test would use, so it shows where k should sit.
--       IS_TRANSIENT = 'NO' with large FS is the #29 candidate. Same unverified column as C1a.
SELECT TABLE_CATALOG AS DATABASE_NAME, TABLE_SCHEMA AS SCHEMA_NAME, TABLE_NAME, IS_TRANSIENT,
       (UPPER(TABLE_SCHEMA) LIKE '%!_STAG' ESCAPE '!' OR UPPER(TABLE_SCHEMA) LIKE '%!_STG' ESCAPE '!'
        OR UPPER(TABLE_SCHEMA) LIKE '%!_STAGING' ESCAPE '!')                                   AS IN_APP_SUFFIX_PATTERN,
       COUNT_IF(NOT COALESCE(DELETED, FALSE))                                                 AS LIVE_ROWS,
       COUNT_IF(COALESCE(DELETED, FALSE))                                                     AS DROPPED_INCARNATIONS,
       ROUND(SUM(ACTIVE_BYTES) / POWER(1024, 3), 2)                                           AS ACTIVE_GB,
       ROUND(SUM(TIME_TRAVEL_BYTES) / POWER(1024, 3), 2)                                      AS TT_GB,
       ROUND(SUM(FAILSAFE_BYTES) / POWER(1024, 3), 2)                                         AS FS_GB,
       ROUND(SUM(IFF(COALESCE(DELETED, FALSE), 0, FAILSAFE_BYTES)) / POWER(1024, 3), 2)       AS FS_GB_ON_LIVE_ROW,
       ROUND(SUM(IFF(COALESCE(DELETED, FALSE), FAILSAFE_BYTES, 0)) / POWER(1024, 3), 2)       AS FS_GB_ON_DROPPED,
       ROUND((SUM(TIME_TRAVEL_BYTES) + SUM(FAILSAFE_BYTES))
             / NULLIF(SUM(IFF(COALESCE(DELETED, FALSE), 0, ACTIVE_BYTES)), 0), 1)            AS TT_FS_PER_LIVE_ACTIVE
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS
WHERE (TABLE_SCHEMA ILIKE '%STAG%' OR TABLE_SCHEMA ILIKE '%STG%')
GROUP BY 1, 2, 3, 4, 5
HAVING SUM(FAILSAFE_BYTES) > 0
ORDER BY FS_GB DESC
LIMIT 25;

-- C3 (#45) NO SQL HERE. Re-run snowflake/run/DIAG_CS_SELF_COST.sql (already on the runbox branch) top to
--       bottom in its own worksheet and paste back every grid; the before-numbers are the 2026-09-26 run.

-- C4a (#40) Which notification integrations exist, are they enabled, and which ALERT_ROUTES point at each?
--       Expect OVERWATCH_EMAIL (native e-mail alerts, snowflake/native_alert_templates.sql; NOT routed through
--       ALERT_ROUTES, so 0 routes there is normal) and OVERWATCH_WEBHOOK_TEAMS (Teams delivery,
--       snowflake/webhook_delivery.sql; >= 1 enabled route expected). The SHOW lists ALL notification
--       integrations (as the Alerts page does) so a route naming a missing or non-OVERWATCH integration shows
--       up with EXISTS_IN_ACCOUNT = FALSE instead of being filtered out. Run the SHOW and the SELECT together:
--       the SELECT reads the SHOW's grid via RESULT_SCAN. The "type" / "enabled" SHOW columns are from the docs.
SHOW NOTIFICATION INTEGRATIONS;
WITH s AS (
    SELECT UPPER("name") AS NAME_U, "type"::STRING AS INTEG_TYPE, "enabled"::STRING AS ENABLED
    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
), w AS (
    SELECT 'OVERWATCH_EMAIL' AS NAME_U, 'native e-mail alerts (not via ALERT_ROUTES)' AS EXPECTED_FOR
    UNION ALL
    SELECT 'OVERWATCH_WEBHOOK_TEAMS', 'Teams delivery via ALERT_ROUTES'
), r AS (
    SELECT UPPER(INTEGRATION_NAME) AS NAME_U, COUNT_IF(ENABLED) AS ENABLED_ROUTES, COUNT(*) AS ALL_ROUTES
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
    GROUP BY 1
), sw AS (
    SELECT COALESCE(s.NAME_U, w.NAME_U) AS NAME_U, w.EXPECTED_FOR, s.NAME_U IS NOT NULL AS EXISTS_IN_ACCOUNT,
           s.INTEG_TYPE, s.ENABLED
    FROM s FULL OUTER JOIN w ON w.NAME_U = s.NAME_U
)
SELECT COALESCE(sw.NAME_U, r.NAME_U) AS INTEGRATION, sw.EXPECTED_FOR,
       COALESCE(sw.EXISTS_IN_ACCOUNT, FALSE) AS EXISTS_IN_ACCOUNT, sw.INTEG_TYPE, sw.ENABLED,
       COALESCE(r.ENABLED_ROUTES, 0) AS ENABLED_ALERT_ROUTES, COALESCE(r.ALL_ROUTES, 0) AS ALL_ALERT_ROUTES
FROM sw FULL OUTER JOIN r ON r.NAME_U = sw.NAME_U
WHERE COALESCE(sw.NAME_U, r.NAME_U) LIKE 'OVERWATCH%' OR sw.EXPECTED_FOR IS NOT NULL OR r.NAME_U IS NOT NULL
ORDER BY 1;

-- C4b (#40) What are OVERWATCH_EMAIL's properties: ENABLED and ALLOWED_RECIPIENTS (the only addresses
--       SYSTEM$SEND_EMAIL may reach)? AN ERROR HERE IS ITSELF THE ANSWER: "does not exist or not authorized"
--       = no e-mail path (C4a's EXISTS_IN_ACCOUNT will already say FALSE); then run C2c + C2d on their own.
--       (When pasting back you may replace the addresses with a count.)
DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;

-- C4c (#40) MANUAL STEP (not SQL) -- does the Streamlit-in-Snowflake app keep a ?page= parameter?
--       The app reads st.query_params['page'] and matches it to a page name lower-cased with spaces as '-'
--       (app/core/state.py requested_page), so page=alerts should open the Alerts page.
--       1. Open OVERWATCH in Snowsight and copy the address bar URL (it ends in
--          .../#/streamlit-apps/DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP).
--       2. Test A: append ?page=alerts to the END of that URL; open it in a NEW browser tab.
--       3. Test B: put ?page=alerts BEFORE the '#' instead (https://app.snowflake.com/<org>/<acct>/?page=alerts#/streamlit-apps/...).
--       4. Test C: paste the Test-A link into a Teams chat to yourself and click it from Teams (the path a Teams
--          alert link would take).
--       For each test report: (a) which page the app opened on (Alerts, or your default landing page);
--       (b) whether the address bar still shows page=alerts after the app finishes loading;
--       (c) then click a different page in the OVERWATCH sidebar -- does the address bar's page= change with it?
--       Alerts in all three = deep links work; default page every time = SiS drops the parameter and #40's
--       alert links must point at the app root instead.

-- C2c (#30) Which maintained objects did NO query read in the window, and how many maintenance credits do
--       they burn? HEAVIEST BLOCK IN THE FILE -- keep it last. ONE pass over ACCESS_HISTORY:
--       BASE_OBJECTS_ACCESSED + DIRECT_OBJECTS_ACCESSED (reads; DIRECT is where a materialized view read by
--       name lands) and OBJECTS_MODIFIED (writes) are flattened in the same scan and kept only for the
--       maintained objects, joined on objectId = the maintenance row's table id (the repo's D4 join key;
--       names break on quoted identifiers). A MERGE/UPDATE can list its own target as "accessed", so
--       READ_QUERIES_PURE drops queries that also wrote the object: VERDICT 'UNREAD (only its own DML touched
--       it)' = nobody but the loader looks at it. Maintenance window = C2a's [today-N, today); reads run from
--       the same start through now. With C2_DAYS = 90, C2d's ALL CREDITS_TOTAL should equal C2a's ALL
--       CREDITS_90D to within rounding (a built-in check). Reads by OTHER accounts through a data share are
--       not in this account's ACCESS_HISTORY, so treat UNREAD as "no read seen in this account".
--       One row per (service, object), UNREAD first: paste the first ~40 rows plus the whole C2d grid.
--       Cost: minutes on an X-Small (a guess). If it runs past ~10 min, cancel, change C2_DAYS to 30 and re-run
--       from the SET line. AN ERROR HERE IS ITSELF THE ANSWER: "invalid identifier 'TABLE_ID'" (or
--       'BASE_TABLE_ID') means that history view has no table id in this account -- paste the error.
SET C2_DAYS = 90;
WITH maint AS (
    SELECT 'CLUSTERING' AS SERVICE, TABLE_ID AS OBJ_ID,
           COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN') AS OBJECT_FQN,
           SUM(CREDITS_USED) AS CREDITS, MAX(START_TIME) AS LAST_MAINT
    FROM SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
    WHERE START_TIME >= DATEADD('day', -1 * LEAST(GREATEST($C2_DAYS, 1), 90), CURRENT_DATE())
      AND START_TIME < CURRENT_DATE() AND CREDITS_USED > 0
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT 'SEARCH_OPT', BASE_TABLE_ID,
           COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(BASE_TABLE_NAME, 'UNKNOWN'),
           SUM(CREDITS_USED), MAX(START_TIME)
    FROM SNOWFLAKE.ACCOUNT_USAGE.SEARCH_OPTIMIZATION_HISTORY
    WHERE START_TIME >= DATEADD('day', -1 * LEAST(GREATEST($C2_DAYS, 1), 90), CURRENT_DATE())
      AND START_TIME < CURRENT_DATE() AND CREDITS_USED > 0
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT 'MV_REFRESH', TABLE_ID,
           COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN') || '.' || COALESCE(TABLE_NAME, 'UNKNOWN'),
           SUM(CREDITS_USED), MAX(START_TIME)
    FROM SNOWFLAKE.ACCOUNT_USAGE.MATERIALIZED_VIEW_REFRESH_HISTORY
    WHERE START_TIME >= DATEADD('day', -1 * LEAST(GREATEST($C2_DAYS, 1), 90), CURRENT_DATE())
      AND START_TIME < CURRENT_DATE() AND CREDITS_USED > 0
    GROUP BY 1, 2, 3
), id_map AS (
    SELECT DISTINCT OBJ_ID, OBJECT_FQN FROM maint WHERE OBJ_ID IS NOT NULL
), touch AS (
    SELECT a.QUERY_ID, a.USER_NAME, a.QUERY_START_TIME,
           f.value:"objectId"::NUMBER AS OBJ_ID, g.value:"k"::STRING AS KIND
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => ARRAY_CONSTRUCT(
             OBJECT_CONSTRUCT('k', 'R', 'a', a.BASE_OBJECTS_ACCESSED),
             OBJECT_CONSTRUCT('k', 'R', 'a', a.DIRECT_OBJECTS_ACCESSED),
             OBJECT_CONSTRUCT('k', 'W', 'a', a.OBJECTS_MODIFIED))) g,
         LATERAL FLATTEN(input => g.value:"a") f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -1 * LEAST(GREATEST($C2_DAYS, 1), 90), CURRENT_DATE())
      AND f.value:"objectId"::NUMBER IN (SELECT OBJ_ID FROM id_map)
), per_q AS (
    SELECT i.OBJECT_FQN, t.QUERY_ID, ANY_VALUE(t.USER_NAME) AS USER_NAME, MAX(t.QUERY_START_TIME) AS TS,
           MAX(IFF(t.KIND = 'R', 1, 0)) AS READ_IT, MAX(IFF(t.KIND = 'W', 1, 0)) AS WROTE_IT
    FROM touch t JOIN id_map i ON i.OBJ_ID = t.OBJ_ID
    GROUP BY 1, 2
), per_obj AS (
    SELECT OBJECT_FQN,
           COUNT_IF(READ_IT = 1)                                                AS READ_QUERIES_ANY,
           COUNT_IF(READ_IT = 1 AND WROTE_IT = 0)                               AS READ_QUERIES_PURE,
           COUNT(DISTINCT IFF(READ_IT = 1 AND WROTE_IT = 0, USER_NAME, NULL))   AS PURE_READERS,
           COUNT_IF(WROTE_IT = 1)                                               AS WRITE_QUERIES,
           MAX(IFF(READ_IT = 1 AND WROTE_IT = 0, TS, NULL))                     AS LAST_PURE_READ
    FROM per_q GROUP BY 1
), obj AS (
    SELECT SERVICE, OBJECT_FQN, COUNT(DISTINCT OBJ_ID) AS INCARNATIONS, COUNT_IF(OBJ_ID IS NULL) AS ROWS_WITHOUT_ID,
           SUM(CREDITS) AS CREDITS, MAX(LAST_MAINT) AS LAST_MAINT
    FROM maint GROUP BY 1, 2
), price AS (
    SELECT COALESCE(MAX(TRY_TO_DOUBLE(VALUE)), 3.68) AS USD_PER_CREDIT
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD'
)
SELECT o.SERVICE, o.OBJECT_FQN, o.INCARNATIONS, o.ROWS_WITHOUT_ID,
       ROUND(o.CREDITS, 3)                                     AS CREDITS,
       ROUND(o.CREDITS * p.USD_PER_CREDIT, 2)                  AS USD,
       o.LAST_MAINT,
       COALESCE(po.READ_QUERIES_ANY, 0)                        AS READ_QUERIES_ANY,
       COALESCE(po.READ_QUERIES_PURE, 0)                       AS READ_QUERIES_PURE,
       COALESCE(po.PURE_READERS, 0)                            AS PURE_READERS,
       COALESCE(po.WRITE_QUERIES, 0)                           AS WRITE_QUERIES,
       po.LAST_PURE_READ,
       CASE WHEN o.INCARNATIONS = 0                    THEN 'UNKNOWN (no table id)'
            WHEN COALESCE(po.READ_QUERIES_ANY, 0) = 0  THEN 'UNREAD (no access at all)'
            WHEN COALESCE(po.READ_QUERIES_PURE, 0) = 0 THEN 'UNREAD (only its own DML touched it)'
            ELSE 'READ' END                                    AS VERDICT
FROM obj o
LEFT JOIN per_obj po ON po.OBJECT_FQN = o.OBJECT_FQN
CROSS JOIN price p
ORDER BY IFF(VERDICT = 'READ', 1, 0), CREDITS DESC;

-- C2d (#30) What is the #30 summary -- objects, credits, and credits on unread objects, per service + ALL?
--       Run it RIGHT AFTER C2c: it re-reads C2c's grid through RESULT_SCAN (no second ACCESS_HISTORY scan).
--       If C2c failed or was cancelled this errors too; that is expected. Object counts are DISTINCT names,
--       so the ALL row counts a table with two maintenance services once (credits still add across services).
SELECT IFF(GROUPING(SERVICE) = 1, 'ALL', SERVICE)                                        AS SVC,
       COUNT(DISTINCT OBJECT_FQN)                                                       AS OBJECTS,
       ROUND(SUM(CREDITS), 2)                                                           AS CREDITS_TOTAL,
       ROUND(SUM(USD), 2)                                                               AS USD_TOTAL,
       COUNT(DISTINCT IFF(VERDICT LIKE 'UNREAD%', OBJECT_FQN, NULL))                    AS UNREAD_OBJECTS,
       ROUND(SUM(IFF(VERDICT LIKE 'UNREAD%', CREDITS, 0)), 2)                           AS CREDITS_ON_UNREAD,
       ROUND(SUM(IFF(VERDICT LIKE 'UNREAD%', USD, 0)), 2)                               AS USD_ON_UNREAD,
       COUNT(DISTINCT IFF(VERDICT = 'UNREAD (no access at all)', OBJECT_FQN, NULL))     AS NEVER_TOUCHED_OBJECTS,
       ROUND(SUM(IFF(VERDICT = 'UNREAD (no access at all)', CREDITS, 0)), 2)            AS CREDITS_NEVER_TOUCHED,
       ROUND(100 * SUM(IFF(VERDICT LIKE 'UNREAD%', CREDITS, 0)) / NULLIF(SUM(CREDITS), 0), 1) AS PCT_CREDITS_UNREAD,
       COUNT(DISTINCT IFF(VERDICT LIKE 'UNKNOWN%', OBJECT_FQN, NULL))                   AS UNKNOWN_NO_ID_OBJECTS
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
GROUP BY ROLLUP (SERVICE)
ORDER BY IFF(SVC = 'ALL', 0, 1), CREDITS_TOTAL DESC;

-- ---------------- end of group C, GROUP P follows. If Run All stopped above, copy the error as
-- ---------------- that block's answer, highlight from the block AFTER it to the end of the file, run again.

-- =====================================================================================
--  GROUP P  --  PR B follow-ups (v4.600.0 deployed). READ-ONLY: SELECT / DESCRIBE / RESULT_SCAN /
--  SET session variables / ALTER SESSION SET TIMEZONE only. Run top to bottom; paste back every grid
--  (an empty grid is an answer: say 'no rows'). Snowsight 'Run All' stops at the first error, so the order
--  is simplest-first: P1 reads only OVERWATCH tables; P2 reads CONTROL_STATUS (the role read it in
--  PROBES_WAVE2 E1-E6); P3 goes LAST because it is the HEAVIEST and most complex block (two 3-day
--  QUERY_HISTORY reads + one 3-day QUERY_ATTRIBUTION_HISTORY read, capped at 200 CALLs). If a block
--  errors, run the remaining blocks by selecting them.
--    P1 (#31) revert preview: which booked savings the daily scan saw undone, and what left the ROI numerator
--    P2 (#14) CONTROL_STATUS columns: is there an Informatica error-message column, and is it filled?
--    P3 (#14) does the drill's SESSION_ID + [CALL start, CALL end] child linkage (and its CONTROL_STATUS
--             +/-5 min clip) find the children QUERY_ATTRIBUTION_HISTORY proves (ROOT_QUERY_ID = the CALL)?
-- =====================================================================================

-- P1a (#31) REVERT PREVIEW: which booked savings now leave the run-rate -- every ledger row whose booked
-- warehouse change the daily scan later saw undone. The rv CTE is the app's own
-- mart_sql._ledger_revert_select() rendered verbatim (registry-only; REVERTED_AT = the undoing change's
-- CHANGE_SEEN_AT, TIMESTAMP_LTZ). SAVINGS_LEDGER columns per mart_sql.savings_ledger(); registry columns
-- (CHANGE_ID / WAREHOUSE_NAME / SETTING / OLD_VALUE / NEW_VALUE / CHANGE_SEEN_AT) per the same builder.
-- LEFT_ROI_NUMERATOR = STATE 'VERIFIED' and VERIFIED_AT inside SAVINGS_ACTIVE_MONTHS=12 of account-today:
-- this row's VERIFIED_USD actually LEFT Proof's ROI numerator (the STATE test matters: V153 also stamps
-- VERIFIED_AT on a REJECTED row, and a reverted VERIFIED row older than 12 months had already aged out).
-- LEFT_OPEN_PIPELINE = an ESTIMATED row: its ESTIMATED_USD left ESTIMATED_OPEN_USD instead.
-- REVERT_VIA_PARTNER = TRUE when the carried revert touched a different setting (inherited from a same-scan
-- partner -> REVERT_KIND 'partial'). A manual row (SOURCE_CHANGE_ID NULL) is never revert-checked, so it never
-- appears here (nor a twin: a twin is by definition a SOURCE_CHANGE_ID-NULL manual row).
WITH rv AS (
    SELECT g.CHANGE_ID AS BOOKED_CHANGE_ID, x.REVERT_CHANGE_ID, x.REVERTED_AT, x.REVERT_OLD_VALUE,
           x.REVERT_NEW_VALUE,
           IFF(MAX(IFF(x.BOOKED_CHANGE_ID = g.CHANGE_ID AND x.REVERT_KIND = 'full', 1, 0)) OVER (PARTITION BY g.CHANGE_ID) = 1,
               'full', 'partial') AS REVERT_KIND,
           x.REVERT_SETTING
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY g
    JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY p
      ON p.WAREHOUSE_NAME = g.WAREHOUSE_NAME
     AND p.CHANGE_SEEN_AT = g.CHANGE_SEEN_AT
     AND p.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')
     AND (p.CHANGE_ID = g.CHANGE_ID OR CASE p.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(p.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(p.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(p.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(p.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(p.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END < CASE p.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(p.OLD_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(p.OLD_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(p.OLD_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(p.OLD_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(p.OLD_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END)
    JOIN (
        SELECT b.CHANGE_ID AS BOOKED_CHANGE_ID, n.CHANGE_ID AS REVERT_CHANGE_ID,
               n.CHANGE_SEEN_AT AS REVERTED_AT, n.OLD_VALUE AS REVERT_OLD_VALUE,
               n.NEW_VALUE AS REVERT_NEW_VALUE, n.SETTING AS REVERT_SETTING,
               IFF(MAX(CASE n.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(n.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(n.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(n.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(n.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(n.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END) OVER (PARTITION BY b.CHANGE_ID) >= CASE b.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(b.OLD_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(b.OLD_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(b.OLD_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(b.OLD_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(b.OLD_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END, 'full', 'partial') AS REVERT_KIND
        FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY b
        JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY n
          ON n.WAREHOUSE_NAME = b.WAREHOUSE_NAME
         AND n.SETTING = b.SETTING
         AND n.CHANGE_SEEN_AT > b.CHANGE_SEEN_AT
         AND CASE n.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(n.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(n.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(n.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(n.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(n.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END > CASE b.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(b.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(b.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(b.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(b.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(b.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END
        WHERE b.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')
        QUALIFY ROW_NUMBER() OVER (PARTITION BY b.CHANGE_ID ORDER BY n.CHANGE_SEEN_AT, n.CHANGE_ID) = 1
    ) x ON x.BOOKED_CHANGE_ID = p.CHANGE_ID
    WHERE g.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')
    QUALIFY ROW_NUMBER() OVER (PARTITION BY g.CHANGE_ID ORDER BY x.REVERTED_AT, IFF(x.BOOKED_CHANGE_ID = g.CHANGE_ID, 0, 1), x.REVERT_CHANGE_ID) = 1
)
SELECT l.ITEM_ID, l.STATE,
       COALESCE(NULLIF(TRIM(l.FINDING_TYPE), ''),
                CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END, 'unclassified') AS FINDING_TYPE,
       COALESCE(NULLIF(TRIM(l.TARGET_OBJECT), ''), r.WAREHOUSE_NAME) AS TARGET_OBJECT,
       r.SETTING AS BOOKED_SETTING, r.OLD_VALUE AS BOOKED_OLD_VALUE, r.NEW_VALUE AS BOOKED_NEW_VALUE,
       r.CHANGE_SEEN_AT AS BOOKED_SEEN_AT,
       l.VERIFIED_USD, l.VERIFIED_AT, l.ESTIMATED_USD,
       COALESCE(l.STATE = 'VERIFIED'
                AND l.VERIFIED_AT >= DATEADD('month', -12, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE),
                FALSE) AS LEFT_ROI_NUMERATOR,
       COALESCE(l.STATE = 'ESTIMATED', FALSE) AS LEFT_OPEN_PIPELINE,
       rv.REVERTED_AT, rv.REVERT_KIND, rv.REVERT_SETTING, rv.REVERT_OLD_VALUE, rv.REVERT_NEW_VALUE,
       rv.REVERT_CHANGE_ID,
       rv.REVERT_SETTING <> r.SETTING AS REVERT_VIA_PARTNER
FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
JOIN rv ON rv.BOOKED_CHANGE_ID = l.SOURCE_CHANGE_ID
LEFT JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r ON r.CHANGE_ID = l.SOURCE_CHANGE_ID
ORDER BY rv.REVERTED_AT DESC, l.ITEM_ID
LIMIT 200;
-- Empty grid = nothing booked has been undone: v4.600.0's revert gate changes no number today.

-- P1b (#31) TOTALS: how many reverted VERIFIED items / $ of the last 12 months left the ROI numerator?
-- Predicates mirror mart_sql.savings_summary_quarter() exactly (twin + rv CTEs rendered verbatim from
-- _ledger_twin_select / _ledger_revert_select; active window = VERIFIED_AT >= account-today minus 12 months;
-- LIVE = VERIFIED and not a twin = its _live). Cross-checks: VERIFIED_ACTIVE_MONTHLY_USD and
-- ESTIMATED_OPEN_USD must equal Proof's ROI numerator / open pipeline on v4.600.0;
-- ACTIVE_USD_IGNORING_REVERTS = the pre-#31 (v4.599) numerator; the difference = REVERTED_ACTIVE_USD.
-- REVERTED_ESTIMATED_* = what left ESTIMATED_OPEN_USD (the open pipeline) for the same reason.
WITH twin AS (
    SELECT m.ITEM_ID AS TWIN_ITEM_ID, r.CHANGE_ID AS TWIN_CHANGE_ID, a.ITEM_ID AS TWIN_AUTO_ITEM_ID
    FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER m
    JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r
      ON UPPER(r.WAREHOUSE_NAME) = UPPER(TRIM(m.TARGET_OBJECT))
     AND r.SETTING = IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE', UPPER(TRIM(m.FINDING_TYPE)))
     AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ >= DATEADD('hour', -1, m.CREATED_AT)
     AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ < DATEADD('day', 3, m.CREATED_AT)
    JOIN DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER a
      ON a.SOURCE_CHANGE_ID = r.CHANGE_ID
     AND a.STATE <> 'ESTIMATED'
    WHERE m.SOURCE_CHANGE_ID IS NULL
      AND UPPER(TRIM(m.FINDING_TYPE)) IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')
    QUALIFY ROW_NUMBER() OVER (PARTITION BY m.ITEM_ID ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1
),
rv AS (
    SELECT g.CHANGE_ID AS BOOKED_CHANGE_ID, x.REVERT_CHANGE_ID, x.REVERTED_AT, x.REVERT_OLD_VALUE,
           x.REVERT_NEW_VALUE,
           IFF(MAX(IFF(x.BOOKED_CHANGE_ID = g.CHANGE_ID AND x.REVERT_KIND = 'full', 1, 0)) OVER (PARTITION BY g.CHANGE_ID) = 1,
               'full', 'partial') AS REVERT_KIND,
           x.REVERT_SETTING
    FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY g
    JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY p
      ON p.WAREHOUSE_NAME = g.WAREHOUSE_NAME
     AND p.CHANGE_SEEN_AT = g.CHANGE_SEEN_AT
     AND p.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')
     AND (p.CHANGE_ID = g.CHANGE_ID OR CASE p.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(p.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(p.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(p.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(p.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(p.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END < CASE p.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(p.OLD_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(p.OLD_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(p.OLD_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(p.OLD_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(p.OLD_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END)
    JOIN (
        SELECT b.CHANGE_ID AS BOOKED_CHANGE_ID, n.CHANGE_ID AS REVERT_CHANGE_ID,
               n.CHANGE_SEEN_AT AS REVERTED_AT, n.OLD_VALUE AS REVERT_OLD_VALUE,
               n.NEW_VALUE AS REVERT_NEW_VALUE, n.SETTING AS REVERT_SETTING,
               IFF(MAX(CASE n.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(n.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(n.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(n.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(n.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(n.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END) OVER (PARTITION BY b.CHANGE_ID) >= CASE b.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(b.OLD_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(b.OLD_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(b.OLD_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(b.OLD_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(b.OLD_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END, 'full', 'partial') AS REVERT_KIND
        FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY b
        JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY n
          ON n.WAREHOUSE_NAME = b.WAREHOUSE_NAME
         AND n.SETTING = b.SETTING
         AND n.CHANGE_SEEN_AT > b.CHANGE_SEEN_AT
         AND CASE n.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(n.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(n.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(n.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(n.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(n.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END > CASE b.SETTING WHEN 'SIZE' THEN CASE UPPER(REPLACE(TRIM(COALESCE(b.NEW_VALUE, '')), '-', '')) WHEN 'XSMALL' THEN 1 WHEN 'SMALL' THEN 2 WHEN 'MEDIUM' THEN 3 WHEN 'LARGE' THEN 4 WHEN 'XLARGE' THEN 5 WHEN '2XLARGE' THEN 6 WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 WHEN '5XLARGE' THEN 9 WHEN '6XLARGE' THEN 10 WHEN 'XXLARGE' THEN 6 END WHEN 'AUTO_SUSPEND' THEN IFF(COALESCE(TRY_TO_DOUBLE(b.NEW_VALUE), 0) <= 0, 1000000000000, TRY_TO_DOUBLE(b.NEW_VALUE)) WHEN 'MAX_CLUSTERS' THEN TRY_TO_DOUBLE(b.NEW_VALUE) WHEN 'SCALING_POLICY' THEN CASE UPPER(TRIM(b.NEW_VALUE)) WHEN 'ECONOMY' THEN 1 WHEN 'STANDARD' THEN 2 END END
        WHERE b.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')
        QUALIFY ROW_NUMBER() OVER (PARTITION BY b.CHANGE_ID ORDER BY n.CHANGE_SEEN_AT, n.CHANGE_ID) = 1
    ) x ON x.BOOKED_CHANGE_ID = p.CHANGE_ID
    WHERE g.SETTING IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'SCALING_POLICY', 'SIZE')
    QUALIFY ROW_NUMBER() OVER (PARTITION BY g.CHANGE_ID ORDER BY x.REVERTED_AT, IFF(x.BOOKED_CHANGE_ID = g.CHANGE_ID, 0, 1), x.REVERT_CHANGE_ID) = 1
),
f AS (
    SELECT l.STATE, l.VERIFIED_USD, l.ESTIMATED_USD, rv.REVERTED_AT, rv.REVERT_KIND,
           t.TWIN_ITEM_ID IS NULL AS NOT_TWIN,
           l.STATE = 'VERIFIED' AND t.TWIN_ITEM_ID IS NULL AS LIVE,
           l.VERIFIED_AT >= DATEADD('month', -12, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS IN_12M
    FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l
    LEFT JOIN twin t ON t.TWIN_ITEM_ID = l.ITEM_ID
    LEFT JOIN rv ON rv.BOOKED_CHANGE_ID = l.SOURCE_CHANGE_ID
)
SELECT
    COUNT_IF(LIVE AND IN_12M AND REVERTED_AT IS NOT NULL) AS REVERTED_ACTIVE_ITEMS,
    ROUND(SUM(IFF(LIVE AND IN_12M AND REVERTED_AT IS NOT NULL, COALESCE(VERIFIED_USD, 0), 0)), 2) AS REVERTED_ACTIVE_USD,
    COUNT_IF(LIVE AND IN_12M AND REVERT_KIND = 'full') AS REVERTED_FULL_ITEMS,
    ROUND(SUM(IFF(LIVE AND IN_12M AND REVERT_KIND = 'full', COALESCE(VERIFIED_USD, 0), 0)), 2) AS REVERTED_FULL_USD,
    COUNT_IF(LIVE AND IN_12M AND REVERT_KIND = 'partial') AS REVERTED_PARTIAL_ITEMS,
    ROUND(SUM(IFF(LIVE AND IN_12M AND REVERT_KIND = 'partial', COALESCE(VERIFIED_USD, 0), 0)), 2) AS REVERTED_PARTIAL_USD,
    ROUND(SUM(IFF(LIVE AND IN_12M, COALESCE(VERIFIED_USD, 0), 0)), 2) AS ACTIVE_USD_IGNORING_REVERTS,
    COUNT_IF(LIVE AND IN_12M AND REVERTED_AT IS NULL) AS VERIFIED_ACTIVE_ITEMS,
    ROUND(SUM(IFF(LIVE AND IN_12M AND REVERTED_AT IS NULL, COALESCE(VERIFIED_USD, 0), 0)), 2) AS VERIFIED_ACTIVE_MONTHLY_USD,
    COUNT_IF(LIVE AND REVERTED_AT IS NOT NULL) AS REVERTED_VERIFIED_ALL_TIME,
    COUNT_IF(STATE = 'ESTIMATED' AND NOT_TWIN AND REVERTED_AT IS NOT NULL) AS REVERTED_ESTIMATED_ITEMS,
    ROUND(SUM(IFF(STATE = 'ESTIMATED' AND NOT_TWIN AND REVERTED_AT IS NOT NULL, COALESCE(ESTIMATED_USD, 0), 0)), 2) AS REVERTED_ESTIMATED_USD,
    ROUND(SUM(IFF(STATE = 'ESTIMATED' AND NOT_TWIN AND REVERTED_AT IS NULL, COALESCE(ESTIMATED_USD, 0), 0)), 2) AS ESTIMATED_OPEN_USD,
    MIN(IFF(LIVE AND IN_12M, REVERTED_AT, NULL)) AS FIRST_ACTIVE_REVERT_AT,
    MAX(IFF(LIVE AND IN_12M, REVERTED_AT, NULL)) AS LAST_ACTIVE_REVERT_AT
FROM f;

-- P2a (#14) which CONTROL_STATUS does the app read? The ETL_CONTROL_STATUS_FQN row of the OVERWATCH SETTINGS
-- table (components.load_settings reads KEY/VALUE and upper-cases KEY; KEY/VALUE/UPDATED_AT/UPDATED_BY per
-- mart_sql.settings()), then the session variable P2b-P2d use. An empty/missing VALUE falls back to the V134
-- seed ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS; FALLBACK_USED = TRUE means the app shows 'Not configured' today
-- (config.py default ''), so P2/P3 then describe the seed table, not one the app reads.
SELECT KEY, VALUE, UPDATED_AT, UPDATED_BY
FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE UPPER(KEY) = 'ETL_CONTROL_STATUS_FQN';
SET CTL_FQN = (SELECT COALESCE(NULLIF(TRIM(MAX(IFF(UPPER(KEY) = 'ETL_CONTROL_STATUS_FQN', VALUE, NULL))), ''),
                               'ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS')
               FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);
SELECT $CTL_FQN AS CTL_FQN_IN_USE,
       (SELECT NULLIF(TRIM(MAX(IFF(UPPER(KEY) = 'ETL_CONTROL_STATUS_FQN', VALUE, NULL))), '') IS NULL
        FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS FALLBACK_USED;

-- P2b (#14) what does a FAILED CONTROL_STATUS row actually carry? The 2 newest FAILED rows and the 2 newest
-- other rows of the last 60 days, each as one JSON object (every column, NULLs kept): an Informatica error
-- column shows its real text on a failure and its emptiness on a success. Failed set = etl_control_sql
-- FAILED_TASK_STATUSES, matched on UPPER(TASK_STATUS) like the app; WORKFLOW_NAME / TASK_NAME / TASK_STATUS /
-- TASK_START_DTTM per etl_control_sql.py. OBJECT_CONSTRUCT_KEEP_NULL(*) is a Snowflake built-in (not used
-- elsewhere in the repo). Only IS_FAILED = FALSE rows back = no failure recorded in 60 days (an error
-- column's content then stays unproven -- P2c says the same per column).
SELECT COALESCE(UPPER(TASK_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED'), FALSE) AS IS_FAILED,
       WORKFLOW_NAME, TASK_NAME, TASK_STATUS, TASK_START_DTTM,
       OBJECT_CONSTRUCT_KEEP_NULL(*) AS ROW_JSON
FROM IDENTIFIER($CTL_FQN)
WHERE TASK_START_DTTM >= DATEADD('day', -60, CURRENT_TIMESTAMP())
QUALIFY ROW_NUMBER() OVER (
            PARTITION BY COALESCE(UPPER(TASK_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED'), FALSE)
            ORDER BY TASK_START_DTTM DESC) <= 2
ORDER BY IS_FAILED DESC, TASK_START_DTTM DESC;

-- P2c (#14) is an error-text column actually FILLED? One row per CONTROL_STATUS column, read from the rows
-- themselves (no metadata privilege needed, works for a table or a view): how many of the last 60 days' FAILED
-- rows vs other rows carry a non-blank value, and the newest failed row's value (first 300 chars).
-- A real error-message column reads FILLED_ON_FAILED ~ FAILED_ROWS with FILLED_ON_OTHER ~ 0. Error-message
-- CANDIDATES (by name) sort first. FLATTEN over an OBJECT yields KEY / VALUE (repo precedent:
-- cortex_sql.py LATERAL FLATTEN ... F.KEY / F.VALUE); IS_NULL_VALUE (a JSON null from KEEP_NULL) is a Snowflake
-- built-in the repo does not use. Keys come back upper-case as stored (unquoted column names).
WITH r AS (
    SELECT COALESCE(UPPER(TASK_STATUS) IN ('ABORTED', 'ERROR', 'ERRORED', 'FAILED', 'KILLED', 'STOPPED', 'TERMINATED'), FALSE) AS IS_FAILED,
           TASK_START_DTTM,
           OBJECT_CONSTRUCT_KEEP_NULL(*) AS O
    FROM IDENTIFIER($CTL_FQN)
    WHERE TASK_START_DTTM >= DATEADD('day', -60, CURRENT_TIMESTAMP())
),
kv AS (
    SELECT r.IS_FAILED, r.TASK_START_DTTM, f.KEY::VARCHAR AS COLUMN_NAME,
           IFF(IS_NULL_VALUE(f.VALUE), NULL, NULLIF(TRIM(TO_VARCHAR(f.VALUE)), '')) AS V
    FROM r, LATERAL FLATTEN(INPUT => r.O) f
)
SELECT COLUMN_NAME,
       UPPER(COLUMN_NAME) LIKE ANY ('%ERR%', '%MSG%', '%MESSAGE%', '%REASON%', '%FAIL%', '%DESC%', '%TEXT%',
                                    '%DETAIL%', '%RESULT%', '%RETURN%', '%CODE%', '%LOG%', '%NOTE%',
                                    '%COMMENT%') AS ERROR_TEXT_CANDIDATE,
       COUNT_IF(IS_FAILED) AS FAILED_ROWS,
       COUNT_IF(IS_FAILED AND V IS NOT NULL) AS FILLED_ON_FAILED,
       COUNT_IF(NOT IS_FAILED) AS OTHER_ROWS,
       COUNT_IF(NOT IS_FAILED AND V IS NOT NULL) AS FILLED_ON_OTHER,
       COUNT(DISTINCT IFF(IS_FAILED, V, NULL)) AS DISTINCT_FAILED_VALUES,
       LEFT(MAX_BY(V, IFF(IS_FAILED AND V IS NOT NULL, TASK_START_DTTM, NULL)), 300) AS NEWEST_FAILED_VALUE
FROM kv
GROUP BY COLUMN_NAME
ORDER BY ERROR_TEXT_CANDIDATE DESC, FILLED_ON_FAILED DESC, COLUMN_NAME;

-- P2d (#14) what columns and types does CONTROL_STATUS have? DESCRIBE TABLE on the session variable (the task's
-- literal ask).
-- DESCRIBE TABLE also describes a VIEW (Snowflake docs: DESCRIBE TABLE and DESCRIBE VIEW are interchangeable),
-- so after P2b/P2c succeeded an error here is unexpected -- if it happens, it is itself the answer (grant
-- shape), P2c already has the column names; skip P2e (it reads this result).
DESCRIBE TABLE IDENTIFIER($CTL_FQN);
-- P2e (#14) which of P2d's columns look like error text, and of what type? (reads P2d's result: run
-- immediately after P2d). DESCRIBE output columns are lower-case and must be double-quoted: "name" (repo
-- precedent: snowflake/task_audit.sql / alert_pipeline_check.sql read "name" from SHOW TASKS);
-- "type" and "comment" are from Snowflake docs (unverified column).
SELECT "name" AS COLUMN_NAME, "type" AS DATA_TYPE, "comment" AS COLUMN_COMMENT,
       UPPER("name") LIKE ANY ('%ERR%', '%MSG%', '%MESSAGE%', '%REASON%', '%FAIL%', '%DESC%', '%TEXT%',
                               '%DETAIL%', '%RESULT%', '%RETURN%', '%CODE%', '%LOG%', '%NOTE%',
                               '%COMMENT%') AS ERROR_TEXT_CANDIDATE
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
ORDER BY ERROR_TEXT_CANDIDATE DESC, "name";

-- P3 (#14) SESSION_ID LINKAGE CHECK -- does the Tonight drill find every child statement of a task CALL?
-- (HEAVIEST block of group P; LAST so an error here costs nothing above.) For up to 200 of the newest CALLs
-- that STARTED in the last 3 days and ENDED more than 9 hours ago (QUERY_ATTRIBUTION_HISTORY lags up to ~8h per
-- Snowflake docs; the app's own comments say ~6h), compare per CALL:
--   SESSION children = the drill's LINKAGE predicates (etl_control_sql.run_task_evidence_scan child_rows): same
--     SESSION_ID, START_TIME within [CALL start, COALESCE(CALL end, now)], not the CALL itself, QUERY_TYPE <> 'CALL';
--   CLIPPED_BY_TASK_WINDOW = those session children that child_rows' OTHER predicate would still drop: the
--     drill also clips children to the task's CONTROL_STATUS envelope for the run (bounds: MIN(TASK_START_DTTM),
--     MAX(COALESCE(TASK_END_DTTM, now)) per RUN_ID + TASK_NAME) +/- EVIDENCE_SLACK_MIN = 5 min. IN_TASK_WINDOW =
--     the CALL's start falls in such an envelope (the drill's CALL match) -- FALSE = the drill never shows it;
--   QAH children = QUERY_ATTRIBUTION_HISTORY rows with ROOT_QUERY_ID = the CALL's QUERY_ID (the rollup that
--     run_cost_attribution_scan / insights_sql.call_children_costs rely on), minus the CALL's own row.
-- A CALL counts as a task CALL when its bare procedure name (the drill's CALL-target regex) equals the proc
-- key (insights.proc_key: drop '(...)', last dot-part, no quotes, upper) of a CONTROL_STATUS.TASK_NAME seen
-- in the last 4 days. Every database is kept (the linkage does not depend on it; the drill's same-database
-- preference is not modelled); CALLS_IN_CONTROL_DB counts the CALLs in CONTROL_STATUS's own database.
-- QAH omits very short / warehouse-less statements, so SESSION >= QAH is NORMAL. The question is the reverse:
-- QAH_CHILDREN_MISSED = children QAH proves belong to the CALL that the session rule does NOT find, split by
-- why: NOT_IN_QH (no QUERY_HISTORY row in the 3-day window), OTHER_SESSION (ran in a different session: the
-- linkage is wrong for it), OUTSIDE_WINDOW (same session, started outside the CALL's window). QAH_NESTED_CALLS
-- are child CALL rows, which the drill skips BY DESIGN (their own statements share the session and ARE
-- counted), so the fair comparison is SESSION_CHILDREN vs QAH_NONCALL_CHILDREN. A NESTED task CALL that is
-- itself checked shows QAH_CHILDREN = 0 (QAH roots its children at the OUTER CALL) -- not a miss.
-- READ: the first 19 columns are totals over ALL checked CALLs (identical on every row -- read row 1); the
-- rest are the up-to-15 worst CALLs (a QAH miss first, then a task-window clip, then the biggest
-- QAH-over-session gap). Empty grid = no CALL in QUERY_HISTORY matched a CONTROL_STATUS proc name in the
-- window (itself an answer). QUERY_HISTORY QUERY_ID / SESSION_ID / START_TIME / END_TIME / EXECUTION_STATUS /
-- QUERY_TYPE / QUERY_TEXT / DATABASE_NAME and QUERY_ATTRIBUTION_HISTORY QUERY_ID / ROOT_QUERY_ID / START_TIME
-- are all read in etl_control_sql.py (run_task_evidence_scan, run_cost_attribution_scan); CONTROL_STATUS
-- RUN_ID / TASK_NAME / TASK_START_DTTM / TASK_END_DTTM likewise.
SET CTL_FQN = (SELECT COALESCE(NULLIF(TRIM(MAX(IFF(UPPER(KEY) = 'ETL_CONTROL_STATUS_FQN', VALUE, NULL))), ''),
                               'ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS')
               FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);   -- P3 sets its own variable (same as P2a), so it runs standalone
WITH tw AS (   -- each task's CONTROL_STATUS envelope per run, as the drill's tasks/bounds CTEs build it
    SELECT RUN_ID,
           UPPER(TRIM(REPLACE(SPLIT_PART(SPLIT_PART(TASK_NAME, '(', 1), '.', -1), '"', ''))) AS PROC_KEY,
           MIN(TASK_START_DTTM) AS T_START,
           MAX(COALESCE(TASK_END_DTTM, CURRENT_TIMESTAMP())) AS T_END
    FROM IDENTIFIER($CTL_FQN)
    WHERE TASK_NAME IS NOT NULL AND TASK_START_DTTM IS NOT NULL
      AND TASK_START_DTTM >= DATEADD('day', -4, CURRENT_TIMESTAMP())
    GROUP BY RUN_ID, TASK_NAME
),
procs AS (
    SELECT DISTINCT PROC_KEY FROM tw
),
call_hits AS (
    SELECT qh.QUERY_ID, qh.SESSION_ID, qh.START_TIME, qh.END_TIME, qh.EXECUTION_STATUS,
           REPLACE(REGEXP_SUBSTR(UPPER(qh.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$"]+)', 1, 1, 'e', 1), '"', '') AS CALL_TARGET,
           UPPER(qh.DATABASE_NAME) AS SESSION_DATABASE
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
    WHERE qh.START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
      AND qh.END_TIME <= DATEADD('hour', -9, CURRENT_TIMESTAMP())
      AND qh.QUERY_TYPE = 'CALL'
),
calls AS (
    SELECT h.QUERY_ID, h.SESSION_ID, h.START_TIME, h.END_TIME, h.EXECUTION_STATUS,
           SPLIT_PART(h.CALL_TARGET, '.', -1) AS PROC_KEY,
           NULLIF(IFF(ARRAY_SIZE(SPLIT(h.CALL_TARGET, '.')) = 3, SPLIT_PART(h.CALL_TARGET, '.', 1),
                      h.SESSION_DATABASE), '') AS CALL_DATABASE
    FROM call_hits h
    WHERE SPLIT_PART(h.CALL_TARGET, '.', -1) IN (SELECT PROC_KEY FROM procs)
    QUALIFY ROW_NUMBER() OVER (ORDER BY h.START_TIME DESC, h.QUERY_ID) <= 200
),
cwin AS (   -- the envelope the drill matches this CALL to (CALL start inside it +/- 5 min), newest run first
    SELECT c.QUERY_ID AS CALL_ID,
           DATEADD('minute', -5, w.T_START) AS W_START, DATEADD('minute', 5, w.T_END) AS W_END
    FROM calls c
    JOIN tw w
      ON w.PROC_KEY = c.PROC_KEY
     AND w.RUN_ID IS NOT NULL
     AND c.START_TIME >= DATEADD('minute', -5, w.T_START)
     AND c.START_TIME <= DATEADD('minute', 5, w.T_END)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.QUERY_ID ORDER BY w.T_START DESC, w.T_END DESC) = 1
),
qah AS (
    SELECT DISTINCT a.ROOT_QUERY_ID AS CALL_ID, a.QUERY_ID AS CHILD_ID
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
    WHERE a.START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
      AND a.ROOT_QUERY_ID IN (SELECT QUERY_ID FROM calls)
      AND a.QUERY_ID <> a.ROOT_QUERY_ID
),
kids AS (   -- ONE QUERY_HISTORY read serves both the session rule and the classification of QAH children
    SELECT k.QUERY_ID, k.SESSION_ID, k.START_TIME, k.QUERY_TYPE
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY k
    LEFT JOIN (SELECT DISTINCT SESSION_ID FROM calls) cs ON cs.SESSION_ID = k.SESSION_ID
    LEFT JOIN (SELECT DISTINCT CHILD_ID FROM qah) qc ON qc.CHILD_ID = k.QUERY_ID
    WHERE k.START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
      AND (cs.SESSION_ID IS NOT NULL OR qc.CHILD_ID IS NOT NULL)
),
sess AS (   -- the drill's session linkage (child_rows' ON clause), plus whether its task-window clip drops the child
    SELECT c.QUERY_ID AS CALL_ID, k.QUERY_ID AS CHILD_ID,
           w.CALL_ID IS NOT NULL AND (k.START_TIME < w.W_START OR k.START_TIME > w.W_END) AS CLIPPED
    FROM calls c
    JOIN kids k
      ON k.SESSION_ID = c.SESSION_ID
     AND k.START_TIME >= c.START_TIME AND k.START_TIME <= COALESCE(c.END_TIME, CURRENT_TIMESTAMP())
     AND k.QUERY_ID <> c.QUERY_ID AND k.QUERY_TYPE <> 'CALL'
    LEFT JOIN cwin w ON w.CALL_ID = c.QUERY_ID
),
s_n AS (
    SELECT CALL_ID, COUNT(DISTINCT CHILD_ID) AS SESSION_CHILDREN,
           COUNT(DISTINCT IFF(CLIPPED, CHILD_ID, NULL)) AS CLIPPED_BY_TASK_WINDOW
    FROM sess GROUP BY CALL_ID
),
q_n AS (
    SELECT q.CALL_ID,
           COUNT(DISTINCT q.CHILD_ID) AS QAH_CHILDREN,
           COUNT(DISTINCT IFF(k.QUERY_TYPE = 'CALL', q.CHILD_ID, NULL)) AS QAH_NESTED_CALLS,
           COUNT(DISTINCT IFF(s.CHILD_ID IS NULL AND k.QUERY_ID IS NULL, q.CHILD_ID, NULL)) AS MISS_NOT_IN_QH,
           COUNT(DISTINCT IFF(s.CHILD_ID IS NULL AND k.QUERY_TYPE <> 'CALL'
                              AND k.SESSION_ID <> c.SESSION_ID, q.CHILD_ID, NULL)) AS MISS_OTHER_SESSION,
           COUNT(DISTINCT IFF(s.CHILD_ID IS NULL AND k.QUERY_TYPE <> 'CALL'
                              AND k.SESSION_ID = c.SESSION_ID, q.CHILD_ID, NULL)) AS MISS_OUTSIDE_WINDOW
    FROM qah q
    JOIN calls c ON c.QUERY_ID = q.CALL_ID
    LEFT JOIN sess s ON s.CALL_ID = q.CALL_ID AND s.CHILD_ID = q.CHILD_ID
    LEFT JOIN kids k ON k.QUERY_ID = q.CHILD_ID
    GROUP BY q.CALL_ID
),
per_call AS (
    SELECT c.QUERY_ID AS CALL_QUERY_ID, c.PROC_KEY, c.CALL_DATABASE, c.EXECUTION_STATUS,
           c.START_TIME AS CALL_START_TIME, c.END_TIME AS CALL_END_TIME,
           w.CALL_ID IS NOT NULL AS IN_TASK_WINDOW,
           COALESCE(s.SESSION_CHILDREN, 0) AS SESSION_CHILDREN,
           COALESCE(s.CLIPPED_BY_TASK_WINDOW, 0) AS CLIPPED_BY_TASK_WINDOW,
           COALESCE(q.QAH_CHILDREN, 0) AS QAH_CHILDREN,
           COALESCE(q.QAH_NESTED_CALLS, 0) AS QAH_NESTED_CALLS,
           COALESCE(q.QAH_CHILDREN, 0) - COALESCE(q.QAH_NESTED_CALLS, 0) AS QAH_NONCALL_CHILDREN,
           COALESCE(q.MISS_NOT_IN_QH, 0) AS MISS_NOT_IN_QH,
           COALESCE(q.MISS_OTHER_SESSION, 0) AS MISS_OTHER_SESSION,
           COALESCE(q.MISS_OUTSIDE_WINDOW, 0) AS MISS_OUTSIDE_WINDOW,
           COALESCE(q.MISS_NOT_IN_QH, 0) + COALESCE(q.MISS_OTHER_SESSION, 0)
             + COALESCE(q.MISS_OUTSIDE_WINDOW, 0) AS QAH_CHILDREN_MISSED
    FROM calls c
    LEFT JOIN cwin w ON w.CALL_ID = c.QUERY_ID
    LEFT JOIN s_n s ON s.CALL_ID = c.QUERY_ID
    LEFT JOIN q_n q ON q.CALL_ID = c.QUERY_ID
)
SELECT COUNT(*) OVER () AS CALLS_CHECKED,
       (SELECT COUNT(DISTINCT PROC_KEY) FROM per_call) AS PROCS,
       SUM(IFF(CALL_DATABASE = UPPER(SPLIT_PART($CTL_FQN, '.', 1)), 1, 0)) OVER () AS CALLS_IN_CONTROL_DB,
       SUM(IFF(IN_TASK_WINDOW, 1, 0)) OVER () AS CALLS_IN_TASK_WINDOW,
       SUM(IFF(QAH_CHILDREN > 0, 1, 0)) OVER () AS CALLS_WITH_QAH_CHILDREN,
       ROUND(AVG(SESSION_CHILDREN) OVER (), 1) AS AVG_SESSION_CHILDREN,
       ROUND(AVG(QAH_CHILDREN) OVER (), 1) AS AVG_QAH_CHILDREN,
       ROUND(AVG(QAH_NONCALL_CHILDREN) OVER (), 1) AS AVG_QAH_NONCALL_CHILDREN,
       SUM(IFF(SESSION_CHILDREN < QAH_CHILDREN, 1, 0)) OVER () AS CALLS_SESSION_LT_QAH,
       SUM(IFF(SESSION_CHILDREN < QAH_NONCALL_CHILDREN, 1, 0)) OVER () AS CALLS_SESSION_LT_QAH_NONCALL,
       SUM(IFF(SESSION_CHILDREN - CLIPPED_BY_TASK_WINDOW = 0 AND QAH_NONCALL_CHILDREN > 0, 1, 0)) OVER () AS CALLS_EMPTY_DRILL_BUT_QAH_HAS,
       SUM(IFF(QAH_CHILDREN_MISSED > 0, 1, 0)) OVER () AS CALLS_WITH_QAH_MISS,
       SUM(QAH_CHILDREN_MISSED) OVER () AS TOTAL_QAH_CHILDREN_MISSED,
       SUM(MISS_NOT_IN_QH) OVER () AS TOTAL_MISS_NOT_IN_QH,
       SUM(MISS_OTHER_SESSION) OVER () AS TOTAL_MISS_OTHER_SESSION,
       SUM(MISS_OUTSIDE_WINDOW) OVER () AS TOTAL_MISS_OUTSIDE_WINDOW,
       SUM(QAH_NESTED_CALLS) OVER () AS TOTAL_QAH_NESTED_CALLS,
       SUM(CLIPPED_BY_TASK_WINDOW) OVER () AS TOTAL_CLIPPED_BY_TASK_WINDOW,
       MIN(CALL_START_TIME) OVER () AS OLDEST_CALL_START,
       CALL_QUERY_ID, PROC_KEY, CALL_DATABASE, EXECUTION_STATUS, CALL_START_TIME, CALL_END_TIME, IN_TASK_WINDOW,
       SESSION_CHILDREN, CLIPPED_BY_TASK_WINDOW, QAH_CHILDREN, QAH_NONCALL_CHILDREN, QAH_CHILDREN_MISSED,
       MISS_NOT_IN_QH, MISS_OTHER_SESSION, MISS_OUTSIDE_WINDOW
FROM per_call
QUALIFY ROW_NUMBER() OVER (ORDER BY QAH_CHILDREN_MISSED DESC, CLIPPED_BY_TASK_WINDOW DESC,
                                    QAH_NONCALL_CHILDREN - SESSION_CHILDREN DESC,
                                    CALL_START_TIME DESC) <= 15
ORDER BY QAH_CHILDREN_MISSED DESC, CLIPPED_BY_TASK_WINDOW DESC, QAH_NONCALL_CHILDREN - SESSION_CHILDREN DESC,
         CALL_START_TIME DESC;
-- Read: TOTAL_QAH_CHILDREN_MISSED = 0 -> the drill's session linkage is sound (it finds every child QAH proves).
-- TOTAL_MISS_OTHER_SESSION > 0 -> some procs run statements outside the CALL's session (the drill under-reports
-- them; ROOT_QUERY_ID is the only exact link, ~6-8h late). TOTAL_MISS_OUTSIDE_WINDOW > 0 -> CALL END_TIME vs
-- child START_TIME skew (widen the child window). TOTAL_CLIPPED_BY_TASK_WINDOW > 0 -> CONTROL_STATUS's end stamp
-- lands > 5 min before the CALL's real end (or a clock/timezone skew), so the drill's own clip hides children
-- the linkage found. CALLS_IN_TASK_WINDOW << CALLS_CHECKED -> many name-matched CALLs fall outside every
-- CONTROL_STATUS envelope (another environment's or a manual CALL; or a clock skew > 5 min).
-- CALLS_EMPTY_DRILL_BUT_QAH_HAS > 0 is the user-visible failure: the drill's breakdown (session children left
-- after the task-window clip) is empty for a CALL that QAH proves ran non-CALL statements.

-- ---------------- end of group P, end of file.
ALTER SESSION UNSET TIMEZONE;   -- as PROBES_WAVE2 ends: gives this worksheet back its own clock (harmless either way; if Run All stopped earlier, run this line on its own)
