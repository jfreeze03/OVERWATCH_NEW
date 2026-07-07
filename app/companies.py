"""ALFA / Trexis company scoping — the ONE place tenant names may appear.

ALFA and Trexis share a single Snowflake account, so scoping is deliberately
hardcoded (owner decision, 2026-07). Rules:

- Trexis owns exactly the four ``WH_TRXS_*`` warehouses, the ``TRXS_*``
  database families, and ``TRXS_*`` users. ALFA is the default for everything
  else (including account-level rows with no object context).
- ``KEBARR1`` holds both companies' roles and is classified as **ALFA** by
  explicit policy override.

This scoping is a shared-account convenience filter, not a security boundary;
Snowflake RBAC under Streamlit-in-Snowflake is the boundary. The same rules
are seeded into ``DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE`` by V001 and
``tests/test_companies.py`` keeps code and seed in sync.
"""

from __future__ import annotations

from .core.sqlsafe import assert_no_control_tokens, in_list, like_any, not_in_list, sql_literal

COMPANIES = ("ALFA", "Trexis", "ALL")
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
    return "Trexis" if wh in TREXIS_WAREHOUSES else "ALFA"


def classify_database(name: object) -> str:
    db = str(name or "").strip().upper()
    return "Trexis" if db in TREXIS_DATABASES or db.startswith("TRXS_") else "ALFA"


def classify_user(name: object) -> str:
    # Offline heuristic (name prefix + override). Live scoping is role-based
    # via COMPANY_FOR_USER — see user_clause. Kept for the seed-sync test and
    # any Python-side labeling where role data isn't queryable.
    user = str(name or "").strip().upper()
    if user in USER_COMPANY_OVERRIDES:
        return USER_COMPANY_OVERRIDES[user]
    return "Trexis" if user.startswith(TREXIS_USER_PREFIX) else "ALFA"


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
        clause = not_in_list(column, TREXIS_WAREHOUSES)
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
    if company in ("Trexis", "ALFA"):
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


def database_options(company: str) -> tuple[str, ...]:
    """Known databases for the sidebar picker, scoped to the company."""
    company = str(company or DEFAULT_COMPANY)
    if company == "Trexis":
        return TREXIS_DATABASES
    if company == "ALFA":
        return ALFA_DATABASES
    return tuple(dict.fromkeys((*ALFA_DATABASES, *TREXIS_DATABASES)))


def database_equals_clause(database: str, column: str = "DATABASE_NAME") -> str:
    """Exact-match clause for the selected database ('' = no filter)."""
    db = str(database or "").strip()
    if not db:
        return ""
    return assert_no_control_tokens(in_list(column, [db]))


def company_case_sql(warehouse_col: str = "WAREHOUSE_NAME") -> str:
    """CASE expression labeling rows by company for the ALL view."""
    literals = ", ".join(sql_literal(w) for w in TREXIS_WAREHOUSES)
    return f"CASE WHEN UPPER({warehouse_col}) IN ({literals}) THEN 'Trexis' ELSE 'ALFA' END"
