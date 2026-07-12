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
  AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
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
  AND UPPER(e.WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
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
    fact. Attribution law (v4.34.1): the fact's COMPANY column scopes
    warehouses in the denominator; the TRXS role heuristic (live-round-3
    lesson) only picks display rows AFTER the share is computed, so an
    excluded role keeps its slice and this company's roles never absorb it."""
    days = bounded_days(days, 400)
    where = and_where(f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
                      _company_arm(company))
    vis = and_where(companies.role_clause(company, "ROLE_NAME"))
    return f"""
WITH scoped AS (
    SELECT
        WAREHOUSE_NAME,
        COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
        SUM(QUERIES) AS QUERY_COUNT,
        SUM(EXEC_SEC) AS ELAPSED_SEC
    FROM {mart_object("FACT_QUERY_ROLE_HOURLY")}
    WHERE {where}
    GROUP BY WAREHOUSE_NAME, ROLE_NAME
), shared AS (
    SELECT scoped.*,
           RATIO_TO_REPORT(ELAPSED_SEC) OVER (PARTITION BY WAREHOUSE_NAME) AS ELAPSED_SHARE
    FROM scoped
)
SELECT WAREHOUSE_NAME, ROLE_NAME, QUERY_COUNT, ROUND(ELAPSED_SEC, 1) AS ELAPSED_SEC, ELAPSED_SHARE
FROM shared
WHERE {vis}
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
    scope_where = and_where(f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                            f"DIMENSION = {sql_literal(dim)}",
                            _company_arm(company))
    # Same global-share law as the live builder (live math fix 2026-07-11):
    # visibility rules pick which rows display; the share denominator is the
    # company's WHOLE scoped activity. USER$ personal databases attribute to
    # their owner's company; users attribute by role membership.
    vis = ""
    if dim == "DATABASE":
        vis = companies.database_visibility_clause(company, "KEY_NAME")
    elif dim == "USER":
        vis = companies.user_clause(company, "KEY_NAME")
    return f"""
WITH scoped AS (
    SELECT KEY_NAME, EXEC_SEC, ALLOC_CREDITS
    FROM {mart_object("MART_COST_ALLOCATION_DAILY")}
    WHERE {scope_where}
)
SELECT
    COALESCE(KEY_NAME, 'NONE') AS DIMENSION,
    ROUND(SUM(EXEC_SEC), 1) AS ELAPSED_SEC,
    SUM(ALLOC_CREDITS) / NULLIF((SELECT SUM(ALLOC_CREDITS) FROM scoped), 0) AS ELAPSED_SHARE,
    ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
FROM scoped
WHERE {and_where(vis)}
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



def unused_roles_via_fact(days: int = 90) -> str:
    """security_sql.unused_roles contract from FACT_QUERY_ROLE_HOURLY (live
    round 6: the live version was p95 32s). Coverage-guarded: returns ZERO
    rows unless the fact actually spans the window — an empty result makes
    run_mart_first fall back to live, so a young fact can never fake
    'unused' (a role used 60d ago must not be revoke fodder because the
    fact is 3 days old). Run the 90d backfill to activate this path."""
    days = bounded_days(days, 400)
    return f"""
WITH cov AS (
    SELECT MIN(HOUR_TS) AS FIRST_TS FROM {mart_object("FACT_QUERY_ROLE_HOURLY")}
)
SELECT r.NAME AS ROLE_NAME, r.CREATED_ON,
       (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
         WHERE g.ROLE = r.NAME AND g.DELETED_ON IS NULL) AS GRANTED_TO_USERS
FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
LEFT JOIN (
    SELECT DISTINCT ROLE_NAME
    FROM {mart_object("FACT_QUERY_ROLE_HOURLY")}
    WHERE HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())
) q ON q.ROLE_NAME = r.NAME
WHERE r.DELETED_ON IS NULL AND q.ROLE_NAME IS NULL
  AND r.NAME NOT IN ('PUBLIC')
  AND (SELECT FIRST_TS FROM cov) <= DATEADD('day', -{days} + 1, CURRENT_TIMESTAMP())
ORDER BY GRANTED_TO_USERS DESC, r.CREATED_ON
LIMIT 500
"""


def tag_coverage_daily(days: int, company: str = "ALL") -> str:
    """cost_sql.tag_coverage contract from MART_TAG_COVERAGE_DAILY (V031) —
    the user-grain column the family mart could not carry. Qualified (c.)
    per the alias-shadow rule; same 60s exec floor as live."""
    days = bounded_days(days, 400)
    where = and_where(f"c.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
                      _company_arm(company, "c.COMPANY"))
    return f"""
SELECT
    c.USER_NAME,
    ROUND(SUM(c.EXEC_SEC), 1) AS EXEC_SEC,
    ROUND(SUM(c.UNTAGGED_EXEC_SEC), 1) AS UNTAGGED_EXEC_SEC,
    ROUND(100 * (1 - SUM(c.UNTAGGED_EXEC_SEC) / NULLIF(SUM(c.EXEC_SEC), 0)), 1) AS TAGGED_PCT,
    SUM(c.QUERIES) AS QUERIES
FROM {mart_object("MART_TAG_COVERAGE_DAILY")} c
WHERE {where}
GROUP BY c.USER_NAME
HAVING SUM(c.EXEC_SEC) > 60
ORDER BY UNTAGGED_EXEC_SEC DESC
LIMIT 30
"""


def lock_wait_daily(days: int, company: str = "ALL") -> str:
    """Lock waits from MART_LOCK_WAIT_DAILY (V035) — the live scan read
    46-56 GB per view; the daily task pays that once. Same ranking as the
    live builder: never-acquired first (those are the aborted statements)."""
    d = max(1, min(int(days), 90))
    comp = ""
    if company and company != "ALL":
        comp = (f"    AND (c.COMPANY = {companies.sql_literal(company)}"
                " OR UPPER(c.COMPANY) = 'ALL')\n")
    return f"""SELECT
    c.DATABASE_NAME,
    c.SCHEMA_NAME,
    c.OBJECT_NAME,
    c.LOCK_TYPE,
    SUM(c.WAIT_EVENTS) AS WAIT_EVENTS,
    SUM(c.ACQUIRED_WAIT_SEC) AS ACQUIRED_WAIT_SEC,
    SUM(c.NEVER_ACQUIRED) AS NEVER_ACQUIRED,
    MAX(c.LAST_SEEN) AS LAST_SEEN
FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY c
WHERE c.DAY >= DATEADD('day', -{d}, CURRENT_DATE())
{comp}GROUP BY 1, 2, 3, 4
ORDER BY NEVER_ACQUIRED DESC, ACQUIRED_WAIT_SEC DESC
LIMIT 50"""


def lock_wait_spikes(company: str = "ALL", database: str = "") -> str:
    """Objects whose last-day lock waits run >=3x their prior 6-day daily
    average (Codex r8 #13) — mart-only by design; pre-V035 this is empty
    and the panel stays quiet. The mart carries DATABASE_NAME, so the
    sidebar database filter narrows it (Joe 2026-07-11)."""
    comp = ""
    if company and company != "ALL":
        comp = (f"        AND (c.COMPANY = {companies.sql_literal(company)}"
                " OR UPPER(c.COMPANY) = 'ALL')\n")
    dbf = companies.database_equals_clause(database, "c.DATABASE_NAME")
    if dbf:
        comp += f"        AND {dbf}\n"
    return f"""SELECT * FROM (
    SELECT
        c.DATABASE_NAME, c.SCHEMA_NAME, c.OBJECT_NAME,
        SUM(IFF(c.DAY >= DATEADD('day', -1, CURRENT_DATE()), c.WAIT_EVENTS, 0)) AS LAST_DAY_WAITS,
        ROUND(SUM(IFF(c.DAY < DATEADD('day', -1, CURRENT_DATE()), c.WAIT_EVENTS, 0)) / 6.0, 1)
            AS PRIOR_DAILY_AVG,
        SUM(IFF(c.DAY >= DATEADD('day', -1, CURRENT_DATE()), c.NEVER_ACQUIRED, 0))
            AS LAST_DAY_NEVER_ACQ
    FROM DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY c
    WHERE c.DAY >= DATEADD('day', -7, CURRENT_DATE())
{comp}    GROUP BY 1, 2, 3
) g
WHERE g.LAST_DAY_WAITS >= 5 AND g.LAST_DAY_WAITS > 3 * GREATEST(g.PRIOR_DAILY_AVG, 1)
ORDER BY g.LAST_DAY_WAITS DESC
LIMIT 20"""


def monthly_spend_by_warehouse(months: int = 12, company: str = "ALL") -> str:
    """Monthly credits by warehouse from the efficiency mart — the boss chart.
    The mart accrues history going forward; the live WMH fallback carries the
    13-month back view until then."""
    m = max(2, min(int(months), 13))
    comp = ""
    if company and company != "ALL":
        comp = (f"    AND (c.COMPANY = {companies.sql_literal(company)}"
                " OR UPPER(c.COMPANY) = 'ALL')\n")
    return f"""SELECT
    TO_CHAR(DATE_TRUNC('month', c.DAY), 'YYYY-MM') AS MONTH,
    c.WAREHOUSE_NAME,
    SUM(c.CREDITS_TOTAL) AS CREDITS
FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY c
WHERE c.DAY >= DATEADD('month', -{m}, DATE_TRUNC('month', CURRENT_DATE()))
  AND UPPER(c.WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
{comp}GROUP BY 1, 2
ORDER BY 1, 2"""


def live_monthly_spend_by_warehouse(months: int = 12, company: str = "ALL") -> str:
    """13-month live fallback over WAREHOUSE_METERING_HISTORY; company via
    COMPANY_FOR_WAREHOUSE outside the aggregation (V030 shape law)."""
    m = max(2, min(int(months), 13))
    comp = ""
    if company and company != "ALL":
        comp = (f"WHERE (w.COMPANY = {companies.sql_literal(company)}"
                " OR UPPER(w.COMPANY) = 'ALL')\n")
    return f"""SELECT w.MONTH, w.WAREHOUSE_NAME, w.CREDITS
FROM (
    SELECT g.MONTH, g.WAREHOUSE_NAME, g.CREDITS,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY
    FROM (
        SELECT TO_CHAR(DATE_TRUNC('month', START_TIME), 'YYYY-MM') AS MONTH,
               WAREHOUSE_NAME,
               SUM(CREDITS_USED) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE START_TIME >= DATEADD('month', -{m}, DATE_TRUNC('month', CURRENT_DATE()))
          AND WAREHOUSE_ID > 0
        GROUP BY 1, 2
    ) g
) w
{comp}ORDER BY w.MONTH, w.WAREHOUSE_NAME"""


def pattern_cost(days: int = 30, company: str = "ALL", limit: int = 25) -> str:
    """Measured $ per repeated statement pattern (V036) — the silent-spend
    table. Attribution credits are MEASURED compute; the sample text rides
    in from the family mart by hash."""
    d = max(1, min(int(days), 90))
    lim = max(5, min(int(limit), 100))
    comp = ""
    if company and company != "ALL":
        comp = (f"    AND (p.COMPANY = {companies.sql_literal(company)}"
                " OR UPPER(p.COMPANY) = 'ALL')\n")
    return f"""SELECT
    p.QUERY_HASH,
    ANY_VALUE(f.SAMPLE_TEXT) AS SAMPLE_TEXT,
    SUM(p.RUNS) AS RUNS,
    SUM(p.CREDITS_ATTRIBUTED) AS CREDITS,
    SUM(p.CREDITS_ATTRIBUTED) / NULLIF(SUM(p.RUNS), 0) AS CREDITS_PER_RUN,
    HLL_ESTIMATE(HLL_COMBINE(p.USERS_HLL)) AS USERS
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY p
LEFT JOIN (
    SELECT QUERY_HASH, ANY_VALUE(SAMPLE_TEXT) AS SAMPLE_TEXT
    FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
    WHERE DAY >= DATEADD('day', -{d}, CURRENT_DATE())
    GROUP BY QUERY_HASH
) f ON f.QUERY_HASH = p.QUERY_HASH
WHERE p.DAY >= DATEADD('day', -{d}, CURRENT_DATE())
{comp}GROUP BY p.QUERY_HASH
HAVING SUM(p.CREDITS_ATTRIBUTED) > 0.01
ORDER BY CREDITS DESC
LIMIT {lim}"""


# ---------------------------------------------------------------------------
# Compare mode (Phase 1, period vs period) — every reader takes explicit
# half-open ISO windows [a0, a1) / [b0, b1) computed by app.logic.compare.
# All facts/marts, zero ACCOUNT_USAGE — the compare tab is pinned at a live
# -scan budget of 0.
# ---------------------------------------------------------------------------

def _iso(d: object) -> str:
    """Validated ISO date literal for the compare windows."""
    from datetime import date
    return date.fromisoformat(str(d)).isoformat()


def _side_windows(a_start: str, a_end: str, b_start: str, b_end: str,
                  col: str = "DAY") -> tuple[str, str, str]:
    a0, a1 = _iso(a_start), _iso(a_end)
    b0, b1 = _iso(b_start), _iso(b_end)
    in_a = f"({col} >= '{a0}' AND {col} < '{a1}')"
    in_b = f"({col} >= '{b0}' AND {col} < '{b1}')"
    if b1 == a0:  # adjacent windows (the default pairings): one contiguous
        return in_a, in_b, f"({col} >= '{b0}' AND {col} < '{a1}')"  # range prunes best (r13 #11)
    return in_a, in_b, f"(({in_a}) OR ({in_b}))"


def compare_warehouse_credits(a_start: str, a_end: str, b_start: str, b_end: str,
                              company: str = "ALL") -> str:
    """Per-warehouse credits for both sides — movers AND the strip's
    company-scopable spend total (FACT_WAREHOUSE_DAILY, exact metering)."""
    in_a, in_b, either = _side_windows(a_start, a_end, b_start, b_end)
    comp = ""
    if company and company != "ALL":
        comp = f"  AND COMPANY = {companies.sql_literal(company)}\n"
    return f"""SELECT
    WAREHOUSE_NAME,
    SUM(IFF({in_a}, CREDITS_TOTAL, 0)) AS A_CREDITS,
    SUM(IFF({in_b}, CREDITS_TOTAL, 0)) AS B_CREDITS
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
WHERE {either}
{comp}GROUP BY WAREHOUSE_NAME
HAVING SUM(CREDITS_TOTAL) > 0
ORDER BY ABS(A_CREDITS - B_CREDITS) DESC
LIMIT 100"""


def compare_activity(a_start: str, a_end: str, b_start: str, b_end: str,
                     company: str = "ALL") -> str:
    """Volume shape per side from FACT_QUERY_HOURLY (company-scoped ops
    grain — r11 #12: these metrics never come from metering-daily)."""
    # r12 #10: direct bounds on HOUR_TS — CAST(col) in the WHERE defeats
    # partition pruning; timestamp-vs-date-literal comparison prunes fine.
    in_a, _in_b, either = _side_windows(a_start, a_end, b_start, b_end,
                                        col="HOUR_TS")
    comp = ""
    if company and company != "ALL":
        comp = f"  AND COMPANY = {companies.sql_literal(company)}\n"
    return f"""SELECT
    IFF({in_a}, 'A', 'B') AS SIDE,
    SUM(QUERY_COUNT) AS QUERIES,
    SUM(FAILED_COUNT) AS FAILS,
    SUM(QUEUED_SEC_SUM) AS QUEUED_SEC,
    SUM(SPILL_REMOTE_GB) AS SPILL_REMOTE_GB
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
WHERE {either}
{comp}GROUP BY 1"""


def compare_billed(a_start: str, a_end: str, b_start: str, b_end: str) -> str:
    """Account-billed credits per side (FACT_METERING_DAILY — account-wide
    by construction; the strip labels it so)."""
    in_a, _in_b, either = _side_windows(a_start, a_end, b_start, b_end)
    return f"""SELECT
    IFF({in_a}, 'A', 'B') AS SIDE,
    SUM(CREDITS_BILLED) AS CREDITS_BILLED
FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
WHERE {either}
GROUP BY 1"""


def compare_pattern_costs(a_start: str, a_end: str, b_start: str, b_end: str,
                          company: str = "ALL", limit: int = 25) -> str:
    """Pattern movers: measured attribution $ per parameterized hash, both
    sides (MART_PATTERN_COST_DAILY v2). The silent-spend delta."""
    in_a, in_b, either = _side_windows(a_start, a_end, b_start, b_end, col="p.DAY")
    lim = max(5, min(int(limit), 100))
    comp = ""
    if company and company != "ALL":
        comp = (f"  AND (p.COMPANY = {companies.sql_literal(company)}"
                " OR UPPER(p.COMPANY) = 'ALL')\n")
    return f"""SELECT
    p.QUERY_HASH,
    ANY_VALUE(f.SAMPLE_TEXT) AS SAMPLE_TEXT,
    SUM(IFF({in_a}, p.RUNS, 0)) AS A_RUNS,
    SUM(IFF({in_b}, p.RUNS, 0)) AS B_RUNS,
    SUM(IFF({in_a}, p.CREDITS_ATTRIBUTED, 0)) AS A_CREDITS,
    SUM(IFF({in_b}, p.CREDITS_ATTRIBUTED, 0)) AS B_CREDITS
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY p
LEFT JOIN (
    SELECT QUERY_HASH, ANY_VALUE(SAMPLE_TEXT) AS SAMPLE_TEXT
    FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
    WHERE DAY >= LEAST('{_iso(a_start)}'::DATE, '{_iso(b_start)}'::DATE)
      AND DAY < GREATEST('{_iso(a_end)}'::DATE, '{_iso(b_end)}'::DATE)
    GROUP BY QUERY_HASH
) f ON f.QUERY_HASH = p.QUERY_HASH
WHERE {either}
{comp}GROUP BY p.QUERY_HASH
HAVING GREATEST(SUM(IFF({in_a}, p.CREDITS_ATTRIBUTED, 0)),
                SUM(IFF({in_b}, p.CREDITS_ATTRIBUTED, 0))) > 0.01
ORDER BY ABS(A_CREDITS - B_CREDITS) DESC
LIMIT {lim}"""


def fact_monthly_spend_by_warehouse(months: int = 12, company: str = "ALL") -> str:
    """Boss-chart fallback from FACT_WAREHOUSE_DAILY (r14 #5): the fact is
    backfilled 365 days, so the 13-month live WAREHOUSE_METERING_HISTORY
    scan is no longer the only long-view source. The accruing efficiency
    mart stays primary; this replaces the LIVE fallback."""
    m = max(2, min(int(months), 13))
    comp = ""
    if company and company != "ALL":
        comp = f"  AND COMPANY = {companies.sql_literal(company)}\n"
    return f"""SELECT
    TO_CHAR(DATE_TRUNC('month', DAY), 'YYYY-MM') AS MONTH,
    WAREHOUSE_NAME,
    SUM(CREDITS_TOTAL) AS CREDITS
FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
WHERE DAY >= DATEADD('month', -{m}, DATE_TRUNC('month', CURRENT_DATE()))
{comp}GROUP BY 1, 2
ORDER BY 1, 2"""
# ---------------------------------------------------------------------------
# V041 loader pass — readers for the new facts/marts. Contracts mirror the
# live builders they relieve; grain degrades are labeled by the caller's
# source string. Coverage gates follow the unused_roles_via_fact pattern:
# an accruing mart that cannot span the asked window returns ZERO rows, so
# run_mart_first falls back to live instead of silently under-reporting.
# ---------------------------------------------------------------------------

def alloc_xdim_attribution(days: int, dimension: str, company: str = "ALL",
                           database: str = "") -> str:
    """cost_sql.allocated_attribution contract from FACT_COST_ALLOC_XDIM_DAILY
    (V041 R2) — the database/user-filtered attribution that used to pay two
    live QUERY_HISTORY scans per filter value; user-within-database is now
    mart-served on Spend. Global-share law preserved (v4.33.1): company scope
    (warehouse grain, matching the live builder) sets the denominator; the
    database filter and dimension visibility rules only pick which rows
    DISPLAY. No schema grain here by design — schema-filtered views stay on
    the live builder. Qualified (x.) per the alias-shadow rule."""
    days = bounded_days(days, 400)
    dim = str(dimension or "USER").upper()
    if dim not in ("USER", "DATABASE"):
        raise ValueError(f"dimension must be USER/DATABASE, got {dimension!r}")
    dim_col = "x.USER_NAME" if dim == "USER" else "x.DATABASE_NAME"
    scope_where = and_where(
        f"x.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        companies.warehouse_clause(company, "x.WAREHOUSE_NAME"),
    )
    vis = (companies.user_clause(company, "KEY_NAME") if dim == "USER"
           else companies.database_visibility_clause(company, "KEY_NAME"))
    display = and_where(companies.database_equals_clause(database, "DATABASE_NAME"), vis)
    return f"""
WITH cov AS (
    SELECT MIN(DAY) AS FIRST_DAY FROM {mart_object("FACT_COST_ALLOC_XDIM_DAILY")}
),
scoped AS (
    SELECT {dim_col} AS KEY_NAME, x.DATABASE_NAME, x.EXEC_SEC, x.ALLOC_CREDITS
    FROM {mart_object("FACT_COST_ALLOC_XDIM_DAILY")} x
    WHERE {scope_where}
)
SELECT
    COALESCE(KEY_NAME, 'NONE') AS DIMENSION,
    ROUND(SUM(EXEC_SEC), 1) AS ELAPSED_SEC,
    SUM(ALLOC_CREDITS) / NULLIF((SELECT SUM(ALLOC_CREDITS) FROM scoped), 0) AS ELAPSED_SHARE,
    ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
FROM scoped
WHERE {display}
  AND (SELECT FIRST_DAY FROM cov) <= DATEADD('day', -{days} + 1, CURRENT_DATE())
GROUP BY KEY_NAME
ORDER BY ALLOC_CREDITS DESC
LIMIT 100
"""


def ai_code_user_rollup(days: int, company: str = "ALL") -> str:
    """cortex_sql.cortex_code_user_rollup contract from FACT_AI_USAGE_DAILY
    (V041 R3; cortex_users p50 17.6s x12 was the worst user-facing key on
    Chargeback & AI). Day-grain degrades, labeled by the caller: FIRST/LAST
    _USAGE are days, EMAIL is not in the fact (NULL). Cortex Code sources
    only — the Functions rows bill the account, not a user. Qualified (a.)
    per the alias-shadow rule. The live view stays as fallback WITH its
    probe semantics (the 002139 subscription class)."""
    days = bounded_days(days, 400)
    where = and_where(
        f"a.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
        "a.SOURCE IN ('Snowsight', 'CLI')",
        companies.user_clause(company, "a.USER_NAME"),
    )
    return f"""
SELECT
    a.USER_NAME,
    NULL AS EMAIL,
    a.SOURCE,
    COUNT(DISTINCT a.DAY) AS ACTIVE_DAYS,
    SUM(COALESCE(a.REQUESTS, 0)) AS TOTAL_REQUESTS,
    SUM(COALESCE(a.CREDITS, 0)) AS TOTAL_CREDITS,
    SUM(COALESCE(a.TOKENS, 0)) AS TOTAL_TOKENS,
    MIN(a.DAY) AS FIRST_USAGE,
    MAX(a.DAY) AS LAST_USAGE,
    SUM(COALESCE(a.CREDITS, 0)) / NULLIF(SUM(COALESCE(a.REQUESTS, 0)), 0) AS CREDITS_PER_REQUEST,
    SUM(COALESCE(a.CREDITS, 0)) / NULLIF(COUNT(DISTINCT a.DAY), 0) AS AVG_DAILY_CREDITS
FROM {mart_object("FACT_AI_USAGE_DAILY")} a
WHERE {where}
GROUP BY a.USER_NAME, a.SOURCE
ORDER BY TOTAL_CREDITS DESC
LIMIT 500
"""


def ops_diag_top_queries(days: int, company: str = "ALL", limit: int = 50) -> str:
    """ops_sql.top_queries_by_elapsed contract from MART_OPS_DIAG_HOURLY
    (V041 R7) — the UNFILTERED Operations first paint only: an entity or
    schema filter needs the true filtered top-N, which only the live scan
    has (the mart keeps each hour's global top-20). Coverage-gated while
    the mart accrues toward the asked window."""
    days = bounded_days(days, 90)
    limit = max(1, min(int(limit), 500))
    where = and_where(
        "d.KIND = 'TOP_ELAPSED'",
        f"d.HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        _company_arm(company, "d.COMPANY"),
    )
    return f"""
WITH cov AS (
    SELECT MIN(HOUR_TS) AS FIRST_TS FROM {mart_object("MART_OPS_DIAG_HOURLY")}
)
SELECT
    d.QUERY_ID, d.START_TIME, d.USER_NAME, d.WAREHOUSE_NAME, d.WAREHOUSE_SIZE,
    d.DATABASE_NAME, d.QUERY_TYPE, d.EXECUTION_STATUS, d.ELAPSED_SEC, d.QUEUED_SEC,
    d.SPILL_REMOTE_GB, d.QUERY_PREVIEW
FROM {mart_object("MART_OPS_DIAG_HOURLY")} d
WHERE {where}
  AND (SELECT FIRST_TS FROM cov) <= DATEADD('day', -{days} + 1, CURRENT_TIMESTAMP())
ORDER BY d.ELAPSED_SEC DESC
LIMIT {limit}
"""


def ops_diag_failures(days: int, company: str = "ALL") -> str:
    """ops_sql.failures_by_error contract from MART_OPS_DIAG_HOURLY (V041 R7).
    USERS_AFFECTED is the PEAK HOURLY distinct-user count, not the window
    distinct — the caller labels the source. Unfiltered first paint only;
    coverage-gated like the top-queries reader."""
    days = bounded_days(days, 90)
    where = and_where(
        "d.KIND = 'FAIL_FAMILY'",
        f"d.HOUR_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())",
        _company_arm(company, "d.COMPANY"),
    )
    return f"""
WITH cov AS (
    SELECT MIN(HOUR_TS) AS FIRST_TS FROM {mart_object("MART_OPS_DIAG_HOURLY")}
)
SELECT
    d.ERROR_CODE,
    d.ERROR_MESSAGE,
    SUM(d.FAILURES) AS FAILURES,
    MAX(d.USERS_AFFECTED) AS USERS_AFFECTED,
    MAX(d.LAST_SEEN) AS LAST_SEEN
FROM {mart_object("MART_OPS_DIAG_HOURLY")} d
WHERE {where}
  AND (SELECT FIRST_TS FROM cov) <= DATEADD('day', -{days} + 1, CURRENT_TIMESTAMP())
GROUP BY d.ERROR_CODE, d.ERROR_MESSAGE
ORDER BY FAILURES DESC
LIMIT 50
"""


def platform_score_inputs(days: int = 30) -> str:
    """mart_sql.score_inputs_daily contract from FACT_PLATFORM_SCORE_DAILY
    (V041 R8): the four per-day input aggregates load once daily; weights
    stay in Python. The V041 first fill MERGEs the full 30-day window, so
    no coverage gate is needed — empty means undeployed, and run_mart_first
    falls back to the live aggregation."""
    days = max(7, min(int(days or 30), 120))
    return f"""
SELECT DAY, CREDITS_BILLED, QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB,
       TASK_RUNS, TASK_FAILED, CRIT_RAISED, HIGH_RAISED
FROM {mart_object("FACT_PLATFORM_SCORE_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY DAY
"""
