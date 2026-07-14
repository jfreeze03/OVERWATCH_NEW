"""Security & governance SQL builders."""

from __future__ import annotations

from app import companies
from app.data.common import and_where, bounded_days


def users_without_mfa(company: str = "ALL") -> str:
    """Users lacking MFA who actually password-login — evidence from
    FACT_LOGIN_DAILY (loaded hourly), so the 30-day LOGIN_HISTORY scan runs
    once in the loader instead of on every page view. The page falls back to
    users_without_mfa_live() while the fact is empty/undeployed, because an
    empty evidence set must never read as "all clear"."""
    where = and_where(
        "U.DELETED_ON IS NULL",
        "COALESCE(U.DISABLED, FALSE) = FALSE",
        "COALESCE(U.HAS_PASSWORD, FALSE) = TRUE",
        "COALESCE(U.EXT_AUTHN_DUO, FALSE) = FALSE",
        companies.user_clause(company, "U.NAME"),
    )
    return f"""
WITH password_logins AS (
    SELECT
        USER_NAME,
        SUM(PASSWORD_LOGINS)              AS PASSWORD_LOGINS_30D,
        MAX(IFF(PASSWORD_LOGINS > 0, DAY, NULL)) AS LAST_PASSWORD_LOGIN
    FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
    WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())
    GROUP BY USER_NAME
    HAVING PASSWORD_LOGINS_30D > 0
)
SELECT
    U.NAME AS USER_NAME,
    U.LOGIN_NAME,
    U.LAST_SUCCESS_LOGIN,
    PL.PASSWORD_LOGINS_30D,
    PL.LAST_PASSWORD_LOGIN
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
JOIN password_logins PL ON PL.USER_NAME = U.NAME
WHERE {where}
ORDER BY PL.PASSWORD_LOGINS_30D DESC
LIMIT 200
"""


def users_without_mfa_live(company: str = "ALL") -> str:
    """Users lacking MFA who actually password-login (login-evidence based).

    Cross-checks LOGIN_HISTORY so SSO/key-pair-only users are not false
    positives — an MFA gap only matters where passwords are really used.
    """
    where = and_where(
        "U.DELETED_ON IS NULL",
        "COALESCE(U.DISABLED, FALSE) = FALSE",
        "COALESCE(U.HAS_PASSWORD, FALSE) = TRUE",
        "COALESCE(U.EXT_AUTHN_DUO, FALSE) = FALSE",
        companies.user_clause(company, "U.NAME"),
    )
    return f"""
WITH password_logins AS (
    SELECT
        USER_NAME,
        COUNT(*) AS PASSWORD_LOGINS_30D,
        MAX(EVENT_TIMESTAMP) AS LAST_PASSWORD_LOGIN
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE EVENT_TIMESTAMP >= DATEADD('day', -30, CURRENT_TIMESTAMP())
      AND FIRST_AUTHENTICATION_FACTOR = 'PASSWORD'
      AND IS_SUCCESS = 'YES'
    GROUP BY USER_NAME
)
SELECT
    U.NAME AS USER_NAME,
    U.LOGIN_NAME,
    U.LAST_SUCCESS_LOGIN,
    PL.PASSWORD_LOGINS_30D,
    PL.LAST_PASSWORD_LOGIN
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
JOIN password_logins PL ON PL.USER_NAME = U.NAME
WHERE {where}
ORDER BY PL.PASSWORD_LOGINS_30D DESC
LIMIT 200
"""


def failed_logins(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, maximum=30)
    where = and_where(
        f"EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "IS_SUCCESS = 'NO'",
        companies.user_clause(company),
    )
    return f"""
SELECT
    USER_NAME,
    COUNT(*) AS FAILED_ATTEMPTS,
    COUNT(DISTINCT CLIENT_IP) AS DISTINCT_IPS,
    MAX(EVENT_TIMESTAMP) AS LAST_ATTEMPT,
    MAX_BY(ERROR_MESSAGE, EVENT_TIMESTAMP) AS LAST_ERROR
FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
WHERE {where}
GROUP BY USER_NAME
ORDER BY FAILED_ATTEMPTS DESC
LIMIT 100
"""


def expiring_credentials(days_ahead: int = 30, company: str = "ALL") -> str:
    """ACCOUNT_USAGE.CREDENTIALS expiry watch (EXPIRATION_DATE, TIMESTAMP_LTZ).
    The caller still guards in case an edition lacks the column."""
    """Credentials expiring within the horizon (or already expired).

    Source: ACCOUNT_USAGE.CREDENTIALS (passwords, RSA keys, programmatic
    access tokens). Rows without an expiry never appear here by design.
    """
    days_ahead = max(1, min(int(days_ahead), 365))
    # NOTE: no DELETED_ON predicate — this account's CREDENTIALS view does
    # not expose the column (sibling of the V020 EXPIRES_AT->EXPIRATION_DATE
    # discovery; live error 2026-07-08 "invalid identifier 'DELETED_ON'").
    where = and_where(
        "EXPIRATION_DATE IS NOT NULL",
        f"EXPIRATION_DATE <= DATEADD('day', {days_ahead}, CURRENT_TIMESTAMP())",
        companies.user_clause(company, "USER_NAME"),
    )
    return f"""
SELECT
    USER_NAME,
    NAME AS CREDENTIAL_NAME,
    TYPE AS CREDENTIAL_TYPE,
    CREATED_ON,
    EXPIRATION_DATE AS EXPIRES_AT,
    DATEDIFF('day', CURRENT_TIMESTAMP(), EXPIRATION_DATE) AS DAYS_TO_EXPIRY,
    IFF(EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING') AS STATUS
FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
WHERE {where}
ORDER BY EXPIRATION_DATE
LIMIT 300
"""


def recent_role_grants(days: int) -> str:
    """Recently granted roles to users (account-wide governance view)."""
    days = bounded_days(days, maximum=90)
    return f"""
SELECT
    GRANTEE_NAME AS USER_NAME,
    ROLE AS GRANTED_ROLE,
    GRANTED_BY,
    CREATED_ON AS GRANTED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE CREATED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND DELETED_ON IS NULL
ORDER BY CREATED_ON DESC
LIMIT 200
"""


def admin_role_holders() -> str:
    """Current holders of the admin roles (owner 2026-07-13: the only roles
    with access are SNOW_ACCOUNTADMINS / SNOW_SYSADMINS); short, known list."""
    return """
SELECT
    ROLE AS ADMIN_ROLE,
    GRANTEE_NAME AS USER_NAME,
    GRANTED_BY,
    CREATED_ON AS GRANTED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE DELETED_ON IS NULL
  AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
ORDER BY ROLE, USER_NAME
"""


def recent_ddl_changes(days: int, company: str = "ALL", database: str = "", schema_contains: str = "") -> str:
    """Who changed what: DDL/DCL statements grouped by user and object type."""
    days = bounded_days(days, maximum=30)
    from app.core.sqlsafe import contains_filter

    where = and_where(
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        ("QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN', "
         "'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'CREATE_TABLE_AS_SELECT', 'RENAME_TABLE', 'TRUNCATE_TABLE')"),
        companies.user_clause(company),
        companies.database_clause(company),
    )
    return f"""
SELECT
    DATE(START_TIME) AS DAY,
    USER_NAME,
    ROLE_NAME,
    QUERY_TYPE,
    DATABASE_NAME,
    COUNT(*) AS STATEMENTS,
    MAX(START_TIME) AS LAST_CHANGE,
    MAX_BY(LEFT(QUERY_TEXT, 160), START_TIME) AS LAST_STATEMENT_PREVIEW
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1, 2, 3, 4, 5
ORDER BY LAST_CHANGE DESC
LIMIT 300
"""


def failed_login_reasons(days: int, company: str = "ALL") -> str:
    """Failed logins grouped by reason — network-policy blocks surface
    separately from bad credentials."""
    days = bounded_days(days)
    where = and_where(
        f"EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "IS_SUCCESS = 'NO'",
        companies.user_clause(company, "USER_NAME"),
    )
    return f"""
SELECT
    COALESCE(ERROR_MESSAGE, 'UNKNOWN') AS REASON,
    IFF(COALESCE(ERROR_MESSAGE, '') ILIKE '%network%', 'NETWORK POLICY', 'CREDENTIAL / OTHER') AS CATEGORY,
    COUNT(*) AS ATTEMPTS,
    COUNT(DISTINCT USER_NAME) AS USERS,
    COUNT(DISTINCT CLIENT_IP) AS SOURCE_IPS,
    MAX(EVENT_TIMESTAMP) AS LAST_SEEN
FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
WHERE {where}
GROUP BY 1, 2
ORDER BY ATTEMPTS DESC
LIMIT 50
"""


def admin_role_activity(days: int) -> str:
    """Daily statement volume under break-glass admin roles. Routine work
    belongs on SNOW_SYSADMINS; this line should hug zero."""
    days = bounded_days(days)
    return f"""
SELECT
    DATE_TRUNC('day', START_TIME) AS DAY,
    ROLE_NAME,
    COUNT(*) AS STATEMENTS,
    COUNT(DISTINCT USER_NAME) AS USERS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
  AND ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
GROUP BY 1, 2
ORDER BY 1
"""


def trust_center_findings() -> str:
    """Latest Trust Center run per scanner. Needs the TRUST_CENTER_VIEWER
    application role (the account already pays for Trust Center scans)."""
    return """
SELECT
    SCANNER_NAME,
    UPPER(SEVERITY) AS SEVERITY,
    TOTAL_AT_RISK_COUNT,
    CREATED_ON AS SCANNED_AT
FROM SNOWFLAKE.TRUST_CENTER.FINDINGS
QUALIFY ROW_NUMBER() OVER (PARTITION BY SCANNER_ID ORDER BY CREATED_ON DESC) = 1
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
         WHEN 'MEDIUM' THEN 2 ELSE 3 END, TOTAL_AT_RISK_COUNT DESC
LIMIT 100
"""


def governance_counts() -> str:
    """One statement, four governance-drift counts (warehouse checks come
    from SHOW WAREHOUSES client-side — this account lacks the WAREHOUSES view)."""
    return """
SELECT
    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
      WHERE U.DELETED_ON IS NULL AND U.DISABLED = FALSE
        AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
        -- ONE definition of "MFA gap" app-wide (review #10): the same
        -- password-login evidence the Access panel lists, not the old
        -- created-7-days-ago proxy that disagreed with it on one page.
        AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                    WHERE L.USER_NAME = U.NAME
                      AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                      AND L.PASSWORD_LOGINS > 0)) AS MFA_GAP_USERS,
    -- CREDENTIALS on this account exposes no DELETED_ON (live 2026-07-08).
    -- One scan serves both credential counts (r20 #17).
    C.EXPIRED_CREDENTIALS,
    C.EXPIRING_CREDENTIALS,
    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
      WHERE DELETED_ON IS NULL
        AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
        AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS BREAKGLASS_GRANTS_30D
FROM (SELECT COUNT_IF(EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS EXPIRED_CREDENTIALS,
             COUNT_IF(EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP()
                      AND DATEADD('day', 10, CURRENT_TIMESTAMP())) AS EXPIRING_CREDENTIALS
      FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS) C
"""


def show_warehouses_sql() -> str:
    """SHOW-based (WAREHOUSES view absent on this account); LIMIT keeps the
    row-cap rewrite away. resource_monitor/auto_suspend parsed client-side."""
    return "SHOW WAREHOUSES LIMIT 500"


def show_databases_sql() -> str:
    """SHOW-based database inventory (ACCOUNT_USAGE.DATABASES absent on this
    account, mirroring SHOW WAREHOUSES). Feeds the sidebar picker so new
    databases appear without a code change (item 8c, 2026-07-14); the hardcoded
    lists in companies.py stay the offline fallback."""
    return "SHOW DATABASES LIMIT 500"


def role_privilege_matrix() -> str:
    """Auditor sheet: privileges per role aggregated by object type."""
    return """
SELECT GRANTEE_NAME AS ROLE_NAME, GRANTED_ON AS OBJECT_TYPE, PRIVILEGE,
       COUNT(*) AS GRANT_COUNT
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
WHERE DELETED_ON IS NULL
GROUP BY 1, 2, 3
ORDER BY ROLE_NAME, GRANT_COUNT DESC
LIMIT 5000
"""


def unused_roles(days: int = 90) -> str:
    """Roles never assumed in the window but still granted — revoke fodder."""
    days = bounded_days(days)
    return f"""
SELECT r.NAME AS ROLE_NAME, r.CREATED_ON,
       (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
         WHERE g.ROLE = r.NAME AND g.DELETED_ON IS NULL) AS GRANTED_TO_USERS
FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
LEFT JOIN (
    SELECT DISTINCT ROLE_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
) q ON q.ROLE_NAME = r.NAME
WHERE r.DELETED_ON IS NULL AND q.ROLE_NAME IS NULL
  AND r.NAME NOT IN ('PUBLIC')
ORDER BY GRANTED_TO_USERS DESC, r.CREATED_ON
LIMIT 500
"""


def direct_role_grants() -> str:
    """Current role->user grants (auditors reconcile this against HR)."""
    return """
SELECT GRANTEE_NAME AS USER_NAME, COUNT(*) AS ROLE_COUNT,
       LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ROLES
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE DELETED_ON IS NULL
GROUP BY 1
ORDER BY ROLE_COUNT DESC
LIMIT 1000
"""


def grant_changes(days: int = 90) -> str:
    """Grants added or revoked in the window — the quarterly diff sheet."""
    days = bounded_days(days, 180)
    return f"""
SELECT ROLE, GRANTEE_NAME AS USER_NAME,
       IFF(DELETED_ON IS NOT NULL, 'REVOKED', 'GRANTED') AS CHANGE,
       COALESCE(DELETED_ON, CREATED_ON) AS CHANGED_AT, GRANTED_BY
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE CREATED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
   OR DELETED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
ORDER BY CHANGED_AT DESC
LIMIT 2000
"""

def client_drivers(days: int = 30, company: str = "ALL") -> str:
    """Driver/version inventory from ACCOUNT_USAGE.SESSIONS: which driver,
    which version, reported by which program, used by whom — the "when do
    we need to upgrade" sheet.

    PROGRAM is what the client self-reports in CLIENT_ENVIRONMENT: JDBC and
    Python tools usually set it (DBeaver, VS Code); plenty of ODBC tools
    (Erwin) do not, so '(not reported)' is honest, not a bug. STATUS compares
    each version against the newest version of the SAME driver seen in this
    account this window — the only latest-version truth available from
    inside Snowflake. Version key pads dot-segments so 3.10.2 > 3.9.1.
    SESSIONS lags up to ~3h; POSIX classes only (no backslashes survive
    the string layers — V022 lesson).
    """
    days = bounded_days(days, maximum=90)
    where = and_where(
        f"CREATED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "CLIENT_APPLICATION_ID IS NOT NULL",
        companies.user_clause(company, "USER_NAME"),
    )
    return f"""
WITH s AS (
    SELECT
        USER_NAME,
        CREATED_ON,
        COALESCE(NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                 CLIENT_APPLICATION_ID) AS DRIVER,
        COALESCE(NULLIF(TRIM(REGEXP_SUBSTR(CLIENT_APPLICATION_ID, '[0-9][0-9.]*$')), ''), '?') AS VERSION,
        COALESCE(TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING, '(not reported)') AS PROGRAM
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE {where}
),
keyed AS (
    SELECT s.*,
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 1), ''), '0'), 6, '0') ||
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 2), ''), '0'), 6, '0') ||
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 3), ''), '0'), 6, '0') ||
           LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 4), ''), '0'), 6, '0') AS VKEY
    FROM s
),
grouped AS (
    SELECT DRIVER, VERSION, PROGRAM, MAX(VKEY) AS VKEY,
           COUNT(DISTINCT USER_NAME) AS USERS,
           COUNT(*) AS SESSIONS,
           MIN(DATE(CREATED_ON)) AS FIRST_SEEN,
           MAX(DATE(CREATED_ON)) AS LAST_SEEN,
           LEFT(LISTAGG(DISTINCT USER_NAME, ', ') WITHIN GROUP (ORDER BY USER_NAME), 160) AS SAMPLE_USERS
    FROM keyed
    GROUP BY DRIVER, VERSION, PROGRAM
)
SELECT DRIVER, VERSION, PROGRAM, USERS, SESSIONS, FIRST_SEEN, LAST_SEEN, SAMPLE_USERS,
       FIRST_VALUE(VERSION) OVER (PARTITION BY DRIVER ORDER BY VKEY DESC) AS NEWEST_IN_ACCOUNT,
       IFF(VKEY < MAX(VKEY) OVER (PARTITION BY DRIVER), 'BEHIND', 'CURRENT') AS STATUS
FROM grouped
ORDER BY DRIVER, VKEY DESC, SESSIONS DESC
LIMIT 500
"""


def day_ddl(day: object, company: str = "ALL") -> str:
    """Replay: DDL that landed on one day. Role-grain company scoping —
    Trexis automation roles stay off ALFA's replay (live finding)."""
    from app import companies as _companies
    from app.data.common import and_where, day_literal

    lit = day_literal(day)
    where = and_where(
        f"DATE(START_TIME) = {lit}",
        "EXECUTION_STATUS = 'SUCCESS'",
        """QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                     'ALTER_TABLE_MODIFY_COLUMN', 'DROP', 'RENAME', 'ALTER_SESSION',
                     'CREATE_VIEW', 'ALTER_WAREHOUSE_SUSPEND', 'ALTER_WAREHOUSE_RESUME',
                     'GRANT', 'REVOKE', 'TRUNCATE_TABLE')""",
        _companies.role_clause(company),
    )
    return f"""
SELECT START_TIME, USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
       LEFT(QUERY_TEXT, 140) AS DDL_PREVIEW
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
ORDER BY START_TIME
LIMIT 300
"""


def day_grants(day: object, company: str = "ALL") -> str:
    """Replay: role grants created or revoked on one day (role-scoped)."""
    from app import companies as _companies
    from app.data.common import and_where, day_literal

    lit = day_literal(day)
    where = and_where(
        f"(DATE(CREATED_ON) = {lit} OR DATE(DELETED_ON) = {lit})",
        _companies.role_clause(company, "ROLE"),
    )
    return f"""
SELECT CREATED_ON AS GRANTED_AT, DELETED_ON, ROLE, GRANTED_TO, GRANTEE_NAME, GRANTED_BY
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE {where}
ORDER BY GRANTED_AT
LIMIT 200
"""


def new_network_logins(days: int = 7) -> str:
    """r25 #6 (owner pick): privileged logins from never-before-seen networks.

    Baseline = 90 days of LOGIN_HISTORY for break-glass users (same role list
    as admin_role_holders); a row surfaces only when a (user, IP) pair FIRST
    appears inside the triage window. An IP quiet for 90+ days re-flags on
    purpose — better a stale re-flag than a silent novel network.
    """
    days = bounded_days(days)
    return f"""
WITH admins AS (
    SELECT DISTINCT GRANTEE_NAME AS USER_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
),
hist AS (
    SELECT L.USER_NAME,
           COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
           L.EVENT_TIMESTAMP, L.IS_SUCCESS, L.FIRST_AUTHENTICATION_FACTOR
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
    JOIN admins A ON A.USER_NAME = L.USER_NAME
    WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
),
first_seen AS (
    SELECT USER_NAME, CLIENT_IP, MIN(EVENT_TIMESTAMP) AS FIRST_SEEN
    FROM hist
    GROUP BY USER_NAME, CLIENT_IP
)
SELECT F.USER_NAME,
       F.CLIENT_IP,
       F.FIRST_SEEN,
       COUNT(*) AS LOGINS,
       SUM(IFF(H.IS_SUCCESS = 'YES', 1, 0)) AS SUCCESSES,
       MAX(H.EVENT_TIMESTAMP) AS LAST_LOGIN,
       MAX(H.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
FROM first_seen F
JOIN hist H ON H.USER_NAME = F.USER_NAME AND H.CLIENT_IP = F.CLIENT_IP
WHERE F.FIRST_SEEN >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY F.USER_NAME, F.CLIENT_IP, F.FIRST_SEEN
ORDER BY F.FIRST_SEEN DESC
LIMIT 200
"""


def egress_daily(days: int = 30) -> str:
    """r25 #7a (owner pick): outbound bytes by day and destination — the
    exfil canary and the surprise-transfer-bill canary are the same chart."""
    days = bounded_days(days)
    return f"""
SELECT DATE(START_TIME) AS DAY,
       COALESCE(TARGET_CLOUD, 'INTERNAL') AS TARGET_CLOUD,
       COALESCE(TARGET_REGION, '(same region)') AS TARGET_REGION,
       TRANSFER_TYPE,
       ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 3), 3) AS GB
FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3, 4
HAVING SUM(BYTES_TRANSFERRED) > 0
ORDER BY DAY, GB DESC
"""


def unload_activity(days: int = 30, company: str = "ALL") -> str:
    """r25 #7b (owner pick): who runs COPY INTO <location> (QUERY_TYPE
    'UNLOAD'), per user/day. GB_OUT sums both QUERY_HISTORY byte counters —
    for an unload exactly one of them carries the payload, so the sum is the
    real figure, not an overstatement. SAMPLE_TARGET = newest statement's
    first 120 chars, so the destination is visible without a drill."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "QUERY_TYPE = 'UNLOAD'",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.user_clause(company, "USER_NAME"),
    )
    return f"""
SELECT DATE(START_TIME) AS DAY,
       USER_NAME,
       ROLE_NAME,
       COUNT(*) AS UNLOADS,
       ROUND((SUM(COALESCE(BYTES_WRITTEN, 0))
            + SUM(COALESCE(BYTES_WRITTEN_TO_RESULT, 0))) / POWER(1024, 3), 3) AS GB_OUT,
       MAX(START_TIME) AS LAST_UNLOAD,
       MAX_BY(LEFT(QUERY_TEXT, 120), START_TIME) AS SAMPLE_TARGET
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY DAY DESC, GB_OUT DESC
LIMIT 300
"""

