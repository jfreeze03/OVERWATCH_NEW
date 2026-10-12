-- ====================================================================================================
--  V176 PREFLIGHT (read-only; run BEFORE applying V176, as SNOW_ACCOUNTADMINS). The grids reuse arm [27]'s OWN V176
--  text (outputs/gen_v176.py): its ag and rt CTEs and its one skip predicate. The first statement pins the session to
--  Central, the zone of the arm's titles and keys. Changes nothing.
-- ====================================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P176.1a what arm [27] raises today (V174) and after V176, per window: every direct admin-tier grant created in the
--         last 26h (what one hourly scan reads) and in the last 30 days, one HIGH event each today. V176_SKIPS should
--         be the deploys' Streamlit runtime grants only (P176.1b lists every row); V176_STILL_RAISES is every other
--         grant, raised exactly as before. Events the arm already raised are not subtracted (one scan raises each
--         grant once).
WITH ag AS (
    SELECT GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY,
           ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS,
           CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                   'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
),
rt AS (
    SELECT GRANTEE_NAME, MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE = 'SYSTEM$MANAGED'
      AND GRANTED_BY IS NULL
      AND REGEXP_LIKE(GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+')
    GROUP BY GRANTEE_NAME
),
v AS (
    SELECT g.GRANTEE_NAME, g.ROLE, g.CREATED_ON,
           NOT COALESCE(g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+') AND (g.ROLE = 'SNOW_ACCOUNTADMINS' OR g.ROLE = 'SNOW_SYSADMINS') AND rt.GRANTEE_NAME IS NOT NULL, FALSE) AS V176_RAISES
    FROM ag g
    LEFT JOIN rt
      ON rt.GRANTEE_NAME = g.GRANTEE_NAME
     AND g.CREATED_ON BETWEEN DATEADD('second', -60, rt.MARK_FROM) AND DATEADD('second', 60, rt.MARK_TO)
    WHERE g.CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
),
w AS (
    SELECT 'last 26h' AS WINDOW_NAME, DATEADD('hour', -26, CURRENT_TIMESTAMP()) AS SINCE
    UNION ALL
    SELECT 'last 30 days', DATEADD('day', -30, CURRENT_TIMESTAMP())
)
SELECT w.WINDOW_NAME,
       COUNT(v.ROLE) AS V174_RAISES,
       COUNT_IF(NOT v.V176_RAISES) AS V176_SKIPS,
       COUNT_IF(v.V176_RAISES) AS V176_STILL_RAISES,
       COUNT(DISTINCT IFF(NOT v.V176_RAISES, v.GRANTEE_NAME, NULL)) AS RUNTIME_USERS_SKIPPED,
       LISTAGG(DISTINCT IFF(v.V176_RAISES, v.GRANTEE_NAME || ' (' || v.ROLE || ')', NULL), ', ') AS STILL_RAISED
FROM w
LEFT JOIN v
       ON v.CREATED_ON >= w.SINCE
GROUP BY w.WINDOW_NAME
ORDER BY w.WINDOW_NAME;

-- P176.1b every direct admin-tier grant of the last 30 days with its V176 verdict. Expect each 'skipped by V176' row
--         to be an STPLATSTREAMLIT<digits> user, GRANTED_BY NULL, SNOW_ACCOUNTADMINS (or SNOW_SYSADMINS) and
--         HAS_SYSTEM_MANAGED_COGRANT TRUE. A 'still raises' row with GRANTED_BY NULL is a real user's no-grantor grant:
--         it raises as before. IN_LAST_26H marks what the next hourly scan reads.
WITH ag AS (
    SELECT GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY,
           ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS,
           CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                   'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
),
rt AS (
    SELECT GRANTEE_NAME, MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE = 'SYSTEM$MANAGED'
      AND GRANTED_BY IS NULL
      AND REGEXP_LIKE(GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+')
    GROUP BY GRANTEE_NAME
)
SELECT IFF(NOT COALESCE(g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+') AND (g.ROLE = 'SNOW_ACCOUNTADMINS' OR g.ROLE = 'SNOW_SYSADMINS') AND rt.GRANTEE_NAME IS NOT NULL, FALSE), 'still raises', 'skipped by V176') AS V176_VERDICT,
       g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP()) AS IN_LAST_26H,
       g.GRANTEE_NAME, g.ROLE, g.GRANTED_BY, g.CREATED_CT AS GRANTED_AT_CENTRAL, g.DELETED_ON,
       rt.GRANTEE_NAME IS NOT NULL AS HAS_SYSTEM_MANAGED_COGRANT, g.PRIOR_GRANTS
FROM ag g
LEFT JOIN rt
  ON rt.GRANTEE_NAME = g.GRANTEE_NAME
 AND g.CREATED_ON BETWEEN DATEADD('second', -60, rt.MARK_FROM) AND DATEADD('second', 60, rt.MARK_TO)
WHERE g.CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
ORDER BY V176_VERDICT, g.CREATED_ON DESC
LIMIT 500;

-- P176.2 the runtime-user SEC_ADMIN_GRANT events still open: resolve them after the apply. In Alerts > Open events
--        (Group by rule off), turn on Bulk select and tick only the rows whose TITLE and RAISED_AT match this grid
--        (this query already checked the rule, the runtime name and the no-grantor 'by ?' detail), then resolve
--        as EXPECTED with the note: Streamlit runtime user system grant on app deploy (GRANTED_BY NULL);
--        excluded from SEC_ADMIN_GRANT by V176. EXPECTED, not NOISE: NOISE would count against the rule's precision.
--        Nothing is re-sent to Teams. HAS_SYSTEM_MANAGED_COGRANT FALSE = V176 would still raise that grant: check it first.
WITH rt AS (
    SELECT GRANTEE_NAME, MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE = 'SYSTEM$MANAGED'
      AND GRANTED_BY IS NULL
      AND REGEXP_LIKE(GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+')
    GROUP BY GRANTEE_NAME
)
SELECT e.EVENT_ID, e.STATUS, e.RAISED_AT, e.TITLE, LEFT(e.DETAIL, 120) AS DETAIL_START,
       rt.GRANTEE_NAME IS NOT NULL AS HAS_SYSTEM_MANAGED_COGRANT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN rt
  ON rt.GRANTEE_NAME = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
WHERE e.RULE_ID = 'SEC_ADMIN_GRANT'
  AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND REGEXP_LIKE(SPLIT_PART(e.DEDUPE_KEY, '|', 2), 'STPLATSTREAMLIT[0-9]+')
  AND SPLIT_PART(e.DEDUPE_KEY, '|', 3) IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
  AND e.DETAIL LIKE 'Granted % Central by ?. %'
ORDER BY e.RAISED_AT;
