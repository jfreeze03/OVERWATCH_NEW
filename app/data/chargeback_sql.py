"""Department chargeback builders.

Exact usage (not billed): WAREHOUSE_METERING_HISTORY joined to DEPARTMENT_MAP — exact
credits, idle included (a department owns its warehouse's idle time), always
reconciling to the scoped total via the 'Unmapped' bucket. The role lens is
elapsed-share allocation *within* each warehouse and is labeled allocated.
"""

from __future__ import annotations

from app import companies
from app.config import core_object
from app.data.common import and_where, bounded_days, scope_window_where

_DEPT = (
    "COALESCE(D.DEPARTMENT, 'Unmapped')"
)
# A LEFT JOIN straight to DEPARTMENT_MAP fans out — double-counting a warehouse's credits
# across departments — if the map holds >1 WAREHOUSE row for one name. The app MERGE stores
# NAME uppercased and dedups case-sensitively, but a hand-seeded lower/mixed-case or duplicate
# row is legal under the case-SENSITIVE PK yet collides on this case-INSENSITIVE join. Collapse
# the map to ONE row per UPPER(NAME) (latest UPDATED_AT wins) so this money join can't fan out.
_MAP_JOIN = (
    f"LEFT JOIN (\n"
    f"    SELECT NAME, DEPARTMENT, OWNER FROM {core_object('DEPARTMENT_MAP')}\n"
    f"    WHERE MAP_TYPE = 'WAREHOUSE'\n"
    f"    QUALIFY ROW_NUMBER() OVER "
    f"(PARTITION BY UPPER(NAME) ORDER BY UPDATED_AT DESC NULLS LAST, NAME) = 1\n"
    f") D ON UPPER(D.NAME) = UPPER(M.WAREHOUSE_NAME)"
)


def department_window_credits(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Exact credits per department and warehouse for the window.
    Mart-backed (FACT_WAREHOUSE_DAILY), so it honors the long window (v4.54)."""
    days = bounded_days(days, 365)
    # Filter on the fact's evidence-based COMPANY column (COMPANY_FOR_WAREHOUSE at
    # load, honors COMPANY_SCOPE mappings), NOT the WH_ALFA_% name pattern — a
    # warehouse mapped to a company whose NAME doesn't match was silently dropped
    # from the total and never surfaced as UNKNOWN (V044 evidence-based scope).
    company_filter = "" if str(company).upper() in ("ALL", "") else f"M.COMPANY = {companies.sql_literal(company)}"
    where = and_where(
        scope_window_where("M.DAY", days, bounds=bounds),
        company_filter,
    )
    return f"""
SELECT
    {_DEPT} AS DEPARTMENT,
    M.WAREHOUSE_NAME,
    M.COMPANY,
    SUM(COALESCE(M.CREDITS_TOTAL, 0)) AS CREDITS_TOTAL
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY M
{_MAP_JOIN}
WHERE {where}
GROUP BY 1, 2, 3
HAVING SUM(COALESCE(M.CREDITS_TOTAL, 0)) > 0
ORDER BY DEPARTMENT, CREDITS_TOTAL DESC
"""


def role_share_within_warehouse(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Elapsed-time share per (warehouse, role) — multiply by that warehouse's
    exact credits for the allocated role slice.

    Attribution law (v4.34.1): shares are computed over the WHOLE warehouse
    partition first; role visibility (the 2026-07-08 Trexis-leak fix) picks
    display rows AFTER. An excluded role keeps its slice of the denominator,
    so displayed shares can sum below 1 on shared warehouses — the old form
    renormalized to 1 over this company's roles and over-billed them."""
    days = bounded_days(days)
    where = and_where(
        scope_window_where("START_TIME", days, bounds=bounds),
        "WAREHOUSE_NAME IS NOT NULL",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.warehouse_clause(company),
    )
    vis = and_where(companies.role_clause(company, "ROLE_NAME"))
    return f"""
WITH scoped AS (
    SELECT
        WAREHOUSE_NAME,
        COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
        COUNT(*) AS QUERY_COUNT,
        SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS ELAPSED_SEC
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE {where}
    GROUP BY 1, 2
), shared AS (
    SELECT scoped.*,
           RATIO_TO_REPORT(ELAPSED_SEC) OVER (PARTITION BY WAREHOUSE_NAME) AS ELAPSED_SHARE
    FROM scoped
)
SELECT WAREHOUSE_NAME, ROLE_NAME, QUERY_COUNT, ELAPSED_SEC, ELAPSED_SHARE
FROM shared
WHERE {vis}
ORDER BY WAREHOUSE_NAME, ELAPSED_SEC DESC
LIMIT 2000
"""


def department_month_credits(month: str, company: str = "ALL") -> str:
    """Exact per-department/warehouse credits for one calendar month
    (statement export). ``month`` must be YYYY-MM."""
    text = str(month or "").strip()
    if len(text) != 7 or text[4] != "-" or not text.replace("-", "").isdigit():
        raise ValueError(f"month must be YYYY-MM, got {text!r}")
    month_start = f"DATE '{text}-01'"
    # Evidence-based COMPANY column, not the WH name pattern — same fix as
    # department_window_credits (audit #3); the monthly statement must not drop
    # COMPANY_SCOPE-mapped warehouses whose names don't match.
    month_company = "" if str(company).upper() in ("ALL", "") else f"M.COMPANY = {companies.sql_literal(company)}"
    where = and_where(
        f"M.DAY >= {month_start}",
        f"M.DAY < DATEADD('month', 1, {month_start})",
        month_company,
    )
    return f"""
SELECT
    {_DEPT} AS DEPARTMENT,
    COALESCE(D.OWNER, 'Unassigned') AS DEPT_OWNER,
    M.WAREHOUSE_NAME,
    M.COMPANY,
    SUM(COALESCE(M.CREDITS_TOTAL, 0)) AS CREDITS_TOTAL,
    SUM(COALESCE(M.CREDITS_COMPUTE, M.CREDITS_TOTAL)) AS CREDITS_COMPUTE
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY M
{_MAP_JOIN}
WHERE {where}
GROUP BY 1, 2, 3, 4
HAVING SUM(COALESCE(M.CREDITS_TOTAL, 0)) > 0
ORDER BY DEPARTMENT, CREDITS_TOTAL DESC
"""


def department_map() -> str:
    return f"""
SELECT MAP_TYPE, NAME, DEPARTMENT, OWNER, UPDATED_AT, UPDATED_BY
FROM {core_object("DEPARTMENT_MAP")}
ORDER BY MAP_TYPE, DEPARTMENT, NAME
"""


def role_department_map_join(days: int, company: str = "ALL") -> str:
    """Role usage tagged with the role's department (usage lens, allocated)."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "WAREHOUSE_NAME IS NOT NULL",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.warehouse_clause(company),
    )
    return f"""
SELECT
    COALESCE(R.DEPARTMENT, 'Unmapped role') AS ROLE_DEPARTMENT,
    COALESCE(Q.ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
    COUNT(*) AS QUERY_COUNT,
    SUM(COALESCE(Q.TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS ELAPSED_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY Q
LEFT JOIN {core_object("DEPARTMENT_MAP")} R
       ON R.MAP_TYPE = 'ROLE' AND UPPER(R.NAME) = UPPER(Q.ROLE_NAME)
WHERE {where}
GROUP BY 1, 2
ORDER BY ELAPSED_SEC DESC
LIMIT 500
"""
