"""Security & governance SQL builders."""

from __future__ import annotations

from datetime import timedelta

from app import companies
from app.config import core_object, mart_object
from app.core.sqlsafe import contains_filter, in_list, sql_literal
from app.data.common import (
    account_today_sql,
    and_where,
    bounded_days,
    resolve_effective_window,
    scope_window_where,
)
from app.logic.client_support import NO_CLIENT_ID, SNOWFLAKE_RUN_DRIVERS, SNOWFLAKE_RUN_PROGRAM_PREFIXES
from app.logic.identity_auth import SERVICE_TYPES
from app.logic.policy_coverage import FAMILY_NAME_PATTERN, FAMILY_SUFFIX_PATTERN
from app.logic.security import capped_window

# --- Admin-role tiers (codified AS-IS per owner decision 2026-09-10; byte-identical to the
# former inlined literals — one source of truth, no behaviour change). Two single-use sites
# stay inline for byte-identity and are commented at their call sites: governance_counts uses
# BREAK_GLASS on column ROLE inside a plain (non-f) SQL string, and effective_access applies
# REACHES_ADMIN_ROLES as a genuine two-line SQL list (its parity with this constant is locked in
# tests/migrations/test_v075_security_operating_model.py). ----------------------------------
ADMIN_HOLDER_ROLES: tuple[str, ...] = ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")
BREAK_GLASS_ROLES: tuple[str, ...] = ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS")
ELEVATED_ROLES: tuple[str, ...] = (
    "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS", "ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN",
)
REACHES_ADMIN_ROLES: tuple[str, ...] = (
    "SNOW_ACCOUNTADMINS", "ACCOUNTADMIN", "SNOW_SYSADMINS", "SECURITYADMIN",
)
# V162 (Next-Fifty #39, owner 2026-09-29): the admin tier the two HOURLY identity alerts watch, in the order
# SP_ALERT_SCAN writes it -- SEC_LOGIN_TAKEOVER is CRITICAL when the user held one of these directly at the
# successful login, and SEC_ADMIN_GRANT raises once per direct grant of one to a user -- and their off-hours
# window: 20:00-06:00 America/Chicago plus Saturday/Sunday (ISO weekdays 6 and 7). The SQL literals live in arms
# [26] / [27]; tests/test_security_alert_parity.py locks them to these constants. No builder here reads them (the
# app's own grant-anomaly helpers keep their older 07-19 business day and 5-role ELEVATED_ROLES on purpose).
ALERT_ADMIN_ROLES: tuple[str, ...] = (
    "ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS",
)
OFF_HOURS_START_HOUR = 20          # Central hour >= this is off-hours
OFF_HOURS_END_HOUR = 6             # Central hour < this is off-hours
OFF_HOURS_WEEKEND_ISO: tuple[int, ...] = (6, 7)


#: Pre-LIMIT window totals that capped Security feeds carry beside their rows (UNCAPPED-AGGREGATE: a
#: SQL LIMIT below run()'s row cap never sets res.truncated, so len() of the frame silently saturates at
#: the LIMIT). KPIs, badges and captions read these; tables and the export pack drop them. The DDL/DCL
#: feeds' three totals are listed in DDL_WINDOW_TOTAL_COLUMNS below and joined in here.
WINDOW_TOTAL_COLUMNS: tuple[str, ...] = (
    "TOTAL_USERS_WIN", "TOTAL_ENROLLED_WIN",                       # MFA gaps / single-factor logins
    "TOTAL_BURSTS_WIN", "TOTAL_BROKE_WIN", "TOTAL_HIGH_WIN",       # account-takeover candidates
    "TOTAL_PAIRS_WIN",                                             # new networks
    "TOTAL_CHANGES_WIN", "GRANTED_WIN", "REVOKED_WIN",             # recent grant changes
    "TOTAL_SCOPES_WIN", "TOTAL_GRANTS_WIN",                        # least-privilege scopes / shortlist
    "TOTAL_PATHS_WIN", "TOTAL_PATH_USERS_WIN", "TOTAL_HIGH_RISK_USERS_WIN",
    "TOTAL_SELF_ESCALATORS_WIN", "USER_PATH_RANK",                 # effective access
    "TOTAL_GROUPS_WIN", "HIGH_RISK_GROUPS_WIN", "UNREGISTERED_GROUPS_WIN",   # DDL/DCL changes
)


def _limit_clause(limit: int | None, default: int) -> str:
    """``LIMIT n`` for the export pack's larger reads (it passes its own row cap so run()'s n+1 check
    can flag a truncated sheet); the on-page panels keep each builder's historical default."""
    n = default if limit is None else max(1, min(int(limit), 100_000))
    return f"LIMIT {n}"


def _admin_roles_in(column: str, roles: tuple[str, ...]) -> str:
    """Emit ``<column> IN ('R1', 'R2', ...)`` — byte-identical to the former inlined
    single-line literals (single quotes, ', ' separators, exact role order)."""
    return f"{column} IN (" + ", ".join(f"'{r}'" for r in roles) + ")"


def users_without_mfa(company: str = "ALL", *, limit: int | None = None) -> str:
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
    PL.LAST_PASSWORD_LOGIN,
    COUNT(*) OVER () AS TOTAL_USERS_WIN
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
JOIN password_logins PL ON PL.USER_NAME = U.NAME
WHERE {where}
ORDER BY PL.PASSWORD_LOGINS_30D DESC
{_limit_clause(limit, 200)}
"""


def users_without_mfa_live(company: str = "ALL", *, limit: int | None = None) -> str:
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
    PL.LAST_PASSWORD_LOGIN,
    COUNT(*) OVER () AS TOTAL_USERS_WIN
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
JOIN password_logins PL ON PL.USER_NAME = U.NAME
WHERE {where}
ORDER BY PL.PASSWORD_LOGINS_30D DESC
{_limit_clause(limit, 200)}
"""


def failed_logins(days: int, company: str = "ALL", *, bounds: tuple | None = None,
                  limit: int | None = None) -> str:
    days = bounded_days(days, maximum=30)
    bounds = capped_window(days, bounds, 30)[1]   # the 30d cap holds under calendar bounds too
    _scope = (resolve_effective_window(days, "EVENT_TIMESTAMP", bounds=bounds)[1]
              if bounds is not None
              else f"EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        "IS_SUCCESS = 'NO'",
        companies.user_scope_subquery(company, source="SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY",
                                      distinct_where=_scope),
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
{_limit_clause(limit, 100)}
"""


def single_factor_logins(days: int = 30, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """#16: successful PASSWORD logins that landed with NO second factor — behavioral
    MFA-bypass proof, independent of the HAS_MFA config flag.

    ``users_without_mfa``/``users_without_mfa_live`` are config-anchored (HAS_MFA=FALSE)
    and structurally cannot surface a user who IS enrolled (HAS_MFA=TRUE) yet still
    authenticated single-factor. This reads live LOGIN_HISTORY for PASSWORD-first,
    no-second-factor, successful logins; the USERS join exposes HAS_MFA so the render
    separates the genuine bypass (enrolled) from the not-yet-enrolled. Read-only."""
    days = bounded_days(days, maximum=30)
    bounds = capped_window(days, bounds, 30)[1]   # the 30d cap holds under calendar bounds too
    _scope = (resolve_effective_window(days, "L.EVENT_TIMESTAMP", bounds=bounds)[1]
              if bounds is not None
              else f"L.EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    _scope_sub = (resolve_effective_window(days, "EVENT_TIMESTAMP", bounds=bounds)[1]
                  if bounds is not None
                  else f"EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        "L.FIRST_AUTHENTICATION_FACTOR = 'PASSWORD'",
        "COALESCE(TO_VARCHAR(L.SECOND_AUTHENTICATION_FACTOR), '') = ''",
        "L.IS_SUCCESS = 'YES'",
        companies.user_scope_subquery(company, "L.USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY",
                                      distinct_where=_scope_sub),
    )
    return f"""
WITH single_factor AS (
    SELECT L.USER_NAME,
           COUNT(*) AS SINGLE_FACTOR_LOGINS,
           COUNT(DISTINCT L.CLIENT_IP) AS DISTINCT_IPS,
           MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
           MAX(L.EVENT_TIMESTAMP) AS LAST_SEEN_AT
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
    WHERE {where}
    GROUP BY L.USER_NAME
)
SELECT S.USER_NAME,
       COALESCE(U.HAS_MFA, FALSE) AS HAS_MFA,
       S.SINGLE_FACTOR_LOGINS,
       S.DISTINCT_IPS,
       S.FIRST_SEEN,
       S.LAST_SEEN_AT,
       U.LAST_SUCCESS_LOGIN,
       COUNT(*) OVER () AS TOTAL_USERS_WIN,
       SUM(IFF(COALESCE(U.HAS_MFA, FALSE), 1, 0)) OVER () AS TOTAL_ENROLLED_WIN
FROM single_factor S
JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS U
  ON U.NAME = S.USER_NAME AND U.DELETED_ON IS NULL
ORDER BY HAS_MFA DESC, S.SINGLE_FACTOR_LOGINS DESC
LIMIT 200
"""


# A brute-force "breakthrough" is a SUCCESS immediately preceded by a dense burst of failures;
# this bounds how far back the burst can reach so scattered typos across days never qualify.
_TAKEOVER_BURST_HOURS = 6
# logic.insights.takeover_severity's High: a breakthrough with 10+ failures or 3+ failing IPs. The SQL
# pre-LIMIT High total mirrors it (tests/test_security_c05_fixes.py locks the parity).
TAKEOVER_HIGH_FAILURES = 10
TAKEOVER_HIGH_FAIL_IPS = 3


def login_takeover_candidates(days: int = 7, company: str = "ALL", min_failures: int = 5, *, bounds: tuple | None = None) -> str:
    """Account-takeover candidates: a BURST of failed logins for a user immediately
    FOLLOWED BY a success — the brute-force breakthrough ``failed_logins``' count-only
    lens can't express (it reads IS_SUCCESS='NO' only, never correlating a later success).

    Per user over the window: failures, distinct failing IPs, first/last failure, and the
    earliest SUCCESS that followed a dense burst (>= min_failures failures within
    ``_TAKEOVER_BURST_HOURS``h before it). SUCCEEDED_AFTER (a success followed the BURST) is
    the dangerous signal — a terminal lock-out burst with no later success is a locked-out
    user, not a breach, and a routine login after an isolated typo is not a breakthrough.
    Anchoring the success to a bounded pre-success burst (not merely the earliest failure
    anywhere in the window) is what keeps those benign cases off the dangerous list
    (bug-hunt 2026-08-30). Needs event-grain ordering, so it reads live LOGIN_HISTORY
    (day-grain FACT_SECURITY_LOGIN_DAILY can't express intra-window ordering). Scored by
    ``logic.insights.takeover_severity``. Read-only review — confirm before acting."""
    days = bounded_days(days, maximum=30)
    bounds = capped_window(days, bounds, 30)[1]   # the 30d cap holds under calendar bounds too
    min_failures = max(2, min(int(min_failures), 100))
    _scope = (resolve_effective_window(days, "EVENT_TIMESTAMP", bounds=bounds)[1]
              if bounds is not None
              else f"EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    ev_where = and_where(
        _scope,
        companies.user_scope_subquery(company, source="SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY",
                                      distinct_where=_scope),
    )
    return f"""
WITH ev AS (
    SELECT USER_NAME, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP, ERROR_MESSAGE
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
    WHERE {ev_where}
),
fails AS (
    SELECT USER_NAME,
           COUNT(*) AS FAILURES,
           COUNT(DISTINCT CLIENT_IP) AS FAIL_IPS,
           MIN(EVENT_TIMESTAMP) AS FIRST_FAILURE,
           MAX(EVENT_TIMESTAMP) AS LAST_FAILURE,
           MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 200), EVENT_TIMESTAMP) AS LAST_ERROR
    FROM ev
    WHERE IS_SUCCESS = 'NO'
    GROUP BY USER_NAME
    HAVING COUNT(*) >= {min_failures}
),
breakthroughs AS (
    -- a success is a breakthrough only if >= {min_failures} failures preceded it within
    -- {_TAKEOVER_BURST_HOURS}h (a real concentrated attempt), so a login after a stray typo
    -- and a terminal lock-out burst (no success after) are both correctly excluded.
    SELECT s.USER_NAME, s.EVENT_TIMESTAMP AS SUCCESS_TS,
           MIN(fl.EVENT_TIMESTAMP) AS BURST_FIRST_FAILURE
    FROM ev s
    JOIN fails cand ON cand.USER_NAME = s.USER_NAME
    JOIN ev fl
      ON fl.USER_NAME = s.USER_NAME
     AND fl.IS_SUCCESS = 'NO'
     AND fl.EVENT_TIMESTAMP < s.EVENT_TIMESTAMP
     AND fl.EVENT_TIMESTAMP >= DATEADD('hour', -{_TAKEOVER_BURST_HOURS}, s.EVENT_TIMESTAMP)
    WHERE s.IS_SUCCESS = 'YES'
    GROUP BY s.USER_NAME, s.EVENT_TIMESTAMP
    HAVING COUNT(*) >= {min_failures}
),
first_break AS (
    SELECT USER_NAME,
           MIN(SUCCESS_TS) AS FIRST_SUCCESS_AFTER,
           MIN_BY(BURST_FIRST_FAILURE, SUCCESS_TS) AS BREAKTHROUGH_FROM
    FROM breakthroughs
    GROUP BY USER_NAME
),
succ_total AS (
    SELECT USER_NAME, COUNT(*) AS SUCCESSES
    FROM ev
    WHERE IS_SUCCESS = 'YES'
    GROUP BY USER_NAME
)
SELECT f.USER_NAME,
       f.FAILURES,
       COALESCE(t.SUCCESSES, 0) AS SUCCESSES,
       f.FAIL_IPS,
       f.FIRST_FAILURE,
       f.LAST_FAILURE,
       fb.FIRST_SUCCESS_AFTER,
       IFF(fb.FIRST_SUCCESS_AFTER IS NOT NULL, TRUE, FALSE) AS SUCCEEDED_AFTER,
       -- _MIN suffix (not the MINS_TO_ prefix) so the shared table machinery auto-humanizes this
       -- duration to Hr/Min instead of a raw minutes count (the prettifier drops _MIN -> "Breakthrough").
       DATEDIFF('minute', fb.BREAKTHROUGH_FROM, fb.FIRST_SUCCESS_AFTER) AS BREAKTHROUGH_MIN,
       f.LAST_ERROR,
       -- pre-LIMIT totals: in a spray over 100+ users the burst count is the finding, not the LIMIT
       COUNT(*) OVER () AS TOTAL_BURSTS_WIN,
       SUM(IFF(fb.FIRST_SUCCESS_AFTER IS NOT NULL, 1, 0)) OVER () AS TOTAL_BROKE_WIN,
       SUM(IFF(fb.FIRST_SUCCESS_AFTER IS NOT NULL
               AND (f.FAILURES >= {TAKEOVER_HIGH_FAILURES} OR f.FAIL_IPS >= {TAKEOVER_HIGH_FAIL_IPS}),
               1, 0)) OVER () AS TOTAL_HIGH_WIN
FROM fails f
LEFT JOIN first_break fb ON fb.USER_NAME = f.USER_NAME
LEFT JOIN succ_total t ON t.USER_NAME = f.USER_NAME
ORDER BY SUCCEEDED_AFTER DESC, f.FAILURES DESC
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


def recent_role_grants(days: int, *, bounds: tuple | None = None, limit: int | None = None) -> str:
    """Recently granted roles to users (account-wide governance view)."""
    days = bounded_days(days, maximum=90)
    bounds = capped_window(days, bounds, 90)[1]   # the 90d cap holds under calendar bounds too
    _scope = (resolve_effective_window(days, "CREATED_ON", bounds=bounds)[1]
              if bounds is not None
              else f"CREATED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    return f"""
SELECT
    GRANTEE_NAME AS USER_NAME,
    ROLE AS GRANTED_ROLE,
    GRANTED_BY,
    CREATED_ON AS GRANTED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE {_scope}
  AND DELETED_ON IS NULL
ORDER BY CREATED_ON DESC
{_limit_clause(limit, 200)}
"""


def admin_role_holders(company: str = "ALL") -> str:
    """Current holders of the admin roles (owner 2026-07-13: the only roles
    with access are SNOW_ACCOUNTADMINS / SNOW_SYSADMINS); short, known list."""
    where = and_where(
        "DELETED_ON IS NULL",
        _admin_roles_in("ROLE", ADMIN_HOLDER_ROLES),
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


def user_auth_inventory(company: str = "ALL") -> str:
    """rank 9: auth-readiness inventory for the single-factor password deprecation — every
    enabled user who holds a password, is LEGACY_SERVICE, or DIRECTLY holds an ELEVATED_ROLES
    role, with TYPE / HAS_RSA_PUBLIC_KEY / HAS_PASSWORD / HAS_MFA and 30-day password-login
    evidence (LEFT-joined: a no-recent-login admin must still surface, unlike users_without_mfa).
    Pre-LIMIT window totals (TOTAL_CANDIDATES, ADMIN_PW_NO_MFA_TOTAL) keep headline counts honest
    under the cap. The page runs this probe=True — TYPE / HAS_RSA_PUBLIC_KEY are first reads here."""
    where = and_where(
        "U.DELETED_ON IS NULL",
        "COALESCE(U.DISABLED, FALSE) = FALSE",
        "(COALESCE(U.HAS_PASSWORD, FALSE) = TRUE"
        " OR UPPER(COALESCE(U.TYPE, '')) = 'LEGACY_SERVICE'"
        " OR A.GRANTEE_NAME IS NOT NULL)",
        companies.user_clause(company, "U.NAME"),
    )
    return f"""
WITH password_logins AS (
    SELECT USER_NAME,
           SUM(PASSWORD_LOGINS) AS PASSWORD_LOGINS_30D,
           MAX(IFF(PASSWORD_LOGINS > 0, DAY, NULL)) AS LAST_PASSWORD_LOGIN
    FROM {core_object('FACT_LOGIN_DAILY')}
    WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())
    GROUP BY USER_NAME
), admin_grants AS (
    SELECT GRANTEE_NAME,
           LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ADMIN_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND {_admin_roles_in("ROLE", ELEVATED_ROLES)}
    GROUP BY GRANTEE_NAME
), inv AS (
    SELECT
        U.NAME AS USER_NAME,
        U.LOGIN_NAME,
        U.EMAIL,
        NULLIF(UPPER(TRIM(U.TYPE)), '') AS USER_TYPE,
        COALESCE(U.HAS_PASSWORD, FALSE) AS HAS_PASSWORD,
        COALESCE(U.HAS_MFA, FALSE) AS HAS_MFA,
        COALESCE(U.HAS_RSA_PUBLIC_KEY, FALSE) AS HAS_RSA_PUBLIC_KEY,
        U.LAST_SUCCESS_LOGIN,
        PL.PASSWORD_LOGINS_30D,
        PL.LAST_PASSWORD_LOGIN,
        A.GRANTEE_NAME IS NOT NULL AS IS_ADMIN,
        A.ADMIN_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
    LEFT JOIN password_logins PL ON PL.USER_NAME = U.NAME
    LEFT JOIN admin_grants A ON A.GRANTEE_NAME = U.NAME
    WHERE {where}
)
SELECT inv.*,
       COUNT(*) OVER () AS TOTAL_CANDIDATES,
       SUM(IFF(IS_ADMIN AND HAS_PASSWORD AND NOT HAS_MFA, 1, 0)) OVER () AS ADMIN_PW_NO_MFA_TOTAL
FROM inv
-- risk first before the cap: LEGACY_SERVICE, then password-without-MFA, then admins, then usage
ORDER BY IFF(USER_TYPE = 'LEGACY_SERVICE', 0, 1), IFF(HAS_PASSWORD AND NOT HAS_MFA, 0, 1),
         IS_ADMIN DESC, COALESCE(PASSWORD_LOGINS_30D, 0) DESC, USER_NAME
LIMIT 1000
"""


def service_users() -> str:
    """rank 9: names of non-human users (USERS.TYPE in identity_auth.SERVICE_TYPES) so the dormant
    and reawakening scans can band service accounts separately. Account-wide name membership only;
    the page runs it probe=True and falls back to one unbanded table if TYPE is unreadable."""
    where = and_where("U.DELETED_ON IS NULL", in_list("U.TYPE", tuple(sorted(SERVICE_TYPES))))
    return f"""
SELECT U.NAME AS USER_NAME, UPPER(U.TYPE) AS USER_TYPE
FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
WHERE {where}
ORDER BY U.NAME
"""


def admin_network_policy_coverage(company: str = "ALL") -> str:
    """rank 9: which directly-granted admin users (ELEVATED_ROLES) carry a USER-level network
    policy. The 2026-09-29 S1b probe read USER (1), ACCOUNT (1) and INTEGRATION (3) network-policy
    rows here, but another account may not expose USER rows (docs: up to 2h latency), so the page
    still runs this probe=True and treats USER_POLICY_REFS = 0 as needs_setup, never as 'no admin is
    covered'. Next-Fifty #43: np_totals also counts ACCOUNT-domain rows (ACCOUNT_POLICY_REFS, same
    scan) so the caption can say an account-level policy is set -- a fact line, not a coverage
    verdict; account-level (CIS 3.1) and service-account (CIS 3.2) coverage stay Trust Center CIS
    scanners."""
    where = and_where(
        "DELETED_ON IS NULL",
        _admin_roles_in("ROLE", ELEVATED_ROLES),
        companies.user_clause(company, "GRANTEE_NAME"),
    )
    return f"""
WITH admins AS (
    SELECT GRANTEE_NAME AS USER_NAME,
           LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ADMIN_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE {where}
    GROUP BY GRANTEE_NAME
), np AS (
    SELECT UPPER(REF_ENTITY_DOMAIN) AS REF_DOMAIN, REF_ENTITY_NAME, POLICY_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
    WHERE POLICY_KIND = 'NETWORK_POLICY'
), np_totals AS (
    SELECT COUNT(*) AS NETWORK_POLICY_REFS,
           COUNT_IF(REF_DOMAIN = 'USER') AS USER_POLICY_REFS,
           COUNT_IF(REF_DOMAIN = 'ACCOUNT') AS ACCOUNT_POLICY_REFS
    FROM np
)
SELECT A.USER_NAME,
       A.ADMIN_ROLES,
       MAX(NP.POLICY_NAME) AS USER_NETWORK_POLICY,
       T.NETWORK_POLICY_REFS,
       T.USER_POLICY_REFS,
       T.ACCOUNT_POLICY_REFS
FROM admins A
CROSS JOIN np_totals T
LEFT JOIN np NP ON NP.REF_DOMAIN = 'USER' AND NP.REF_ENTITY_NAME = A.USER_NAME
GROUP BY A.USER_NAME, A.ADMIN_ROLES, T.NETWORK_POLICY_REFS, T.USER_POLICY_REFS, T.ACCOUNT_POLICY_REFS
ORDER BY USER_NETWORK_POLICY NULLS FIRST, A.USER_NAME
"""


# Next-Fifty #43: a database "family" is its name up to the last underscore (X_PRD, X_DEV -> X); the environment is
# the part after it. Name-derived on purpose: no environment or tenant name is hard-coded. REGEXP_LIKE anchors the
# whole name, so a name with no inner underscore is its own family with no environment. One source for both #43
# builders; the patterns live in app.logic.policy_coverage, whose db_family is the Python mirror used on the
# SHOW DATABASES names (the unmasked databases of a masked database's family).
def _db_family(col: str) -> str:
    return (f"IFF(REGEXP_LIKE({col}, '{FAMILY_NAME_PATTERN}'), REGEXP_REPLACE({col}, '{FAMILY_SUFFIX_PATTERN}', ''), "
            f"{col})")


def _db_env_suffix(col: str) -> str:
    return f"IFF(REGEXP_LIKE({col}, '{FAMILY_NAME_PATTERN}'), REGEXP_SUBSTR({col}, '[^_]+$'), NULL)"


# Fully qualified keys for every #43 distinct count (never S1a's unqualified REF_ENTITY_NAME / POLICY_NAME: 325 vs
# 1,255 masked tables on the 2026-09-29 probe).
_PR_ENTITY_FQN = "COALESCE(REF_DATABASE_NAME, '') || '.' || COALESCE(REF_SCHEMA_NAME, '') || '.' || REF_ENTITY_NAME"
_PR_POLICY_FQN = "COALESCE(POLICY_DB, '') || '.' || COALESCE(POLICY_SCHEMA, '') || '.' || POLICY_NAME"
_PR_KIND = "REPLACE(UPPER(TRIM(POLICY_KIND)), ' ', '_')"
DATA_POLICY_KINDS: tuple[str, ...] = ("MASKING_POLICY", "ROW_ACCESS_POLICY", "PROJECTION_POLICY", "AGGREGATION_POLICY")


def data_policy_coverage() -> str:
    """Next-Fifty #43 Phase 1: masking / row-access / projection / aggregation policy coverage from the
    policy-reference view (up to about 2h behind). The 2026-09-29 probes proved every column this reads: S1b read
    seven (POLICY_KIND, REF_ENTITY_DOMAIN, REF_DATABASE_NAME, REF_SCHEMA_NAME, REF_ENTITY_NAME, REF_COLUMN_NAME,
    POLICY_STATUS), S1a read POLICY_NAME, and S0b's column list shows POLICY_DB and POLICY_SCHEMA.

    One row per database that has a column-level masking reference, with the account totals repeated on every
    row. ``tot`` is LEFT JOINed to ``by_db`` ON 1 = 1, so a zero-masking account still returns exactly one row
    (DATABASE_NAME NULL) carrying the totals. Totals come from ``tot`` and are never summed from the per-database
    rows: distinct policies are not additive across databases. Account-wide (no company scope). REF_ENTITY_DOMAIN
    'TAG' is tag-based masking; every other masking domain is column-level. Kinds are normalised, and any kind
    outside the four data-policy kinds and NETWORK_POLICY is listed in OTHER_POLICY_KINDS, so a spelling change
    can never read as "no row-access policy". PKIND / PSTATUS avoid the alias-shadow rule and sqlglot's KIND
    keyword. No row cap: one row per masked database (run()'s cap still applies)."""
    kinds = ", ".join(f"'{k}'" for k in (*DATA_POLICY_KINDS, "NETWORK_POLICY"))
    fam, env = _db_family("REF_DATABASE_NAME"), _db_env_suffix("REF_DATABASE_NAME")
    return f"""
WITH pr AS (
    SELECT {_PR_KIND} AS PKIND,
           UPPER(REF_ENTITY_DOMAIN) AS REF_DOMAIN,
           REF_DATABASE_NAME,
           {_PR_ENTITY_FQN} AS ENTITY_FQN,
           IFF(REF_COLUMN_NAME IS NULL, NULL, {_PR_ENTITY_FQN} || '.' || REF_COLUMN_NAME) AS COLUMN_FQN,
           {_PR_POLICY_FQN} AS POLICY_FQN,
           UPPER(POLICY_STATUS) AS PSTATUS
    FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
), tot AS (
    SELECT
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG', COLUMN_FQN, NULL)) AS TOTAL_MASKED_COLUMNS,
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG', ENTITY_FQN, NULL)) AS TOTAL_MASKED_OBJECTS,
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG', REF_DATABASE_NAME, NULL)) AS TOTAL_MASKED_DATABASES,
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY', POLICY_FQN, NULL)) AS TOTAL_MASKING_POLICIES,
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN = 'TAG', ENTITY_FQN, NULL)) AS MASKING_TAGS,
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN = 'TAG', REF_DATABASE_NAME, NULL)) AS MASKING_TAG_DATABASES,
        COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN = 'TAG', POLICY_FQN, NULL)) AS TAG_MASKING_POLICIES,
        COUNT_IF(PKIND = 'MASKING_POLICY' AND PSTATUS <> 'ACTIVE') AS MASKING_REFS_NOT_ACTIVE,
        COUNT(DISTINCT IFF(PKIND = 'ROW_ACCESS_POLICY', ENTITY_FQN, NULL)) AS ROW_ACCESS_OBJECTS,
        COUNT(DISTINCT IFF(PKIND = 'ROW_ACCESS_POLICY', POLICY_FQN, NULL)) AS ROW_ACCESS_POLICIES,
        COUNT(DISTINCT IFF(PKIND = 'PROJECTION_POLICY', ENTITY_FQN, NULL)) AS PROJECTION_OBJECTS,
        COUNT(DISTINCT IFF(PKIND = 'PROJECTION_POLICY', POLICY_FQN, NULL)) AS PROJECTION_POLICIES,
        COUNT(DISTINCT IFF(PKIND = 'AGGREGATION_POLICY', ENTITY_FQN, NULL)) AS AGGREGATION_OBJECTS,
        COUNT(DISTINCT IFF(PKIND = 'AGGREGATION_POLICY', POLICY_FQN, NULL)) AS AGGREGATION_POLICIES,
        LISTAGG(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND PSTATUS <> 'ACTIVE', PSTATUS, NULL), ', ') AS NOT_ACTIVE_STATUSES,
        LISTAGG(DISTINCT IFF(PKIND IN ({kinds}), NULL, PKIND), ', ') AS OTHER_POLICY_KINDS
    FROM pr
), by_db AS (
    SELECT REF_DATABASE_NAME AS DATABASE_NAME, {fam} AS DATABASE_FAMILY, {env} AS NAME_SUFFIX,
           COUNT(DISTINCT ENTITY_FQN) AS MASKED_OBJECTS,
           COUNT(DISTINCT COLUMN_FQN) AS MASKED_COLUMNS,
           COUNT(DISTINCT POLICY_FQN) AS MASKING_POLICIES,
           COUNT_IF(PSTATUS <> 'ACTIVE') AS REFS_NOT_ACTIVE
    FROM pr
    WHERE PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG' AND REF_DATABASE_NAME IS NOT NULL
    GROUP BY 1, 2, 3
)
SELECT B.DATABASE_NAME,
       IFF(COUNT(B.DATABASE_NAME) OVER (PARTITION BY B.DATABASE_FAMILY) >= 2, B.NAME_SUFFIX, NULL) AS ENVIRONMENT,
       B.MASKED_OBJECTS, B.MASKED_COLUMNS, B.MASKING_POLICIES, B.REFS_NOT_ACTIVE,
       T.TOTAL_MASKED_COLUMNS, T.TOTAL_MASKED_OBJECTS, T.TOTAL_MASKED_DATABASES, T.TOTAL_MASKING_POLICIES,
       T.MASKING_TAGS, T.MASKING_TAG_DATABASES, T.TAG_MASKING_POLICIES, T.MASKING_REFS_NOT_ACTIVE,
       T.ROW_ACCESS_OBJECTS, T.ROW_ACCESS_POLICIES, T.PROJECTION_OBJECTS, T.PROJECTION_POLICIES,
       T.AGGREGATION_OBJECTS, T.AGGREGATION_POLICIES, T.NOT_ACTIVE_STATUSES, T.OTHER_POLICY_KINDS
FROM tot T
LEFT JOIN by_db B ON 1 = 1
ORDER BY B.MASKED_COLUMNS DESC NULLS LAST, B.DATABASE_NAME
"""


def masking_environment_parity() -> str:
    """Next-Fifty #43 Phase 1: the environment grouping -- information only, not a gap list.

    Grain: (DATABASE_FAMILY, SCHEMA_NAME, OBJECT_NAME), for every masked schema.object in a family that has 2+
    databases with column masking. A family's databases HERE are only those with at least one column-level
    masking reference (see _db_family), hence MASKED_FAMILY_DATABASES: a database with no column-level masking
    reference (tag-only masking included) is in neither #43 table; the page lists those from SHOW DATABASES
    (policy_coverage.unmasked_family_databases).
    COLUMN_SET is the sorted distinct masked-column list in one database. SAME means masked in every masked family
    database on one column set; otherwise DIFFERS. TOTAL_NAMES and DIFFERING_NAMES are window totals taken before
    the LIMIT (the uncapped-aggregate rule)."""
    fam = _db_family("REF_DATABASE_NAME")
    return f"""
WITH m AS (
    SELECT REF_DATABASE_NAME AS DB, {fam} AS DATABASE_FAMILY, REF_SCHEMA_NAME AS SCH, REF_ENTITY_NAME AS OBJ,
           REF_COLUMN_NAME AS COL
    FROM SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES
    WHERE {_PR_KIND} = 'MASKING_POLICY' AND UPPER(REF_ENTITY_DOMAIN) <> 'TAG'
      AND REF_DATABASE_NAME IS NOT NULL AND REF_COLUMN_NAME IS NOT NULL
), per_obj AS (
    SELECT DATABASE_FAMILY, DB, SCH, OBJ, COUNT(DISTINCT COL) AS MASKED_COLUMNS,
           LISTAGG(DISTINCT COL, ',') WITHIN GROUP (ORDER BY COL) AS COLUMN_SET
    FROM m GROUP BY DATABASE_FAMILY, DB, SCH, OBJ
), fam_dbs AS (
    SELECT DATABASE_FAMILY, DB, COUNT(*) OVER (PARTITION BY DATABASE_FAMILY) AS MASKED_FAMILY_DATABASES
    FROM (SELECT DISTINCT DATABASE_FAMILY, DB FROM per_obj)
), names AS (
    SELECT DISTINCT P.DATABASE_FAMILY, P.SCH, P.OBJ
    FROM per_obj P JOIN fam_dbs F ON F.DATABASE_FAMILY = P.DATABASE_FAMILY AND F.DB = P.DB
    WHERE F.MASKED_FAMILY_DATABASES >= 2
), grid AS (
    SELECT N.DATABASE_FAMILY, N.SCH, N.OBJ, F.DB, F.MASKED_FAMILY_DATABASES, P.MASKED_COLUMNS, P.COLUMN_SET
    FROM names N
    JOIN fam_dbs F ON F.DATABASE_FAMILY = N.DATABASE_FAMILY
    LEFT JOIN per_obj P ON P.DATABASE_FAMILY = N.DATABASE_FAMILY AND P.DB = F.DB AND P.SCH = N.SCH AND P.OBJ = N.OBJ
), named AS (
    SELECT DATABASE_FAMILY, SCH AS SCHEMA_NAME, OBJ AS OBJECT_NAME,
           COUNT(COLUMN_SET) AS DATABASES_MASKED, MAX(MASKED_FAMILY_DATABASES) AS MASKED_FAMILY_DATABASES,
           COUNT(DISTINCT COLUMN_SET) AS COLUMN_SETS,
           IFF(COUNT(COLUMN_SET) = MAX(MASKED_FAMILY_DATABASES) AND COUNT(DISTINCT COLUMN_SET) = 1, 'SAME', 'DIFFERS') AS PARITY,
           LISTAGG(IFF(COLUMN_SET IS NULL, NULL, DB || ' (' || MASKED_COLUMNS || ')'), ', ') WITHIN GROUP (ORDER BY DB) AS MASKED_IN,
           NULLIF(LISTAGG(IFF(COLUMN_SET IS NULL, DB, NULL), ', ') WITHIN GROUP (ORDER BY DB), '') AS NO_MASKING_REF_IN
    FROM grid GROUP BY DATABASE_FAMILY, SCH, OBJ
)
SELECT DATABASE_FAMILY, SCHEMA_NAME, OBJECT_NAME, PARITY, DATABASES_MASKED, MASKED_FAMILY_DATABASES, COLUMN_SETS,
       MASKED_IN, NO_MASKING_REF_IN,
       COUNT(*) OVER () AS TOTAL_NAMES,
       SUM(IFF(PARITY = 'DIFFERS', 1, 0)) OVER () AS DIFFERING_NAMES
FROM named
ORDER BY IFF(PARITY = 'DIFFERS', 0, 1), DATABASE_FAMILY, SCHEMA_NAME, OBJECT_NAME
LIMIT 1000
"""


def _recent_ddl_ctes(days: int, company: str, database: str, schema_contains: str,
                     bounds: tuple | None) -> str:
    """The ``grouped`` + company-``scoped`` CTEs shared by ``recent_ddl_changes`` and its uncapped
    chart rollup ``recent_ddl_changes_rollup``, so the two can never disagree on scope or window.

    The cap is the standard 90-day live QUERY_HISTORY clamp (it was 30), the same window
    ``recent_ddl_changes_fact`` serves, so a stale-extract fallback no longer silently drops days
    31-90 under the same label. Calendar bounds are capped to it too (they used to bypass it)."""
    days = bounded_days(days)
    bounds = capped_window(days, bounds, 90)[1]
    # C11: scope change evidence by ACTOR *or* OBJECT, not actor AND object. GRANT /
    # REVOKE and other account-level DDL carry a NULL DATABASE_NAME (dropped by the
    # database lens), and a cross-company change (a Trexis user's DDL in an ALFA
    # database) failed both lenses under the old AND — visible only under ALL. OR-ing
    # follows the actor's company for context-less DDL and shows cross-company changes
    # under both lenses (union semantics; the disclosure caption states the rule).
    _uc = companies.user_clause(company, "g.USER_NAME")
    _dc = companies.database_company_scope(company, "g.DATABASE_NAME")
    _actor_or_object = f"({_uc} OR {_dc})" if _uc and _dc else (_uc or _dc)
    where = and_where(
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
        scope_window_where("START_TIME", days, bounds=bounds),
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
               WHEN q.QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')
                    AND MAX(IFF(q.QUERY_TEXT ILIKE '%OR REPLACE%', 1, 0)) = 1 THEN 55
               ELSE 30 END
          + IFF({_admin_roles_in('q.ROLE_NAME', BREAK_GLASS_ROLES)}, 10, 0)
          + IFF(COALESCE(q.DATABASE_NAME, '') ILIKE '%PROD%', 10, 0)
        ) AS RISK_SCORE
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {where}
    GROUP BY 1, 2, 3, 4, 5, 6
), scoped AS (
    SELECT g.*
    FROM grouped g
    WHERE {scope_where}
)"""


# The DDL/DCL detail feeds keep their newest-300 table cap, but every headline reads these pre-LIMIT
# totals, computed over the registry-DEDUPED rows (a window beside the QUALIFY would count the registry
# join's fan-out). UNCAPPED-AGGREGATE: never a count of the 300 newest groups. The page drops them
# before display.
DDL_WINDOW_TOTAL_COLUMNS: tuple[str, ...] = (
    "TOTAL_GROUPS_WIN", "HIGH_RISK_GROUPS_WIN", "UNREGISTERED_GROUPS_WIN",
)
_DDL_TOTALS_SELECT = """SELECT f.*,
       COUNT(*) OVER () AS TOTAL_GROUPS_WIN,
       SUM(IFF(UPPER(f.RISK_LEVEL) IN ('CRITICAL', 'HIGH'), 1, 0)) OVER () AS HIGH_RISK_GROUPS_WIN,
       SUM(IFF(f.CHANGE_REGISTRATION = 'UNREGISTERED', 1, 0)) OVER () AS UNREGISTERED_GROUPS_WIN
FROM final f
ORDER BY f.LAST_CHANGE DESC
LIMIT 300"""
# The two DDL charts (statements/day by change kind, statements by user) aggregate the WHOLE scoped
# window: one row per (DAY, QUERY_TYPE) and one per USER_NAME, never the newest 300 groups (the oldest
# days used to vanish from the per-day chart once a window held more than 300 groups).
_DDL_ROLLUP_SELECT = """SELECT IFF(GROUPING(s.USER_NAME) = 0, 'USER', 'DAY_TYPE') AS GRAIN,
       s.DAY, s.QUERY_TYPE, s.USER_NAME,
       SUM(s.STATEMENTS) AS STATEMENTS
FROM scoped s
GROUP BY GROUPING SETS ((s.DAY, s.QUERY_TYPE), (s.USER_NAME))"""


def recent_ddl_changes(days: int, company: str = "ALL", database: str = "", schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """Who changed what: DDL/DCL statements grouped by user and object type (the newest 300 groups,
    with the pre-LIMIT window totals in DDL_WINDOW_TOTAL_COLUMNS)."""
    ctes = _recent_ddl_ctes(days, company, database, schema_contains, bounds)
    return f"""{ctes}, final AS (
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
)
{_DDL_TOTALS_SELECT}
"""


def recent_ddl_changes_rollup(days: int, company: str = "ALL", database: str = "", schema_contains: str = "",
                              *, bounds: tuple | None = None) -> str:
    """Uncapped chart rollup for the live DDL/DCL panel (same scope and window as
    ``recent_ddl_changes``): statements per (DAY, QUERY_TYPE) and per USER_NAME."""
    ctes = _recent_ddl_ctes(days, company, database, schema_contains, bounds)
    return f"""{ctes}
{_DDL_ROLLUP_SELECT}
"""


def failed_login_reasons(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Failed logins grouped by reason — network-policy blocks surface
    separately from bad credentials."""
    # r6-bug13: match failed_logins()' 30d cap. This breakdown is presented as the
    # decomposition of the failed-logins table directly above it; a wider (90d) window
    # here made the two panels disagree under one "90 days" scope and never reconcile.
    days = bounded_days(days, maximum=30)
    bounds = capped_window(days, bounds, 30)[1]   # the 30d cap holds under calendar bounds too
    _scope = (resolve_effective_window(days, "EVENT_TIMESTAMP", bounds=bounds)[1]
              if bounds is not None
              else f"EVENT_TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        "IS_SUCCESS = 'NO'",
        companies.user_scope_subquery(company, "USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY",
                                      distinct_where=_scope),
    )
    # REASON is the SAME coarse ERROR_CATEGORY bucketing the FACT loader (V075) writes,
    # so this live/fallback path renders the identical rows as failed_login_reasons_fact
    # instead of a per-raw-message breakdown that looks like a different panel on a window
    # flip (bug-hunt 2026-08-30). Failures only (IS_SUCCESS='NO'), so no SUCCESS bucket.
    return f"""
SELECT
    CASE
      WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%network%' THEN 'NETWORK POLICY'
      WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%disabled%' THEN 'DISABLED USER'
      WHEN COALESCE(ERROR_MESSAGE, '') ILIKE '%mfa%' THEN 'MFA'
      WHEN COALESCE(ERROR_MESSAGE, '') ILIKE ANY ('%password%', '%credential%', '%authentication%')
        THEN 'CREDENTIAL'
      ELSE 'OTHER'
    END AS REASON,
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


def admin_role_activity(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Daily statement volume under break-glass admin roles. Routine work
    belongs on SNOW_SYSADMINS; this line should hug zero."""
    days = bounded_days(days)
    # the 90d live QUERY_HISTORY cap holds under calendar bounds too ('Current year' used to scan ~273d)
    bounds = capped_window(days, bounds, 90)[1]
    _scope = (resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
              if bounds is not None
              else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        _admin_roles_in("ROLE_NAME", BREAK_GLASS_ROLES),
        companies.user_scope_subquery(company, "USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
                                      distinct_where=_scope),
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
    -- r31: NULL (not 0) when FACT_LOGIN_DAILY has NO trailing-30d coverage, so a stale/empty
    -- login fact reads as UNKNOWN in the governance-drift score rather than a clean "no MFA gap"
    -- (C8 no-data-vs-clean). Otherwise the per-user EXISTS matches nothing and the gap silently
    -- scores 0. The dedicated MFA panel already gates on fact coverage the same way.
    IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY
         WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())) = 0, NULL,
        (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
          WHERE U.DELETED_ON IS NULL AND COALESCE(U.DISABLED, FALSE) = FALSE
            AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
            -- ONE definition of "MFA gap" app-wide (review #10): the same
            -- password-login evidence the Access panel lists, not the old
            -- created-7-days-ago proxy that disagreed with it on one page.
            AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                        WHERE L.USER_NAME = U.NAME
                          AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                          AND L.PASSWORD_LOGINS > 0))) AS MFA_GAP_USERS,
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


#: #20: the governance tag keys whose object-level coverage is scored. Whitelisted
#: (never raw UI text) and injected through sql_literal.
_GOV_TAG_KEYS: tuple[str, ...] = ("COST_OWNER", "SENSITIVITY", "SERVICE_TIER", "APP_OWNER")


def object_tag_probe() -> str:
    """#20: cheap existence probe for TAG_REFERENCES. The view is UNVERIFIED on this
    account (no prior reader in the repo), so callers run this with probe=True and
    degrade to an honest 'not available' branch when it errors, instead of a red row."""
    # OBJECT_DELETED rides the probe: the coverage reads filter on it, so a view without the column
    # fails here (an 'unavailable' state with the error) instead of mid-panel.
    return "SELECT TAG_NAME, DOMAIN, OBJECT_DELETED FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES LIMIT 1"


def object_tag_coverage(company: str = "ALL",
                        tags: tuple[str, ...] = _GOV_TAG_KEYS) -> str:
    """#20: per-governance-tag coverage over the base-table inventory.

    For each governance tag key, what fraction of in-scope base tables carry it.
    ACCOUNT_USAGE.TABLES (the verified inventory) is the DENOMINATOR; TAG_REFERENCES
    (the tag-assignment side) is LEFT-joined so an untagged table still counts toward
    TOTAL. Only LIVE tag references count (``OBJECT_DELETED IS NULL``): TAG_REFERENCES keeps a
    dropped table's row, and a CREATE OR REPLACE (or DROP + CREATE) without COPY TAGS leaves an
    untagged table with the SAME name, which the old name-only join counted as tagged through its
    predecessor's row (inflated score, missing from the worklist, a false 'clean').
    Scoped to DOMAIN='TABLE' — this account has no ACCOUNT_USAGE.WAREHOUSES/DATABASES
    views (see ``show_warehouses_sql``), so warehouse/database tag coverage has no
    honest denominator and is deliberately out of v1. Company scope via the
    COMPANY_FOR_DATABASE UDF (``database_company_scope``) on TABLE_CATALOG
    (same mechanism as insights_sql.storage_waste).
    Tag keys are whitelisted through sql_literal — never raw UI text."""
    # dict.fromkeys de-dups while preserving order: a duplicate or case-variant tag
    # key would otherwise fan out the keys CTE and double every TOTAL/TAGGED count.
    keys = tuple(dict.fromkeys(str(t).strip().upper() for t in tags if str(t).strip())) \
        or _GOV_TAG_KEYS
    in_list = ", ".join(sql_literal(k) for k in keys)
    key_rows = " UNION ALL ".join(f"SELECT {sql_literal(k)} AS TAG_NAME" for k in keys)
    where = and_where(
        "t.DELETED IS NULL",
        "t.TABLE_TYPE = 'BASE TABLE'",
        companies.database_company_scope(company, "t.TABLE_CATALOG"),
    )
    return f"""
WITH tbls AS (
    -- DISTINCT so TOTAL is provably the count of distinct live base-table FQNs,
    -- independent of any duplicate rows the inventory view might carry.
    SELECT DISTINCT t.TABLE_CATALOG, t.TABLE_SCHEMA, t.TABLE_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES t
    WHERE {where}
),
tagged AS (
    SELECT DISTINCT
        r.OBJECT_DATABASE, r.OBJECT_SCHEMA, r.OBJECT_NAME, UPPER(r.TAG_NAME) AS TAG_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES r
    WHERE r.DOMAIN = 'TABLE'
      AND r.OBJECT_DELETED IS NULL
      AND UPPER(r.TAG_NAME) IN ({in_list})
),
keys AS ({key_rows})
SELECT
    k.TAG_NAME,
    COUNT(*)             AS TOTAL,
    COUNT(g.OBJECT_NAME) AS TAGGED,
    ROUND(100.0 * COUNT(g.OBJECT_NAME) / NULLIF(COUNT(*), 0), 1) AS COVERAGE_PCT
FROM keys k
CROSS JOIN tbls t
LEFT JOIN tagged g
    ON g.OBJECT_DATABASE = t.TABLE_CATALOG
   AND g.OBJECT_SCHEMA   = t.TABLE_SCHEMA
   AND g.OBJECT_NAME     = t.TABLE_NAME
   AND g.TAG_NAME        = k.TAG_NAME
GROUP BY k.TAG_NAME
ORDER BY COVERAGE_PCT ASC, k.TAG_NAME
"""


def untagged_objects(company: str = "ALL", tag_name: str = "COST_OWNER",
                     limit: int = 200) -> str:
    """#20: the in-scope base tables MISSING a given governance tag — the worklist
    behind a coverage gap. LEFT JOIN TAG_REFERENCES for that one key and keep the
    tables with no match, biggest first (largest untagged asset = highest-value fix).
    One tag key, whitelisted via sql_literal; company-scoped like object_tag_coverage."""
    key = str(tag_name or "").strip().upper() or "COST_OWNER"
    n = int(max(1, min(1000, limit)))
    where = and_where(
        "t.DELETED IS NULL",
        "t.TABLE_TYPE = 'BASE TABLE'",
        companies.database_company_scope(company, "t.TABLE_CATALOG"),
    )
    return f"""
WITH tagged AS (
    SELECT DISTINCT r.OBJECT_DATABASE, r.OBJECT_SCHEMA, r.OBJECT_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES r
    WHERE r.DOMAIN = 'TABLE' AND r.OBJECT_DELETED IS NULL AND UPPER(r.TAG_NAME) = {sql_literal(key)}
)
SELECT
    t.TABLE_CATALOG || '.' || t.TABLE_SCHEMA || '.' || t.TABLE_NAME AS FQN,
    t.TABLE_CATALOG AS DATABASE_NAME,
    t.TABLE_SCHEMA  AS SCHEMA_NAME,
    t.ROW_COUNT,
    t.BYTES
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES t
LEFT JOIN tagged g
    ON g.OBJECT_DATABASE = t.TABLE_CATALOG
   AND g.OBJECT_SCHEMA   = t.TABLE_SCHEMA
   AND g.OBJECT_NAME     = t.TABLE_NAME
WHERE {where}
  AND g.OBJECT_NAME IS NULL
ORDER BY t.BYTES DESC NULLS LAST
LIMIT {n}
"""


def show_warehouses_sql() -> str:
    """SHOW-based (WAREHOUSES view absent on this account); LIMIT keeps the
    row-cap rewrite away. resource_monitor/auto_suspend parsed client-side."""
    return "SHOW WAREHOUSES LIMIT 500"


def show_resource_monitors_sql() -> str:
    """Resource-monitor inventory: name, credit quota, used/remaining credits,
    LEVEL (ACCOUNT / WAREHOUSE / unassigned), reset frequency, and the NOTIFY /
    SUSPEND / SUSPEND_IMMEDIATE trigger thresholds. Metadata only — no
    ACCOUNT_USAGE view is involved. Columns are parsed client-side by
    ``logic/monitors``, tolerant of SHOW column drift. No LIMIT: monitors are few
    and the run-layer row cap is disabled with ``max_rows=0`` (a bare SHOW is
    passed verbatim, so this never depends on LIMIT being valid for this SHOW)."""
    return "SHOW RESOURCE MONITORS"


SHOW_DATABASES_LIMIT = 500


def show_databases_sql() -> str:
    """SHOW-based database inventory (ACCOUNT_USAGE.DATABASES absent on this
    account, mirroring SHOW WAREHOUSES). Feeds the sidebar picker so new
    databases appear without a code change (item 8c, 2026-07-14); the hardcoded
    lists in companies.py stay the offline fallback. The #43 environment grouping reuses this exact read (same
    SQL, metadata tier, max_rows=0: the sidebar's cache entry, no extra scan)."""
    return f"SHOW DATABASES LIMIT {SHOW_DATABASES_LIMIT}"


def show_shares_sql() -> str:
    """rec#25: outbound/inbound share inventory — the data-exposure surface.

    SHOW-based like SHOW WAREHOUSES/DATABASES: this account has no ACCOUNT_USAGE
    shares view, and SHOW SHARES is the one read that names every share, its KIND
    (OUTBOUND=we expose / INBOUND=we consume), the database it exposes, the
    consumer accounts (``to``), and any marketplace listing. Columns are parsed
    client-side by logic/exposure.classify_share_exposure; LIMIT keeps the
    row-cap rewrite away."""
    return "SHOW SHARES LIMIT 500"


def show_grants_to_share_sql(share_name: str) -> str:
    """rec#8 drill: the objects one outbound share exposes.

    SHOW GRANTS TO SHARE lists every privilege the share carries (USAGE on the
    exposed database/schema, SELECT / REFERENCE_USAGE on the objects) — the
    "what does this share actually expose" answer behind the SHOW SHARES
    inventory. Metadata command, not an ACCOUNT_USAGE scan.

    Injection: the share name is an IDENTIFIER in a SHOW command, NOT a
    string-literal position, so ``sql_literal`` is WRONG here (it would emit
    ``SHOW GRANTS TO SHARE 'X'``, a syntax error). Wrap it as a double-quoted
    identifier and double any embedded double-quote — the only escape from a
    ``"``-quoted identifier is ``"``, which is doubled, so this is closed
    against injection and still handles shares created as quoted identifiers
    (spaces / lowercase). No LIMIT: SHOW rejects it, which is why the run()
    call must pass ``max_rows=0``."""
    ident = '"' + str(share_name).replace('"', '""') + '"'
    return f"SHOW GRANTS TO SHARE {ident}"


def role_privilege_matrix(*, limit: int | None = None) -> str:
    """Auditor sheet: privileges per role aggregated by object type."""
    return f"""
SELECT GRANTEE_NAME AS ROLE_NAME, GRANTED_ON AS OBJECT_TYPE, PRIVILEGE,
       COUNT(*) AS GRANT_COUNT
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
WHERE DELETED_ON IS NULL
GROUP BY 1, 2, 3
ORDER BY ROLE_NAME, GRANT_COUNT DESC
{_limit_clause(limit, 5000)}
"""


def unused_roles(days: int = 90, *, limit: int | None = None) -> str:
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
{_limit_clause(limit, 500)}
"""


def role_holders(role: str) -> str:
    """rec#26 drill: who currently holds one role (revoke-decision context).

    GRANTS_TO_USERS filtered to the clicked role, active grants only — the same
    source as the parent ``unused_roles`` list's GRANTED_TO_USERS count, so the
    two reconcile. ROLE here is a WHERE-clause STRING comparison (a
    string-literal position), so use ``sql_literal`` (NOT identifier quoting):
    the verbatim ROLE_NAME value matches on exact ``=`` (do not upper/lower it).
    Up to ~2h ACCOUNT_USAGE latency, so a grant added in the last hour may not
    appear — confirm with the owner before revoking."""
    return f"""
SELECT GRANTEE_NAME AS USER_NAME, GRANTED_BY, CREATED_ON AS GRANTED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE ROLE = {sql_literal(role)} AND DELETED_ON IS NULL
ORDER BY USER_NAME
LIMIT 500
"""


def role_privileges(role: str) -> str:
    """rec#26 drill: what one role grants — what a revoke would remove.

    GRANTS_TO_ROLES where the clicked role is the GRANTEE: every privilege the
    role holds and on which object. GRANTEE_NAME is a WHERE-clause STRING
    comparison (a string-literal position), so use ``sql_literal`` (NOT
    identifier quoting); the verbatim ROLE_NAME value matches on exact ``=``.
    Up to ~2h ACCOUNT_USAGE latency — confirm with the owner before revoking."""
    return f"""
SELECT PRIVILEGE, GRANTED_ON AS OBJECT_TYPE, NAME AS OBJECT, GRANTED_BY, CREATED_ON
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
WHERE GRANTEE_NAME = {sql_literal(role)} AND DELETED_ON IS NULL
ORDER BY GRANTED_ON, PRIVILEGE
LIMIT 1000
"""


def direct_role_grants(*, limit: int | None = None) -> str:
    """Current role->user grants (auditors reconcile this against HR)."""
    return f"""
SELECT GRANTEE_NAME AS USER_NAME, COUNT(*) AS ROLE_COUNT,
       LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ROLES
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE DELETED_ON IS NULL
GROUP BY 1
ORDER BY ROLE_COUNT DESC
{_limit_clause(limit, 1000)}
"""


def grant_changes(days: int = 90, *, limit: int | None = None) -> str:
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
{_limit_clause(limit, 2000)}
"""


def recent_grant_changes(days: int = 30, company: str = "ALL", limit: int = 500, *,
                         onset: object = None) -> str:
    """Most-recent grant/revoke CHANGES across roles, users, and objects — the
    "who changed what for whom, and when" access-change feed (owner ask 2026-08-17).

    Each row is ONE change event at its own time: a role granted to a user, a
    privilege granted on an object to a role, or either revoked. Sourced from
    ACCOUNT_USAGE.GRANTS_TO_USERS (role -> user) and GRANTS_TO_ROLES (privilege ->
    role); a grant and its later revoke are two rows at their own timestamps, so a
    same-window grant+revoke both show. GRANTED_BY is the actor ('(system)' when
    Snowflake-internal). Newest first.

    ``company`` scopes by the GRANTEE (owner ask 2026-08-17): a role granted to a
    Trexis user or a privilege granted to a %TRXS% role is Trexis. Role-grain grants
    use the role heuristic (role_clause); user-grain grants use user classification.
    'ALL' = account-wide (both clauses collapse to no-op).

    ``onset`` (an incident's start; the auto-investigation feed): the rows are cut to the
    change registries' onset window (change_impact_sql._onset_window on CHANGED_AT: onset -
    ONSET_LEAD_DAYS .. onset + ONSET_AFTER_DAYS) and ordered NEAREST onset first, so post-onset
    churn can no longer push the pre-onset trigger past the LIMIT (a newest-first read from now
    did). ``days`` is then ignored (as in the registries): the trailing cutoff gives way to a prune
    bounded on BOTH sides from the onset (change_impact_sql._onset_prune_bounds: that window plus a
    day of slack each side), so an old incident scans its onset's few days of GRANTS_*, not every
    day since. TOTAL_CHANGES_WIN is the onset window's pre-LIMIT count. Without ``onset`` the SQL
    is unchanged."""
    days = bounded_days(days, 365)
    limit = max(10, min(int(limit or 500), 2000))
    onset_where, order_by = "", "CHANGED_AT DESC"
    if onset is None:
        cutoff = f"DATEADD('day', -{days}, CURRENT_TIMESTAMP())"
        span, arm_span = f"CREATED_ON >= {cutoff} OR DELETED_ON >= {cutoff}", f">= {cutoff}"
    else:
        # R1-060's twin for grants (holistic review): the registries' onset window + nearest-first
        # order. The prune is two-sided from the onset literal -- a trailing cutoff wide enough to
        # reach an old onset scanned up to 365 days of GRANTS_* to keep ~4.
        from app.data.change_impact_sql import _onset_prune_bounds, _onset_window
        _win = _onset_window("CHANGED_AT", onset)   # ValueError on a non-timestamp: no text reaches the SQL
        onset_where, order_by = f"\n  AND {_win[0]}", f"{_win[1]}, CHANGED_AT DESC"
        lo, hi = _onset_prune_bounds(onset)
        span = f"CREATED_ON BETWEEN {lo} AND {hi} OR DELETED_ON BETWEEN {lo} AND {hi}"
        arm_span = f"BETWEEN {lo} AND {hi}"
    u_scope = companies.user_scope_subquery(company, "GRANTEE_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS",
                                            distinct_where=span)   # grantee is a user
    r_scope = companies.role_clause(company, "GRANTEE_NAME")   # grantee is a role
    # Owner finding 2026-08-17: every object CREATE (incl. the TMP_* stages/formats
    # procs make per run) records an OWNERSHIP grant BY the creating role TO itself
    # — object-lifecycle noise, not an access change, and it buried the feed (500-row
    # cap full of self-OWNERSHIP on TMP_ objects). A real ownership TRANSFER has a
    # different grantor and still shows. NULL-safe: an unattributed grantor never
    # matches the grantee, so it stays visible.
    self_own = ("NOT (PRIVILEGE = 'OWNERSHIP' "
                "AND COALESCE(GRANTED_BY, '') = GRANTEE_NAME)")
    # v4.528 perf: each source table was scanned TWICE (a CREATED_ON->GRANTED arm and a
    # DELETED_ON->REVOKED arm), and for a company scope the u_scope DISTINCT sub-scan ran
    # in both user arms — measured 1-2 min. Cross-join each row to a 2-row event generator
    # and pick the arm's timestamp with IFF, so each table (and each scope sub-scan) is read
    # ONCE. Row-equivalent: the GRANTED candidate survives iff CREATED_ON >= cutoff, the
    # REVOKED iff DELETED_ON >= cutoff (DELETED_ON NULL -> NULL>=cutoff is UNKNOWN -> dropped),
    # exactly the old per-arm predicates; every column expression, the self_own filter, the
    # outer CHANGED_AT-IS-NOT-NULL / ORDER BY / LIMIT are unchanged. The extra
    # (CREATED_ON >= cutoff OR DELETED_ON >= cutoff) predicate is redundant with the IFF filter
    # but LOAD-BEARING for micro-partition pruning: the IFF-over-the-join-column alone cannot
    # prune, so this literal-cutoff predicate keeps the base scan pruned to relevant partitions.
    # (Onset mode keeps the same shape with a two-sided ``span``: the BETWEEN literals prune.)
    ev = "(SELECT 'GRANTED' AS CHG UNION ALL SELECT 'REVOKED' AS CHG)"
    return f"""
WITH changes AS (
    SELECT IFF(ev.CHG = 'GRANTED', CREATED_ON, DELETED_ON) AS CHANGED_AT,
           ev.CHG AS CHANGE, 'Role -> user' AS GRANT_TYPE,
           GRANTED_BY AS CHANGED_BY, GRANTEE_NAME AS GRANTEE, ROLE AS WHAT
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    CROSS JOIN {ev} ev
    WHERE {and_where(f"({span})", f"IFF(ev.CHG = 'GRANTED', CREATED_ON, DELETED_ON) {arm_span}", u_scope)}
    UNION ALL
    SELECT IFF(ev.CHG = 'GRANTED', CREATED_ON, DELETED_ON), ev.CHG, 'Privilege -> role',
           GRANTED_BY, GRANTEE_NAME,
           PRIVILEGE || ' ON ' || GRANTED_ON || ' ' || COALESCE(NAME, '')
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    CROSS JOIN {ev} ev
    WHERE {and_where(f"({span})", f"IFF(ev.CHG = 'GRANTED', CREATED_ON, DELETED_ON) {arm_span}", r_scope, self_own)}
)
SELECT CHANGED_AT, CHANGE, GRANT_TYPE,
       COALESCE(NULLIF(TRIM(CHANGED_BY), ''), '(system)') AS CHANGED_BY,
       GRANTEE, WHAT,
       -- pre-LIMIT window totals (the day_grants / day_ddl pattern): the KPI tiles read these, so a
       -- 90/180-day window with more than {limit} changes reports the true count, not the display cap
       COUNT(*) OVER () AS TOTAL_CHANGES_WIN,
       SUM(IFF(CHANGE = 'GRANTED', 1, 0)) OVER () AS GRANTED_WIN,
       SUM(IFF(CHANGE = 'REVOKED', 1, 0)) OVER () AS REVOKED_WIN
FROM changes
WHERE CHANGED_AT IS NOT NULL{onset_where}
ORDER BY {order_by}
LIMIT {limit}
"""


def admin_grant_context(days: int = 90, company: str = "ALL") -> str:
    """Admin-role grants in the window with the *time-context* for triage (Sec2).

    'WHO gained admin roles' already surfaces via grant_changes/break-glass
    activity; this adds the delta a reviewer actually acts on:

      * PRIOR_GRANTS — how many earlier grants of *this* role to *this* user are
        on record. Zero means a first-ever admin elevation (the strongest
        escalation signal), not a renewal of standing access.
      * DOW_ISO / HOUR_OF_DAY — when the grant landed (account-local), so a
        weekend or off-hours grant outside a normal deploy window stands out.

    Scoped to the admin roles this account actually uses (SNOW_* pair) plus the
    standard admin roles, defensively. The prior-grant count is a correlated
    read of the same GRANTS_TO_USERS history (retains revoked rows), so a role
    granted, revoked, and re-granted is NOT counted as first-time.
    """
    days = bounded_days(days, 180)
    date_pred = f"g.CREATED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"
    where = and_where(
        _admin_roles_in("g.ROLE", ELEVATED_ROLES),
        date_pred,
        companies.user_clause(company, "g.GRANTEE_NAME"),
    )
    return f"""
SELECT g.ROLE AS ADMIN_ROLE, g.GRANTEE_NAME AS USER_NAME,
       g.CREATED_ON AS GRANTED_AT, g.GRANTED_BY,
       (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS p
         WHERE p.ROLE = g.ROLE AND p.GRANTEE_NAME = g.GRANTEE_NAME
           AND p.CREATED_ON < g.CREATED_ON) AS PRIOR_GRANTS,
       -- Account-local (Chicago), pinned explicitly so the off-hours/weekend flag
       -- grant_anomaly_flags computes matches the operator's clock even if the session zone
       -- (Central today only via the account default; ALTER SESSION is a no-op under
       -- owner's-rights SiS) ever changes. Mirrors unload_risk_events.
       DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', g.CREATED_ON)) AS DOW_ISO,
       HOUR(CONVERT_TIMEZONE('America/Chicago', g.CREATED_ON)) AS HOUR_OF_DAY
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
WHERE {where}
ORDER BY g.CREATED_ON DESC
LIMIT 500
"""


# ---------------------------------------------------------------------------
# rec#24: least-privilege — held table grants vs. what queries actually touched.
# Both builders bridge GRANTS_TO_ROLES -> TABLE_STORAGE_METRICS (by UPPER name)
# -> ACCESS_HISTORY (by numeric object id, NOT the DB.SCHEMA.TABLE string, per
# the D4 audit lesson in insights_sql), and count a table "touched" if ANY query
# read OR modified it in the window (object-level, so a grant exercised only
# through role inheritance still counts as used — never a false "unused"). Needs
# Enterprise-edition ACCESS_HISTORY; the page degrades via guard(). Deliberately
# NOT canaried (Standard edition would be permanent alert noise).
# ---------------------------------------------------------------------------

_TOUCHED_CTE = """touched AS (
    SELECT DISTINCT f.value:"objectId"::NUMBER AS OBJECT_ID
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND f.value:"objectId" IS NOT NULL
    UNION
    SELECT DISTINCT f.value:"objectId"::NUMBER AS OBJECT_ID
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.OBJECTS_MODIFIED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
      AND f.value:"objectId" IS NOT NULL
)"""

_TBL_CTE = """tbl AS (
    SELECT ID AS OBJECT_ID,
           UPPER(TABLE_CATALOG) AS C, UPPER(TABLE_SCHEMA) AS S, UPPER(TABLE_NAME) AS N,
           TABLE_CATALOG || '.' || TABLE_SCHEMA || '.' || TABLE_NAME AS FQN
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS
    WHERE DELETED = FALSE
)"""

# Only privileges whose exercise ACCESS_HISTORY actually records: SELECT shows in
# BASE_OBJECTS_ACCESSED, INSERT/UPDATE/DELETE in OBJECTS_MODIFIED. REFERENCES
# (exercised by FK DDL) and TRUNCATE (metadata) leave no ACCESS_HISTORY trace, so
# they would ALWAYS read as "unused" — excluded so the tool never emits a false
# revoke signal for a privilege it structurally cannot observe.
_DATA_PRIVS = "('SELECT', 'INSERT', 'UPDATE', 'DELETE')"


def access_evidence_days() -> str:
    """rec#24: DENSITY and recency of ACCESS_HISTORY over the last 90 days.

    Least-privilege verdicts are only trustworthy when history is both deep and
    current. COVERAGE_DAYS counts DISTINCT days that actually carry access rows —
    density, so a handful of stray old rows can't inflate it the way MIN(timestamp)
    span would (an account briefly on Enterprise 85 days ago then thin since would
    otherwise report "85 days" and flag genuinely-used tables as unused).
    LATEST_DAYS_AGO is how stale the newest row is — recency, because if
    ACCESS_HISTORY stopped being populated (e.g. an edition downgrade) recent usage
    isn't captured and every recent grant would falsely read as unused. COVERAGE_DAYS
    is 0 and LATEST_DAYS_AGO NULL when the view is empty or absent."""
    return """
SELECT COUNT(DISTINCT DATE(a.QUERY_START_TIME)) AS COVERAGE_DAYS,
       DATEDIFF('day', MAX(a.QUERY_START_TIME), CURRENT_TIMESTAMP()) AS LATEST_DAYS_AGO
FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a
WHERE a.QUERY_START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
"""


def grant_scope_usage(days: int = 90, limit: int | None = None) -> str:
    """rec#24: per (role, database, schema) — how many granted tables were used.

    GRANTED_TABLES = distinct tables (by name) the role holds a data privilege on
    in that schema; TOUCHED_TABLES = how many were read or modified in the window.
    Usage is collapsed to the table NAME first (MAX over its object ids), so a
    drop->recreate that leaves two DELETED=FALSE ids for one name can neither
    double-count nor split a used table into a used+unused pair. The Python layer
    (logic/least_privilege.classify_grant_scopes) labels UNUSED / OVER-BROAD /
    FOCUSED from the two counts. Worst (most unused) first.

    No LIMIT of its own by default (the client_drivers precedent): it used to end LIMIT 500, below
    run()'s default row cap, so a cut feed could never set res.truncated while the four KPIs counted
    the kept rows and the least-unused scopes (often every UNUSED single-table scope) silently fell
    off. run()'s cap is now the only cut and the page marks the KPIs as floors when it fires.
    TOTAL_SCOPES_WIN is the pre-cap scope count for the 'showing N of M' caption."""
    days = bounded_days(days, 90)
    _limit = f"\nLIMIT {max(1, int(limit))}" if limit else ""
    return f"""
WITH {_TOUCHED_CTE.format(days=days)},
{_TBL_CTE},
grants AS (
    SELECT DISTINCT GRANTEE_NAME AS ROLE_NAME,
           UPPER(TABLE_CATALOG) AS C, UPPER(TABLE_SCHEMA) AS S, UPPER(NAME) AS N
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE DELETED_ON IS NULL AND GRANTED_ON = 'TABLE'
      AND PRIVILEGE IN {_DATA_PRIVS}
      AND TABLE_CATALOG IS NOT NULL AND TABLE_SCHEMA IS NOT NULL
),
grant_tbl AS (
    -- one row per (role, table name); touched if ANY of the name's ids was touched
    SELECT g.ROLE_NAME, g.C, g.S, g.N,
           MAX(IFF(u.OBJECT_ID IS NULL, 0, 1)) AS TOUCHED
    FROM grants g
    JOIN tbl t ON t.C = g.C AND t.S = g.S AND t.N = g.N
    LEFT JOIN touched u ON u.OBJECT_ID = t.OBJECT_ID
    GROUP BY 1, 2, 3, 4
)
SELECT ROLE_NAME,
       C AS DATABASE_NAME, S AS SCHEMA_NAME,
       COUNT(*) AS GRANTED_TABLES,
       SUM(TOUCHED) AS TOUCHED_TABLES,
       COUNT(*) OVER () AS TOTAL_SCOPES_WIN
FROM grant_tbl
GROUP BY 1, 2, 3
HAVING COUNT(*) >= 1
ORDER BY (COUNT(*) - SUM(TOUCHED)) DESC, GRANTED_TABLES DESC{_limit}
"""


def unused_table_grants(days: int = 90, limit: int | None = None) -> str:
    """rec#24: the revoke shortlist — table grants on tables no query has read or
    modified in the window. One row per (role, privilege, table name); a name is
    listed only when NONE of its object ids was touched (MAX over ids), so a
    drop->recreate that leaves a stale untouched id can never surface a live,
    in-use table's name as a revoke candidate. Review before revoking.

    Worst scope first (most untouched grants per role x database x schema), then role/object: it was
    ORDER BY role LIMIT 500, so a role late in the alphabet vanished from the shortlist and from its
    REVOKE script, and a selected scope row could post-filter to empty with no notice. No LIMIT of
    its own by default, so run()'s row cap is the only (detectable) cut; TOTAL_GRANTS_WIN is the
    pre-cap total."""
    days = bounded_days(days, 90)
    _limit = f"\nLIMIT {max(1, int(limit))}" if limit else ""
    return f"""
WITH {_TOUCHED_CTE.format(days=days)},
{_TBL_CTE},
grants AS (
    SELECT GRANTEE_NAME AS ROLE_NAME, PRIVILEGE,
           UPPER(TABLE_CATALOG) AS C, UPPER(TABLE_SCHEMA) AS S, UPPER(NAME) AS N
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE DELETED_ON IS NULL AND GRANTED_ON = 'TABLE'
      AND PRIVILEGE IN {_DATA_PRIVS}
      AND TABLE_CATALOG IS NOT NULL AND TABLE_SCHEMA IS NOT NULL
)
SELECT g.ROLE_NAME, g.PRIVILEGE, ANY_VALUE(t.FQN) AS OBJECT_NAME,
       COUNT(*) OVER (PARTITION BY g.ROLE_NAME, t.C, t.S) AS SCOPE_UNUSED_GRANTS,
       COUNT(*) OVER () AS TOTAL_GRANTS_WIN
FROM grants g
JOIN tbl t ON t.C = g.C AND t.S = g.S AND t.N = g.N
LEFT JOIN touched u ON u.OBJECT_ID = t.OBJECT_ID
GROUP BY g.ROLE_NAME, g.PRIVILEGE, t.C, t.S, t.N
HAVING MAX(IFF(u.OBJECT_ID IS NULL, 0, 1)) = 0
ORDER BY SCOPE_UNUSED_GRANTS DESC, g.ROLE_NAME, OBJECT_NAME, g.PRIVILEGE{_limit}
"""


def _snowflake_run_predicate() -> str:
    """SQL twin of client_support.who_upgrades, rendered from the SAME constants (one allow-list): a
    PROGRAM prefix (ILIKE '<prefix>%') or an exact DRIVER, both case-insensitive."""
    prog = " OR ".join(f"PROGRAM ILIKE {sql_literal(p + '%')}" for p in SNOWFLAKE_RUN_PROGRAM_PREFIXES)
    drivers = ", ".join(sql_literal(d.upper()) for d in SNOWFLAKE_RUN_DRIVERS)
    return f"{prog} OR UPPER(DRIVER) IN ({drivers})"


def client_drivers(days: int = 30, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Driver/version inventory from ACCOUNT_USAGE.SESSIONS: which driver,
    which version, reported by which program, used by whom — the "when do
    we need to upgrade" sheet. Snowflake's own support floor joins on in
    app/logic/client_support (Next-Fifty #34, client_version_info below).

    PROGRAM is what the client self-reports in CLIENT_ENVIRONMENT: JDBC and
    Python tools usually set it (DBeaver, VS Code); plenty of ODBC tools
    (Erwin) do not, so '(not reported)' is honest, not a bug. A NULL or blank
    CLIENT_APPLICATION_ID is kept and labelled '(no client id)' (it used to be
    dropped, or to render as a green CURRENT row with an empty DRIVER).

    UPGRADED_BY splits Snowflake's own services (the Snowflake Web App /
    Snowsight backend, SnowServices ingress: client_support's allow-list)
    from the customer's clients. STATUS compares each customer version against
    the newest version of the SAME driver seen among the CUSTOMER's rows this
    window — the only in-account latest-version truth. A Snowflake-run row
    reads 'SNOWFLAKE-RUN' and never sets the newest; a row with no version
    ('?') has a NULL key, reads 'NO VERSION', and is never the newest ('?'
    used to sort above every digit and win MAX). Version key pads dot-segments
    so 3.10.2 > 3.9.1. SESSIONS lags up to ~3h; POSIX classes only (no
    backslashes survive the string layers — V022 lesson).

    No LIMIT of its own (it used to end LIMIT 500, below run()'s default
    cap, so a cut feed could never set res.truncated): the page derives the
    support KPIs, the upgrade caption and the BEHIND count from these rows,
    so run()'s cap must be the only cut and must be detectable. A real
    account has a few dozen DRIVER x VERSION x PROGRAM rows.
    """
    days = bounded_days(days, maximum=90)
    _scope = (resolve_effective_window(days, "CREATED_ON", bounds=bounds)[1]
              if bounds is not None
              else f"CREATED_ON >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        companies.user_scope_subquery(company, "USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.SESSIONS",
                                      distinct_where=_scope),
    )
    return f"""
WITH s AS (
    SELECT
        USER_NAME,
        CREATED_ON,
        COALESCE(NULLIF(TRIM(REGEXP_REPLACE(CLIENT_APPLICATION_ID, ' [0-9][0-9.]*$', '')), ''),
                 NULLIF(TRIM(CLIENT_APPLICATION_ID), ''), {sql_literal(NO_CLIENT_ID)}) AS DRIVER,
        COALESCE(NULLIF(TRIM(REGEXP_SUBSTR(CLIENT_APPLICATION_ID, '[0-9][0-9.]*$')), ''), '?') AS VERSION,
        COALESCE(TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING, '(not reported)') AS PROGRAM
    FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
    WHERE {where}
),
keyed AS (
    SELECT s.*,
           IFF(VERSION = '?', NULL,
               LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 1), ''), '0'), 6, '0') ||
               LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 2), ''), '0'), 6, '0') ||
               LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 3), ''), '0'), 6, '0') ||
               LPAD(COALESCE(NULLIF(SPLIT_PART(VERSION, '.', 4), ''), '0'), 6, '0')) AS VKEY,
           IFF({_snowflake_run_predicate()}, 'SNOWFLAKE', 'CUSTOMER') AS UPGRADED_BY
    FROM s
),
grouped AS (
    SELECT DRIVER, VERSION, PROGRAM, UPGRADED_BY, MAX(VKEY) AS VKEY,
           COUNT(DISTINCT USER_NAME) AS USERS,
           COUNT(*) AS SESSIONS,
           MIN(DATE(CREATED_ON)) AS FIRST_SEEN,
           MAX(DATE(CREATED_ON)) AS LAST_SEEN,
           LEFT(LISTAGG(DISTINCT USER_NAME, ', ') WITHIN GROUP (ORDER BY USER_NAME), 160) AS SAMPLE_USERS
    FROM keyed
    GROUP BY DRIVER, VERSION, PROGRAM, UPGRADED_BY
)
SELECT DRIVER, VERSION, PROGRAM, UPGRADED_BY, USERS, SESSIONS, FIRST_SEEN, LAST_SEEN, SAMPLE_USERS,
       IFF(UPGRADED_BY = 'SNOWFLAKE', NULL,
           FIRST_VALUE(IFF(VKEY IS NULL, NULL, VERSION))
               OVER (PARTITION BY DRIVER, UPGRADED_BY ORDER BY VKEY DESC NULLS LAST)) AS NEWEST_IN_ACCOUNT,
       CASE WHEN UPGRADED_BY = 'SNOWFLAKE' THEN 'SNOWFLAKE-RUN'
            WHEN VKEY IS NULL THEN 'NO VERSION'
            WHEN VKEY < MAX(VKEY) OVER (PARTITION BY DRIVER, UPGRADED_BY) THEN 'BEHIND'
            ELSE 'CURRENT' END AS STATUS
FROM grouped
ORDER BY DRIVER, VKEY DESC NULLS LAST, SESSIONS DESC
"""


def client_version_info() -> str:
    """Snowflake's own per-driver support floor (Next-Fifty #34): one row per entry of
    SYSTEM$CLIENT_VERSION_INFO(), with the minimum supported, nearing-end-of-support and recommended
    version. Key names and flatten shape are the probe's (PROBES_NEXT_FIFTY_WAVE4.sql W4c, the only form
    proven live on this account); GET_PATH, not the ':' path form, keeps the builder canary-parse-clean.
    RAW_ENTRY carries each entry's own JSON so app/logic/client_support can still read a field whose key
    Snowflake spells differently. A metadata call (no ACCOUNT_USAGE scan): the page runs it
    tier='metadata', probe=True, and a failure renders the support column 'unavailable', never clean."""
    return """
SELECT GET_PATH(f.value, 'clientId')::STRING AS CLIENT_ID,
       GET_PATH(f.value, 'clientAppId')::STRING AS CLIENT_APP_ID,
       GET_PATH(f.value, 'minimumSupportedVersion')::STRING AS MIN_SUPPORTED_VERSION,
       GET_PATH(f.value, 'minimumNearingEndOfSupportVersion')::STRING AS NEARING_EOS_VERSION,
       GET_PATH(f.value, 'recommendedVersion')::STRING AS RECOMMENDED_VERSION,
       TO_JSON(f.value) AS RAW_ENTRY
FROM TABLE(FLATTEN(INPUT => TRY_PARSE_JSON(SYSTEM$CLIENT_VERSION_INFO()))) f
"""


def day_ddl(day: object, company: str = "ALL") -> str:
    """Replay: DDL that landed on one day. Role-grain company scoping —
    Trexis automation roles stay off ALFA's replay (live finding)."""
    from app import companies as _companies
    from app.data.common import and_where, day_literal

    lit = day_literal(day)
    # QUERY_TYPE list aligned with recent_ddl_changes so the day-replay covers the same
    # security-relevant identity/role/policy DDL the main change-risk feed shows (a DROP_ROLE /
    # ALTER_USER / masking-policy change was previously invisible in the replay). Warehouse
    # suspend/resume (system auto-cycle noise) dropped, and the cap raised + ordered newest-first
    # so a busy day no longer evicts late-day security DDL (bug-hunt 2026-08-30).
    where = and_where(
        f"DATE(START_TIME) = {lit}",
        "EXECUTION_STATUS = 'SUCCESS'",
        ("(QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW', 'ALTER', "
         "'ALTER_TABLE_MODIFY_COLUMN', 'ALTER_SESSION', 'ALTER_USER', 'CREATE_USER', 'DROP_USER', "
         "'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE', 'DROP', 'GRANT', 'REVOKE', "
         "'CREATE_TABLE_AS_SELECT', 'RENAME', 'RENAME_TABLE', 'TRUNCATE_TABLE') "
         "OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' "
         "OR QUERY_TYPE ILIKE '%ROLE%' OR QUERY_TYPE ILIKE 'DROP%' "
         "OR QUERY_TYPE ILIKE 'TRUNCATE%')"),
        _companies.role_clause(company),
    )
    return f"""
SELECT START_TIME, USER_NAME, ROLE_NAME, QUERY_TYPE, DATABASE_NAME, SCHEMA_NAME,
       LEFT(QUERY_TEXT, 140) AS DDL_PREVIEW,
       -- Uncapped day total (pre-LIMIT): the replay headline reads THIS so a busy
       -- migration day (>500/300 DDL) reports the true count, not the display cap.
       COUNT(*) OVER () AS TOTAL_DDL_WIN
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
ORDER BY START_TIME DESC
LIMIT 500
"""


def day_grants(day: object, company: str = "ALL") -> str:
    """Replay: role grants created or revoked on one day (grantee-scoped).

    GRANTS_TO_USERS is a role->user table, so company must scope by the GRANTEE (the
    receiving user, via COMPANY_FOR_USER) — NOT by the granted role's name. Keying off
    ROLE dropped a grant of a shared admin role (SNOW_SYSADMINS) to an in-company user
    from that company's replay, and dropped generically-named roles granted to ALFA
    users; it also disagreed with the access-changes feed (recent_grant_changes), which
    already scopes GRANTS_TO_USERS by GRANTEE_NAME. (bug-hunt 2026-08-30)"""
    from app import companies as _companies
    from app.data.common import and_where, day_literal

    lit = day_literal(day)
    where = and_where(
        f"(DATE(CREATED_ON) = {lit} OR DATE(DELETED_ON) = {lit})",
        _companies.user_clause(company, "GRANTEE_NAME"),
    )
    return f"""
SELECT CREATED_ON AS GRANTED_AT, DELETED_ON, ROLE, GRANTED_TO, GRANTEE_NAME, GRANTED_BY,
       -- Uncapped day total (pre-LIMIT): the replay headline reads THIS so >200 grant
       -- changes in a day reports the true count, not the display cap.
       COUNT(*) OVER () AS TOTAL_GRANTS_WIN
FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
WHERE {where}
ORDER BY GRANTED_AT
LIMIT 200
"""


#: The new-network panel's baseline: a (user, IP) pair is "new" only when it was not seen in the
#: NETWORK_BASELINE_DAYS before the triage window STARTS (owner pick r25: a 90-day baseline).
NETWORK_BASELINE_DAYS = 90


def new_network_logins(days: int = 7, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """r25 #6 (owner pick): privileged logins from never-before-seen networks.

    Baseline = the 90 days of LOGIN_HISTORY BEFORE the triage window starts, for break-glass users
    (same role list as admin_role_holders); a row surfaces only when a (user, IP) pair FIRST
    appears inside the triage window. The history used to be a fixed last-90-days, so a 90-day (or
    wider) window had NO baseline and listed every admin's routine office/VPN IP as new; it now
    reaches back window + 90 days (at most 180, inside LOGIN_HISTORY's 365-day retention). An IP
    quiet for 90+ days re-flags on purpose — better a stale re-flag than a silent novel network.
    COUNT(*) OVER () is the pre-LIMIT pair total (the caption reads it, never len() of 200 rows).
    """
    days = bounded_days(days)
    bounds = capped_window(days, bounds, 90)[1]
    if bounds is not None:
        _start, _ = bounds
        _hist_start = f"'{(_start - timedelta(days=NETWORK_BASELINE_DAYS)).isoformat()}'"
        _first_seen_scope = resolve_effective_window(days, "F.FIRST_SEEN", bounds=bounds)[1]
        # the volume columns count the pair's logins INSIDE the window (Last month used to add September's)
        _volume_scope = resolve_effective_window(days, "H.EVENT_TIMESTAMP", bounds=bounds)[1]
    else:
        _hist_start = f"DATEADD('day', -{days + NETWORK_BASELINE_DAYS}, CURRENT_TIMESTAMP())"
        _first_seen_scope = f"F.FIRST_SEEN >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"
        _volume_scope = ""
    admin_where = and_where(
        "DELETED_ON IS NULL",
        _admin_roles_in("ROLE", ADMIN_HOLDER_ROLES),
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
    WHERE L.EVENT_TIMESTAMP >= {_hist_start}
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
       MAX_BY(H.FIRST_AUTHENTICATION_FACTOR, H.EVENT_TIMESTAMP) AS AUTH_FACTOR,
       COUNT(*) OVER () AS TOTAL_PAIRS_WIN
FROM first_seen F
JOIN hist H ON H.USER_NAME = F.USER_NAME AND H.CLIENT_IP = F.CLIENT_IP
WHERE {and_where(_first_seen_scope, _volume_scope)}
GROUP BY F.USER_NAME, F.CLIENT_IP, F.FIRST_SEEN
ORDER BY F.FIRST_SEEN DESC
LIMIT 200
"""


def dormant_reawakening(dormant_gap_days: int = 45, recent_days: int = 7,
                        baseline_days: int = 365, company: str = "ALL") -> str:
    """Sec5: a long-dormant account that JUST logged in — the signal
    LAST_SUCCESS_LOGIN structurally cannot express.

    ACCOUNT_USAGE.USERS keeps only the most-recent login, so a woken dormant
    account reads as freshly active there. This reads successful LOGIN_HISTORY
    over ``baseline_days`` and flags a user whose login inside the last
    ``recent_days`` followed a >= ``dormant_gap_days`` gap — a real gap between
    consecutive logins (LAG), or a first-in-window login for an account created
    long before (the deep-dormant case, since LOGIN_HISTORY retains ~365 days, so
    a >365d silence leaves a single login here whose gap is measured from
    CREATED_ON). Company scope is user-role only (LOGIN_HISTORY has no object
    grain). Read-only review — service accounts legitimately log in rarely and
    will surface; no auto-alert is raised."""
    baseline_days = bounded_days(baseline_days, 365)
    recent_days = max(1, min(int(recent_days), 90))
    gap = max(7, min(int(dormant_gap_days), 365))
    where = and_where(
        f"L.EVENT_TIMESTAMP >= DATEADD('day', -{baseline_days}, CURRENT_TIMESTAMP())",
        "L.IS_SUCCESS = 'YES'",
        companies.user_scope_subquery(company, "L.USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY",
                                      distinct_where=f"EVENT_TIMESTAMP >= DATEADD('day', -{baseline_days}, CURRENT_TIMESTAMP())"),
    )
    return f"""
WITH logins AS (
    SELECT L.USER_NAME, L.EVENT_TIMESTAMP,
           COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
           L.FIRST_AUTHENTICATION_FACTOR AS AUTH_FACTOR,
           LAG(L.EVENT_TIMESTAMP) OVER (
               PARTITION BY L.USER_NAME ORDER BY L.EVENT_TIMESTAMP) AS PREV_LOGIN
    FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
    WHERE {where}
),
roles AS (
    SELECT GRANTEE_NAME, COUNT(*) AS ROLE_COUNT,
           LISTAGG(ROLE, ', ') WITHIN GROUP (ORDER BY ROLE) AS ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
    GROUP BY GRANTEE_NAME
),
woke AS (
    SELECT l.USER_NAME, l.EVENT_TIMESTAMP AS WAKE_LOGIN, l.CLIENT_IP, l.AUTH_FACTOR,
           l.PREV_LOGIN AS LAST_ACTIVE_BEFORE, u.EMAIL, u.CREATED_ON,
           DATEDIFF('day', COALESCE(l.PREV_LOGIN, u.CREATED_ON), l.EVENT_TIMESTAMP) AS GAP_DAYS
    FROM logins l
    JOIN SNOWFLAKE.ACCOUNT_USAGE.USERS u
      ON u.NAME = l.USER_NAME AND u.DELETED_ON IS NULL
    WHERE l.EVENT_TIMESTAMP >= DATEADD('day', -{recent_days}, CURRENT_TIMESTAMP())
    QUALIFY ROW_NUMBER() OVER (PARTITION BY l.USER_NAME ORDER BY GAP_DAYS DESC) = 1
)
SELECT w.USER_NAME, w.EMAIL, w.LAST_ACTIVE_BEFORE, w.WAKE_LOGIN, w.GAP_DAYS,
       w.CLIENT_IP, w.AUTH_FACTOR, w.CREATED_ON,
       COALESCE(r.ROLE_COUNT, 0) AS ROLE_COUNT,
       LEFT(COALESCE(r.ROLES, ''), 300) AS ROLES
FROM woke w
LEFT JOIN roles r ON r.GRANTEE_NAME = w.USER_NAME
WHERE w.GAP_DAYS >= {gap}
ORDER BY w.GAP_DAYS DESC, ROLE_COUNT DESC
LIMIT 200
"""


def unload_activity(days: int = 30, company: str = "ALL", database: str = "",
                    schema_contains: str = "", *, bounds: tuple | None = None) -> str:
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
    _scope = (resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
              if bounds is not None
              else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        "QUERY_TYPE = 'UNLOAD'",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.user_scope_subquery(company, "USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
                                      distinct_where=_scope),
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


def unload_activity_totals(days: int = 30, company: str = "ALL", database: str = "",
                           schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """Uncapped window totals for the unload KPI tiles (runs / GB out / distinct users). The
    ``unload_activity`` feed groups per (day, user, role) and is LIMIT-300, so SUMMING it
    understates the tiles once the window has > 300 (day,user,role) groups — a wide-window,
    multi-user account (bug-hunt wdmz68vd4). Same scope, aggregated to ONE uncapped row."""
    days = bounded_days(days)
    _scope = (resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
              if bounds is not None
              else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        "QUERY_TYPE = 'UNLOAD'",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.user_scope_subquery(company, "USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
                                      distinct_where=_scope),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
SELECT COUNT(*) AS UNLOADS,
       ROUND((SUM(COALESCE(BYTES_WRITTEN, 0))
             + SUM(COALESCE(BYTES_WRITTEN_TO_RESULT, 0))) / POWER(1024, 3), 3) AS GB_OUT,
       COUNT(DISTINCT USER_NAME) AS USERS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
"""


def unload_risk_events(days: int = 30, company: str = "ALL", database: str = "",
                       schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """#18: per-EVENT unload egress for the behavioral exfiltration score.

    Sibling to ``unload_activity``, which GROUPs BY day and so discards the three
    things the score needs: the hour of each event, its individual destination, and
    a per-user volume baseline. This keeps each COPY INTO <location> as its own row
    plus the account-local hour/weekday (CONVERT_TIMEZONE to America/Chicago, matching
    insights_sql so "off-hours" means the operator's clock, not UTC) and a per-user
    MEDIAN(GB_OUT) over the same window, so "unusual volume" is judged user-relative
    rather than against one global threshold. Same company/db/schema scoping as
    ``unload_activity`` (no new grants). The row cap serves the detector: each user
    is capped to its 50 highest-volume unloads (QUALIFY) and then the 500 largest
    events overall are returned — so one busy loader can't evict everyone else's
    events (a global recency cap would), and the retained set favours the big,
    exfil-like events the score exists to rank. The per-user MEDIAN/COUNT baselines
    are computed over the full window before the cap, so they stay accurate."""
    days = bounded_days(days)
    _scope = (resolve_effective_window(days, "START_TIME", bounds=bounds)[1]
              if bounds is not None
              else f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
        "QUERY_TYPE = 'UNLOAD'",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.user_scope_subquery(company, "USER_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
                                      distinct_where=_scope),
        companies.database_equals_clause(database),
        contains_filter("SCHEMA_NAME", schema_contains),
    )
    return f"""
WITH ev AS (
    SELECT
        QUERY_ID,
        USER_NAME,
        ROLE_NAME,
        START_TIME,
        HOUR(CONVERT_TIMEZONE('America/Chicago', START_TIME)) AS HOUR_OF_DAY,
        DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', START_TIME)) AS DOW_ISO,
        ROUND((COALESCE(BYTES_WRITTEN, 0)
              + COALESCE(BYTES_WRITTEN_TO_RESULT, 0)) / POWER(1024, 3), 3) AS GB_OUT,
        REGEXP_SUBSTR(
            QUERY_TEXT,
            'COPY[[:space:]]+INTO[[:space:]]+([^[:space:]()]+)',
            1, 1, 'ie', 1
        ) AS TARGET_LOCATION,
        LEFT(QUERY_TEXT, 120) AS SAMPLE_TARGET
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where}
)
SELECT
    QUERY_ID,
    USER_NAME,
    ROLE_NAME,
    START_TIME,
    HOUR_OF_DAY,
    DOW_ISO,
    GB_OUT,
    TARGET_LOCATION,
    SAMPLE_TARGET,
    MEDIAN(GB_OUT) OVER (PARTITION BY USER_NAME) AS USER_MEDIAN_GB,
    COUNT(*)       OVER (PARTITION BY USER_NAME) AS USER_UNLOAD_COUNT
FROM ev
QUALIFY ROW_NUMBER() OVER (PARTITION BY USER_NAME ORDER BY GB_OUT DESC) <= 50
ORDER BY GB_OUT DESC
LIMIT 500
"""


def _local_company_clause(company: str, column: str = "COMPANY") -> str:
    value = str(company or "ALL").strip().upper()
    return "" if value == "ALL" else f"(UPPER({column}) = {sql_literal(value)} OR UPPER({column}) = 'ALL')"


def security_exception_queue(company: str = "ALL", limit: int = 100) -> str:
    """V075 local exception queue; no ACCOUNT_USAGE work on first paint.

    The cap is applied PER DOMAIN (QUALIFY ROW_NUMBER() partitioned by DOMAIN), not as a
    single cross-domain LIMIT: this frame also feeds domain_posture scoring, and a global
    top-N would let one noisy arm (e.g. a Terraform-driven CHANGE RISK flood, whose rows
    carry precise recent EVENT_TS and outrank the day-midnight IDENTITY/PRIVILEGE rows
    within a severity tier) evict another domain's findings entirely — scoring the starved
    domain as 100/Healthy, a false all-clear. Per-domain ranking means no arm can crowd out
    another, and the cap is far above where domain_posture's penalty saturates (~20/domain).

    The cap is for SCORING only. The displayed counts (each domain's 'N open', the page verdict's
    'N open finding(s)', the 'Decision queue (n)' badge) read the uncapped totals below: window
    functions in the SELECT are evaluated BEFORE QUALIFY, so they see every queued row — a week of
    ~4,000 CHANGE RISK rows used to read '100 open'. DOMAIN_FINDINGS mirrors domain_posture's
    impact rule (NULL -> 1, floor 1)."""
    limit = max(1, min(int(limit or 100), 300))
    value = str(company or "ALL").strip().upper()
    company_clause = "" if value == "ALL" else (
        f"(UPPER(COMPANY) IN ('ALL', {sql_literal(value)}) "
        f"OR UPPER(COALESCE(ACTOR_COMPANY, '')) = {sql_literal(value)} "
        f"OR UPPER(COALESCE(OBJECT_COMPANY, '')) = {sql_literal(value)})"
    )
    where = and_where(company_clause)
    _sev_order = ("CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 "
                  "WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END")
    return f"""
SELECT DOMAIN, COMPANY, ACTOR_COMPANY, OBJECT_COMPANY,
       ENTITY_TYPE, ENTITY_KEY, SEVERITY, TITLE, DETAIL,
       IMPACT_COUNT, DETECTED_AT, CONFIDENCE, OWNER, ACTION_ID, STATUS,
       COUNT(*) OVER (PARTITION BY DOMAIN) AS DOMAIN_ROWS,
       SUM(GREATEST(COALESCE(IMPACT_COUNT, 1), 1)) OVER (PARTITION BY DOMAIN) AS DOMAIN_FINDINGS,
       COUNT(*) OVER () AS TOTAL_ROWS
FROM {core_object('V_SECURITY_EXCEPTION_QUEUE')}
WHERE {where}
QUALIFY ROW_NUMBER() OVER (PARTITION BY DOMAIN ORDER BY {_sev_order}, DETECTED_AT DESC) <= {limit}
ORDER BY {_sev_order}, DETECTED_AT DESC
"""


# V151 lockstep (tests/migrations/test_v151_security_change_risk_identity_policy_drops.py): the
# CHANGE RISK queue KEEPS a TF_* / DBA_MAINT_DB.PUBLIC DESTRUCTIVE row when its QUERY_TYPE names a
# USER / ROLE / POLICY object or its whitespace-normalized statement opens with one of these
# keyword-anchored DROP openers (4 identity + 13 policy kinds). Byte-equal to the V151 view's
# ILIKE ANY / LIKE ANY lists -- extend both together. Never a bare '%POLICY%' preview substring:
# a TF_* TRUNCATE of an insurance FACT_POLICY table is routine ETL and must stay excluded.
CHANGE_RISK_KEEP_QUERY_TYPES: tuple[str, ...] = ("%USER%", "%ROLE%", "%POLICY%")
CHANGE_RISK_KEEP_PREVIEWS: tuple[str, ...] = (
    "DROP USER %", "DROP ROLE %", "DROP DATABASE ROLE %", "DROP APPLICATION ROLE %",
    "DROP MASKING POLICY %", "DROP ROW ACCESS POLICY %", "DROP NETWORK POLICY %",
    "DROP PASSWORD POLICY %", "DROP SESSION POLICY %", "DROP AUTHENTICATION POLICY %",
    "DROP AGGREGATION POLICY %", "DROP PROJECTION POLICY %", "DROP JOIN POLICY %",
    "DROP PACKAGES POLICY %", "DROP PRIVACY POLICY %", "DROP STORAGE LIFECYCLE POLICY %",
    "DROP BACKUP POLICY %",
)
# The Terraform service-role test, byte-identical to the V088/V151 view's (literal '_' via ~ ESCAPE).
_TF_ROLE_SQL = "UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'"


def _identity_policy_drop_predicate() -> str:
    """The V151 keep test as a boolean SQL expression over FACT_SECURITY_CHANGE columns. The
    patterns are module constants, rendered through sql_literal (never raw text)."""
    types = ", ".join(sql_literal(p) for p in CHANGE_RISK_KEEP_QUERY_TYPES)
    previews = ", ".join(sql_literal(p) for p in CHANGE_RISK_KEEP_PREVIEWS)
    return (f"(COALESCE(QUERY_TYPE, '') ILIKE ANY ({types})"
            " OR LTRIM(REGEXP_REPLACE(UPPER(COALESCE(QUERY_PREVIEW, '')), '[[:space:]]+', ' '))"
            f" LIKE ANY ({previews}))")


def change_risk_destructive_breakdown(days: int = 7) -> str:
    """Diagnostic (owner 2026-08-17): the ACTUAL actors and objects behind the
    CHANGE RISK "DESTRUCTIVE" flood, so an exclusion can be precise instead of a
    guess. Groups the DROP/TRUNCATE rows at RISK_SCORE>=70: the rows the CHANGE RISK
    arm evaluates before its TF_* / app-scratch exclusion (CHANGE_KIND='DESTRUCTIVE',
    trailing window) by role / database / schema / QUERY_TYPE, and flags whether each
    role matches the Terraform service-role convention (TF_*) so it's visible at a
    glance whether a TF_* exclusion would actually clear the noise or whether the
    drivers are something else. DROP_CLASS splits identity / policy drops (the V151
    keep list: DROP USER / ROLE / (kind) POLICY, which the queue keeps even for TF_*
    roles once V151 is applied) from every other object; TF_IDENTITY_POLICY_EVENTS is
    the pre-LIMIT count of TF_* identity / policy drops."""
    days = bounded_days(days, 30)
    keep = _identity_policy_drop_predicate()
    return f"""
SELECT
    COALESCE(NULLIF(TRIM(ROLE_NAME), ''), '(no role attributed)') AS ROLE_NAME,
    COALESCE(NULLIF(TRIM(DATABASE_NAME), ''), '(no database)') AS DATABASE_NAME,
    COALESCE(NULLIF(TRIM(SCHEMA_NAME), ''), '(none)') AS SCHEMA_NAME,
    COALESCE(NULLIF(TRIM(QUERY_TYPE), ''), '(unknown)') AS QUERY_TYPE,
    IFF({keep}, 'identity / policy', 'other object') AS DROP_CLASS,
    IFF({_TF_ROLE_SQL}, 'TF_* service', 'other') AS ROLE_CLASS,
    COUNT(*) AS EVENTS,
    -- Grand total across ALL groups, evaluated BEFORE the LIMIT 200, so the headline
    -- KPI counts every destructive event even when the per-group table is capped.
    SUM(COUNT(*)) OVER () AS TOTAL_EVENTS,
    -- V151: TF_* identity / policy drops the CHANGE RISK queue keeps (pre-LIMIT total).
    SUM(COUNT_IF({_TF_ROLE_SQL} AND {keep})) OVER () AS TF_IDENTITY_POLICY_EVENTS,
    COUNT(DISTINCT USER_NAME) AS USERS,
    MAX(EVENT_TS) AS LAST_SEEN
FROM {core_object('FACT_SECURITY_CHANGE')}
WHERE CHANGE_KIND = 'DESTRUCTIVE'
  AND RISK_SCORE >= 70
  AND EVENT_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY EVENTS DESC
LIMIT 200
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
    WHERE SOURCE_NAME IN ('FACT_SECURITY_CHANGE', 'SECURITY_TRUST_SNAPSHOT', 'OW_QH_EXTRACT')
), qhx AS (
    -- r31: the CHANGE RISK feed loads from OW_QH_EXTRACT, but SP_LOAD_SECURITY_FACTS stamps
    -- FACT_SECURITY_CHANGE OK/now every hour regardless of whether the extract delivered recent
    -- rows — so a STALLED OW_QH_EXTRACT left CHANGE RISK reading COMPLETE/Healthy-100 with no fresh
    -- changes (a false all-clear). Also require the EXTRACT to be fresh. Gating on the extract's
    -- recency (not the change fact's newest row) distinguishes a stalled feed (coverage unknown)
    -- from a legitimately QUIET account (fresh extract, simply no changes = coverage complete).
    SELECT COUNT_IF(SOURCE_NAME = 'OW_QH_EXTRACT'
                    AND COALESCE(STATUS, '') = 'OK'
                    AND SNAPSHOT_TS >= DATEADD('hour', -3, CURRENT_TIMESTAMP())) > 0 AS QHX_FRESH
    FROM fresh
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
           AND SNAPSHOT_TS >= DATEADD('hour', -3, CURRENT_TIMESTAMP())
           AND QHX_FRESH,
           'COMPLETE', IFF(COALESCE(STATUS, '') = 'OK', 'STALE', 'INCOMPLETE')),
       SNAPSHOT_TS
FROM fresh, qhx WHERE SOURCE_NAME = 'FACT_SECURITY_CHANGE'
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
    # COVERAGE_DAYS is DENSITY (COUNT DISTINCT DAY), not the MIN..MAX span: the standing loader
    # backfills only the last few days, so a past multi-day outage leaves a permanent interior
    # gap. A span-based count reports that gap as covered, so fact_coverage_complete would promote
    # a hole-ridden mart over the complete live LOGIN_HISTORY and undercount failed/new-network
    # logins in the gap window (a real attack there goes invisible). Mirrors access_evidence_days
    # (bug-hunt 2026-08-30).
    days = bounded_days(days, maximum=90)
    return f"""
SELECT MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY, COUNT(*) AS FACT_ROWS,
       COUNT(DISTINCT DAY) AS COVERAGE_DAYS,
       MAX(LOAD_TS) AS LAST_LOAD
FROM {core_object('FACT_LOGIN_DAILY')}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
"""


def security_login_fact_coverage(days: int = 30, *, bounds: tuple | None = None, lookback: int = 0) -> str:
    """Coverage contract for the V075 login evidence fact, measured over exactly the span a served
    read covers: the trailing ``days`` window (or the calendar ``bounds``), extended back by
    ``lookback`` baseline days (the new-network panel's 90). Pair it with
    ``logic.security.coverage_required_days(days, bounds, lookback=...)``.

    One 90-day density read used to gate a 7- or 30-day window, so a hole INSIDE the served window
    passed on the other 80+ days and failed logins in the hole silently went uncounted under a mart
    source label. LAST_DAY stays the fact's newest day (freshness), never the window's last.

    COVERAGE_DAYS counts COMPLETE days only (before today), the exact days coverage_required_days
    asks for. The span also holds today, and counting today's partition let it stand in for a missing
    interior day: with today loaded, a 7-day window holed on one day still counted 7 of the 8 days
    it reads and passed. Today is neither required nor counted.

    The first day of a period-to-date window (no baseline) serves today alone: COVERAGE_DAYS is 0
    there and so is the requirement, and freshness decides. LAST_DAY only sees days from the span
    start on, so the fact serves once today's partition is loaded, and the live reader before that."""
    # DENSITY, not span — see login_fact_coverage. An interior gap must deflate COVERAGE_DAYS so
    # fact_coverage_complete keeps the page on the live path until the fact is genuinely dense.
    days = bounded_days(days, maximum=90)
    lookback = max(0, min(int(lookback or 0), NETWORK_BASELINE_DAYS))
    if bounds is not None:
        _start, _end = bounds
        _first = (_start - timedelta(days=lookback)).isoformat()
        # the requirement stops at the earlier of the range end and the account's today (Python
        # account_today), so the count stops there too, on the same Central clock
        return f"""
SELECT MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY, COUNT(*) AS FACT_ROWS,
       COUNT(DISTINCT IFF(DAY < LEAST('{_end.isoformat()}'::DATE, {account_today_sql()}), DAY, NULL))
           AS COVERAGE_DAYS,
       MAX(LOAD_TS) AS LAST_LOAD
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE DAY >= '{_first}'
"""
    return f"""
SELECT MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY, COUNT(*) AS FACT_ROWS,
       COUNT(DISTINCT IFF(DAY < CURRENT_DATE(), DAY, NULL)) AS COVERAGE_DAYS,
       MAX(LOAD_TS) AS LAST_LOAD
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE DAY >= DATEADD('day', -{days + lookback}, CURRENT_DATE())
"""


def failed_logins_fact(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    days = bounded_days(days, maximum=30)
    # the 30d cap holds under calendar bounds too: 'Current year' read a fact the loader purges at 180d
    bounds = capped_window(days, bounds, 30)[1]
    where = and_where(
        scope_window_where("DAY", days, bounds=bounds),
        "FAILURES > 0",
        _local_company_clause(company),
    )
    return f"""
SELECT USER_NAME,
       SUM(FAILURES) AS FAILED_ATTEMPTS,
       -- the loader stores CLIENT_IP as COALESCE(CLIENT_IP,'(none)') (V105), so a bare
       -- COUNT(DISTINCT) would count that sentinel as a real source IP, inflating this
       -- spray signal by 1 vs the live twin (which drops NULL). Exclude it to match.
       COUNT(DISTINCT NULLIF(CLIENT_IP, '(none)')) AS DISTINCT_IPS,
       MAX(LAST_SEEN) AS LAST_ATTEMPT,
       MAX_BY(ERROR_CATEGORY, LAST_SEEN) AS LAST_ERROR
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE {where}
GROUP BY USER_NAME
ORDER BY FAILED_ATTEMPTS DESC
LIMIT 100
"""


def failed_login_reasons_fact(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    days = bounded_days(days, maximum=30)
    # the 30d cap holds under calendar bounds too: 'Current year' read a fact the loader purges at 180d
    bounds = capped_window(days, bounds, 30)[1]
    where = and_where(
        scope_window_where("DAY", days, bounds=bounds),
        "FAILURES > 0",
        _local_company_clause(company),
    )
    return f"""
SELECT ERROR_CATEGORY AS REASON,
       IFF(ERROR_CATEGORY = 'NETWORK POLICY', 'NETWORK POLICY', 'CREDENTIAL / OTHER') AS CATEGORY,
       SUM(FAILURES) AS ATTEMPTS,
       COUNT(DISTINCT USER_NAME) AS USERS,
       -- r31: the loader stores CLIENT_IP as COALESCE(CLIENT_IP,'(none)') (V105), so a bare
       -- COUNT(DISTINCT) counts that sentinel as a real source IP, inflating the spray signal
       -- by 1 vs the live twin failed_login_reasons (raw LOGIN_HISTORY drops NULL). NULLIF it —
       -- the same fix r28b applied to the sibling failed_logins_fact's DISTINCT_IPS.
       COUNT(DISTINCT NULLIF(CLIENT_IP, '(none)')) AS SOURCE_IPS,
       MAX(LAST_SEEN) AS LAST_SEEN
FROM {core_object('FACT_SECURITY_LOGIN_DAILY')}
WHERE {where}
GROUP BY 1, 2
ORDER BY ATTEMPTS DESC
LIMIT 50
"""


def new_network_logins_fact(days: int = 7, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Fact twin of ``new_network_logins``: the same baseline anchored at the window START (window +
    90 days of FACT_SECURITY_LOGIN_DAILY, at most 180 — exactly the loader's retention). The page
    serves it only when ``security_login_fact_coverage(..., lookback=90)`` proves that span dense."""
    days = bounded_days(days, maximum=90)
    bounds = capped_window(days, bounds, 90)[1]
    if bounds is not None:
        _start, _ = bounds
        _hist_start = f"'{(_start - timedelta(days=NETWORK_BASELINE_DAYS)).isoformat()}'"
        _first_seen_scope = resolve_effective_window(days, "f.FIRST_SEEN", bounds=bounds)[1]
        _volume_scope = resolve_effective_window(days, "h.DAY", bounds=bounds)[1]
    else:
        _hist_start = f"DATEADD('day', -{days + NETWORK_BASELINE_DAYS}, CURRENT_DATE())"
        _first_seen_scope = f"f.FIRST_SEEN >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())"
        _volume_scope = f"h.DAY >= DATEADD('day', -{days}, CURRENT_DATE())"
    admin_where = and_where(
        "DELETED_ON IS NULL",
        _admin_roles_in("ROLE", ADMIN_HOLDER_ROLES),
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
    WHERE f.DAY >= {_hist_start}
    GROUP BY 1, 2
)
SELECT f.USER_NAME, f.CLIENT_IP, f.FIRST_SEEN,
       SUM(h.LOGINS) AS LOGINS, SUM(h.SUCCESSES) AS SUCCESSES,
       MAX(h.LAST_SEEN) AS LAST_LOGIN,
       MAX_BY(h.AUTH_FACTOR, h.LAST_SEEN) AS AUTH_FACTOR,
       COUNT(*) OVER () AS TOTAL_PAIRS_WIN
FROM first_seen f
JOIN {core_object('FACT_SECURITY_LOGIN_DAILY')} h
  ON h.USER_NAME = f.USER_NAME AND h.CLIENT_IP = f.CLIENT_IP
 -- Bound the volume sums to the triage window, like the live new_network_logins path (whose pairs'
 -- logins all fall inside it); the fact retains 180 days, so an unbounded join inflated LOGINS /
 -- SUCCESSES for a pair with older activity (bug-hunt 2026-08-30).
 AND {_volume_scope}
WHERE {_first_seen_scope}
GROUP BY 1, 2, 3
ORDER BY f.FIRST_SEEN DESC
LIMIT 200
"""


def _recent_ddl_fact_ctes(days: int, company: str, database: str, schema_contains: str,
                          bounds: tuple | None) -> str:
    """The ``grouped`` + company-``scoped`` CTEs shared by ``recent_ddl_changes_fact`` and its chart
    rollup. Calendar bounds are capped to the 90-day window too (FACT_SECURITY_CHANGE is purged at
    180 days, so an uncapped 'Current year' silently lost its first months)."""
    days = bounded_days(days, maximum=90)
    bounds = capped_window(days, bounds, 90)[1]
    actor_company = companies.user_clause(company, "g.USER_NAME")
    object_company = companies.database_company_scope(company, "g.DATABASE_NAME")
    actor_or_object = (
        f"({actor_company} OR {object_company})"
        if actor_company and object_company else (actor_company or object_company)
    )
    _scope = (resolve_effective_window(days, "f.EVENT_TS", bounds=bounds)[1]
              if bounds is not None
              else f"f.EVENT_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())")
    where = and_where(
        _scope,
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
)"""


def recent_ddl_changes_fact(days: int, company: str = "ALL", database: str = "",
                            schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    ctes = _recent_ddl_fact_ctes(days, company, database, schema_contains, bounds)
    return f"""{ctes}, final AS (
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
)
{_DDL_TOTALS_SELECT}
"""


def recent_ddl_changes_rollup_fact(days: int, company: str = "ALL", database: str = "",
                                   schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """Uncapped chart rollup for the fact-served DDL/DCL panel (same scope and window as
    ``recent_ddl_changes_fact``)."""
    ctes = _recent_ddl_fact_ctes(days, company, database, schema_contains, bounds)
    return f"""{ctes}
{_DDL_ROLLUP_SELECT}
"""


def effective_access(company: str = "ALL") -> str:
    user_filter = companies.user_scope_subquery(company, "g.GRANTEE_NAME", source="SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS",
                                                distinct_where="DELETED_ON IS NULL")
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
), paths AS (
    SELECT r.USER_NAME, r.DIRECT_ROLE, r.EFFECTIVE_ROLE, r.DEPTH, r.ACCESS_PATH,
           COALESCE(p.PRIVILEGES, 0) AS PRIVILEGES,
           COALESCE(p.OWNERSHIP_GRANTS, 0) AS OWNERSHIP_GRANTS,
           COALESCE(p.MANAGE_GRANTS, 0) AS MANAGE_GRANTS,
           COALESCE(p.SENSITIVE_PRIVILEGES, 0) AS SENSITIVE_PRIVILEGES,
           LEAST(100, COALESCE(p.OWNERSHIP_GRANTS, 0) * 10
                      + COALESCE(p.MANAGE_GRANTS, 0) * 25
                      + COALESCE(p.SENSITIVE_PRIVILEGES, 0) * 20) AS RISK_SCORE,
           -- Sec2: does this path inherit an admin role? (the self-escalation surface)
           IFF(r.EFFECTIVE_ROLE IN ('SNOW_ACCOUNTADMINS', 'ACCOUNTADMIN', 'SNOW_SYSADMINS',
                                    'SECURITYADMIN'), TRUE, FALSE) AS REACHES_ADMIN
    FROM role_tree r
    LEFT JOIN privilege_rollup p ON p.ROLE_NAME = r.EFFECTIVE_ROLE
), ranked AS (
    -- each user's own best path: an escalation path (manage grants / admin reach) first
    SELECT x.*,
           ROW_NUMBER() OVER (PARTITION BY x.USER_NAME
                              ORDER BY IFF(x.MANAGE_GRANTS > 0 OR x.REACHES_ADMIN, 0, 1),
                                       x.RISK_SCORE DESC, x.DEPTH) AS USER_PATH_RANK
    FROM paths x
), totals AS (
    -- scope-wide KPI totals over EVERY path, before the 3000-row cap (UNCAPPED-AGGREGATE): the Users,
    -- Effective paths, High-risk users and Can-self-escalate tiles read these, never len() of the cut
    -- frame. High risk = RISK_SCORE >= 70 on some path; self-escalation = MANAGE GRANTS on some path
    -- (logic.security.escalation_flags' SELF_ESCALATION).
    SELECT COUNT(*) AS TOTAL_PATHS_WIN,
           COUNT(DISTINCT USER_NAME) AS TOTAL_PATH_USERS_WIN,
           COUNT(DISTINCT IFF(RISK_SCORE >= 70, USER_NAME, NULL)) AS TOTAL_HIGH_RISK_USERS_WIN,
           COUNT(DISTINCT IFF(MANAGE_GRANTS > 0, USER_NAME, NULL)) AS TOTAL_SELF_ESCALATORS_WIN
    FROM paths
)
SELECT p.USER_NAME, p.DIRECT_ROLE, p.EFFECTIVE_ROLE, p.DEPTH, p.ACCESS_PATH,
       p.PRIVILEGES, p.OWNERSHIP_GRANTS, p.MANAGE_GRANTS, p.SENSITIVE_PRIVILEGES,
       p.RISK_SCORE, p.REACHES_ADMIN, p.USER_PATH_RANK,
       t.TOTAL_PATHS_WIN, t.TOTAL_PATH_USERS_WIN, t.TOTAL_HIGH_RISK_USERS_WIN, t.TOTAL_SELF_ESCALATORS_WIN
FROM ranked p
CROSS JOIN totals t
-- The 3000-row cap can no longer drop evidence on a tie: every user's own best path first (so each
-- in-scope user stays selectable), then every escalation path, then risk. r31 floated a manage path
-- only to a key of 100, which TIED every ownership-heavy RISK_SCORE=100 path, and USER_NAME broke the
-- tie, so a late-alphabet MANAGE GRANTS holder was still cut once that tier passed 3000 rows.
ORDER BY IFF(p.USER_PATH_RANK = 1, 0, 1),
         IFF(p.MANAGE_GRANTS > 0 OR p.REACHES_ADMIN, 0, 1),
         GREATEST(RISK_SCORE, IFF(COALESCE(p.MANAGE_GRANTS, 0) > 0, 100, 0)) DESC,
         p.USER_NAME, p.DEPTH
LIMIT 3000
"""


def egress_baseline(days: int = 30, *, bounds: tuple | None = None) -> str:
    span = max(1, min(int(days or 30), 45))
    if bounds is not None:
        # vs-prior on the calendar window: CURRENT = the given month [start, end),
        # PRIOR = the whole month before it [prior_start, start). BASELINE_DAYS is the
        # current month's span so the label matches the compared window.
        start, end = bounds
        prior_start = (start - timedelta(days=1)).replace(day=1)
        _current_pred = (f"START_TIME >= '{start.isoformat()}' "
                         f"AND START_TIME < '{end.isoformat()}'")
        _prior_pred = (f"START_TIME >= '{prior_start.isoformat()}' "
                       f"AND START_TIME < '{start.isoformat()}'")
        _outer_pred = (f"START_TIME >= '{prior_start.isoformat()}' "
                       f"AND START_TIME < '{end.isoformat()}'")
        _baseline = (end - start).days
    else:
        _current_pred = f"START_TIME >= DATEADD('day', -{span}, CURRENT_TIMESTAMP())"
        _prior_pred = f"START_TIME < DATEADD('day', -{span}, CURRENT_TIMESTAMP())"
        _outer_pred = f"START_TIME >= DATEADD('day', -{span * 2}, CURRENT_TIMESTAMP())"
        _baseline = span
    return f"""
WITH periods AS (
    SELECT COALESCE(TARGET_REGION, '(same region)') AS TARGET_REGION,
           ROUND(SUM(IFF({_current_pred},
                         BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 3) AS CURRENT_GB,
           ROUND(SUM(IFF({_prior_pred},
                         BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 3) AS PRIOR_GB
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
    WHERE {_outer_pred}
      -- TRUE egress only: a same-region internal transfer (both
      -- TARGET_REGION and TARGET_CLOUD NULL) moves no data out of the account, so it must not
      -- be scored as a new/spiking outbound destination on this exfiltration lens (bug-hunt 2026-08-30).
      AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)
    GROUP BY 1
)
SELECT TARGET_REGION, CURRENT_GB, PRIOR_GB, {_baseline} AS BASELINE_DAYS,
       IFF(PRIOR_GB = 0 AND CURRENT_GB > 0, 'NEW',
           IFF(CURRENT_GB > PRIOR_GB * 2 AND CURRENT_GB >= 1, 'SPIKE', 'NORMAL')) AS BEHAVIOR
FROM periods
WHERE CURRENT_GB > 0 OR PRIOR_GB > 0
ORDER BY IFF(BEHAVIOR IN ('NEW', 'SPIKE'), 0, 1), CURRENT_GB DESC
LIMIT 100
"""
