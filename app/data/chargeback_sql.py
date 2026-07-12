"""Department chargeback builders.

Billing truth: WAREHOUSE_METERING_HISTORY joined to DEPARTMENT_MAP — exact
credits, idle included (a department owns its warehouse's idle time), always
reconciling to the scoped total via the 'Unmapped' bucket. The role lens is
elapsed-share allocation *within* each warehouse and is labeled allocated.
"""

from __future__ import annotations

from app import companies
from app.config import core_object
from app.data.common import and_where, bounded_days

_DEPT = (
    "COALESCE(D.DEPARTMENT, 'Unmapped')"
)
_MAP_JOIN = (
    f"LEFT JOIN {core_object('DEPARTMENT_MAP')} D "
    "ON D.MAP_TYPE = 'WAREHOUSE' AND UPPER(D.NAME) = UPPER(M.WAREHOUSE_NAME)"
)


def department_window_credits(days: int, company: str = "ALL") -> str:
    """Exact credits per department and warehouse for the window."""
    days = bounded_days(days)
    where = and_where(
        f"M.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company, "M.WAREHOUSE_NAME"),
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


def role_share_within_warehouse(days: int, company: str = "ALL") -> str:
    """Elapsed-time share per (warehouse, role) — multiply by that warehouse's
    exact credits for the allocated role slice. Shares sum to 1 per warehouse."""
    days = bounded_days(days)
    where = and_where(
        f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())",
        "WAREHOUSE_NAME IS NOT NULL",
        "EXECUTION_STATUS = 'SUCCESS'",
        companies.warehouse_clause(company),
        # Role-grain leak fix (2026-07-08): Trexis roles on shared warehouses
        # must not appear under an ALFA scope.
        companies.role_clause(company),
    )
    return f"""
SELECT
    WAREHOUSE_NAME,
    COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
    COUNT(*) AS QUERY_COUNT,
    SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS ELAPSED_SEC,
    RATIO_TO_REPORT(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)))
        OVER (PARTITION BY WAREHOUSE_NAME) AS ELAPSED_SHARE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE {where}
GROUP BY 1, 2
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
    where = and_where(
        f"M.DAY >= {month_start}",
        f"M.DAY < DATEADD('month', 1, {month_start})",
        companies.warehouse_clause(company, "M.WAREHOUSE_NAME"),
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
