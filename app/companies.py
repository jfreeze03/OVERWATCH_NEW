"""ALFA / Trexis company scoping — the ONE place tenant names may appear.

ALFA and Trexis share a single Snowflake account, so scoping is deliberately
hardcoded (owner decision, 2026-07). Rules:

- Trexis owns the ``WH_TRXS_*`` warehouses, the ``TRXS_*`` database
  families, and users holding ``%TRXS%`` roles. Since V044 (#18, owner
  2026-07-13) ALFA needs evidence too — ``WH_ALFA_*`` names, ``ALFA%``/
  ``ADMIN`` databases, ``%ALFA%`` or DBA roles. Anything with no evidence
  classifies **UNKNOWN** and surfaces on Cost -> Chargeback for an explicit
  COMPANY_SCOPE mapping before it's charged to anyone.
- ``KEBARR1`` holds both companies' roles and is classified as **ALFA** by
  explicit policy override.

This scoping is a shared-account convenience filter, not a security boundary;
Snowflake RBAC under Streamlit-in-Snowflake is the boundary. The same rules
are seeded into ``DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE`` by V001 and
``tests/test_companies.py`` keeps code and seed in sync.
"""

from __future__ import annotations

from .core.sqlsafe import assert_no_control_tokens, in_list, like_any, not_in_list, sql_literal

COMPANIES = ("ALFA", "Trexis", "UNKNOWN", "ALL")
DEFAULT_COMPANY = "ALFA"

TREXIS_WAREHOUSES = (
    "WH_TRXS_LOAD",
    "WH_TRXS_QUERY",
    "WH_TRXS_TRANSFORM",
    "WH_TRXS_UNLOAD",
    "WH_TRXS_LINEAGE",
)

TREXIS_DATABASES = (
    "TRXS_ABC_METADATA_DEV",
    "TRXS_ABC_METADATA_PRD",
    "TRXS_ABC_METADATA_SIT",
    "TRXS_EDW_DEV",
    "TRXS_EDW_PRD",
    "TRXS_EDW_SIT",
    "TRXS_GW_DATA_DEV",
    "TRXS_GW_DATA_PRD",
    "TRXS_GW_DATA_SIT",
)

TREXIS_USER_PREFIX = "TRXS_"

# Users whose company differs from what prefix rules would say.
# KEBARR1 holds both ALFA and Trexis roles; policy: treated as ALFA.
USER_COMPANY_OVERRIDES = {
    "KEBARR1": "ALFA",
}

ALFA_DATABASES = (
    "ALFA_EDW_PRD",
    "ALFA_EDW_MGM",
    "ALFA_EDW_DEV",
    "ALFA_EDW_SAN",
    "ALFA_EDW_PHX",
    "ALFA_EDW_SEA",
    "ALFA_EDW_SIT",
    "ADMIN",
)
ALFA_DATABASE_PATTERNS = ("ALFA%", "ADMIN")

ENVIRONMENTS = ("ALL", "PROD", "NONPROD")
DEFAULT_ENVIRONMENT = "ALL"
_PROD_DB_EXACT = ("ALFA_EDW_PRD", "ALFA_EDW_MGM")
_PROD_DB_SUFFIX = ("_PRD",)


# ---------------------------------------------------------------------------
# Python-side classification (for tagging frames already in memory)
# ---------------------------------------------------------------------------

def classify_warehouse(name: object) -> str:
    wh = str(name or "").strip().upper()
    if wh in TREXIS_WAREHOUSES:
        return "Trexis"
    return "ALFA" if wh.startswith("WH_ALFA_") else "UNKNOWN"


def classify_database(name: object) -> str:
    db = str(name or "").strip().upper()
    if db in TREXIS_DATABASES or db.startswith("TRXS_"):
        return "Trexis"
    if db.startswith("ALFA") or db in ("ADMIN", "DBA_MAINT_DB"):
        return "ALFA"      # DBA_MAINT_DB: app infra, seeded ALFA in V044
    return "UNKNOWN"


def classify_databases(names, company: str = "ALL", environment: str = "ALL") -> tuple:
    """Filter a LIVE database inventory (SHOW DATABASES names) to a company and
    environment using the same rules as classify_database / classify_environment
    (item 8c, 2026-07-14). Lets the picker track real inventory instead of the
    hardcoded lists; databases_for() stays the offline fallback."""
    comp = str(company or DEFAULT_COMPANY)
    env = str(environment or "ALL").upper()
    out = []
    for n in names:
        name = str(n or "").strip()
        if not name:
            continue
        if comp not in ("ALL", "") and classify_database(name) != comp:
            continue
        if env in ("PROD", "NONPROD") and classify_environment(name) != env:
            continue
        out.append(name.upper())
    return tuple(sorted(dict.fromkeys(out)))


def classify_user(name: object) -> str:
    # Offline heuristic (name prefix + override). Live scoping is role-based
    # via COMPANY_FOR_USER — see user_clause. Kept for the seed-sync test and
    # any Python-side labeling where role data isn't queryable.
    user = str(name or "").strip().upper()
    if user in USER_COMPANY_OVERRIDES:
        return USER_COMPANY_OVERRIDES[user]
    if user.startswith(TREXIS_USER_PREFIX):
        return "Trexis"
    return "UNKNOWN"      # role evidence lives server-side (COMPANY_FOR_USER)


def classify_environment(database: object) -> str:
    db = str(database or "").strip().upper()
    if db in _PROD_DB_EXACT or any(db.endswith(sfx) for sfx in _PROD_DB_SUFFIX):
        return "PROD"
    return "NONPROD"


# ---------------------------------------------------------------------------
# SQL clause builders (validated; return '' for the ALL scope)
# ---------------------------------------------------------------------------

def warehouse_clause(company: str, column: str = "WAREHOUSE_NAME") -> str:
    company = str(company or DEFAULT_COMPANY)
    if company == "Trexis":
        clause = in_list(column, TREXIS_WAREHOUSES)
    elif company == "ALFA":
        clause = f"(UPPER(COALESCE({column}, '')) LIKE 'WH!_ALFA!_%' ESCAPE '!')"
    elif company == "UNKNOWN":
        exclude = not_in_list(column, TREXIS_WAREHOUSES)
        clause = f"({exclude} AND UPPER(COALESCE({column}, '')) NOT LIKE 'WH!_ALFA!_%' ESCAPE '!')"
    else:
        clause = ""
    return assert_no_control_tokens(clause)


def database_clause(company: str, column: str = "DATABASE_NAME") -> str:
    company = str(company or DEFAULT_COMPANY)
    if company == "Trexis":
        clause = like_any(column, (*TREXIS_DATABASES, "TRXS_%"))
    elif company == "ALFA":
        include = like_any(column, ALFA_DATABASE_PATTERNS)
        exclude = not_in_list(column, TREXIS_DATABASES)
        clause = f"({include} AND {exclude})" if include and exclude else include or exclude
    elif company == "UNKNOWN":
        clause = (f"(UPPER(COALESCE({column}, '')) NOT LIKE 'TRXS!_%' ESCAPE '!' "
                  f"AND UPPER(COALESCE({column}, '')) NOT LIKE 'ALFA%' "
                  f"AND UPPER(COALESCE({column}, '')) NOT IN ('ADMIN', 'DBA_MAINT_DB'))")
    else:
        clause = ""
    return assert_no_control_tokens(clause)


# The account's Trexis users have ordinary names (e.g. SSLONSKY) and
# @trexis.com emails — they are NOT prefixed TRXS_. They are identified by
# holding a role that carries _TRXS_ (e.g. SNOW_PRI_GFR_PRD_TRXS_DATA_TEAM).
# COMPANY_FOR_USER (V019) resolves that by role membership with the KEBARR1
# ALFA override baked in, so every user-grained scope routes through it —
# one source of truth, and it passes the injection gate (no subquery text).
COMPANY_FOR_USER_FN = "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER"


def user_clause(company: str, column: str = "USER_NAME") -> str:
    """Company scope for user-grained sources, by role membership."""
    company = str(company or DEFAULT_COMPANY)
    if company in ("Trexis", "ALFA", "UNKNOWN"):
        clause = f"{COMPANY_FOR_USER_FN}({column}) = {sql_literal(company)}"
    else:
        clause = ""
    return assert_no_control_tokens(clause)


def environment_clause(environment: str, column: str = "DATABASE_NAME") -> str:
    env = str(environment or DEFAULT_ENVIRONMENT).upper()
    # '!' as the LIKE escape char keeps the underscore literal without
    # backslash-escaping ambiguity across clients.
    if env == "PROD":
        exact = in_list(column, _PROD_DB_EXACT)
        clause = f"({exact} OR UPPER({column}) LIKE '%!_PRD' ESCAPE '!')"
    elif env == "NONPROD":
        exact = not_in_list(column, _PROD_DB_EXACT, allow_null=False)
        clause = f"({exact} AND UPPER({column}) NOT LIKE '%!_PRD' ESCAPE '!')"
    else:
        clause = ""
    return assert_no_control_tokens(clause)


def role_clause(company: str, column: str = "ROLE_NAME") -> str:
    """Company scope for ROLE-grain rows (heuristic: _TRXS_ in the name).

    Live finding 2026-07-08: Trexis Terraform roles (TF_O_TRXS_SYSADMIN_*)
    topped ALFA's role-usage chart because they run on shared/default
    warehouses that classify ALFA. Role names are the only company signal
    at role grain; mirrors classify_user's prefix heuristic.
    """
    company = str(company or DEFAULT_COMPANY)
    if company == "Trexis":
        clause = f"UPPER({column}) LIKE '%TRXS%'"
    elif company == "ALFA":
        clause = (f"(UPPER(COALESCE({column}, '')) LIKE '%ALFA%' "
                  f"OR UPPER(COALESCE({column}, '')) IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'))")
    elif company == "UNKNOWN":
        clause = (f"(COALESCE(UPPER({column}), '') NOT LIKE '%TRXS%' "
                  f"AND COALESCE(UPPER({column}), '') NOT LIKE '%ALFA%' "
                  f"AND COALESCE(UPPER({column}), '') NOT IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'))")
    else:
        clause = ""
    return assert_no_control_tokens(clause)


def database_visibility_clause(company: str, column: str = "DATABASE_NAME") -> str:
    """Which DATABASE-grain rows belong in a company's lens (live finding
    2026-07-11: USER$SSLONSKY — a Trexis user's personal database — showed
    under ALFA because attribution scoped company at WAREHOUSE grain only).

    Semantics: exclude the OTHER company's databases rather than pattern-
    matching our own (DBA_MAINT_DB and other shared/unclassified databases
    stay visible). Personal ``USER$<name>`` databases attribute to their
    OWNER's company via COMPANY_FOR_USER. NULL / 'NONE' (no database
    context) stays visible everywhere — that activity is real."""
    company = str(company or DEFAULT_COMPANY)
    if company not in ("ALFA", "Trexis"):
        return ""
    literals = ", ".join(sql_literal(d) for d in TREXIS_DATABASES)
    trexish = (f"(UPPER({column}) IN ({literals}) "
               f"OR UPPER({column}) LIKE 'TRXS!_%' ESCAPE '!')")
    personal_owner = f"{COMPANY_FOR_USER_FN}(SUBSTR({column}, 6))"
    if company == "Trexis":
        clause = (f"({column} IS NULL OR UPPER({column}) = 'NONE' OR {trexish} "
                  f"OR (UPPER({column}) LIKE 'USER$%' AND {personal_owner} = 'Trexis'))")
    else:
        clause = (f"({column} IS NULL OR UPPER({column}) = 'NONE' "
                  f"OR (NOT {trexish} AND (UPPER({column}) NOT LIKE 'USER$%' "
                  f"OR {personal_owner} = 'ALFA')))")
    return assert_no_control_tokens(clause)


def database_options(company: str) -> tuple[str, ...]:
    """Known databases for the sidebar picker, scoped to the company."""
    company = str(company or DEFAULT_COMPANY)
    if company == "Trexis":
        return TREXIS_DATABASES
    if company == "ALFA":
        return ALFA_DATABASES
    return tuple(dict.fromkeys((*ALFA_DATABASES, *TREXIS_DATABASES)))


def databases_for(company: str, environment: str = "ALL") -> tuple[str, ...]:
    """Database picker options scoped to company AND environment.

    The picker offering DEV/SAN databases while the Environment filter said
    PROD was a live finding (2026-07-08): ALFA + PROD must be exactly
    (ALFA_EDW_PRD, ALFA_EDW_MGM). Uses the same classify_environment rules
    as the SQL environment_clause so the list and the filter cannot drift.
    """
    env = str(environment or DEFAULT_ENVIRONMENT).upper()
    dbs = database_options(company)
    if env not in ("PROD", "NONPROD"):
        return dbs
    return tuple(db for db in dbs if classify_environment(db) == env)


def database_equals_clause(database: str, column: str = "DATABASE_NAME") -> str:
    """Exact-match clause for the selected database ('' = no filter)."""
    db = str(database or "").strip()
    if not db:
        return ""
    return assert_no_control_tokens(in_list(column, [db]))


def database_case_sql(database_col: str = "DATABASE_NAME") -> str:
    """CASE labeling rows by company at DATABASE grain. company_case_sql
    tests membership in the WAREHOUSE list — applied to a database column it
    labeled every row ALFA (the storage-movers bug, review #7)."""
    return f"DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE({database_col})"


def company_case_sql(warehouse_col: str = "WAREHOUSE_NAME") -> str:
    """Company label for the ALL view via the evidence-based UDF (item 8b,
    2026-07-14) so the live path matches the marts: COMPANY_SCOPE lookup, then
    WH_ALFA_* -> ALFA, else UNKNOWN. Superseded the old 'Trexis else ALFA'
    CASE that mislabeled every residual/unmapped warehouse as ALFA."""
    return f"DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE({warehouse_col})"
