-- =====================================================================================
--  PROBES_STREAMLIT_PLATFORM_USERS_2026-10-12.sql  --  READ-ONLY. Nothing is created, changed or granted.
--  WHY: after every `snow streamlit deploy --replace`, Teams gets a HIGH SEC_ADMIN_GRANT alert like
--  "Admin role SNOW_ACCOUNTADMINS granted to STPLATSTREAMLIT98004567325 - first time | Granted ... by ?".
--  These STPLATSTREAMLIT* users look like Snowflake-managed accounts the Streamlit runtime creates for the app and
--  grants the app OWNER's role, with no human grantor ("by ?" = GRANTED_BY is NULL). Snowflake does not document
--  them, so before OVERWATCH ignores them in a SECURITY rule, these checks prove what they are, so the exclusion can
--  be exact (an attacker must not be able to hide a real admin grant behind a look-alike name).
--  RUN AS SNOW_ACCOUNTADMINS, on this worksheet's warehouse. Paste back every grid ('no rows' is an answer).
-- =====================================================================================
USE ROLE SNOW_ACCOUNTADMINS;
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- S1 Every STPLATSTREAMLIT* user Snowflake has created (live and dropped), newest first.
-- DECIDES: TYPE / OWNER / HAS_PASSWORD / HAS_RSA_PUBLIC_KEY show whether these are Snowflake-managed service users
--    (no password, no key, owned by a system role or NULL). One user per deploy = a CREATED_ON on each deploy day.
SELECT NAME, LOGIN_NAME, TYPE, OWNER, CREATED_ON, DELETED_ON, DISABLED, HAS_PASSWORD, HAS_RSA_PUBLIC_KEY,
       DEFAULT_ROLE, LEFT(COMMENT, 200) AS COMMENT
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS
WHERE NAME ILIKE 'STPLAT%'
ORDER BY CREATED_ON DESC
LIMIT 200;

-- S2 The role grants those users received: which role, who granted it (NULL = a system grant), when, and whether it
--    is still live.
-- DECIDES: every row with GRANTED_BY NULL and ROLE = the app owner's role (SNOW_ACCOUNTADMINS today) = the exclusion
--    can require "GRANTED_BY IS NULL" as well as the name, so a human-made grant (GRANTED_BY set) still alerts.
SELECT g.GRANTEE_NAME, g.ROLE, g.GRANTED_BY, g.CREATED_ON, g.DELETED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
WHERE g.GRANTEE_NAME ILIKE 'STPLAT%'
ORDER BY g.CREATED_ON DESC
LIMIT 200;

-- S3 Is any other user's admin-role grant also system-made (GRANTED_BY NULL), last 365 days?
-- DECIDES: rows here that are NOT STPLAT* users = "GRANTED_BY IS NULL" alone is not specific enough to exclude on.
SELECT g.GRANTEE_NAME, g.ROLE, g.CREATED_ON, g.DELETED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
WHERE g.GRANTED_BY IS NULL
  AND g.CREATED_ON >= DATEADD('day', -365, CURRENT_TIMESTAMP())
  AND g.GRANTEE_NAME NOT ILIKE 'STPLAT%'
ORDER BY g.CREATED_ON DESC
LIMIT 100;

-- S4 Do these users log in, and from where? Last 30 days.
-- DECIDES: logins here mean they can also raise SEC_NEW_ADMIN_NETWORK / SEC_LOGIN_TAKEOVER (they hold an admin-tier
--    role), so the exclusion has to cover those rules too.
SELECT l.USER_NAME, l.REPORTED_CLIENT_TYPE, l.FIRST_AUTHENTICATION_FACTOR, l.IS_SUCCESS,
       COUNT(*) AS LOGINS, COUNT(DISTINCT l.CLIENT_IP) AS IPS, MIN(l.EVENT_TIMESTAMP) AS FIRST_AT,
       MAX(l.EVENT_TIMESTAMP) AS LAST_AT
FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY l
WHERE l.USER_NAME ILIKE 'STPLAT%'
  AND l.EVENT_TIMESTAMP >= DATEADD('day', -30, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3, 4
ORDER BY LAST_AT DESC;

-- S5 Which OVERWATCH alerts these users have raised so far (any rule), and their state.
SELECT e.RULE_ID, e.STATUS, COUNT(*) AS EVENTS, MIN(e.RAISED_AT) AS FIRST_RAISED, MAX(e.RAISED_AT) AS LAST_RAISED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
WHERE e.TITLE ILIKE '%STPLAT%' OR e.DEDUPE_KEY ILIKE '%STPLAT%'
GROUP BY 1, 2
ORDER BY LAST_RAISED DESC;

-- S6 The app object's own creation time, to line up with S1's CREATED_ON (one user per deploy?).
SHOW STREAMLITS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT "name", "created_on", "owner", "query_warehouse"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- S7 What OVERWATCH's security change log recorded FOR these users (the statements they ran), last 30 days.
-- DECIDES: rows here are the runtime's own statements, which stay in Security > Changes (the fix only stops the
--    admin-grant ALERT); a high RISK_LEVEL here would need its own look.
SELECT f.USER_NAME, f.CHANGE_KIND, f.RISK_LEVEL, f.QUERY_TYPE, COUNT(*) AS CHANGES, MAX(f.EVENT_TS) AS LAST_AT
FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE f
WHERE f.USER_NAME ILIKE 'STPLAT%'
  AND f.DAY >= DATEADD('day', -30, CURRENT_DATE())
GROUP BY 1, 2, 3, 4
ORDER BY CHANGES DESC
LIMIT 50;

-- S8 Posture-metric alert rules you created (they count grants too, so a deploy could also trip one).
-- DECIDES: a rule on BREAKGLASS_GRANTS_30D or GRANT_CHANGES_24H means those counts need the same exclusion.
SELECT RULE_ID, METRIC_NAME, THRESHOLD_NUM, ENABLED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
WHERE COALESCE(METRIC_NAME, '') <> ''
ORDER BY RULE_ID;

ALTER SESSION UNSET TIMEZONE;
