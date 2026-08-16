"""Security & governance SQL builders."""

from __future__ import annotations

from app import companies
from app.config import core_object, mart_object
from app.core.sqlsafe import contains_filter, sql_literal
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
        "COALESCE(U.HAS_MFA, FALSE) = FALSE",  # triage #10: match governance_counts (native MFA counts, not Duo-only)
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
        "COALESCE(U.HAS_MFA, FALSE) = FALSE",  # triage #10: match governance_counts (native MFA counts, not Duo-only)
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


def admin_role_holders(company: str = "ALL") -> str:
    """Current holders of the admin roles (owner 2026-07-13: the only roles
    with access are SNOW_ACCOUNTADMINS / SNOW_SYSADMINS); short, known list."""
    where = and_where(
        "DELETED_ON IS NULL",
        "ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')",
        companies.user_clause(company, "GRANTEE_NAME"),
    )
    return f"""
SELECT
    ROLE AS ADMIN_ROLE,
    GRANTEE_NAME AS USER_NAME,
    GRANTED_BY,
    CREATED_ON AS GRANTED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE {where}
ORDER BY ROLE, USER_NAME
"""


def recent_ddl_changes(days: int, company: str = "ALL", database: str = "", schema_contains: str = "") -> str:
    """Who changed what: DDL/DCL statements grouped by user and object type."""
    days = bounded_days(days, maximum=30)
    # C11: scope change evidence by ACTOR *or* OBJECT, not actor AND object. GRANT /
    # REVOKE and other account-level DDL carry a NULL DATABASE_NAME (dropped by the
    # database lens), and a cross-company change (a Trexis user's DDL in an ALFA
    # database) failed both lenses under the old AND — visible only under ALL. OR-ing
    # follows the actor's company for context-less DDL and shows cross-company changes
    # under both lenses (union semantics; the disclosure caption states the rule).
    _uc = companies.user_clause(company, "g.USER_NAME")
    _dc = companies.database_clause(company, "g.DATABASE_NAME")
    _actor_or_object = f"({_uc} OR {_dc})" if _uc and _dc else (_uc or _dc)
    where = and_where(
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "EXECUTION_STATUS = 'SUCCESS'",
        ("(QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW', 'ALTER', 'ALTER_TABLE_MODIFY_COLUMN', "
         "'ALTER_SESSION', 'ALTER_USER', 'CREATE_USER', 'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', "
         "'DROP_ROLE', 'DROP', 'GRANT', 'REVOKE', 'CREATE_TABLE_AS_SELECT', "
         "'RENAME', 'RENAME_TABLE', 'TRUNCATE_TABLE') OR QUERY_TYPE ILIKE '%POLICY%' "
         "OR QUERY_TYPE ILIKE '%USER%' OR QUERY_TYPE ILIKE '%ROLE%' "
         "OR QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%')"),
    )
    scope_where = and_where(_actor_or_object)
    # Resolve role-backed actor company after aggregation so the UDF runs once
    # per displayed change group instead of once per raw history row.
    return f"""
WITH grouped AS (
    SELECT
        DATE(q.START_TIME) AS DAY,
        q.USER_NAME,
        q.ROLE_NAME,
        q.QUERY_TYPE,
        q.DATABASE_NAME,
        q.SCHEMA_NAME,
        COUNT(*) AS STATEMENTS,
        MAX(q.START_TIME) AS LAST_CHANGE,
        MAX_BY(LEFT(q.QUERY_TEXT, 160), q.START_TIME) AS LAST_STATEMENT_PREVIEW,
        MAX_BY(q.QUERY_ID, q.START_TIME) AS QUERY_ID,
        LEAST(100,
          CASE WHEN q.QUERY_TYPE ILIKE 'DROP%' OR q.QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90
               WHEN q.QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 80
               WHEN q.QUERY_TYPE ILIKE '%POLICY%' OR q.QUERY_TYPE ILIKE '%USER%' THEN 85
               WHEN q.QUERY_TYPE ILIKE 'ALTER%' OR q.QUERY_TYPE ILIKE 'RENAME%' THEN 55
               ELSE 30 END
          + IFF(q.ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS'), 10, 0)
          + IFF(COALESCE(q.DATABASE_NAME, '') ILIKE '%PROD%', 10, 0)
        ) AS RISK_SCORE
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
    GROUP BY 1, 2, 3, 4, 5, 6
), scoped AS (
    SELECT g.*
    FROM grouped g
    WHERE {scope_where}
)
SELECT g.*,
       CASE WHEN g.RISK_SCORE >= 90 THEN 'CRITICAL'
            WHEN g.RISK_SCORE >= 70 THEN 'HIGH'
            WHEN g.RISK_SCORE >= 45 THEN 'MEDIUM' ELSE 'LOW' END AS RISK_LEVEL,
       CASE WHEN g.DATABASE_NAME IS NULL OR g.SCHEMA_NAME IS NULL THEN 'NOT_APPLICABLE'
            WHEN r.CHANGE_SEEN_AT IS NOT NULL THEN 'REGISTERED'
            ELSE 'UNREGISTERED' END AS CHANGE_REGISTRATION
FROM scoped g
LEFT JOIN {core_object('OBJECT_CHANGE_REGISTRY')} r
  ON r.DATABASE_NAME = g.DATABASE_NAME
 AND r.SCHEMA_NAME = g.SCHEMA_NAME
 AND ABS(DATEDIFF('hour', r.CHANGE_SEEN_AT, g.LAST_CHANGE)) <= 24
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY g.DAY, g.USER_NAME, g.ROLE_NAME, g.QUERY_TYPE,
                 g.DATABASE_NAME, g.SCHEMA_NAME
    ORDER BY r.CHANGE_SEEN_AT DESC NULLS LAST
) = 1
ORDER BY g.LAST_CHANGE DESC
LIMIT 300
"""


def failed_login_reasons(days: int, company: str = "ALL") -> str:
    """Failed logins grouped by reason — network-policy blocks surface
    separately from bad credentials."""
    # r6-bug13: match failed_logins()' 30d cap. This breakdown is presented as the
    # decomposition of the failed-logins table directly above it; a wider (90d) window
    # here made the two panels disagree under one "90 days" scope and never reconcile.
    days = bounded_days(days, maximum=30)
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


def admin_role_activity(days: int, company: str = "ALL") -> str:
    """Daily statement volume under break-glass admin roles. Routine work
    belongs on SNOW_SYSADMINS; this line should hug zero."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')",
        companies.user_clause(company, "USER_NAME"),
    )
    return f"""
SELECT
    DATE_TRUNC('day', START_TIME) AS DAY,
    ROLE_NAME,
    COUNT(*) AS STATEMENTS,
    COUNT(DISTINCT USER_NAME) AS USERS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
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


def show_shares_sql() -> str:
    """rec#25: outbound/inbound share inventory — the data-exposure surface.

    SHOW-based like SHOW WAREHOUSES/DATABASES: this account has no ACCOUNT_USAGE
    shares view, and SHOW SHARES is the one read that names every share, its KIND
    (OUTBOUND=we expose / INBOUND=we consume), the database it exposes, the
    consumer accounts (``to``), and any marketplace listing. Columns are parsed
    client-side by logic/exposure.classify_share_exposure; LIMIT keeps the
    row-cap rewrite away."""
    return "SHOW SHARES LIMIT 500"


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


def new_network_logins(days: int = 7, company: str = "ALL") -> str:
    """r25 #6 (owner pick): privileged logins from never-before-seen networks.

    Baseline = 90 days of LOGIN_HISTORY for break-glass users (same role list
    as admin_role_holders); a row surfaces only when a (user, IP) pair FIRST
    appears inside the triage window. An IP quiet for 90+ days re-flags on
    purpose — better a stale re-flag than a silent novel network.
    """
    days = bounded_days(days)
    admin_where = and_where(
        "DELETED_ON IS NULL",
        "ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')",
        companies.user_clause(company, "GRANTEE_NAME"),
    )
    return f"""
WITH admins AS (
    SELECT DISTINCT GRANTEE_NAME AS USER_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE {admin_where}
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
       MAX_BY(H.FIRST_AUTHENTICATION_FACTOR, H.EVENT_TIMESTAMP) AS AUTH_FACTOR
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


def unload_activity(days: int = 30, company: str = "ALL", database: str = "",
                    schema_contains: str = "") -> str:
    """r25 #7b (owner pick): who runs COPY INTO <location> (QUERY_TYPE
    'UNLOAD'), per user/day. GB_OUT sums both QUERY_HISTORY byte counters —
    for an unload exactly one of them carries the payload, so the sum is the
    real figure, not an overstatement. SAMPLE_TARGET = newest statement's
    first 120 chars, so the destination is visible without a drill.

    #35: honors the global Database/Schema filter (in addition to the company
    user classification). QUERY_HISTORY records the session's DATABASE_NAME/
    SCHEMA_NAME — the context the unload's source table resolves in — so a
    scoped screen no longer silently widens to company-wide."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "QUERY_TYPE = 'UNLOAD'",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.user_clause(company, "USER_NAME"),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT DATE(START_TIME) AS DAY,
       USER_NAME,
       ROLE_NAME,
       COUNT(*) AS UNLOADS,
       ROUND((SUM(COALESCE(BYTES_WRITTEN, 0))
             + SUM(COALESCE(BYTES_WRITTEN_TO_RESULT, 0))) / POWER(1024, 3), 3) AS GB_OUT,
       MAX(START_TIME) AS LAST_UNLOAD,
       MAX_BY(REGEXP_SUBSTR(
           QUERY_TEXT,
           'COPY[[:space:]]+INTO[[:space:]]+([^[:space:]()]+)',
           1, 1, 'ie', 1
       ), START_TIME) AS TARGET_LOCATION,
       MAX_BY(LEFT(QUERY_TEXT, 120), START_TIME) AS SAMPLE_TARGET,
       MAX_BY(QUERY_ID, START_TIME) AS QUERY_ID
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1, 2, 3
ORDER BY DAY DESC, GB_OUT DESC
LIMIT 300
"""


def _local_company_clause(company: str, column: str = "COMPANY") -> str:
    value = str(company or "ALL").strip().upper()
    return "" if value == "ALL" else f"(UPPER({column}) = {sql_literal(value)} OR UPPER({column}) = 'ALL')"


def security_exception_queue(company: str = "ALL", limit: int = 100) -> str:
    """V075 local exception queue; no ACCOUNT_USAGE work on first paint."""
    limit = max(1, min(int(limit or 100), 300))
    value = str(company or "ALL").strip().upper()
    company_clause = "" if value == "ALL" else (
        f"(UPPER(COMPANY) IN ('ALL', {sql_literal(value)}) "
        f"OR UPPER(COALESCE(ACTOR_COMPANY, '')) = {sql_literal(value)} "
        f"OR UPPER(COALESCE(OBJECT_COMPANY, '')) = {sql_literal(value)})"
    )
    where = and_where(company_clause)
    return f"""
SELECT DOMAIN, COMPANY, ACTOR_COMPANY, OBJECT_COMPANY,
       ENTITY_TYPE, ENTITY_KEY, SEVERITY, TITLE, DETAIL,
       IMPACT_COUNT, DETECTED_AT, CONFIDENCE, OWNER, ACTION_ID, STATUS
FROM {core_object('V_SECURITY_EXCEPTION_QUEUE')}
WHERE {where}
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
         WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END,
         DETECTED_AT DESC
LIMIT {limit}
"""


def security_domain_coverage() -> str:
    """Coverage/freshness contract for the five Security risk domains."""
    return f"""
WITH posture AS (
    SELECT MAX(DAY) AS NEWEST,
           COUNT(DISTINCT IFF(METRIC IN ('MFA_GAP_USERS', 'EXPIRED_CRED', 'EXPIRING_CRED_10D'),
                              METRIC, NULL)) AS ID_SIGNALS,
           COUNT(DISTINCT IFF(METRIC = 'BREAKGLASS_GRANTS_30D', METRIC, NULL)) AS PRIV_SIGNALS
    FROM {mart_object('MART_SECURITY_POSTURE_DAILY')}
), fresh AS (
    SELECT SOURCE_NAME, SNAPSHOT_TS, STATUS
    FROM {core_object('SOURCE_FRESHNESS_STATE')}
    WHERE SOURCE_NAME IN ('FACT_SECURITY_CHANGE', 'SECURITY_TRUST_SNAPSHOT')
)
SELECT 'IDENTITY' AS DOMAIN,
       IFF(ID_SIGNALS >= 3 AND NEWEST >= DATEADD('day', -2, CURRENT_DATE()),
           'COMPLETE', IFF(ID_SIGNALS >= 3, 'STALE', 'INCOMPLETE')) AS COVERAGE,
       NEWEST AS NEWEST FROM posture
UNION ALL
SELECT 'PRIVILEGE',
       IFF(PRIV_SIGNALS >= 1 AND NEWEST >= DATEADD('day', -2, CURRENT_DATE()),
           'COMPLETE', IFF(PRIV_SIGNALS >= 1, 'STALE', 'INCOMPLETE')),
       NEWEST FROM posture
UNION ALL
SELECT 'CHANGE RISK',
       IFF(COALESCE(STATUS, '') = 'OK'
           AND SNAPSHOT_TS >= DATEADD('hour', -3, CURRENT_TIMESTAMP()),
           'COMPLETE', IFF(COALESCE(STATUS, '') = 'OK', 'STALE', 'INCOMPLETE')),
       SNAPSHOT_TS
FROM fresh WHERE SOURCE_NAME = 'FACT_SECURITY_CHANGE'
UNION ALL
SELECT 'DATA MOVEMENT', 'ON_DEMAND', NULL
UNION ALL
SELECT 'TRUST CENTER',
       IFF(COALESCE(STATUS, '') = 'OK'
           AND SNAPSHOT_TS >= DATEADD('hour', -3, CURRENT_TIMESTAMP()),
           'COMPLETE', IFF(COALESCE(STATUS, '') = 'OK', 'STALE', 'INCOMPLETE')),
       SNAPSHOT_TS
FROM fresh WHERE SOURCE_NAME = 'SECURITY_TRUST_SNAPSHOT'
"""


def trust_center_delta() -> str:
    return f"""
SELECT SCANNER_ID, SCANNER_NAME, SEVERITY, CURRENT_COUNT, PRIOR_COUNT,
       COUNT_DELTA, CHANGE_STATE, SCANNED_AT, SNAPSHOT_DAY
FROM {core_object('V_SECURITY_TRUST_DELTA')}
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
         WHEN 'MEDIUM' THEN 2 ELSE 3 END,
         ABS(COUNT_DELTA) DESC, CURRENT_COUNT DESC
LIMIT 200
"""


def login_fact_coverage(days: int = 30) -> str:
    days = bounded_days(days, maximum=90)
    return f"""
SELECT MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY, COUNT(*) AS FACT_ROWS,
       DATEDIFF('day', MIN(DAY), MAX(DAY)) + 1 AS COVERAGE_DAYS,
       MAX(LOAD_TS) AS LAST_LOAD
FROM {core_object('FACT_LOGIN_DAILY')}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
"""


def security_login_fact_coverage(days: int = 30) -> str:
    """Coverage contract for the V075 login evidence fact."""
    days = bounded_days(days, maximum=90)
    return f"""
SELECT MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY, COUNT(*) AS FACT_ROWS,
       DATEDIFF('day', MIN(DAY), MAX(DAY)) + 1 AS COVERAGE_DAYS,
       MAX(LOAD_TS) AS LAST_LOAD
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
"""


def failed_logins_fact(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, maximum=30)
    where = and_where(
        f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        "FAILURES > 0",
        _local_company_clause(company),
    )
    return f"""
SELECT USER_NAME,
       SUM(FAILURES) AS FAILED_ATTEMPTS,
       COUNT(DISTINCT CLIENT_IP) AS DISTINCT_IPS,
       MAX(LAST_SEEN) AS LAST_ATTEMPT,
       MAX_BY(ERROR_CATEGORY, LAST_SEEN) AS LAST_ERROR
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE {where}
GROUP BY USER_NAME
ORDER BY FAILED_ATTEMPTS DESC
LIMIT 100
"""


def failed_login_reasons_fact(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, maximum=30)
    where = and_where(
        f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        "FAILURES > 0",
        _local_company_clause(company),
    )
    return f"""
SELECT ERROR_CATEGORY AS REASON,
       IFF(ERROR_CATEGORY = 'NETWORK POLICY', 'NETWORK POLICY', 'CREDENTIAL / OTHER') AS CATEGORY,
       SUM(FAILURES) AS ATTEMPTS,
       COUNT(DISTINCT USER_NAME) AS USERS,
       COUNT(DISTINCT CLIENT_IP) AS SOURCE_IPS,
       MAX(LAST_SEEN) AS LAST_SEEN
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE {where}
GROUP BY 1, 2
ORDER BY ATTEMPTS DESC
LIMIT 50
"""


def new_network_logins_fact(days: int = 7, company: str = "ALL") -> str:
    days = bounded_days(days, maximum=90)
    admin_where = and_where(
        "DELETED_ON IS NULL",
        "ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')",
        companies.user_clause(company, "GRANTEE_NAME"),
    )
    return f"""
WITH admins AS (
    SELECT DISTINCT GRANTEE_NAME AS USER_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE {admin_where}
), first_seen AS (
    SELECT f.USER_NAME, f.CLIENT_IP, MIN(f.FIRST_SEEN) AS FIRST_SEEN
    FROM {core_object('FACT_SECURITY_LOGIN_DAILY')} f
    JOIN admins a ON a.USER_NAME = f.USER_NAME
    WHERE f.DAY >= DATEADD('day', -90, CURRENT_DATE())
    GROUP BY 1, 2
)
SELECT f.USER_NAME, f.CLIENT_IP, f.FIRST_SEEN,
       SUM(h.LOGINS) AS LOGINS, SUM(h.SUCCESSES) AS SUCCESSES,
       MAX(h.LAST_SEEN) AS LAST_LOGIN,
       MAX_BY(h.AUTH_FACTOR, h.LAST_SEEN) AS AUTH_FACTOR
FROM first_seen f
JOIN {core_object('FACT_SECURITY_LOGIN_DAILY')} h
  ON h.USER_NAME = f.USER_NAME AND h.CLIENT_IP = f.CLIENT_IP
WHERE f.FIRST_SEEN >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3
ORDER BY f.FIRST_SEEN DESC
LIMIT 200
"""


def recent_ddl_changes_fact(days: int, company: str = "ALL", database: str = "",
                            schema_contains: str = "") -> str:
    days = bounded_days(days, maximum=90)
    actor_company = companies.user_clause(company, "g.USER_NAME")
    object_company = companies.database_clause(company, "g.DATABASE_NAME")
    actor_or_object = (
        f"({actor_company} OR {object_company})"
        if actor_company and object_company else (actor_company or object_company)
    )
    where = and_where(
        f"f.EVENT_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        companies.database_equals_clause(database, "f.DATABASE_NAME"),
        contains_filter("f.SCHEMA_NAME", schema_contains),
    )
    scope_where = and_where(actor_or_object)
    # Preserve actor-OR-object parity while limiting role-backed company UDF
    # evaluation to grouped fact rows.
    return f"""
WITH grouped AS (
    SELECT f.DAY, f.USER_NAME, f.ROLE_NAME, f.QUERY_TYPE, f.DATABASE_NAME, f.SCHEMA_NAME,
           COUNT(*) AS STATEMENTS, MAX(f.EVENT_TS) AS LAST_CHANGE,
           MAX_BY(f.QUERY_PREVIEW, f.EVENT_TS) AS LAST_STATEMENT_PREVIEW,
           MAX_BY(f.QUERY_ID, f.EVENT_TS) AS QUERY_ID,
           MAX(f.RISK_SCORE) AS RISK_SCORE,
           MAX_BY(f.RISK_LEVEL, f.RISK_SCORE) AS RISK_LEVEL
    FROM {core_object('FACT_SECURITY_CHANGE')} f
    WHERE {where}
    GROUP BY 1, 2, 3, 4, 5, 6
), scoped AS (
    SELECT g.*
    FROM grouped g
    WHERE {scope_where}
)
SELECT g.*,
       CASE WHEN g.DATABASE_NAME IS NULL OR g.SCHEMA_NAME IS NULL THEN 'NOT_APPLICABLE'
            WHEN r.CHANGE_SEEN_AT IS NOT NULL THEN 'REGISTERED'
            ELSE 'UNREGISTERED' END AS CHANGE_REGISTRATION
FROM scoped g
LEFT JOIN {core_object('OBJECT_CHANGE_REGISTRY')} r
  ON r.DATABASE_NAME = g.DATABASE_NAME
 AND r.SCHEMA_NAME = g.SCHEMA_NAME
 AND ABS(DATEDIFF('hour', r.CHANGE_SEEN_AT, g.LAST_CHANGE)) <= 24
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY g.DAY, g.USER_NAME, g.ROLE_NAME, g.QUERY_TYPE,
                 g.DATABASE_NAME, g.SCHEMA_NAME
    ORDER BY r.CHANGE_SEEN_AT DESC NULLS LAST
) = 1
ORDER BY g.LAST_CHANGE DESC
LIMIT 300
"""


def admin_role_activity_fact(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, maximum=90)
    where = and_where(
        f"EVENT_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        "ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')",
        companies.user_clause(company, "USER_NAME"),
    )
    return f"""
SELECT DAY, ROLE_NAME, COUNT(*) AS STATEMENTS, COUNT(DISTINCT USER_NAME) AS USERS
FROM {core_object('FACT_SECURITY_CHANGE')}
WHERE {where}
GROUP BY 1, 2
ORDER BY 1
"""


def effective_access(company: str = "ALL") -> str:
    user_filter = companies.user_clause(company, "g.GRANTEE_NAME")
    where = and_where("g.DELETED_ON IS NULL", user_filter)
    return f"""
WITH RECURSIVE role_tree (USER_NAME, DIRECT_ROLE, EFFECTIVE_ROLE, DEPTH, ACCESS_PATH) AS (
    SELECT g.GRANTEE_NAME, g.ROLE, g.ROLE, 0, g.ROLE
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
    WHERE {where}
    UNION ALL
    SELECT r.USER_NAME, r.DIRECT_ROLE, h.NAME, r.DEPTH + 1,
           r.ACCESS_PATH || ' -> ' || h.NAME
    FROM role_tree r
    JOIN SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES h
      ON h.GRANTED_ON = 'ROLE' AND h.GRANTEE_NAME = r.EFFECTIVE_ROLE
     AND h.DELETED_ON IS NULL
    WHERE r.DEPTH < 10 AND POSITION(' -> ' || h.NAME || ' -> ' IN ' -> ' || r.ACCESS_PATH || ' -> ') = 0
), privilege_rollup AS (
    SELECT GRANTEE_NAME AS ROLE_NAME,
           COUNT(*) AS PRIVILEGES,
           COUNT_IF(PRIVILEGE = 'OWNERSHIP') AS OWNERSHIP_GRANTS,
           COUNT_IF(PRIVILEGE = 'MANAGE GRANTS') AS MANAGE_GRANTS,
           COUNT_IF(PRIVILEGE IN ('CREATE USER', 'CREATE ROLE', 'APPLY MASKING POLICY',
                                  'APPLY ROW ACCESS POLICY')) AS SENSITIVE_PRIVILEGES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE DELETED_ON IS NULL AND GRANTED_ON <> 'ROLE'
    GROUP BY 1
)
SELECT r.USER_NAME, r.DIRECT_ROLE, r.EFFECTIVE_ROLE, r.DEPTH, r.ACCESS_PATH,
       COALESCE(p.PRIVILEGES, 0) AS PRIVILEGES,
       COALESCE(p.OWNERSHIP_GRANTS, 0) AS OWNERSHIP_GRANTS,
       COALESCE(p.MANAGE_GRANTS, 0) AS MANAGE_GRANTS,
       COALESCE(p.SENSITIVE_PRIVILEGES, 0) AS SENSITIVE_PRIVILEGES,
       LEAST(100, COALESCE(p.OWNERSHIP_GRANTS, 0) * 10
                  + COALESCE(p.MANAGE_GRANTS, 0) * 25
                  + COALESCE(p.SENSITIVE_PRIVILEGES, 0) * 20) AS RISK_SCORE
FROM role_tree r
LEFT JOIN privilege_rollup p ON p.ROLE_NAME = r.EFFECTIVE_ROLE
ORDER BY RISK_SCORE DESC, r.USER_NAME, r.DEPTH
LIMIT 3000
"""


def egress_baseline(days: int = 30) -> str:
    span = max(1, min(int(days or 30), 45))
    return f"""
WITH periods AS (
    SELECT COALESCE(TARGET_REGION, '(same region)') AS TARGET_REGION,
           ROUND(SUM(IFF(START_TIME >= DATEADD('day', -{span}, CURRENT_TIMESTAMP()),
                         BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 3) AS CURRENT_GB,
           ROUND(SUM(IFF(START_TIME < DATEADD('day', -{span}, CURRENT_TIMESTAMP()),
                         BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 3) AS PRIOR_GB
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
    WHERE START_TIME >= DATEADD('day', -{span * 2}, CURRENT_TIMESTAMP())
    GROUP BY 1
)
SELECT TARGET_REGION, CURRENT_GB, PRIOR_GB, {span} AS BASELINE_DAYS,
       IFF(PRIOR_GB = 0 AND CURRENT_GB > 0, 'NEW',
           IFF(CURRENT_GB > PRIOR_GB * 2 AND CURRENT_GB >= 1, 'SPIKE', 'NORMAL')) AS BEHAVIOR
FROM periods
WHERE CURRENT_GB > 0 OR PRIOR_GB > 0
ORDER BY IFF(BEHAVIOR IN ('NEW', 'SPIKE'), 0, 1), CURRENT_GB DESC
LIMIT 100
"""
