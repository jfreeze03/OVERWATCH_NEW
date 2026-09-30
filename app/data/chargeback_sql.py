"""Department chargeback builders.

Exact usage (not billed): WAREHOUSE_METERING_HISTORY joined to DEPARTMENT_MAP — exact
credits, idle included (a department owns its warehouse's idle time), always
reconciling to the scoped total via the 'Unmapped' bucket. The role lens is
elapsed-share allocation *within* each warehouse and is labeled allocated.
"""

from __future__ import annotations

from app import companies
from app.config import MAX_MART_WINDOW_DAYS, core_object, mart_object
from app.core.sqlsafe import sql_literal
from app.data.common import account_today_sql, and_where, bounded_days, scope_window_where
from app.logic.showback import COCO_SOURCES, SERVERLESS_ARMS, STORAGE_TIERS

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
    renormalized to 1 over this company's roles and over-billed them.

    Population is ALL statuses (round 19): the warehouse credits this share
    multiplies include failed/timed-out queries' compute, so their elapsed time
    must sit in the denominator too — a role whose long ETL loads time out would
    otherwise be under-charged. This matches the mart twin mart27_sql.role_share,
    whose EXEC_SEC (FACT_QUERY_ROLE_HOURLY) is loaded with no status filter, so
    the allocated $ no longer flips when the panel falls to this live path."""
    days = bounded_days(days)
    where = and_where(
        scope_window_where("START_TIME", days, bounds=bounds),
        "WAREHOUSE_NAME IS NOT NULL",
        companies.warehouse_company_scope(company),
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


def company_allin_showback(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Company all-in showback (#42 Part 1): marts only, one long LINE_KIND frame for
    ``app.logic.showback.company_showback``.

    Every credit leg is cut to ``mdays``: the Window's days that daily metering has closed.
    The newest FACT_METERING_DAILY row is the UTC day still in progress when the loader read
    it, so it is excluded, and today never counts. The storage legs share ``sdays``: the
    account-storage days among those metered days, so a day daily metering skipped is left
    out of every row. COVERAGE rows carry each fact's ledger-wide first/last day (the coverage
    notes name a late or stale source from them). The object-cost and Cortex Code COVERAGE rows
    also carry LOADED_AT, the fact's last load (MAX(LOAD_TS), TIMESTAMP_NTZ Central wall time):
    those facts reload in their own daily runs after the 06:45 CT metering load has moved
    yesterday into the span, so until they run, the span's newest day holds only the part
    loaded the morning before, and the notes say so (R1-13). COMPANY_FOR_USER runs on the grouped user in
    a derived-table projection with the company test in an outer WHERE (the V030 shape law, as
    in mart27_sql.live_monthly_spend_by_warehouse). The row count is bounded: tens of service
    types, a few companies x five serverless arms, one row per Cortex Code user, six storage
    tiers and six coverage rows.
    """
    days = bounded_days(days, MAX_MART_WINDOW_DAYS)
    today = account_today_sql()
    if bounds is not None:
        start, end = bounds
        w_lo = f"'{start.isoformat()}'::DATE"
        w_hi = f"LEAST('{end.isoformat()}'::DATE, {today})"
    else:
        w_lo = f"DATEADD('day', -{days}, {today})"
        w_hi = today
    scoped = str(company or "ALL").upper() not in ("ALL", "")
    lit = companies.sql_literal(company)

    def co(column: str) -> str:
        return f"{column} = {lit}" if scoped else ""

    arms = ", ".join(sql_literal(x) for x in SERVERLESS_ARMS)
    srcs = ", ".join(sql_literal(x) for x in COCO_SOURCES)
    metering_fact = mart_object("FACT_METERING_DAILY")
    warehouse_fact = mart_object("FACT_WAREHOUSE_DAILY")
    object_cost_fact = mart_object("FACT_OBJECT_COST_DAILY")
    ai_fact = mart_object("FACT_AI_USAGE_DAILY")
    storage_db_fact = mart_object("FACT_STORAGE_DAILY")
    storage_acct_fact = mart_object("FACT_STORAGE_ACCOUNT_DAILY")
    tier_sums = ",\n        ".join(
        f"SUM(COALESCE(t.{tier}_BYTES, 0) / POWER(1024, 4) / DAY(LAST_DAY(t.DAY))) AS {tier}_TIB_MO"
        for tier in STORAGE_TIERS)
    tier_rows = "\n".join(
        f"UNION ALL SELECT 'STORAGE_ACCT', NULL, '{tier}', NULL, NULL, a.{tier}_TIB_MO, NULL, NULL, NULL, NULL "
        f"FROM stor_acct a WHERE a.N_DAYS > 0"
        for tier in STORAGE_TIERS)
    # LOADED_AT only on the two keyed facts that reload after the metering load (R1-13); the
    # warehouse fact loads in the metering run itself and storage is one snapshot per day
    last_load = "MAX(x.LOAD_TS)::TIMESTAMP_NTZ"
    coverage_rows = "\n".join(
        f"UNION ALL SELECT 'COVERAGE', NULL, '{name}', NULL, NULL, NULL, MIN(x.DAY), MAX(x.DAY), NULL, {loaded} "
        f"FROM {table} x{where}"
        for name, table, where, loaded in (
            ("FACT_WAREHOUSE_DAILY", warehouse_fact, "", "NULL"),
            ("FACT_OBJECT_COST_DAILY", object_cost_fact, "", last_load),
            ("FACT_AI_USAGE_DAILY", ai_fact, f" WHERE x.SOURCE IN ({srcs})", last_load),
            ("FACT_STORAGE_DAILY", storage_db_fact, "", "NULL"),
            ("FACT_STORAGE_ACCOUNT_DAILY", storage_acct_fact, "", "NULL"),
        ))
    return f"""
WITH win AS (
    SELECT {w_lo} AS W_LO, {w_hi} AS W_HI
), mcov AS (
    SELECT MIN(m.DAY) AS M_FIRST, MAX(m.DAY) AS M_LAST FROM {metering_fact} m
), span AS (
    SELECT GREATEST(w.W_LO, c.M_FIRST) AS LO, LEAST(w.W_HI, c.M_LAST) AS HI
    FROM win w CROSS JOIN mcov c
), mdays AS (
    SELECT DISTINCT m.DAY FROM {metering_fact} m CROSS JOIN span s
    WHERE m.DAY >= s.LO AND m.DAY < s.HI
), sdays AS (
    SELECT DISTINCT t.DAY FROM {storage_acct_fact} t JOIN mdays d ON d.DAY = t.DAY
), metering AS (
    SELECT UPPER(COALESCE(m.SERVICE_TYPE, 'UNKNOWN')) AS SERVICE_TYPE,
           SUM(COALESCE(m.CREDITS_BILLED, 0)) AS CREDITS,
           SUM(COALESCE(m.CREDITS_ADJUSTMENT, 0)) AS CREDITS_ADJUSTMENT
    FROM {metering_fact} m JOIN mdays d ON d.DAY = m.DAY
    GROUP BY 1
), wh AS (
    SELECT w.COMPANY, SUM(COALESCE(w.CREDITS_TOTAL, 0)) AS CREDITS
    FROM {warehouse_fact} w JOIN mdays d ON d.DAY = w.DAY
    WHERE {and_where(co("w.COMPANY"))}
    GROUP BY w.COMPANY
), sl AS (
    SELECT COALESCE(o.COMPANY, 'UNKNOWN') AS COMPANY, o.COST_ARM, SUM(COALESCE(o.CREDITS, 0)) AS CREDITS
    FROM {object_cost_fact} o JOIN mdays d ON d.DAY = o.DAY
    WHERE {and_where(f"o.COST_ARM IN ({arms})", co("COALESCE(o.COMPANY, 'UNKNOWN')"))}
    GROUP BY 1, 2
), coco_user AS (
    SELECT a.USER_NAME, SUM(COALESCE(a.CREDITS, 0)) AS CREDITS
    FROM {ai_fact} a JOIN mdays d ON d.DAY = a.DAY
    WHERE a.SOURCE IN ({srcs})
    GROUP BY a.USER_NAME
), coco AS (
    SELECT c.COMPANY, c.CREDITS
    FROM (
        SELECT u.CREDITS, {companies.COMPANY_FOR_USER_FN}(u.USER_NAME) AS COMPANY
        FROM coco_user u
    ) c
    WHERE {and_where(co("c.COMPANY"))}
), stor_db AS (
    SELECT sd.COMPANY,
           SUM((COALESCE(sd.DB_BYTES, 0) + COALESCE(sd.FAILSAFE_BYTES, 0)) / POWER(1024, 4)
               / DAY(LAST_DAY(sd.DAY))) AS TIB_MO
    FROM {storage_db_fact} sd JOIN sdays d ON d.DAY = sd.DAY
    WHERE {and_where(co("sd.COMPANY"))}
    GROUP BY sd.COMPANY
), stor_acct AS (
    SELECT COUNT(*) AS N_DAYS,
        {tier_sums}
    FROM {storage_acct_fact} t JOIN sdays d ON d.DAY = t.DAY
)
SELECT 'WINDOW' AS LINE_KIND, NULL::VARCHAR AS COMPANY, NULL::VARCHAR AS SERVICE_TYPE,
       NULL::FLOAT AS CREDITS, NULL::FLOAT AS CREDITS_ADJUSTMENT, NULL::FLOAT AS TIB_MO,
       w.W_LO AS FIRST_DAY, DATEADD('day', -1, w.W_HI) AS LAST_DAY, NULL::FLOAT AS DAYS_IN_SPAN,
       NULL::TIMESTAMP_NTZ AS LOADED_AT
FROM win w
UNION ALL SELECT 'SPAN', NULL, NULL, NULL, NULL, NULL, MIN(d.DAY), MAX(d.DAY), COUNT(*), NULL FROM mdays d
UNION ALL SELECT 'STORAGE_SPAN', NULL, NULL, NULL, NULL, NULL, MIN(d.DAY), MAX(d.DAY), COUNT(*), NULL FROM sdays d
UNION ALL SELECT 'METERING', NULL, m.SERVICE_TYPE, m.CREDITS, m.CREDITS_ADJUSTMENT, NULL, NULL, NULL, NULL, NULL
FROM metering m
UNION ALL SELECT 'WAREHOUSE', w.COMPANY, NULL, w.CREDITS, NULL, NULL, NULL, NULL, NULL, NULL FROM wh w
UNION ALL SELECT 'SERVERLESS', s.COMPANY, s.COST_ARM, s.CREDITS, NULL, NULL, NULL, NULL, NULL, NULL FROM sl s
UNION ALL SELECT 'COCO', c.COMPANY, NULL, c.CREDITS, NULL, NULL, NULL, NULL, NULL, NULL FROM coco c
UNION ALL SELECT 'STORAGE_DB', sd.COMPANY, NULL, NULL, NULL, sd.TIB_MO, NULL, NULL, NULL, NULL FROM stor_db sd
{tier_rows}
UNION ALL SELECT 'COVERAGE', NULL, 'FACT_METERING_DAILY', NULL, NULL, NULL, c.M_FIRST, c.M_LAST, NULL, NULL
FROM mcov c
{coverage_rows}
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
        companies.warehouse_company_scope(company),
    )
    return f"""
SELECT
    COALESCE(R.DEPARTMENT, 'Unmapped role') AS ROLE_DEPARTMENT,
    COALESCE(Q.ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
    COUNT(*) AS QUERY_COUNT,
    SUM(COALESCE(Q.TOTAL_ELAPSED_TIME, 0)) / 1000.0 AS ELAPSED_SEC
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY Q
-- Collapse the ROLE map to ONE row per UPPER(NAME) before the join (latest UPDATED_AT
-- wins), exactly as _MAP_JOIN does for WAREHOUSE: a raw LEFT JOIN fans out — doubling
-- this role's QUERY_COUNT / ELAPSED_SEC — if two case-variant ROLE rows (legal under the
-- case-sensitive PK) collide on the case-insensitive join. (bug-hunt round 5)
LEFT JOIN (
    SELECT NAME, DEPARTMENT FROM {core_object("DEPARTMENT_MAP")}
    WHERE MAP_TYPE = 'ROLE'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(NAME) ORDER BY UPDATED_AT DESC NULLS LAST, NAME) = 1
) R ON UPPER(R.NAME) = UPPER(Q.ROLE_NAME)
WHERE {where}
GROUP BY 1, 2
ORDER BY ELAPSED_SEC DESC
LIMIT 500
"""
