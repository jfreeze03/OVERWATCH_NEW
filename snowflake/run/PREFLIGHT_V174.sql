-- Run as SNOW_ACCOUNTADMINS (read-only).
USE ROLE SNOW_ACCOUNTADMINS;
-- ====================================================================================================
--  V174 PREFLIGHT (read-only; run BEFORE applying V174). P174.2-P174.4 run the three arms' OWN V174 text
--  (outputs/gen_v174.py), so each grid is what the first hourly scan after the apply raises. The first statement pins
--  the session to Central, the zone of the arms' titles and keys. Changes nothing.
-- ====================================================================================================
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P174.1 who V174 starts watching: every user holding SNOW_PRI_GFR_PRD_ALFA_DSA by a DIRECT grant (GRANTS_TO_USERS
--        lags up to ~2h), with the admin-tier roles they already hold directly. NEW_TO_ARM_18: arm [18] never watched
--        their logins; NEW_TO_ARM_26: no role of theirs made a takeover CRITICAL. The app treats the same direct
--        holders as OVERWATCH admins (a role granted to a role is not expanded).
WITH dsa AS (
    SELECT GRANTEE_NAME, MIN(CREATED_ON) AS FIRST_GRANTED_ON, MAX_BY(GRANTED_BY, CREATED_ON) AS LAST_GRANTED_BY
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE = 'SNOW_PRI_GFR_PRD_ALFA_DSA'
    GROUP BY GRANTEE_NAME
),
other AS (
    SELECT GRANTEE_NAME,
           LISTAGG(DISTINCT ROLE, ', ') AS OTHER_ADMIN_ROLES,
           COUNT_IF(ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')) AS N_ARM18_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
    GROUP BY GRANTEE_NAME
)
SELECT d.GRANTEE_NAME AS USER_NAME, d.FIRST_GRANTED_ON, d.LAST_GRANTED_BY, o.OTHER_ADMIN_ROLES,
       COALESCE(o.N_ARM18_ROLES, 0) = 0 AS NEW_TO_ARM_18,
       o.GRANTEE_NAME IS NULL AS NEW_TO_ARM_26
FROM dsa d
LEFT JOIN other o
       ON o.GRANTEE_NAME = d.GRANTEE_NAME
ORDER BY NEW_TO_ARM_26 DESC, USER_NAME;

-- P174.2 what the next hourly scan raises for SEC_ADMIN_GRANT after V174 (arm [27]'s own statement): every direct grant
--        of a listed role created in the last 26h that has no event yet. Expect only SNOW_PRI_GFR_PRD_ALFA_DSA grants
--        (the other roles' grants were raised by the scans before); each is one HIGH event, reviewed in Alerts.
WITH cfg AS (
    SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
),
ag AS (
    SELECT GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY,
           ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS,
           CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                   'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT c.RULE_ID,
       'ALL',
       c.SEVERITY,
       LEFT('Admin role ' || g.ROLE || ' granted to ' || g.GRANTEE_NAME ||
            IFF(HOUR(g.CREATED_CT) >= 20 OR HOUR(g.CREATED_CT) < 6 OR DAYOFWEEKISO(g.CREATED_CT) >= 6,
                ' (off-hours)', '') ||
            IFF(g.PRIOR_GRANTS = 0, ' - first time', ''), 300),
       LEFT('Granted ' || TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI') || ' Central by ' ||
            COALESCE(g.GRANTED_BY, '?') || '. ' ||
            IFF(g.PRIOR_GRANTS = 0, 'First grant of this role to this user on record.',
                'Re-grant: ' || g.PRIOR_GRANTS || ' earlier grant(s) of this role to this user on record.') ||
            IFF(g.DELETED_ON IS NULL, ' Still held.',
                ' Since revoked ' ||
                TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', g.DELETED_ON)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI') ||
                ' Central.') ||
            ' | Confirm the change was approved; unexplained = revoke it and review what the user ran (playbook). Source: ACCOUNT_USAGE.GRANTS_TO_USERS - review in Security -> Changes', 2000),
       1,
       c.RULE_ID || '|' || LEFT(g.GRANTEE_NAME, 200) || '|' || g.ROLE || '|' ||
           TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI:SS.FF3')
FROM cfg c
JOIN ag g
  ON c.RULE_ID = 'SEC_ADMIN_GRANT'
 AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())

) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WHERE NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
);

-- P174.3 what the next hourly scan raises for SEC_LOGIN_TAKEOVER after V174 (arm [26]'s own statement, wrapped): the CRIT
--        twin of a holder's episode already raised as WARN in the last 24h, and any episode the scan has not raised
--        yet. WARN_TWIN_STATUS / WARN_TWIN_RESOLUTION_KIND are that WARN row's: the V067 sweep supersedes it only
--        while it is OPEN or ACK; one already resolved or snoozed stays as it is and the CRIT re-opens the episode
--        (it routes and escalates like any CRITICAL): resolve or snooze the CRIT the same way.
SELECT p.*,
       w.STATUS AS WARN_TWIN_STATUS,
       w.RESOLUTION_KIND AS WARN_TWIN_RESOLUTION_KIND,
       CASE WHEN w.STATUS IS NULL THEN NULL
            WHEN w.STATUS IN ('OPEN', 'ACK') THEN 'superseded by this CRIT (V067 sweep, OPEN or ACK)'
            ELSE 'WARN already ' || LOWER(w.STATUS) || ': this CRIT re-opens the episode (resolved or snoozed '
                 || 'WARNs are not superseded) - resolve or snooze it the same way' END AS WARN_TWIN_NOTE
FROM (
WITH cfg AS (
    SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
),
k AS (
    SELECT GREATEST(CEIL(COALESCE(MAX(THRESHOLD_NUM), 5)), 2) AS N
    FROM cfg WHERE RULE_ID = 'SEC_LOGIN_TAKEOVER'
    HAVING COUNT(*) > 0
),
ev AS (
    SELECT USER_NAME, EVENT_ID, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP, REPORTED_CLIENT_TYPE,
           FIRST_AUTHENTICATION_FACTOR, ERROR_MESSAGE
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= DATEADD('hour', -27, CURRENT_TIMESTAMP())
),
fl AS (
    SELECT USER_NAME, EVENT_TIMESTAMP,
           ROW_NUMBER() OVER (PARTITION BY USER_NAME ORDER BY EVENT_TIMESTAMP, EVENT_ID) AS RN
    FROM ev
    WHERE IS_SUCCESS = 'NO'
),
bend AS (
    -- a failure that closes >= N failures by the same user inside 15 minutes (the one N-1 rows back is <= 15 min older)
    SELECT f2.USER_NAME, f2.EVENT_TIMESTAMP AS TS
    FROM fl f2
    CROSS JOIN k
    JOIN fl f1
      ON f1.USER_NAME = f2.USER_NAME
     AND f1.RN = f2.RN - (k.N - 1)
     AND f1.EVENT_TIMESTAMP >= DATEADD('minute', -15, f2.EVENT_TIMESTAMP)
),
seq AS (
    -- one time-ordered stream per burst user: burst ends, then successes; LAST_BEND = newest burst end before the row
    SELECT s.USER_NAME, s.TS, s.IS_SUCC, s.EVENT_ID, s.CLIENT_IP, s.CLIENT_TYPE, s.AUTH_FACTOR,
           MAX(IFF(s.IS_SUCC, NULL, s.TS)) OVER (PARTITION BY s.USER_NAME ORDER BY s.TS, s.IS_SUCC
                                                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS LAST_BEND
    FROM (
        SELECT USER_NAME, TS, FALSE AS IS_SUCC, NULL AS EVENT_ID, NULL AS CLIENT_IP, NULL AS CLIENT_TYPE,
               NULL AS AUTH_FACTOR
        FROM bend
        UNION ALL
        SELECT USER_NAME, EVENT_TIMESTAMP, TRUE, EVENT_ID, CLIENT_IP, REPORTED_CLIENT_TYPE,
               FIRST_AUTHENTICATION_FACTOR
        FROM ev
        WHERE IS_SUCCESS = 'YES'
          AND USER_NAME IN (SELECT USER_NAME FROM bend)
    ) s
),
brk AS (
    -- a success within 60 minutes after a burst ended; PREV_TS = the user's previous such success
    SELECT USER_NAME, TS, EVENT_ID, CLIENT_IP, CLIENT_TYPE, AUTH_FACTOR, LAST_BEND,
           LAG(TS) OVER (PARTITION BY USER_NAME ORDER BY TS, EVENT_ID) AS PREV_TS
    FROM seq
    WHERE IS_SUCC
      AND LAST_BEND >= DATEADD('minute', -60, TS)
),
anc AS (
    -- the episode anchor, raised only inside the alert window (its evidence lies wholly inside ev)
    SELECT USER_NAME, TS, EVENT_ID, CLIENT_IP, CLIENT_TYPE, AUTH_FACTOR, LAST_BEND,
           CONVERT_TIMEZONE('America/Chicago', TS)::TIMESTAMP_NTZ AS TS_CT
    FROM brk
    WHERE (PREV_TS IS NULL OR PREV_TS < DATEADD('minute', -60, TS))
      AND TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
),
det AS (
    -- the anchor's evidence: every failure in the 75 minutes up to it (>= N by construction, a failure
    -- stamped in the same millisecond as the success included: seq orders it first)
    SELECT a.EVENT_ID, COUNT(*) AS N_FAIL, COUNT(DISTINCT f.CLIENT_IP) AS N_FAIL_IPS,
           MIN(f.EVENT_TIMESTAMP) AS FIRST_FAIL, MAX(LEFT(COALESCE(f.ERROR_MESSAGE, ''), 200)) AS SAMPLE_ERROR
    FROM anc a
    JOIN ev f
      ON f.USER_NAME = a.USER_NAME
     AND f.IS_SUCCESS = 'NO'
     AND f.EVENT_TIMESTAMP <= a.TS
     AND f.EVENT_TIMESTAMP >= DATEADD('minute', -75, a.TS)
    GROUP BY a.EVENT_ID
),
adm AS (
    -- admin-tier roles the user held AT the anchor (direct grants; owner list, same as SEC_ADMIN_GRANT)
    SELECT a.EVENT_ID, MIN(g.ROLE) AS ADMIN_ROLE, COUNT(DISTINCT g.ROLE) AS N_ADMIN_ROLES
    FROM anc a
    JOIN SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
      ON g.GRANTEE_NAME = a.USER_NAME
     AND g.ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',
                    'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
     AND g.CREATED_ON <= a.TS
     AND (g.DELETED_ON IS NULL OR g.DELETED_ON > a.TS)
    GROUP BY a.EVENT_ID
),
x AS (
    SELECT a.USER_NAME, a.EVENT_ID, a.TS_CT, a.LAST_BEND, a.TS, a.CLIENT_IP, a.CLIENT_TYPE, a.AUTH_FACTOR,
           d.N_FAIL, d.N_FAIL_IPS, d.FIRST_FAIL, d.SAMPLE_ERROR, m.ADMIN_ROLE, m.N_ADMIN_ROLES,
           (HOUR(a.TS_CT) >= 20 OR HOUR(a.TS_CT) < 6 OR DAYOFWEEKISO(a.TS_CT) >= 6) AS OFF_HOURS
    FROM anc a
    JOIN det d ON d.EVENT_ID = a.EVENT_ID
    LEFT JOIN adm m ON m.EVENT_ID = a.EVENT_ID
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT c.RULE_ID,
       'ALL',
       IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRITICAL', c.SEVERITY),
       LEFT('Possible account takeover: ' || x.USER_NAME || ' logged in ' ||
            DATEDIFF('minute', x.LAST_BEND, x.TS) || ' min after a burst of ' || x.N_FAIL || ' failed logins' ||
            IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL,
                ' (' || IFF(x.OFF_HOURS, 'off-hours', '') ||
                IFF(x.OFF_HOURS AND x.ADMIN_ROLE IS NOT NULL, ', ', '') ||
                IFF(x.ADMIN_ROLE IS NOT NULL, 'admin role ' || x.ADMIN_ROLE, '') || ')', ''), 300),
       LEFT('Success ' || TO_VARCHAR(x.TS_CT, 'YYYY-MM-DD HH24:MI') || ' Central from ' ||
            COALESCE(x.CLIENT_IP, '?') || ' (' || COALESCE(x.CLIENT_TYPE, '?') || ', ' ||
            COALESCE(x.AUTH_FACTOR, '?') || ') after ' || x.N_FAIL || ' failed logins from ' ||
            x.N_FAIL_IPS || ' IP(s) since ' ||
            TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', x.FIRST_FAIL)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI') ||
            ' Central. Off-hours: ' || IFF(x.OFF_HOURS, 'yes', 'no') || '. Admin roles held: ' ||
            COALESCE(x.ADMIN_ROLE || IFF(x.N_ADMIN_ROLES > 1, ' +' || (x.N_ADMIN_ROLES - 1) || ' more', ''), 'none') ||
            '. Sample error: ' || COALESCE(NULLIF(x.SAMPLE_ERROR, ''), 'n/a') ||
            ' | Confirm with the user; unexplained = disable the user and rotate credentials (playbook). Never auto-declares an incident: declare one by hand. Source: ACCOUNT_USAGE.LOGIN_HISTORY - review in Security -> Access', 2000),
       x.N_FAIL,
       c.RULE_ID || '|' || LEFT(x.USER_NAME, 200) || '|' || IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRIT', 'WARN') ||
           '|' || TO_VARCHAR(CONVERT_TIMEZONE('UTC', x.TS)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI:SS.FF3')
FROM cfg c
JOIN x ON c.RULE_ID = 'SEC_LOGIN_TAKEOVER'

) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WHERE NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
)
  -- never mint the WARN band once this anchor's CRIT event exists (any status): a CRIT row is never downgraded
  AND NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS h
    WHERE b.DEDUPE_KEY LIKE '%|WARN|%'
      AND h.RULE_ID = b.RULE_ID
      AND h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|WARN|', '|CRIT|')
)
) p
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS w
       ON w.RULE_ID = p.RULE_ID
      AND p.DEDUPE_KEY LIKE '%|CRIT|%'
      AND w.DEDUPE_KEY = REPLACE(p.DEDUPE_KEY, '|CRIT|', '|WARN|')
ORDER BY p.DEDUPE_KEY;

-- P174.4 what the next hourly scan raises for SEC_NEW_ADMIN_NETWORK after V174 (arm [18]'s own statement): a holder's
--        user + IP pairs first seen (against 90 days) in the last 24h, at or over the rule's threshold.
WITH cfg AS (
    SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
),
recent AS (
    -- V173: this rule's events raised in the last 48h, the date-stripped head precomputed. It
    -- carries the RULE_ID = b.RULE_ID (every b row is this rule) and 48h legs of the V168 guard,
    -- uncorrelated; a NULL key can never satisfy the two guards that read it.
    SELECT DEDUPE_KEY,
           LENGTH(DEDUPE_KEY) AS KEY_LEN,
           LEFT(DEDUPE_KEY, LENGTH(DEDUPE_KEY) - 10) AS KEY_HEAD
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
      AND RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())
      AND DEDUPE_KEY IS NOT NULL
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT c.RULE_ID, 'ALL', c.SEVERITY,
       LEFT(nn.USER_NAME || IFF(nn.SUCCESSES > 0,
           ' logged in from new network ' || nn.CLIENT_IP,
           ': ' || nn.LOGINS || ' failed login attempt(s) from new network ' || nn.CLIENT_IP
               || ' (0 successful)'), 300),
       'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline; successful '
           || nn.SUCCESSES || ' of ' || nn.LOGINS || ' attempt(s). Auth: '
           || COALESCE(nn.AUTH_FACTOR, '?')
           || IFF(nn.SUCCESSES > 0,
                  '. Expected after travel/VPN/host changes; anything else is the finding.',
                  '. No attempt got in; a success from this IP inside its first 24h raises a separate event.'),
       nn.LOGINS,
       c.RULE_ID || '|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, '', '|FAILED')
           || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', nn.FIRST_SEEN)), 'YYYY-MM-DD')
FROM cfg c
JOIN (
    SELECT L.USER_NAME,
           COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
           MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
           COUNT(*) AS LOGINS,
           COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,
           MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
    JOIN (
        SELECT DISTINCT GRANTEE_NAME
        FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
        WHERE DELETED_ON IS NULL
          AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')
    ) A ON A.GRANTEE_NAME = L.USER_NAME
    WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
    GROUP BY 1, 2
    HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
) nn
  ON c.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
 AND nn.LOGINS >= c.THRESHOLD_NUM

) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
-- V173: the V168 guard as three AND-ed NOT EXISTS, each correlated by plain equalities only. V168 had one
-- NOT EXISTS whose every outer reference sat under an OR, which Snowflake cannot decorrelate ('Unsupported
-- subquery type cannot be evaluated', every hourly run from 2026-10-02 07:08). NOT EXISTS (A OR B OR C) =
-- NOT EXISTS (A) AND NOT EXISTS (B) AND NOT EXISTS (C): the same rows are kept.
WHERE NOT EXISTS (   -- (1) the exact key (user|IP[|FAILED]|first-seen day), any age: R2-036
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
)
  AND NOT EXISTS (   -- (2) this pair's V162 undated key (date and '|FAILED' stripped), last 48h
    SELECT 1 FROM recent r
    WHERE r.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')
)
  AND NOT EXISTS (   -- (3) the same exact base and outcome on another first-seen day, last 48h: R2-039
    SELECT 1 FROM recent r
    WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)
      AND r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)
);
