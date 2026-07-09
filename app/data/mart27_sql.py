"""Readers for the V027 mart family (docs/design/V027_MART_FAMILY.md).

Thin SELECTs only — the loaders own the math. Adoption pattern: panels go
fact-first through these builders with their live builders kept as labeled
fallback (the Control Room v4.8.2 pattern), wave 2 after the marts hold data.
Every builder here is canaried so ACCOUNT_USAGE/mart drift pages the error
log before it pages a user.
"""

from __future__ import annotations

from app.config import mart_object
from app.core.sqlsafe import sql_literal
from app.data.common import and_where, bounded_days


def _company_arm(company: str, column: str = "COMPANY") -> str:
    if str(company or "ALL").upper() == "ALL":
        return ""
    return f"{column} = {sql_literal(company)}"


def warehouse_efficiency(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, 400)
    where = and_where(f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      _company_arm(company))
    return f"""
SELECT DAY, WAREHOUSE_NAME, COMPANY, CREDITS_TOTAL, CREDITS_COMPUTE, QUERIES, FAILS,
       QUEUED_MIN, SPILL_GB, P95_S, EXEC_HOURS, BILLED_HOURS, ACTIVE_HOURS,
       IDLE_PCT, CREDITS_PER_QUERY
FROM {mart_object("MART_WAREHOUSE_EFFICIENCY_DAILY")}
WHERE {where}
ORDER BY DAY, CREDITS_TOTAL DESC
LIMIT 5000
"""


def query_families(days: int, limit: int = 200) -> str:
    days = bounded_days(days, 400)
    limit = max(10, min(int(limit or 200), 2000))
    return f"""
SELECT DAY, QUERY_HASH, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES,
       DATABASE_NAME, SCHEMA_NAME, TOTAL_EXEC_SEC, MEDIAN_S, P95_S,
       COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS
FROM {mart_object("MART_QUERY_FAMILY_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY TOTAL_EXEC_SEC DESC
LIMIT {limit}
"""


def role_hourly(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, 400)
    where = and_where(f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
                      _company_arm(company))
    return f"""
SELECT HOUR_TS, ROLE_NAME, WAREHOUSE_NAME, COMPANY, QUERIES, FAILS, EXEC_SEC
FROM {mart_object("FACT_QUERY_ROLE_HOURLY")}
WHERE {where}
ORDER BY HOUR_TS
LIMIT 20000
"""


def schema_hourly(days: int, company: str = "ALL", database: str = "") -> str:
    days = bounded_days(days, 400)
    parts = [f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
             _company_arm(company)]
    if str(database or "").strip():
        parts.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
    return f"""
SELECT HOUR_TS, DATABASE_NAME, SCHEMA_NAME, COMPANY, QUERIES, FAILS,
       QUEUED_SEC, SPILL_GB, P95_S
FROM {mart_object("FACT_QUERY_SCHEMA_HOURLY")}
WHERE {and_where(*parts)}
ORDER BY HOUR_TS
LIMIT 20000
"""


def cost_allocation(days: int, dimension: str, company: str = "ALL") -> str:
    days = bounded_days(days, 400)
    dim = str(dimension or "USER").upper()
    if dim not in ("USER", "DATABASE", "SCHEMA", "ROLE"):
        raise ValueError(f"dimension must be USER/DATABASE/SCHEMA/ROLE, got {dimension!r}")
    where = and_where(f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      f"DIMENSION = {sql_literal(dim)}",
                      _company_arm(company))
    return f"""
SELECT DAY, DIMENSION, KEY_NAME, COMPANY, ALLOC_CREDITS, EXEC_SEC
FROM {mart_object("MART_COST_ALLOCATION_DAILY")}
WHERE {where}
ORDER BY ALLOC_CREDITS DESC
LIMIT 5000
"""


def task_graphs(days: int, company_database: str = "") -> str:
    days = bounded_days(days, 400)
    parts = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company_database or "").strip():
        parts.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(company_database).upper())}")
    return f"""
SELECT DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES,
       TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS
FROM {mart_object("MART_TASK_GRAPH_DAILY")}
WHERE {and_where(*parts)}
ORDER BY DAY, WH_CREDITS DESC
LIMIT 5000
"""


def security_posture(days: int = 90) -> str:
    days = bounded_days(days, 400)
    return f"""
SELECT DAY, METRIC, COMPANY, VALUE
FROM {mart_object("MART_SECURITY_POSTURE_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY DAY, METRIC
LIMIT 10000
"""


def incident_timeline(hours: int = 48, company: str = "ALL") -> str:
    hours = max(1, min(int(hours or 48), 96))
    where = and_where(f"EVENT_TS >= DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())",
                      _company_arm(company))
    return f"""
SELECT EVENT_TS, KIND, COMPANY, SEVERITY, TITLE, REF_ID
FROM {mart_object("MART_INCIDENT_TIMELINE")}
WHERE {where}
ORDER BY EVENT_TS DESC
LIMIT 2000
"""


def ai_usage(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, 400)
    parts = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())"]
    if str(company or "ALL").upper() != "ALL":
        parts.append(f"DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(USER_NAME) = {sql_literal(company)}")
    return f"""
SELECT DAY, USER_NAME, SOURCE, MODEL_NAME, REQUESTS, TOKENS, CREDITS
FROM {mart_object("FACT_AI_USAGE_DAILY")}
WHERE {and_where(*parts)}
ORDER BY DAY, CREDITS DESC
LIMIT 5000
"""
