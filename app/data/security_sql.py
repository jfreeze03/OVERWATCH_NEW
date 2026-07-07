"""Security & governance SQL builders."""

from __future__ import annotations

from app import companies
from app.data.common import and_where, bounded_days


def users_without_mfa(company: str = "ALL") -> str:
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


def recent_ddl_changes(days: int, company: str = "ALL") -> str:
    """Who changed what: DDL/DCL statements grouped by user and object type."""
    days = bounded_days(days, maximum=30)
    where = and_where(
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
