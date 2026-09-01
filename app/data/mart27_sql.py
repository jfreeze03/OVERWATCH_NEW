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
from app.data.common import (
    ai_service_predicate,
    and_where,
    bounded_days,
    resolve_effective_window,
    scope_window_where,
)


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
SELECT DAY, QUERY_HASH, COMPANY, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES,
       DATABASE_NAME, SCHEMA_NAME, TOTAL_EXEC_SEC, MEDIAN_S, P95_S,
       COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS
FROM {mart_object("MART_QUERY_FAMILY_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY TOTAL_EXEC_SEC DESC
LIMIT {limit}
"""


def role_hourly(days: int, company: str = "ALL") -> str:
    days = bounded_days(days, 400)
    # rec#49: anchor on CURRENT_DATE (midnight-aligned) to match the live query
    # summary (_query_scope), not CURRENT_TIMESTAMP — the rolling-24h vs day-aligned
    # mismatch made the ops-diag windows disagree with the summary a DBA reads beside it.
    where = and_where(f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())",
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
    parts = [f"HOUR_TS >= DATEADD('day', -{days}, CURRENT_DATE())",   # rec#49: day-aligned, matches the summary
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


def task_nodes(days: int, company: str = "ALL", database: str = "",
               schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """C18: per-node loader timing from MART_TASK_NODE_DAILY (V058), which loads
    but had no reader. No COMPANY column -> scope via database_clause on
    DATABASE_NAME, same as task_graphs. Mart-ONLY (no live-parity builder: the
    dispatch-queue delay needs SCHEDULED_TIME, which no app live builder computes,
    so a fallback leg would diverge numerically). p95 dispatch queue first surfaces
    the late-start / contention offenders the mart exists to quantify."""
    days = bounded_days(days, 400)
    parts = [scope_window_where("DAY", days, bounds=bounds),
             companies.database_clause(company, "DATABASE_NAME"),
             contains_filter("SCHEMA_NAME", schema_contains)]
    if str(database or "").strip():
        parts.append(f"UPPER(DATABASE_NAME) = {sql_literal(str(database).upper())}")
    return f"""
SELECT DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, RUNS, FAILED,
       AVG_QUEUE_SEC, P95_QUEUE_SEC, MAX_QUEUE_SEC,
       AVG_EXEC_SEC, P95_EXEC_SEC, MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED
FROM {mart_object("MART_TASK_NODE_DAILY")}
WHERE {and_where(*parts)}
ORDER BY P95_QUEUE_SEC DESC NULLS LAST, FAILED DESC
LIMIT 2000
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

def eff_idle_analysis(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """insights_sql.idle_warehouse_analysis contract from the efficiency mart.
    IDLE_CREDITS uses each day's IDLE_PCT x credits (loader-computed from
    billed-vs-active hours), so no metering/query-history join at read time."""
    days = bounded_days(days, 400)
    where = and_where(scope_window_where("DAY", days, bounds=bounds),
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


def eff_sizing_profile(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """insights_sql.warehouse_sizing_profile contract from the efficiency
    mart. P95_ELAPSED_SEC is the peak daily p95 (callers label it).
    Qualified (e.) — the CREDITS_TOTAL output alias shadowed the column in
    later aggregates (same class as the compile-heavy live failure)."""
    days = bounded_days(days, 400)
    where = and_where(scope_window_where("e.DAY", days, bounds=bounds),
                      _company_arm(company, "e.COMPANY"))
    return f"""
SELECT
    e.WAREHOUSE_NAME,
    ANY_VALUE(e.COMPANY) AS COMPANY,
    ROUND(SUM(e.CREDITS_TOTAL), 4) AS CREDITS_TOTAL,
    ROUND(SUM(e.CREDITS_TOTAL * COALESCE(e.IDLE_PCT, 0) / 100)
          / NULLIF(SUM(e.CREDITS_TOTAL), 0) * 100, 1) AS IDLE_PCT,
    SUM(e.QUERIES) AS QUERY_COUNT,
    COUNT_IF(COALESCE(e.QUERIES, 0) > 0) AS ACTIVE_QUERY_DAYS,
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

def family_compile_heavy(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """cost_sql.compile_heavy_families contract from the family mart.
    Company scoping is EXACT via the mart's COMPANY column (V082 regrain) —
    was a lossy ANY_VALUE(DATABASE_NAME) heuristic before. Averages are
    run-weighted across days. Every column is QUALIFIED (f.) — Snowflake
    resolved the bare RUNS inside later aggregates to the SUM(RUNS) AS RUNS
    alias and raised 'aggregate functions cannot be nested' (live, 2026-07-10).
    Qualified references cannot be shadowed."""
    days = bounded_days(days, 400)
    where = and_where(scope_window_where("f.DAY", days, bounds=bounds),
                      _company_arm(company, "f.COMPANY"))
    return f"""
SELECT
    f.QUERY_HASH AS QUERY_PARAMETERIZED_HASH,
    ANY_VALUE(f.SAMPLE_TEXT) AS SAMPLE_TEXT,
    SUM(f.RUNS) AS RUNS,
    ROUND(SUM(f.COMPILE_MS_AVG * f.RUNS) / NULLIF(SUM(f.RUNS), 0) / 1000, 2) AS AVG_COMPILE_S,
    -- Triage #5 (V060): wall-clock elapsed, like the live builder's
    -- TOTAL_ELAPSED_TIME — exec-only time made COMPILE_PCT exceed 100% for the
    -- compile-dominated families this view selects. COALESCE degrades pre-V060
    -- rows (never re-loaded beyond the trailing 2 days) to the old exec basis.
    ROUND(SUM(COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)) / NULLIF(SUM(f.RUNS), 0), 2) AS AVG_TOTAL_S,
    ROUND(SUM(f.COMPILE_MS_AVG * f.RUNS) / 1000
          / NULLIF(SUM(COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)), 0) * 100, 1) AS COMPILE_PCT,
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
    ELAPSED is wall-clock via TOTAL_ELAPSED_SEC (V060), matching the live twin;
    COALESCE degrades pre-V060 rows to the exec basis (v4.70.1 — the verify
    round caught this reader still on exec-time while the same-named metrics'
    live twin used true elapsed, silently changing the materialization-candidate
    gate by source). LAST_RUN degrades to the day grain. Qualified (f.) — see
    family_compile_heavy for the alias-shadow lesson."""
    days = bounded_days(days, 400)
    min_runs = max(2, min(int(min_runs or 10), 1000))
    parts = [f"f.DAY >= DATEADD('day', -{days}, CURRENT_DATE())",
             _company_arm(company, "f.COMPANY"),   # V082: exact company scope, not the DB heuristic
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
    ROUND(SUM(COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)) / 3600.0, 2) AS TOTAL_ELAPSED_HOURS,
    ROUND(SUM(COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)) / NULLIF(SUM(f.RUNS), 0), 2) AS AVG_ELAPSED_SEC,
    ROUND(SUM(COALESCE(f.GB_SCANNED_AVG, 0) * f.RUNS) / 1024, 4) AS TOTAL_TB_SCANNED,
    ROUND(SUM(COALESCE(f.CACHE_PCT_AVG, 0) * f.RUNS) / NULLIF(SUM(f.RUNS), 0) * 100, 1) AS AVG_CACHE_PCT,
    ANY_VALUE(f.SAMPLE_TEXT) AS QUERY_PREVIEW,
    MAX(f.DAY) AS LAST_RUN
FROM {mart_object("MART_QUERY_FAMILY_DAILY")} f
WHERE {where}
GROUP BY f.QUERY_HASH
HAVING SUM(f.RUNS) >= {min_runs}
ORDER BY TOTAL_ELAPSED_HOURS DESC
LIMIT 50
"""

def role_share(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """chargeback_sql.role_share_within_warehouse contract from the role-hour
    fact. Attribution law (v4.34.1): the fact's COMPANY column scopes
    warehouses in the denominator; the TRXS role heuristic (live-round-3
    lesson) only picks display rows AFTER the share is computed, so an
    excluded role keeps its slice and this company's roles never absorb it."""
    days = bounded_days(days, 400)
    # bounds -> the 'Last month' calendar window; the coverage-gate start follows it
    cov_start = (f"'{bounds[0].isoformat()}'" if bounds is not None
                 else f"DATEADD('day', -{days} + 1, CURRENT_DATE())")
    where = and_where(scope_window_where("HOUR_TS", days, bounds=bounds),  # verify round: match live twin's anchor
                      _company_arm(company),
                      # Coverage gate — abstain (zero rows -> live fallback + pool-rematch) ONLY when
                      # the role-hour fact is MATERIALLY SHORTER than the credit POOL fact AND the
                      # window reaches past the role fact's earliest day. That is the only case the
                      # short-share x long-pool over-attribution bites (owner sets
                      # FACT_RETENTION_DAYS_HOURLY below FACT_RETENTION_DAYS_DAILY). Reach-back is
                      # measured against the POOL fact, not the ask, so a young account / short window
                      # (both legs equally limited) still serves. The 7-day slack absorbs the ROUTINE
                      # boundary offset — the role fact starts at the first role-TAGGED query, the
                      # warehouse fact at the first metered (incl. idle) day, so pool commonly leads
                      # by a day or two — while any real retention purge (tens+ of days) still abstains.
                      f"NOT ((SELECT MIN(HOUR_TS)::DATE FROM {mart_object('FACT_QUERY_ROLE_HOURLY')}) "
                      f"> {cov_start} "
                      f"AND (SELECT MIN(DAY) FROM {mart_object('FACT_WAREHOUSE_DAILY')}) "
                      f"< DATEADD('day', -7, (SELECT MIN(HOUR_TS)::DATE FROM {mart_object('FACT_QUERY_ROLE_HOURLY')})))")
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
                          schema_contains: str = "", *, bounds: tuple | None = None) -> str:
    """ops_sql.query_window_summary contract from the schema-hour fact — the
    read that used to force a live QUERY_HISTORY scan whenever a schema
    filter was active. P95 is the peak hourly-group p95 (callers label it)."""
    days = bounded_days(days, 400)
    parts = [scope_window_where("HOUR_TS", days, bounds=bounds),  # triage #12: match live CURRENT_DATE anchor
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


def ai_costs_by_model(days: int, *, bounds: tuple | None = None) -> str:
    """cortex_sql model/source cost contract from FACT_AI_USAGE_DAILY —
    Code + Functions in one read, loaded daily. Qualified (a.) — TOKENS and
    CREDITS output aliases shadowed the columns in the per-1M expression
    (same class as the compile-heavy live failure)."""
    days = bounded_days(days, 400)
    win = scope_window_where("a.DAY", days, bounds=bounds)
    cov_bound = (f"'{bounds[0].isoformat()}'" if bounds is not None
                 else f"DATEADD('day', -{days} + 1, CURRENT_DATE())")
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
WHERE {win}
  -- Coverage gate (same guard as ai_code_user_rollup/_daily, ALL sources here since this
  -- serves Code + Functions): emit ZERO rows unless the fact's earliest day covers the asked
  -- window, so a young/backfilling fact falls back to the live Functions reader (honestly
  -- labeled ' - Functions only' + the 90d cap) instead of answering a 365d question with three
  -- weeks of credits and silently UNDER-REPORTING account AI spend under a full-window label.
  AND (SELECT MIN(a2.DAY) FROM {mart_object("FACT_AI_USAGE_DAILY")} a2)
      <= {cov_bound}
GROUP BY a.SOURCE, a.MODEL_NAME
ORDER BY CREDITS DESC
LIMIT 200
"""


# --- Cortex Code user attribution off the fact (P2) --------------------------
# The two live CORTEX_CODE_* scans behind Cost > Chargeback & AI cost 22s and
# 15s EVERY render — 30 of the Cost page's 88 slow fetches — and the numbers
# they compute are already in FACT_AI_USAGE_DAILY at (DAY, USER_NAME, SOURCE,
# MODEL_NAME) grain from V061 loader arm [9]. These two readers reproduce the
# live builders' output contracts column-for-column so the panel can go
# fact-first with the live scans as fallback.
#
# SOURCE <> 'Functions' on both: the live builders read ONLY the Snowsight/CLI
# code views. The fact also carries the Functions arm (USER_NAME 'ACCOUNT',
# a synthetic account-wide row) — including it would inflate ACTIVE_USERS and
# put a non-person in the per-user chargeback table.
#
# COVERAGE GATE (the same guard as unused_roles_via_fact): emit ZERO rows
# unless the fact's own first day is at or before the start of the asked
# window. run_mart_first/the panel only fall back to live on an EMPTY or
# failed mart read, so without this a 3-week-old fact would answer a 180/365d
# question with three weeks of credits and silently UNDER-REPORT the spend —
# the exact failure mode that makes a budget breach invisible. MIN(DAY) is
# taken over the SAME source filter we serve: the Functions arm's history
# says nothing about how far back the code views were loaded.
_AI_CODE_SOURCE_ARM = "SOURCE <> 'Functions'"


def _ai_code_coverage_cte() -> str:
    return f"""cov AS (
    SELECT MIN(DAY) AS FIRST_DAY
    FROM {mart_object("FACT_AI_USAGE_DAILY")}
    WHERE {_AI_CODE_SOURCE_ARM}
)"""


def _ai_code_window(days: int, bounds: tuple | None = None) -> str:
    if bounds is not None:
        start, end = bounds
        # bounded 'Last month': the coverage guard requires the fact to cover from the
        # window START (else the panel understates the month).
        return (f"DAY >= '{start.isoformat()}' AND DAY < '{end.isoformat()}'\n"
                f"      AND {_AI_CODE_SOURCE_ARM}\n"
                f"      AND (SELECT FIRST_DAY FROM cov) <= '{start.isoformat()}'")
    return (f"DAY >= DATEADD('day', -{days}, CURRENT_DATE())\n"
            f"      AND {_AI_CODE_SOURCE_ARM}\n"
            f"      AND (SELECT FIRST_DAY FROM cov) "
            f"<= DATEADD('day', -{days} + 1, CURRENT_DATE())")


def ai_code_user_rollup(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """cortex_sql.cortex_code_user_rollup contract from FACT_AI_USAGE_DAILY.

    Company scope is applied ONCE per grouped user (a ~50-row set), not per
    fact row — same reasoning as the live builder's outer WHERE.

    FIRST_NAME/LAST_NAME are not on the fact but ARE part of the live
    contract (they drive DISPLAY_NAME on the spend chart, owner ask v4.50),
    so they ride a post-aggregation join to the small USERS dimension. The
    join is pre-collapsed to one row per NAME: a recreated login yields
    several USERS rows, and a fan-out here would DOUBLE a user's credits.
    """
    days = bounded_days(days, 400)
    scope = ""
    if str(company or "ALL").upper() != "ALL":
        scope = f"WHERE {companies.COMPANY_FOR_USER_FN}(b.USER_NAME) = {sql_literal(company)}"
    return f"""
WITH {_ai_code_coverage_cte()},
by_user AS (
    SELECT
        USER_NAME,
        ANY_VALUE(EMAIL) AS EMAIL,
        SOURCE,
        COUNT(DISTINCT DAY) AS ACTIVE_DAYS,
        SUM(COALESCE(REQUESTS, 0)) AS TOTAL_REQUESTS,
        SUM(COALESCE(CREDITS, 0)) AS TOTAL_CREDITS,
        SUM(COALESCE(TOKENS, 0)) AS TOTAL_TOKENS,
        MIN(FIRST_TS) AS FIRST_USAGE,
        MAX(LAST_TS) AS LAST_USAGE
    FROM {mart_object("FACT_AI_USAGE_DAILY")}
    WHERE {_ai_code_window(days, bounds)}
    GROUP BY USER_NAME, SOURCE
),
named AS (
    SELECT NAME, ANY_VALUE(FIRST_NAME) AS FIRST_NAME, ANY_VALUE(LAST_NAME) AS LAST_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS
    WHERE DELETED_ON IS NULL
    GROUP BY NAME
)
SELECT
    b.USER_NAME,
    b.EMAIL,
    n.FIRST_NAME,
    n.LAST_NAME,
    b.SOURCE,
    b.ACTIVE_DAYS,
    b.TOTAL_REQUESTS,
    b.TOTAL_CREDITS,
    b.TOTAL_TOKENS,
    b.FIRST_USAGE,
    b.LAST_USAGE,
    b.TOTAL_CREDITS / NULLIF(b.TOTAL_REQUESTS, 0) AS CREDITS_PER_REQUEST,
    b.TOTAL_CREDITS / NULLIF(b.ACTIVE_DAYS, 0) AS AVG_DAILY_CREDITS
FROM by_user b
LEFT JOIN named n ON n.NAME = b.USER_NAME
{scope}
ORDER BY b.TOTAL_CREDITS DESC
LIMIT 500
"""


def ai_code_daily(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """cortex_sql.cortex_code_daily contract from FACT_AI_USAGE_DAILY.

    ACTIVE_USERS counts distinct USER_NAME where the live builder counts
    distinct USER_ID — the fact has no USER_ID. Same number unless a login
    was dropped and recreated inside the window, where counting the PERSON
    is the honest answer anyway.

    Company scope evaluates COMPANY_FOR_USER once per DISTINCT user rather
    than once per fact row (the live builder's per-raw-row UDF call is the
    P9 finding); the day/source grouping then needs no UDF at all.
    """
    days = bounded_days(days, 400)
    scope = ""
    if str(company or "ALL").upper() != "ALL":
        scope = f"""
      AND USER_NAME IN (
          SELECT USER_NAME FROM (
              SELECT DISTINCT USER_NAME
              FROM {mart_object("FACT_AI_USAGE_DAILY")}
              WHERE {scope_window_where("DAY", days, bounds=bounds)}
                AND {_AI_CODE_SOURCE_ARM}
          )
          WHERE {companies.COMPANY_FOR_USER_FN}(USER_NAME) = {sql_literal(company)}
      )"""
    return f"""
WITH {_ai_code_coverage_cte()}
SELECT
    DAY,
    SOURCE,
    COUNT(DISTINCT USER_NAME) AS ACTIVE_USERS,
    SUM(COALESCE(REQUESTS, 0)) AS TOTAL_REQUESTS,
    SUM(COALESCE(CREDITS, 0)) AS TOTAL_CREDITS,
    SUM(COALESCE(TOKENS, 0)) AS TOTAL_TOKENS
FROM {mart_object("FACT_AI_USAGE_DAILY")}
WHERE {_ai_code_window(days, bounds)}{scope}
GROUP BY DAY, SOURCE
ORDER BY DAY, SOURCE
LIMIT 5000
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


def tag_coverage_daily(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """cost_sql.tag_coverage contract from MART_TAG_COVERAGE_DAILY (V031) —
    the user-grain column the family mart could not carry. Qualified (c.)
    per the alias-shadow rule; same 60s exec floor as live."""
    days = bounded_days(days, 400)
    where = and_where(scope_window_where("c.DAY", days, bounds=bounds),
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


def lock_wait_daily(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """Lock waits from MART_LOCK_WAIT_DAILY (V035) — the live scan read
    46-56 GB per view; the daily task pays that once. Same ranking as the
    live builder: never-acquired first (those are the aborted statements)."""
    d = bounded_days(days, 90)
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
WHERE {scope_window_where("c.DAY", d, bounds=bounds)}
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
        SUM(IFF(c.DAY = DATEADD('day', -1, CURRENT_DATE()), c.WAIT_EVENTS, 0)) AS LAST_DAY_WAITS,
        ROUND(SUM(IFF(c.DAY < DATEADD('day', -1, CURRENT_DATE()), c.WAIT_EVENTS, 0)) / 6.0, 1)
            AS PRIOR_DAILY_AVG,
        SUM(IFF(c.DAY = DATEADD('day', -1, CURRENT_DATE()), c.NEVER_ACQUIRED, 0))
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


def pattern_cost(days: int = 30, company: str = "ALL", limit: int = 25, *, bounds: tuple | None = None) -> str:
    """Measured $ per repeated statement pattern (V036) — the silent-spend
    table. Attribution credits are MEASURED compute; the sample text rides
    in from the family mart by hash."""
    d = bounded_days(days, 90)
    # bounds -> scale the run-rate floor to the calendar-month span, not the trailing d
    span = (bounds[1] - bounds[0]).days if bounds is not None else d
    min_runs = max(2, (5 * span + 29) // 30)
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
    WHERE {scope_window_where("DAY", d, bounds=bounds)}
    GROUP BY QUERY_HASH
) f ON f.QUERY_HASH = p.QUERY_HASH
WHERE {scope_window_where("p.DAY", d, bounds=bounds)}
{comp}GROUP BY p.QUERY_HASH
HAVING SUM(p.CREDITS_ATTRIBUTED) > 0.01 AND SUM(p.RUNS) >= {min_runs}
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
    company-scopable spend total (FACT_WAREHOUSE_DAILY, exact usage).

    #34: each row also carries a per-side COVERAGE contract — A_DAYS/B_DAYS
    (COUNT(DISTINCT DAY)) and A_MIN_DAY/A_MAX_DAY/B_MIN_DAY/B_MAX_DAY — repeated
    from a single-row CTE. A partial backfill (one side missing days) otherwise
    manufactures false movers and 100% deltas: a warehouse present in A but not
    yet loaded in B reads as +100%. The caller gates/annotates the deltas when
    either side's day count is short of its window length."""
    in_a, in_b, either = _side_windows(a_start, a_end, b_start, b_end)
    comp = ""
    comp_where = ""
    if company and company != "ALL":
        _clit = companies.sql_literal(company)
        comp = f"  AND COMPANY = {_clit}\n"
        comp_where = f"\n    WHERE COMPANY = {_clit}"
    return f"""WITH movers AS (
    SELECT
        WAREHOUSE_NAME,
        SUM(IFF({in_a}, CREDITS_TOTAL, 0)) AS A_CREDITS,
        SUM(IFF({in_b}, CREDITS_TOTAL, 0)) AS B_CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
    WHERE {either}
{comp}    GROUP BY WAREHOUSE_NAME
    HAVING SUM(CREDITS_TOTAL) > 0
),
cov AS (
    SELECT
        COUNT(DISTINCT CASE WHEN {in_a} THEN DAY END) AS A_DAYS,
        COUNT(DISTINCT CASE WHEN {in_b} THEN DAY END) AS B_DAYS,
        MIN(CASE WHEN {in_a} THEN DAY END) AS A_MIN_DAY,
        MAX(CASE WHEN {in_a} THEN DAY END) AS A_MAX_DAY,
        MIN(CASE WHEN {in_b} THEN DAY END) AS B_MIN_DAY,
        MAX(CASE WHEN {in_b} THEN DAY END) AS B_MAX_DAY,
        -- Account/company-wide spend TOTALS across every warehouse (not window-ranked, not
        -- LIMITed): the strip's "Warehouse spend" LEVEL KPI must sum ALL warehouses, but the
        -- movers list is ORDER BY |A-B| DESC LIMIT 100, which drops the largest STEADY spender
        -- first -- so summing the returned frame understated the headline. These totals ride the
        -- single-row cov CROSS JOIN and survive the LIMIT (bug-hunt 2026-08-30).
        SUM(IFF({in_a}, CREDITS_TOTAL, 0)) AS TOTAL_A_CREDITS,
        SUM(IFF({in_b}, CREDITS_TOTAL, 0)) AS TOTAL_B_CREDITS,
        -- Loader's GLOBAL reach for this company scope (NOT window-bounded): idle
        -- calendar days inside a window don't lower it, so it separates "loader
        -- hasn't reached this window's end yet" (real partial backfill) from "no
        -- warehouse activity that day" (sparse fact, not a gap). #34 coverage check.
        (SELECT MAX(DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY{comp_where}) AS LOADED_THROUGH
    FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
    WHERE {either}
{comp})
SELECT
    m.WAREHOUSE_NAME,
    m.A_CREDITS,
    m.B_CREDITS,
    cov.A_DAYS,
    cov.B_DAYS,
    cov.A_MIN_DAY,
    cov.A_MAX_DAY,
    cov.B_MIN_DAY,
    cov.B_MAX_DAY,
    cov.TOTAL_A_CREDITS,
    cov.TOTAL_B_CREDITS,
    cov.LOADED_THROUGH
FROM movers m
CROSS JOIN cov
ORDER BY ABS(m.A_CREDITS - m.B_CREDITS) DESC
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
    by construction; the strip labels it so). Carries the AI/OTHER split (C1)
    so the caller prices AI credits at the AI rate, not the compute rate."""
    in_a, _in_b, either = _side_windows(a_start, a_end, b_start, b_end)
    _ai = ai_service_predicate()
    return f"""SELECT
    IFF({in_a}, 'A', 'B') AS SIDE,
    SUM(CREDITS_BILLED) AS CREDITS_BILLED,
    SUM(CASE WHEN {_ai} THEN CREDITS_BILLED ELSE 0 END) AS CREDITS_BILLED_AI,
    SUM(CASE WHEN {_ai} THEN 0 ELSE CREDITS_BILLED END) AS CREDITS_BILLED_OTHER
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


def compare_pattern_costs_by_warehouse(a_start: str, a_end: str, b_start: str,
                                       b_end: str, warehouse: str = "",
                                       limit: int = 25) -> str:
    """Pattern movers scoped to ONE warehouse — the #6 scope-to-warehouse drill.

    MART_PATTERN_COST_DAILY (V037) aggregates WAREHOUSE_NAME away, so a per
    -warehouse pattern cut can only come from the one place
    QUERY_PARAMETERIZED_HASH and WAREHOUSE_NAME coexist: a LIVE
    QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY scan. This is interaction-gated
    in Compare (fires only on a warehouse-row click, never first paint), the
    single exception to the tab's zero-live-scan invariant.

    An exact WAREHOUSE_NAME is a COMPLETE scope, so — like
    cost_sql.compile_heavy_families — NO company predicate is applied (a
    hardcoded company tuple would only subtract, and can zero a warehouse
    mapped to a company via the runtime COMPANY_SCOPE table). Output columns
    mirror compare_pattern_costs (QUERY_HASH/SAMPLE_TEXT/A_RUNS/B_RUNS/
    A_CREDITS/B_CREDITS) so the Compare render block is reused verbatim.
    Credits are measured CREDITS_ATTRIBUTED_COMPUTE (+ query-acceleration) —
    the same attribution basis as the account-wide mart, at ~8h view lag.
    """
    in_a, in_b, either = _side_windows(a_start, a_end, b_start, b_end,
                                       col="q.START_TIME")
    lim = max(5, min(int(limit), 100))
    _credits = ("a.CREDITS_ATTRIBUTED_COMPUTE "
                "+ COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)")
    _earliest = f"LEAST('{_iso(a_start)}'::DATE, '{_iso(b_start)}'::DATE)"
    return f"""
WITH q AS (
    SELECT QUERY_ID, QUERY_PARAMETERIZED_HASH, START_TIME,
           LEFT(QUERY_TEXT, 140) AS SAMPLE_TEXT
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE {either}
      AND WAREHOUSE_NAME = {sql_literal(warehouse)}
      AND QUERY_PARAMETERIZED_HASH IS NOT NULL
)
-- Join the FILTERED window first, THEN aggregate credits: pre-aggregating the
-- whole attribution view is the 139s-family slow path (insights_sql
-- .measured_query_costs / Codex #11) — the pre-filter-then-join order avoids it.
SELECT
    q.QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
    ANY_VALUE(q.SAMPLE_TEXT) AS SAMPLE_TEXT,
    COUNT(DISTINCT CASE WHEN {in_a} THEN q.QUERY_ID END) AS A_RUNS,
    COUNT(DISTINCT CASE WHEN {in_b} THEN q.QUERY_ID END) AS B_RUNS,
    SUM(IFF({in_a}, {_credits}, 0)) AS A_CREDITS,
    SUM(IFF({in_b}, {_credits}, 0)) AS B_CREDITS
FROM q
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
  ON a.QUERY_ID = q.QUERY_ID
 AND a.START_TIME >= DATEADD('day', -1, {_earliest})
GROUP BY q.QUERY_PARAMETERIZED_HASH
HAVING GREATEST(SUM(IFF({in_a}, {_credits}, 0)),
                SUM(IFF({in_b}, {_credits}, 0))) > 0.01
ORDER BY ABS(SUM(IFF({in_a}, {_credits}, 0)) - SUM(IFF({in_b}, {_credits}, 0))) DESC
LIMIT {lim}
"""


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
  AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
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
                           database: str = "", *, bounds: tuple | None = None) -> str:
    """cost_sql.allocated_attribution contract from FACT_COST_ALLOC_XDIM_DAILY
    (V041 R2) — the database/user-filtered attribution that used to pay two
    live QUERY_HISTORY scans per filter value; user-within-database is now
    mart-served on Spend. Global-share law preserved (v4.33.1): company scope
    (warehouse grain, matching the live builder) sets the denominator; the
    database filter and dimension visibility rules only pick which rows
    DISPLAY. No schema grain here by design — schema-filtered views stay on
    the live builder. Qualified (x.) per the alias-shadow rule."""
    # rec 4: the share DENOMINATOR must span the SAME half-open window as the
    # warehouse dollar POOL it will be multiplied by (spend.py), else a 365-day share
    # (including today's partial) applied to the pool's clamped 182-day, today-excluded
    # dollars mis-attributes per-entity cost. resolve_effective_window is the one truth.
    days, _win = resolve_effective_window(days, "x.DAY", bounds=bounds)
    dim = str(dimension or "USER").upper()
    if dim not in ("USER", "DATABASE"):
        raise ValueError(f"dimension must be USER/DATABASE, got {dimension!r}")
    dim_col = "x.USER_NAME" if dim == "USER" else "x.DATABASE_NAME"
    scope_where = and_where(
        _win,
        companies.warehouse_clause(company, "x.WAREHOUSE_NAME"),
    )
    vis = (companies.user_clause(company, "KEY_NAME") if dim == "USER"
           else companies.database_visibility_clause(company, "KEY_NAME"))
    display = and_where(companies.database_equals_clause(database, "DATABASE_NAME"), vis)
    # #14: the coverage probe must be measured on the SAME company/database scope
    # the panel serves. MIN(DAY) over the WHOLE table let one company's year-old
    # rows satisfy the window gate for a DIFFERENT company that only holds three
    # weeks — the young scope's short answer then passed as a full-window one and
    # silently UNDER-REPORTED. Scope cov by the company (warehouse grain, the
    # denominator's scope) and the selected database so FIRST_DAY reflects how far
    # back THIS scope actually reaches. ALL/no-filter -> whole table (unchanged).
    cov_scope = and_where(
        companies.warehouse_clause(company, "x.WAREHOUSE_NAME"),
        companies.database_equals_clause(database, "x.DATABASE_NAME"),
    )
    # #18: the old gate ``FIRST_DAY <= today - days + 1`` permitted ONE missing
    # day, so a 7d answer could be ~14% incomplete (the window's first day absent)
    # and still pass, silently under-reporting. Require BOTH the exact lower bound
    # (the fact reaches the window's first day, today - days) AND full interior
    # coverage (COUNT(DISTINCT DAY) inside the window >= the effective days) so a
    # gappy backfill yields to the live fallback instead of answering short.
    return f"""
WITH cov AS (
    SELECT MIN(DAY) AS FIRST_DAY,
           COUNT(DISTINCT CASE
                   WHEN DAY >= DATEADD('day', -{days}, CURRENT_DATE())
                    AND DAY < CURRENT_DATE() THEN DAY END) AS WINDOW_DAYS
    FROM {mart_object("FACT_COST_ALLOC_XDIM_DAILY")} x
    WHERE {cov_scope}
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
  AND (SELECT FIRST_DAY FROM cov) <= DATEADD('day', -{days}, CURRENT_DATE())
  AND (SELECT WINDOW_DAYS FROM cov) >= {days}
GROUP BY KEY_NAME
ORDER BY ALLOC_CREDITS DESC
LIMIT 100
"""


def ops_diag_top_queries(days: int, company: str = "ALL", limit: int = 50, *,
                         bounds: tuple | None = None) -> str:
    """ops_sql.top_queries_by_elapsed contract from MART_OPS_DIAG_HOURLY
    (V041 R7, corrected v4.36.1) — the UNFILTERED Operations first paint
    only: an entity or schema filter needs the true filtered top-N, which
    only the live scan has. The mart keeps each hour's top-50: a member of
    the global top-50 is by construction inside its own hour's top-50, so
    the unfiltered panel is EXACT, not a sample. Coverage-gated while the
    mart accrues toward the asked window."""
    days = bounded_days(days, 90)
    limit = max(1, min(int(limit), 500))
    cov_bound = (f"'{bounds[0].isoformat()}'" if bounds is not None
                 else f"DATEADD('day', -{days} + 1, CURRENT_DATE())")
    where = and_where(
        "d.KIND = 'TOP_ELAPSED'",
        scope_window_where("d.HOUR_TS", days, bounds=bounds),
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
  AND (SELECT FIRST_TS FROM cov) <= {cov_bound}
ORDER BY d.ELAPSED_SEC DESC
LIMIT {limit}
"""


def ops_diag_failures(days: int, company: str = "ALL", *, bounds: tuple | None = None) -> str:
    """ops_sql.failures_by_error contract from MART_OPS_DIAG_HOURLY (V041 R7,
    corrected v4.36.1). USERS_AFFECTED combines the mart's hourly HLL states
    (V037 precedent) — an honest window approx-distinct, not a peak-hour
    stand-in. Unfiltered first paint only; coverage-gated like the
    top-queries reader."""
    days = bounded_days(days, 90)
    cov_bound = (f"'{bounds[0].isoformat()}'" if bounds is not None
                 else f"DATEADD('day', -{days} + 1, CURRENT_DATE())")
    where = and_where(
        "d.KIND = 'FAIL_FAMILY'",
        scope_window_where("d.HOUR_TS", days, bounds=bounds),
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
    HLL_ESTIMATE(HLL_COMBINE(d.USERS_HLL)) AS USERS_AFFECTED,
    MAX(d.LAST_SEEN) AS LAST_SEEN
FROM {mart_object("MART_OPS_DIAG_HOURLY")} d
WHERE {where}
  AND (SELECT FIRST_TS FROM cov) <= {cov_bound}
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
    # C1 (V061): CREDITS_BILLED_AI is the AI partition of billed credits so the
    # score's budget_pct prices AI at the AI rate. COALESCE(...,0) degrades pre-V061
    # rows (NULL until SP_LOAD_PLATFORM_SCORE backfills) to all-compute, never NaN.
    # Column list stays aligned with score_inputs_daily (run_mart_first swaps them).
    return f"""
SELECT DAY, CREDITS_BILLED, COALESCE(CREDITS_BILLED_AI, 0) AS CREDITS_BILLED_AI,
       QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB,
       TASK_RUNS, TASK_FAILED, CRIT_RAISED, HIGH_RAISED
FROM {mart_object("FACT_PLATFORM_SCORE_DAILY")}
WHERE DAY >= DATEADD('day', -{days}, CURRENT_DATE())
ORDER BY DAY
"""
