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
    where = and_where(
        "DELETED_ON IS NULL",
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
    """Current holders of break-glass roles; should be a short, known list."""
    return """
SELECT
    ROLE AS ADMIN_ROLE,
    GRANTEE_NAME AS USER_NAME,
    GRANTED_BY,
    CREATED_ON AS GRANTED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE DELETED_ON IS NULL
  AND ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'ORGADMIN')
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
    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
      WHERE DELETED_ON IS NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS EXPIRED_CREDENTIALS,
    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
      WHERE DELETED_ON IS NULL
        AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 30, CURRENT_TIMESTAMP())) AS EXPIRING_CREDENTIALS,
    (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
      WHERE DELETED_ON IS NULL
        AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
        AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS BREAKGLASS_GRANTS_30D
"""


def show_warehouses_sql() -> str:
    """SHOW-based (WAREHOUSES view absent on this account); LIMIT keeps the
    row-cap rewrite away. resource_monitor/auto_suspend parsed client-side."""
    return "SHOW WAREHOUSES LIMIT 500"


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

def day_ddl(day: object) -> str:
    """Replay: DDL that landed on one day (drop/alter/create, account-wide)."""
    from app.data.common import day_literal

    lit = day_literal(day)
    return f"""
SELECT START_TIME, USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
       LEFT(QUERY_TEXT, 140) AS DDL_PREVIEW
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE DATE(START_TIME) = {lit}
  AND EXECUTION_STATUS = 'SUCCESS'
  AND QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER',
                     'ALTER_TABLE_MODIFY_COLUMN', 'DROP', 'RENAME', 'ALTER_SESSION',
                     'CREATE_VIEW', 'ALTER_WAREHOUSE_SUSPEND', 'ALTER_WAREHOUSE_RESUME',
                     'GRANT', 'REVOKE', 'TRUNCATE_TABLE')
ORDER BY START_TIME
LIMIT 300
"""


def day_grants(day: object) -> str:
    """Replay: role grants created or revoked on one day."""
    from app.data.common import day_literal

    lit = day_literal(day)
    return f"""
SELECT CREATED_ON AS GRANTED_AT, DELETED_ON, ROLE, GRANTED_TO, GRANTEE_NAME, GRANTED_BY
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE DATE(CREATED_ON) = {lit} OR DATE(DELETED_ON) = {lit}
ORDER BY GRANTED_AT
LIMIT 200
"""
