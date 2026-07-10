"""Readers for the V027 mart family (docs/design/V027_MART_FAMILY.md).

Thin SELECTs only — the loaders own the math. Adoption pattern: panels go
fact-first through these builders with their live builders kept as labeled
fallback (the Control Room v4.8.2 pattern), wave 2 after the marts hold data.
Every builder here is canaried so ACCOUNT_USAGE/mart drift pages the error
log before it pages a user.
"""

from __future__ import annotations

from app import companies
from app.config import mart_object
from app.core.sqlsafe import contains_filter, sql_literal
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


def task_graphs(days: int, company: str = "ALL", database: str = "",
                schema_contains: str = "") -> str:
    """Same filter surface as graph_sql.graph_daily_costs (wave 2 parity)."""
    days = bounded_days(days, 400)
    parts = [f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
             companies.database_clause(company, "DATABASE_NAME"),
             contains_filter("SCHEMA_NAME", schema_contains)]
    if str(database or "").strip():
        parts.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
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
    comp = ("" if str(company or "ALL").upper() == "ALL"
            else f"(COMPANY = {sql_literal(company)} OR UPPER(COMPANY) = 'ALL')")
    where = and_where(f"EVENT_TS >= DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())", comp)
    return f"""
SELECT EVENT_TS AS AT, KIND AS EVENT_TYPE, SEVERITY, TITLE AS LABEL, COMPANY, REF_ID
FROM {mart_object("MART_INCIDENT_TIMELINE")}
WHERE {where}
ORDER BY AT DESC
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


# ---------------------------------------------------------------------------
# Wave 2 (v4.12.0): aggregate readers matching each live builder's output
# contract, so panels swap mart-first without page rewrites. Sources of
# truth: the loaders in V027/V028; grain caveats live in the source labels
# (e.g. p95 here is the PEAK DAILY p95 of the mart, not a raw-row p95).
# ---------------------------------------------------------------------------

def eff_idle_analysis(days: int, company: str = "ALL") -> str:
    """insights_sql.idle_warehouse_analysis contract from the efficiency mart.
    IDLE_CREDITS uses each day's IDLE_PCT x credits (loader-computed from
    billed-vs-active hours), so no metering/query-history join at read time."""
    days = bounded_days(days, 400)
    where = and_where(f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      _company_arm(company))
    return f"""
SELECT
    WAREHOUSE_NAME,
    ANY_VALUE(COMPANY) AS COMPANY,
    SUM(BILLED_HOURS) AS METERED_HOURS,
    GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS,
    ROUND(SUM(CREDITS_TOTAL), 4) AS TOTAL_CREDITS,
    ROUND(SUM(CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100), 4) AS IDLE_CREDITS
FROM {mart_object("MART_WAREHOUSE_EFFICIENCY_DAILY")}
WHERE {where}
GROUP BY WAREHOUSE_NAME
HAVING SUM(CREDITS_TOTAL) > 0
ORDER BY IDLE_CREDITS DESC
LIMIT 100
"""


def eff_sizing_profile(days: int, company: str = "ALL") -> str:
    """insights_sql.warehouse_sizing_profile contract from the efficiency
    mart. P95_ELAPSED_SEC is the peak daily p95 (callers label it).
    Qualified (e.) — the CREDITS_TOTAL output alias shadowed the column in
    later aggregates (same class as the compile-heavy live failure)."""
    days = bounded_days(days, 400)
    where = and_where(f"e.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      _company_arm(company, "e.COMPANY"))
    return f"""
SELECT
    e.WAREHOUSE_NAME,
    ANY_VALUE(e.COMPANY) AS COMPANY,
    ROUND(SUM(e.CREDITS_TOTAL), 4) AS CREDITS_TOTAL,
    ROUND(SUM(e.CREDITS_TOTAL * COALESCE(e.IDLE_PCT, 0) / 100)
          / NULLIF(SUM(e.CREDITS_TOTAL), 0) * 100, 1) AS IDLE_PCT,
    SUM(e.QUERIES) AS QUERY_COUNT,
    MAX(COALESCE(e.P95_S, 0)) AS P95_ELAPSED_SEC,
    ROUND(SUM(COALESCE(e.QUEUED_MIN, 0)) * 60, 1) AS QUEUED_SEC,
    ROUND(SUM(COALESCE(e.SPILL_GB, 0)), 2) AS SPILL_REMOTE_GB
FROM {mart_object("MART_WAREHOUSE_EFFICIENCY_DAILY")} e
WHERE {where}
GROUP BY e.WAREHOUSE_NAME
HAVING SUM(e.CREDITS_TOTAL) > 0
ORDER BY CREDITS_TOTAL DESC
LIMIT 100
"""

def family_compile_heavy(days: int, company: str = "ALL") -> str:
    """cost_sql.compile_heavy_families contract from the family mart.
    Company scoping is database-heuristic (the mart has no user grain);
    averages are run-weighted across days. Every column is QUALIFIED (f.) —
    Snowflake resolved the bare RUNS inside later aggregates to the
    SUM(RUNS) AS RUNS alias and raised 'aggregate functions cannot be
    nested' (live, 2026-07-10). Qualified references cannot be shadowed."""
    days = bounded_days(days, 400)
    where = and_where(f"f.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      companies.database_clause(company, "f.DATABASE_NAME"))
    return f"""
SELECT
    f.QUERY_HASH AS QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(f.SAMPLE_TEXT) AS SAMPLE_TEXT,
    SUM(f.RUNS) AS RUNS,
    ROUND(SUM(f.COMPILE_MS_AVG * f.RUNS) / NULLIF(SUM(f.RUNS), 0) / 1000, 2) AS AVG_COMPILE_S,
    ROUND(SUM(f.TOTAL_EXEC_SEC) / NULLIF(SUM(f.RUNS), 0), 2) AS AVG_TOTAL_S,
    ROUND(SUM(f.COMPILE_MS_AVG * f.RUNS) / 1000
          / NULLIF(SUM(f.TOTAL_EXEC_SEC), 0) * 100, 1) AS COMPILE_PCT,
    ROUND(SUM(f.COMPILE_MS_AVG * f.RUNS) / 3600000, 2) AS TOTAL_COMPILE_HOURS
FROM {mart_object("MART_QUERY_FAMILY_DAILY")} f
WHERE {where}
GROUP BY f.QUERY_HASH
HAVING SUM(f.RUNS) >= 20 AND SUM(f.COMPILE_MS_AVG * f.RUNS) / NULLIF(SUM(f.RUNS), 0) > 500
ORDER BY SUM(f.COMPILE_MS_AVG * f.RUNS) DESC
LIMIT 25
"""

def family_repeat_fingerprints(days: int, company: str = "ALL", min_runs: int = 10,
                               database: str = "", schema_contains: str = "") -> str:
    """insights_sql.repeat_query_fingerprints contract from the family mart.
    ELAPSED here is exec-time (the mart's grain) — callers label the source.
    LAST_RUN degrades to the day grain. Qualified (f.) — see
    family_compile_heavy for the alias-shadow lesson."""
    days = bounded_days(days, 400)
    min_runs = max(2, min(int(min_runs or 10), 1000))
    parts = [f"f.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
             companies.database_clause(company, "f.DATABASE_NAME"),
             contains_filter("f.SCHEMA_NAME", schema_contains)]
    if str(database or "").strip():
        parts.append(f"UPPER(f.DATABASE_NAME) = {sql_literal(str(database).upper())}")
    where = and_where(*parts)
    return f"""
SELECT
    f.QUERY_HASH AS FINGERPRINT,
    SUM(f.RUNS) AS RUNS,
    MAX(f.USERS) AS USERS,
    MAX(f.WAREHOUSES) AS WAREHOUSES,
    ROUND(SUM(f.TOTAL_EXEC_SEC) / 3600.0, 2) AS TOTAL_ELAPSED_HOURS,
    ROUND(SUM(f.TOTAL_EXEC_SEC) / NULLIF(SUM(f.RUNS), 0), 2) AS AVG_ELAPSED_SEC,
    ROUND(SUM(COALESCE(f.GB_SCANNED_AVG, 0) * f.RUNS) / 1024, 4) AS TOTAL_TB_SCANNED,
    ROUND(SUM(COALESCE(f.CACHE_PCT_AVG, 0) * f.RUNS) / NULLIF(SUM(f.RUNS), 0), 1) AS AVG_CACHE_PCT,
    ANY_VALUE(f.SAMPLE_TEXT) AS QUERY_PREVIEW,
    MAX(f.DAY) AS LAST_RUN
FROM {mart_object("MART_QUERY_FAMILY_DAILY")} f
WHERE {where}
GROUP BY f.QUERY_HASH
HAVING SUM(f.RUNS) >= {min_runs}
ORDER BY TOTAL_ELAPSED_HOURS DESC
LIMIT 50
"""

def role_share(days: int, company: str = "ALL") -> str:
    """chargeback_sql.role_share_within_warehouse contract from the role-hour
    fact. Keeps BOTH guards: the fact's COMPANY column and the TRXS role
    heuristic (a Trexis automation role on an ALFA warehouse must not leak
    into ALFA's share — the live-round-3 lesson)."""
    days = bounded_days(days, 400)
    where = and_where(f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
                      _company_arm(company),
                      companies.role_clause(company, "ROLE_NAME"))
    return f"""
SELECT
    WAREHOUSE_NAME,
    COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
    SUM(QUERIES) AS QUERY_COUNT,
    ROUND(SUM(EXEC_SEC), 1) AS ELAPSED_SEC,
    RATIO_TO_REPORT(SUM(EXEC_SEC)) OVER (PARTITION BY WAREHOUSE_NAME) AS ELAPSED_SHARE
FROM {mart_object("FACT_QUERY_ROLE_HOURLY")}
WHERE {where}
GROUP BY WAREHOUSE_NAME, ROLE_NAME
ORDER BY WAREHOUSE_NAME, ELAPSED_SEC DESC
LIMIT 2000
"""


def alloc_attribution(days: int, dimension: str, company: str = "ALL") -> str:
    """cost_sql.allocated_attribution contract (+ ALLOC_CREDITS, which the
    live builder cannot offer): share still ships for the fallback-parity
    path, but mart callers can dollarize ALLOC_CREDITS directly."""
    days = bounded_days(days, 400)
    dim = str(dimension or "USER").upper()
    if dim not in ("USER", "DATABASE", "SCHEMA", "ROLE"):
        raise ValueError(f"dimension must be USER/DATABASE/SCHEMA/ROLE, got {dimension!r}")
    where = and_where(f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      f"DIMENSION = {sql_literal(dim)}",
                      _company_arm(company))
    return f"""
SELECT
    COALESCE(KEY_NAME, 'UNKNOWN') AS DIMENSION,
    ROUND(SUM(EXEC_SEC), 1) AS ELAPSED_SEC,
    RATIO_TO_REPORT(SUM(ALLOC_CREDITS)) OVER () AS ELAPSED_SHARE,
    ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
FROM {mart_object("MART_COST_ALLOCATION_DAILY")}
WHERE {where}
GROUP BY KEY_NAME
ORDER BY ALLOC_CREDITS DESC
LIMIT 100
"""


def schema_window_summary(days: int, company: str = "ALL", database: str = "",
                          schema_contains: str = "") -> str:
    """ops_sql.query_window_summary contract from the schema-hour fact — the
    read that used to force a live QUERY_HISTORY scan whenever a schema
    filter was active. P95 is the peak hourly-group p95 (callers label it)."""
    days = bounded_days(days, 400)
    parts = [f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
             _company_arm(company),
             contains_filter("SCHEMA_NAME", schema_contains)]
    if str(database or "").strip():
        parts.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
    return f"""
SELECT
    SUM(QUERIES) AS QUERY_COUNT,
    SUM(FAILS) AS FAILED_COUNT,
    MAX(COALESCE(P95_S, 0)) AS P95_ELAPSED_SEC,
    ROUND(SUM(COALESCE(QUEUED_SEC, 0)), 1) AS QUEUED_SEC,
    ROUND(SUM(COALESCE(SPILL_GB, 0)), 2) AS SPILL_REMOTE_GB
FROM {mart_object("FACT_QUERY_SCHEMA_HOURLY")}
WHERE {and_where(*parts)}
"""


def ai_costs_by_model(days: int) -> str:
    """cortex_sql model/source cost contract from FACT_AI_USAGE_DAILY —
    Code + Functions in one read, loaded daily. Qualified (a.) — TOKENS and
    CREDITS output aliases shadowed the columns in the per-1M expression
    (same class as the compile-heavy live failure)."""
    days = bounded_days(days, 400)
    return f"""
SELECT
    a.SOURCE AS FUNCTION_NAME,
    COALESCE(a.MODEL_NAME, 'n/a') AS MODEL_NAME,
    SUM(COALESCE(a.REQUESTS, 0)) AS REQUESTS,
    SUM(COALESCE(a.TOKENS, 0)) AS TOKENS,
    ROUND(SUM(COALESCE(a.CREDITS, 0)), 4) AS CREDITS,
    ROUND(SUM(COALESCE(a.CREDITS, 0)) * 1000000
          / NULLIF(SUM(COALESCE(a.TOKENS, 0)), 0), 4) AS CREDITS_PER_1M_TOKENS
FROM {mart_object("FACT_AI_USAGE_DAILY")} a
WHERE a.DAY >= DATEADD('day', -{days}, CURRENT_DATE())
GROUP BY a.SOURCE, a.MODEL_NAME
ORDER BY CREDITS DESC
LIMIT 200
"""

