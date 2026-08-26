"""Read-only SQL builders for the operating and investigation workbenches."""

from __future__ import annotations

from app.config import core_object
from app.core.sqlsafe import clean_filter_text, contains_filter, sql_literal
from app.data.common import and_where, bounded_days


def _entity_type(value: str) -> str:
    return str(value or "").strip().upper()[:40]


def action_center(company: str = "ALL", include_closed: bool = False,
                  limit: int = 500) -> str:
    """Extended ACTION_QUEUE shape installed by V074."""
    cap = max(1, min(int(limit), 1000))
    clauses: list[str] = []
    if not include_closed:
        clauses.append("UPPER(STATUS) IN ('OPEN', 'IN_PROGRESS')")
    if str(company or "ALL").upper() != "ALL":
        clauses.append(
            f"(UPPER(COMPANY) IN ({sql_literal(str(company).upper())}, 'ALL'))"
        )
    return f"""
SELECT ACTION_ID, CREATED_AT, COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS,
       DUE_DATE, DEFER_UNTIL, COMPLETED_AT, RESOLUTION_NOTE, SOURCE,
       SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY, CONFIDENCE, PROOF_SQL,
       ESTIMATED_USD, PERIOD, UPDATED_AT, UPDATED_BY
FROM {core_object("ACTION_QUEUE")}
WHERE {and_where(*clauses)}
ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
              WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END,
         IFF(DUE_DATE < CURRENT_DATE(), 0, 1),
         ESTIMATED_USD DESC NULLS LAST, CREATED_AT
LIMIT {cap}
"""


def action_activity(action_id: str, limit: int = 200) -> str:
    cap = max(1, min(int(limit), 500))
    return f"""
SELECT ACTIVITY_ID, ACTION_ID, ACTIVITY_TYPE, FROM_STATUS, TO_STATUS,
       OWNER_NAME, DUE_DATE, DEFER_UNTIL, NOTE, CREATED_AT, CREATED_BY
FROM {core_object("ACTION_ACTIVITY")}
WHERE ACTION_ID = {sql_literal(str(action_id), 80)}
ORDER BY CREATED_AT DESC
LIMIT {cap}
"""


def evidence_links(entity_type: str, entity_key: str, limit: int = 300) -> str:
    cap = max(1, min(int(limit), 1000))
    kind = _entity_type(entity_type)
    key = str(entity_key or "").strip()[:500]
    return f"""
SELECT LINK_ID, FROM_TYPE, FROM_ID, TO_TYPE, TO_ID, RELATIONSHIP, LABEL,
       QUERY_ID, EVIDENCE_NOTE, CREATED_AT, CREATED_BY
FROM {core_object("EVIDENCE_LINKS")}
WHERE (UPPER(FROM_TYPE) = {sql_literal(kind)} AND UPPER(FROM_ID) = {sql_literal(key.upper())})
   OR (UPPER(TO_TYPE) = {sql_literal(kind)} AND UPPER(TO_ID) = {sql_literal(key.upper())})
ORDER BY CREATED_AT DESC
LIMIT {cap}
"""


def entity_catalog(entity_type: str = "", search: str = "", limit: int = 500) -> str:
    cap = max(1, min(int(limit), 1000))
    clauses = []
    if _entity_type(entity_type):
        clauses.append(f"UPPER(ENTITY_TYPE) = {sql_literal(_entity_type(entity_type))}")
    clean_search = clean_filter_text(search)
    if clean_search:
        needle = contains_filter("ENTITY_KEY", clean_search)
        label = contains_filter("LABEL", clean_search)
        product = contains_filter("DATA_PRODUCT", clean_search)
        clauses.append(f"({needle} OR {label} OR {product})")
    return f"""
SELECT ENTITY_TYPE, ENTITY_KEY, LABEL, COMPANY, TEAM, OWNER_NAME, STEWARD,
       ON_CALL, CRITICALITY, DATA_PRODUCT, SLO_NAME, NOTES, UPDATED_AT, UPDATED_BY
FROM {core_object("ENTITY_CATALOG")}
WHERE {and_where(*clauses)}
ORDER BY CASE UPPER(CRITICALITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
              WHEN 'STANDARD' THEN 2 ELSE 3 END,
         ENTITY_TYPE, COALESCE(LABEL, ENTITY_KEY)
LIMIT {cap}
"""


def entity_record(entity_type: str, entity_key: str) -> str:
    return f"""
SELECT ENTITY_TYPE, ENTITY_KEY, LABEL, COMPANY, TEAM, OWNER_NAME, STEWARD,
       ON_CALL, CRITICALITY, DATA_PRODUCT, SLO_NAME, NOTES, UPDATED_AT, UPDATED_BY
FROM {core_object("ENTITY_CATALOG")}
WHERE UPPER(ENTITY_TYPE) = {sql_literal(_entity_type(entity_type))}
  AND UPPER(ENTITY_KEY) = {sql_literal(str(entity_key or '').strip().upper(), 500)}
LIMIT 1
"""


def related_actions(entity_type: str, entity_key: str, limit: int = 200) -> str:
    cap = max(1, min(int(limit), 500))
    return f"""
SELECT ACTION_ID, CREATED_AT, SEVERITY, TITLE, OWNER, STATUS, DUE_DATE,
       DEFER_UNTIL, ESTIMATED_USD, CONFIDENCE, UPDATED_AT
FROM {core_object("ACTION_QUEUE")}
WHERE UPPER(SOURCE_ENTITY_TYPE) = {sql_literal(_entity_type(entity_type))}
  AND UPPER(SOURCE_ENTITY_KEY) = {sql_literal(str(entity_key or '').strip().upper(), 500)}
ORDER BY IFF(UPPER(STATUS) IN ('OPEN', 'IN_PROGRESS'), 0, 1), UPDATED_AT DESC
LIMIT {cap}
"""


def related_remediations(entity_key: str, limit: int = 100) -> str:
    cap = max(1, min(int(limit), 500))
    return f"""
SELECT EXECUTED_AT, FINDING_TYPE, TARGET_OBJECT, EST_MONTHLY_SAVINGS_USD,
       STATUS, RESULT_NOTE, EXECUTED_BY
FROM {core_object("REMEDIATION_LOG")}
WHERE UPPER(TARGET_OBJECT) = {sql_literal(str(entity_key or '').strip().upper(), 500)}
ORDER BY EXECUTED_AT DESC
LIMIT {cap}
"""


def related_savings(entity_key: str, limit: int = 100) -> str:
    cap = max(1, min(int(limit), 500))
    return f"""
SELECT ITEM_ID, ACTION_ID, CREATED_AT, DESCRIPTION, STATE, ESTIMATED_USD,
       VERIFIED_USD, VERIFIED_AT, VERIFIED_BY, FINDING_TYPE, TARGET_OBJECT,
       PROOF_QUERY_ID, PROOF_RUN_AT
FROM {core_object("SAVINGS_LEDGER")}
WHERE UPPER(TARGET_OBJECT) = {sql_literal(str(entity_key or '').strip().upper(), 500)}
ORDER BY CREATED_AT DESC
LIMIT {cap}
"""


def watchlist(viewer_name: str) -> str:
    return f"""
SELECT VIEWER_NAME, ENTITY_TYPE, ENTITY_KEY, LABEL, CREATED_AT
FROM {core_object("USER_WATCHLIST")}
WHERE UPPER(VIEWER_NAME) = {sql_literal(str(viewer_name or '').strip().upper(), 200)}
ORDER BY CREATED_AT DESC
"""


def experiments(status: str = "", entity_type: str = "", entity_key: str = "",
                limit: int = 300) -> str:
    cap = max(1, min(int(limit), 1000))
    clauses = []
    if str(status or "").strip():
        clauses.append(f"UPPER(STATUS) = {sql_literal(str(status).strip().upper(), 30)}")
    if _entity_type(entity_type):
        clauses.append(f"UPPER(ENTITY_TYPE) = {sql_literal(_entity_type(entity_type))}")
    if str(entity_key or "").strip():
        clauses.append(
            f"UPPER(ENTITY_KEY) = {sql_literal(str(entity_key).strip().upper(), 500)}"
        )
    return f"""
SELECT EXPERIMENT_ID, ACTION_ID, ENTITY_TYPE, ENTITY_KEY, TITLE, HYPOTHESIS,
       BASELINE_NOTE, TARGET_NOTE, ROLLBACK_CONDITION, OBSERVATION_END, STATUS,
       RESULT_NOTE, VERIFIED_USD, CREATED_AT, CREATED_BY, UPDATED_AT, UPDATED_BY
FROM {core_object("OPTIMIZATION_EXPERIMENTS")}
WHERE {and_where(*clauses)}
ORDER BY IFF(UPPER(STATUS) IN ('PLANNED', 'RUNNING', 'OBSERVING'), 0, 1),
         OBSERVATION_END NULLS LAST, UPDATED_AT DESC
LIMIT {cap}
"""


def slo_objectives(active_only: bool = True, entity_type: str = "",
                   entity_key: str = "") -> str:
    clauses = ["ACTIVE"] if active_only else []
    if _entity_type(entity_type):
        clauses.append(f"UPPER(ENTITY_TYPE) = {sql_literal(_entity_type(entity_type))}")
    if str(entity_key or "").strip():
        clauses.append(
            f"UPPER(ENTITY_KEY) = {sql_literal(str(entity_key).strip().upper(), 500)}"
        )
    return f"""
SELECT SLO_ID, NAME, ENTITY_TYPE, ENTITY_KEY, METRIC_KEY, COMPARATOR,
       TARGET_VALUE, ERROR_BUDGET_PCT, WINDOW_DAYS, OWNER_NAME, ACTIVE,
       NOTES, UPDATED_AT, UPDATED_BY
FROM {core_object("SLO_OBJECTIVES")}
WHERE {and_where(*clauses)}
ORDER BY ENTITY_TYPE, ENTITY_KEY, NAME
"""


ENTITY_METRIC_TYPES = (
    "WAREHOUSE",
    "DATABASE",
    "OBJECT",
    "TASK",
    "QUERY_FINGERPRINT",
    "USER",
    "ROLE",
)


def entity_metric_snapshot(entity_type: str = "WAREHOUSE", entity_key: str = "WH",
                           days: int = 30) -> str:
    """Standard long-form metric snapshot for an Entity 360 summary."""
    kind = _entity_type(entity_type)
    if kind not in ENTITY_METRIC_TYPES:
        raise ValueError(f"No metric snapshot is defined for {kind or 'blank'}")
    key = str(entity_key or "").strip().upper()
    if not key:
        raise ValueError("entity_key is required")
    horizon = bounded_days(days, 400)
    literal = sql_literal(key, 500)
    if kind != "WAREHOUSE":
        return _entity_metric_nonwarehouse(kind, literal, horizon)
    if kind == "WAREHOUSE":
        return f"""
SELECT METRIC, VALUE, UNIT, BASIS, AS_OF
FROM (
    SELECT 'Credits' METRIC, SUM(CREDITS_TOTAL)::FLOAT VALUE, 'credits' UNIT,
           'METERED' BASIS, MAX(DAY)::TIMESTAMP_NTZ AS AS_OF
    FROM {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(WAREHOUSE_NAME) = {literal}
    UNION ALL
    SELECT 'Queries', SUM(QUERIES)::FLOAT, 'count', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(WAREHOUSE_NAME) = {literal}
    UNION ALL
    SELECT 'Failures', SUM(FAILS)::FLOAT, 'count', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(WAREHOUSE_NAME) = {literal}
    UNION ALL
    SELECT 'P95 runtime', MAX(P95_S)::FLOAT, 'seconds', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(WAREHOUSE_NAME) = {literal}
    UNION ALL
    SELECT 'Remote spill', SUM(SPILL_GB)::FLOAT, 'GB', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(WAREHOUSE_NAME) = {literal}
)
WHERE AS_OF IS NOT NULL
ORDER BY METRIC
"""
    raise AssertionError("unreachable entity metric type")


# CR15: the entity types the change registries actually track — WAREHOUSE
# settings (WAREHOUSE_CHANGE_REGISTRY) and PROCEDURE/TASK deploys keyed by
# object or database (OBJECT_CHANGE_REGISTRY). Other Entity 360 types have no
# change feed; the caller shows a precise note instead of an empty table.
ENTITY_CHANGE_TYPES = ("WAREHOUSE", "DATABASE", "TASK", "OBJECT")


def entity_recent_changes(entity_type: str, entity_key: str, days: int = 90) -> str:
    """Recent tracked changes affecting one Entity 360 entity (CR15).

    Reads the same change registries the Operations 'Change impact' tab
    surfaces, normalised to one shape (CHANGED_AT / CHANGE / CHANGED_BY /
    VERDICT / DETAIL) so the panel renders a single table. WAREHOUSE reads
    setting deltas; DATABASE/TASK/OBJECT read proc & task DDL (a DATABASE
    matches every tracked object in it, a TASK/OBJECT matches by name or FQN).
    Untracked types raise ValueError so the caller shows a 'not tracked' note.
    """
    kind = _entity_type(entity_type)
    if kind not in ENTITY_CHANGE_TYPES:
        raise ValueError(f"No change tracking is defined for {kind or 'blank'}")
    key = str(entity_key or "").strip().upper()
    if not key:
        raise ValueError("entity_key is required")
    horizon = bounded_days(days, 180)
    literal = sql_literal(key, 500)
    window = f"CHANGE_SEEN_AT >= DATEADD('day', -{horizon}, CURRENT_TIMESTAMP())"
    if kind == "WAREHOUSE":
        return f"""
SELECT
    CHANGE_SEEN_AT AS CHANGED_AT,
    SETTING || ': ' || COALESCE(OLD_VALUE, 'unset') || ' -> '
            || COALESCE(NEW_VALUE, 'unset') AS CHANGE,
    CHANGED_BY, VERDICT, VERDICT_DETAIL AS DETAIL
FROM {core_object('WAREHOUSE_CHANGE_REGISTRY')}
WHERE UPPER(WAREHOUSE_NAME) = {literal}
  AND {window}
ORDER BY CHANGE_SEEN_AT DESC
LIMIT 50
"""
    match = (f"UPPER(DATABASE_NAME) = {literal}" if kind == "DATABASE" else
             f"(UPPER(OBJECT_NAME) = {literal} OR "
             f"UPPER(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || OBJECT_NAME) = {literal})")
    # OBJECT_NAME in OBJECT_CHANGE_REGISTRY is already the fully-qualified DB.SCHEMA.NAME;
    # render it directly below — an extra DB.SCHEMA prefix doubled the identifier.
    return f"""
SELECT
    CHANGE_SEEN_AT AS CHANGED_AT,
    OBJECT_TYPE || ' ' || OBJECT_NAME AS CHANGE,
    CHANGED_BY, VERDICT, VERDICT_DETAIL AS DETAIL
FROM {core_object('OBJECT_CHANGE_REGISTRY')}
WHERE {match}
  AND {window}
ORDER BY CHANGE_SEEN_AT DESC
LIMIT 50
"""


def workload_portfolio(days: int = 30, company: str = "ALL",
                       limit: int = 200) -> str:
    """Measured recurring-query portfolio with cost and behavior evidence."""
    horizon = bounded_days(days, 400)
    cap = max(10, min(int(limit or 200), 500))
    company_clause = (
        "" if str(company or "ALL").upper() == "ALL"
        else f"AND p.COMPANY = {sql_literal(str(company), 40)}"
    )
    return f"""
WITH costs AS (
    SELECT p.QUERY_HASH,
           SUM(p.RUNS) AS RUNS,
           SUM(p.CREDITS_ATTRIBUTED) AS CREDITS,
           COUNT(DISTINCT p.DAY) AS ACTIVE_DAYS,
           COUNT(DISTINCT p.DATABASE_NAME) AS DATABASES,
           HLL_ESTIMATE(HLL_COMBINE(p.USERS_HLL)) AS USERS,
           MAX(p.DAY) AS LAST_SEEN
    FROM {core_object('MART_PATTERN_COST_DAILY')} p
    WHERE p.DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      {company_clause}
    GROUP BY p.QUERY_HASH
), scoped_families AS (
    -- DS #3 (V082): the family mart now carries COMPANY at row grain, so scope on
    -- the real company instead of the lossy ANY_VALUE(DATABASE_NAME) heuristic that
    -- dropped a family whose representative database wasn't in the company's set.
    SELECT DISTINCT p.QUERY_HASH, p.COMPANY
    FROM {core_object('MART_PATTERN_COST_DAILY')} p
    WHERE p.DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      {company_clause}
), families AS (
    SELECT f.QUERY_HASH,
           SUM(f.FAILS) AS FAILS,
           SUM(COALESCE(f.TOTAL_ELAPSED_SEC, f.TOTAL_EXEC_SEC)) AS TOTAL_ELAPSED_SEC,
           ROUND(SUM(COALESCE(f.CACHE_PCT_AVG, 0) * f.RUNS)
                 / NULLIF(SUM(f.RUNS), 0) * 100, 1) AS AVG_CACHE_PCT,
           MAX(f.P95_S) AS P95_SEC,
           ANY_VALUE(f.SAMPLE_TEXT) AS QUERY_PREVIEW
    FROM {core_object('MART_QUERY_FAMILY_DAILY')} f
    JOIN scoped_families s
      ON s.QUERY_HASH = f.QUERY_HASH AND s.COMPANY = f.COMPANY
    WHERE f.DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
    GROUP BY f.QUERY_HASH
)
-- FAILS stays RAW (NULL on a family-mart join miss), like AVG_CACHE_PCT/P95_SEC below:
-- decision.py tracks evidence PRESENCE from notna(FAILS), so coalescing to 0 made a blind
-- family look measured (has_behavior always true, EVIDENCE_COVERAGE floored at 0.33).
-- fillna(0) in decision.py keeps FAIL_PCT correct. (Codex #1, confirmed)
SELECT c.QUERY_HASH AS FINGERPRINT, c.RUNS, f.FAILS AS FAILS,
       ROUND(c.CREDITS, 4) AS CREDITS, c.ACTIVE_DAYS, c.DATABASES, c.USERS,
       ROUND(COALESCE(f.TOTAL_ELAPSED_SEC, 0) / 3600, 2) AS TOTAL_ELAPSED_HOURS,
       f.AVG_CACHE_PCT, f.P95_SEC, c.LAST_SEEN, f.QUERY_PREVIEW
FROM costs c
LEFT JOIN families f ON f.QUERY_HASH = c.QUERY_HASH
WHERE c.RUNS > 0 AND c.CREDITS > 0
  -- Owner finding 2026-08-17: the portfolio ranked OVERWATCH's OWN runtime
  -- ("execute streamlit ... OVERWATCH_APP()") as the #1 ACT-NOW — the monitoring
  -- tool telling you to optimize the monitoring tool. Exclude the app's own
  -- Streamlit-runtime family. COALESCE guards the LEFT-JOIN miss (Codex #1): a
  -- NULL preview must read as "not the app", i.e. stay IN the portfolio.
  AND UPPER(COALESCE(f.QUERY_PREVIEW, '')) NOT LIKE 'EXECUTE STREAMLIT%'
  AND UPPER(COALESCE(f.QUERY_PREVIEW, '')) NOT LIKE '%OVERWATCH_APP%'
ORDER BY c.CREDITS DESC
LIMIT {cap}
"""


def slo_cockpit() -> str:
    """Evaluate configured objectives against existing warehouse/task/family marts."""
    return f"""
WITH objectives AS (
    SELECT SLO_ID, NAME, ENTITY_TYPE, ENTITY_KEY, METRIC_KEY, COMPARATOR,
           TARGET_VALUE, ERROR_BUDGET_PCT, WINDOW_DAYS, OWNER_NAME, NOTES
    FROM {core_object('SLO_OBJECTIVES')}
    WHERE ACTIVE
), warehouse_values AS (
    SELECT o.SLO_ID,
           CASE o.METRIC_KEY
             WHEN 'WAREHOUSE_SUCCESS_PCT' THEN
               100 * (1 - SUM(COALESCE(m.FAILS, 0)) / NULLIF(SUM(m.QUERIES), 0))
             WHEN 'WAREHOUSE_P95_SEC' THEN MAX(m.P95_S)
           END AS CURRENT_VALUE,
           MAX(m.DAY) AS AS_OF
    FROM objectives o
    JOIN {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')} m
      ON UPPER(o.ENTITY_TYPE) = 'WAREHOUSE'
     AND UPPER(m.WAREHOUSE_NAME) = UPPER(o.ENTITY_KEY)
     AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
    WHERE o.METRIC_KEY IN ('WAREHOUSE_SUCCESS_PCT', 'WAREHOUSE_P95_SEC')
    GROUP BY o.SLO_ID, o.METRIC_KEY
), task_values AS (
    SELECT o.SLO_ID,
           CASE o.METRIC_KEY
             WHEN 'TASK_SUCCESS_PCT' THEN
               100 * (1 - SUM(COALESCE(m.FAILED, 0)) / NULLIF(SUM(m.RUNS), 0))
             WHEN 'TASK_P95_EXEC_SEC' THEN MAX(m.P95_EXEC_SEC)
           END AS CURRENT_VALUE,
           MAX(m.DAY) AS AS_OF
    FROM objectives o
    JOIN {core_object('MART_TASK_NODE_DAILY')} m
      ON UPPER(o.ENTITY_TYPE) = 'TASK'
     AND UPPER(m.DATABASE_NAME || '.' || m.SCHEMA_NAME || '.' || m.TASK_NAME)
         = UPPER(o.ENTITY_KEY)
     AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
    WHERE o.METRIC_KEY IN ('TASK_SUCCESS_PCT', 'TASK_P95_EXEC_SEC')
    GROUP BY o.SLO_ID, o.METRIC_KEY
), family_values AS (
    SELECT o.SLO_ID,
           CASE o.METRIC_KEY
             WHEN 'QUERY_SUCCESS_PCT' THEN
               100 * (1 - SUM(COALESCE(m.FAILS, 0)) / NULLIF(SUM(m.RUNS), 0))
             WHEN 'QUERY_P95_SEC' THEN MAX(m.P95_S)
           END AS CURRENT_VALUE,
           MAX(m.DAY) AS AS_OF
    FROM objectives o
    JOIN {core_object('MART_QUERY_FAMILY_DAILY')} m
      ON UPPER(o.ENTITY_TYPE) = 'QUERY_FINGERPRINT'
     AND UPPER(TO_VARCHAR(m.QUERY_HASH)) = UPPER(o.ENTITY_KEY)
     AND m.DAY >= DATEADD('day', -LEAST(o.WINDOW_DAYS, 400), CURRENT_DATE())
    WHERE o.METRIC_KEY IN ('QUERY_SUCCESS_PCT', 'QUERY_P95_SEC')
    GROUP BY o.SLO_ID, o.METRIC_KEY
), measured AS (
    SELECT * FROM warehouse_values
    UNION ALL SELECT * FROM task_values
    UNION ALL SELECT * FROM family_values
)
SELECT o.SLO_ID, o.NAME, o.ENTITY_TYPE, o.ENTITY_KEY, o.METRIC_KEY,
       o.COMPARATOR, o.TARGET_VALUE, o.ERROR_BUDGET_PCT, o.WINDOW_DAYS,
       o.OWNER_NAME, o.NOTES, m.CURRENT_VALUE, m.AS_OF,
       CASE
         WHEN m.CURRENT_VALUE IS NULL THEN 'NO_DATA'
         -- #11: don't call an objective MET/BREACH off stale evidence. AS_OF is the
         -- newest mart DAY in the window; a healthy loader stamps yesterday, so a
         -- value older than 2 days means the loader stalled -- report STALE, not a
         -- stale verdict.
         WHEN m.AS_OF < DATEADD('day', -2, CURRENT_DATE()) THEN 'STALE'
         WHEN o.COMPARATOR = '>=' AND m.CURRENT_VALUE >= o.TARGET_VALUE THEN 'MET'
         WHEN o.COMPARATOR = '<=' AND m.CURRENT_VALUE <= o.TARGET_VALUE THEN 'MET'
         ELSE 'BREACH'
       END AS STATUS,
       IFF(o.METRIC_KEY ILIKE '%SUCCESS_PCT' AND m.CURRENT_VALUE IS NOT NULL,
           ROUND(GREATEST(100 - m.CURRENT_VALUE, 0)
                 / NULLIF(o.ERROR_BUDGET_PCT, 0), 2), NULL) AS BURN_MULTIPLE
FROM objectives o
LEFT JOIN measured m ON m.SLO_ID = o.SLO_ID
ORDER BY CASE STATUS WHEN 'BREACH' THEN 0 WHEN 'STALE' THEN 1 WHEN 'NO_DATA' THEN 2 ELSE 3 END,
         BURN_MULTIPLE DESC NULLS LAST, o.NAME
"""


def data_product_economics(days: int = 30, company: str = "ALL") -> str:
    """Separate measured-object and metered-warehouse economics by product."""
    horizon = bounded_days(days, 400)
    company_value = str(company or "ALL").upper()
    object_company = (
        "" if company_value == "ALL"
        else f"AND UPPER(l.COMPANY) = {sql_literal(company_value, 40)}"
    )
    warehouse_company = (
        "" if company_value == "ALL"
        else f"AND UPPER(w.COMPANY) = {sql_literal(company_value, 40)}"
    )
    # DS #3: scope the CATALOG itself by company. The catalog drives the product list,
    # the object/database maps, warehouse_cost and task_health, so filtering here is the
    # only way to company-scope TASK health (MART_TASK_NODE_DAILY has no COMPANY column)
    # AND the product list + entity counts — the fact-level l/w.COMPANY filters below then
    # become belt-and-suspenders. ENTITY_CATALOG.COMPANY exists (see entity_catalog).
    catalog_company = (
        "" if company_value == "ALL"
        else f"AND UPPER(COMPANY) = {sql_literal(company_value, 40)}"
    )
    return f"""
WITH catalog AS (
    SELECT ENTITY_TYPE, ENTITY_KEY, DATA_PRODUCT, TEAM, OWNER_NAME, CRITICALITY
    FROM {core_object('ENTITY_CATALOG')}
    WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
      {catalog_company}
), products AS (
    SELECT DATA_PRODUCT, MAX(TEAM) AS TEAM, MAX(OWNER_NAME) AS OWNER_NAME,
           -- #12: rank by SEVERITY, not lexically. A plain MAX() over the label returned
           -- 'STANDARD' for a product that contained a CRITICAL entity (alphabetically
           -- S > C), so a critical data product read as standard. Take the most-severe
           -- (MIN rank) instead.
           CASE MIN(CASE UPPER(TRIM(CRITICALITY))
                          WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                          WHEN 'STANDARD' THEN 3 WHEN 'LOW' THEN 4 ELSE 3 END)
             WHEN 1 THEN 'CRITICAL' WHEN 2 THEN 'HIGH'
             WHEN 3 THEN 'STANDARD' WHEN 4 THEN 'LOW' END AS CRITICALITY,
           -- #12: a product whose entities carry different owners has ambiguous
           -- ownership; MAX(OWNER_NAME) hides it, so flag it for the operator to resolve.
           COUNT(DISTINCT NULLIF(TRIM(OWNER_NAME), '')) > 1 AS OWNER_CONFLICT,
           COUNT(*) AS CATALOG_ENTITIES
    FROM catalog GROUP BY DATA_PRODUCT
), object_map AS (
    SELECT ENTITY_KEY, DATA_PRODUCT FROM catalog WHERE ENTITY_TYPE = 'OBJECT'
), database_map AS (
    SELECT ENTITY_KEY, DATA_PRODUCT FROM catalog WHERE ENTITY_TYPE = 'DATABASE'
), object_cost AS (
    SELECT COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) AS DATA_PRODUCT,
           SUM(l.CREDITS) AS OBJECT_CREDITS,
           COUNT(DISTINCT l.OBJECT_FQN) AS COSTED_OBJECTS
    FROM {core_object('FACT_OBJECT_COST_DAILY')} l
    LEFT JOIN object_map om ON UPPER(om.ENTITY_KEY) = UPPER(l.OBJECT_FQN)
    LEFT JOIN database_map dm
      ON om.DATA_PRODUCT IS NULL
     AND UPPER(dm.ENTITY_KEY) = UPPER(SPLIT_PART(l.OBJECT_FQN, '.', 1))
    WHERE l.DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      {object_company}
      AND COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL
    GROUP BY COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT)
), warehouse_cost AS (
    SELECT c.DATA_PRODUCT, SUM(w.CREDITS_TOTAL) AS WAREHOUSE_CREDITS,
           COUNT(DISTINCT w.WAREHOUSE_NAME) AS WAREHOUSES
    FROM {core_object('MART_WAREHOUSE_EFFICIENCY_DAILY')} w
    JOIN catalog c ON c.ENTITY_TYPE = 'WAREHOUSE'
                  AND UPPER(c.ENTITY_KEY) = UPPER(w.WAREHOUSE_NAME)
    WHERE w.DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      {warehouse_company}
    GROUP BY c.DATA_PRODUCT
), task_health AS (
    SELECT c.DATA_PRODUCT, SUM(t.RUNS) AS TASK_RUNS, SUM(t.FAILED) AS TASK_FAILURES
    FROM {core_object('MART_TASK_NODE_DAILY')} t
    JOIN catalog c ON c.ENTITY_TYPE = 'TASK'
                  AND UPPER(c.ENTITY_KEY) =
                      UPPER(t.DATABASE_NAME || '.' || t.SCHEMA_NAME || '.' || t.TASK_NAME)
    WHERE t.DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
    GROUP BY c.DATA_PRODUCT
)
SELECT p.DATA_PRODUCT, p.TEAM, p.OWNER_NAME, p.OWNER_CONFLICT, p.CRITICALITY, p.CATALOG_ENTITIES,
       ROUND(COALESCE(o.OBJECT_CREDITS, 0), 4) AS MEASURED_OBJECT_CREDITS,
       COALESCE(o.COSTED_OBJECTS, 0) AS COSTED_OBJECTS,
       ROUND(COALESCE(w.WAREHOUSE_CREDITS, 0), 4) AS METERED_WAREHOUSE_CREDITS,
       COALESCE(w.WAREHOUSES, 0) AS WAREHOUSES,
       COALESCE(t.TASK_RUNS, 0) AS TASK_RUNS,
       COALESCE(t.TASK_FAILURES, 0) AS TASK_FAILURES,
       ROUND(100 * COALESCE(t.TASK_FAILURES, 0) / NULLIF(t.TASK_RUNS, 0), 2)
           AS TASK_FAIL_PCT
FROM products p
LEFT JOIN object_cost o ON o.DATA_PRODUCT = p.DATA_PRODUCT
LEFT JOIN warehouse_cost w ON w.DATA_PRODUCT = p.DATA_PRODUCT
LEFT JOIN task_health t ON t.DATA_PRODUCT = p.DATA_PRODUCT
ORDER BY MEASURED_OBJECT_CREDITS DESC, METERED_WAREHOUSE_CREDITS DESC
"""


def product_mapping_totals(days: int = 30, company: str = "ALL") -> str:
    """Coverage denominators for the product-economics view (Codex #20): the WHOLE
    account's object cost and catalog-entity counts, so the mapped share and the
    unmapped residual are explicit — an operator can tell whether product economics
    covers 90% or 12% of spend. Same DAY/company basis as data_product_economics;
    TOTAL_OBJECT_CREDITS drops the DATA_PRODUCT predicate that view applies."""
    horizon = bounded_days(days, 400)
    cv = str(company or "ALL").upper()
    obj_co = "" if cv == "ALL" else f"AND UPPER(COMPANY) = {sql_literal(cv, 40)}"
    cat_co = "" if cv == "ALL" else f"AND UPPER(COMPANY) = {sql_literal(cv, 40)}"
    return f"""
SELECT
    (SELECT ROUND(COALESCE(SUM(CREDITS), 0), 4)
       FROM {core_object('FACT_OBJECT_COST_DAILY')}
      WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) {obj_co}) AS TOTAL_OBJECT_CREDITS,
    (SELECT COUNT(*) FROM {core_object('ENTITY_CATALOG')} WHERE 1 = 1 {cat_co}) AS TOTAL_ENTITIES,
    (SELECT COUNT(*) FROM {core_object('ENTITY_CATALOG')}
      WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL {cat_co}) AS MAPPED_ENTITIES
"""


def product_detail(data_product: str) -> str:
    """#14: the constituent catalog entities of one data product, most-severe first.

    DATA_PRODUCT is an ENTITY_CATALOG attribute, not an entity type, so Entity 360's
    catalog-record path finds nothing for it. This lists the objects/warehouses/tasks
    mapped to the product with their ownership and criticality, so opening a product
    from the Products board shows what it's made of instead of an empty record."""
    key = str(data_product or "").strip()
    return f"""
SELECT ENTITY_TYPE, ENTITY_KEY, LABEL, TEAM, OWNER_NAME, CRITICALITY, SLO_NAME
FROM {core_object('ENTITY_CATALOG')}
WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
  AND UPPER(TRIM(DATA_PRODUCT)) = UPPER({sql_literal(key, 300)})
ORDER BY CASE UPPER(TRIM(CRITICALITY))
              WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
              WHEN 'STANDARD' THEN 2 WHEN 'LOW' THEN 3 ELSE 2 END,
         ENTITY_TYPE, ENTITY_KEY
LIMIT 500
"""


def product_consumer_reads(days: int = 30, company: str = "ALL") -> str:
    """#28: per data-product consumer reach + reads trend from ACCESS_HISTORY.

    For each catalogued DATA_PRODUCT: how many DISTINCT users read its objects, and how
    the read volume trends — this window (RECENT) vs the equal window before it (PRIOR)
    — so a costly product nobody reads any more surfaces as a retirement candidate.
    Consumers = distinct accounts whose queries READ the product's objects
    (BASE_OBJECTS_ACCESSED); write-only ETL targets (OBJECTS_MODIFIED) are excluded,
    but a service/pipeline account that READS the data is still counted. Reads are
    mapped to a product the SAME way ``data_product_economics`` maps object cost
    (object_map on the FQN, then database_map on the FQN's database) and cover the same
    object domains the cost ETL charges (Table + Materialized view), so the numerator
    and denominator of cost-per-consumer describe the same objects. A view read resolves
    to its base tables in BASE_OBJECTS_ACCESSED, so usage through views still counts.
    DISTINCT_CONSUMERS is scoped to the RECENT window to align with the object cost's
    own window. Returns raw counts + LAST_READ only — NO dollars (rate lives in the UI).

    Enterprise-edition only (ACCESS_HISTORY) — callers run probe=True and degrade.
    Company scoping is ENTIRELY via the catalog CTE (ENTITY_CATALOG.COMPANY); NEVER a
    COMPANY_FOR_USER predicate on ACCESS_HISTORY (that is one UDF call per scanned row —
    the per-row blowup companies.py warns against). ACCESS_HISTORY has no COMPANY."""
    recent = bounded_days(days, 200)
    horizon = bounded_days(2 * recent, 400)
    cv = str(company or "ALL").upper()
    catalog_company = "" if cv == "ALL" else f"AND UPPER(COMPANY) = {sql_literal(cv, 40)}"
    return f"""
WITH catalog AS (
    SELECT ENTITY_TYPE, ENTITY_KEY, DATA_PRODUCT
    FROM {core_object('ENTITY_CATALOG')}
    WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
      {catalog_company}
), object_map AS (
    SELECT ENTITY_KEY, DATA_PRODUCT FROM catalog WHERE ENTITY_TYPE = 'OBJECT'
), database_map AS (
    SELECT ENTITY_KEY, DATA_PRODUCT FROM catalog WHERE ENTITY_TYPE = 'DATABASE'
), reads AS (
    SELECT UPPER(f.value:"objectName"::STRING) AS FQN,
           a.USER_NAME, a.QUERY_ID, a.QUERY_START_TIME
    FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY a,
         LATERAL FLATTEN(input => a.BASE_OBJECTS_ACCESSED) f
    WHERE a.QUERY_START_TIME >= DATEADD('day', -{horizon}, CURRENT_TIMESTAMP())
      -- match the domains the cost ETL (V048) attributes credits to, or an
      -- actively-queried materialized view would show cost with zero reads and be
      -- falsely flagged for retirement.
      AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
), product_reads AS (
    SELECT COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) AS DATA_PRODUCT,
           r.USER_NAME, r.QUERY_ID, r.QUERY_START_TIME
    FROM reads r
    LEFT JOIN object_map om ON UPPER(om.ENTITY_KEY) = r.FQN
    LEFT JOIN database_map dm
      ON om.DATA_PRODUCT IS NULL
     AND UPPER(dm.ENTITY_KEY) = SPLIT_PART(r.FQN, '.', 1)
    WHERE COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL
)
SELECT DATA_PRODUCT,
       -- DISTINCT_CONSUMERS is scoped to the RECENT window so it aligns with the same
       -- `days`-window object cost it is divided by (data_product_economics scans
       -- `days`, not 2*days); a 2*days reach figure over a `days` cost understates
       -- $/consumer. RECENT/PRIOR reads keep the full horizon for the trend.
       COUNT(DISTINCT IFF(QUERY_START_TIME >= DATEADD('day', -{recent}, CURRENT_TIMESTAMP()),
                          USER_NAME, NULL)) AS DISTINCT_CONSUMERS,
       COUNT(DISTINCT QUERY_ID)  AS TOTAL_READS,
       COUNT(DISTINCT IFF(QUERY_START_TIME >= DATEADD('day', -{recent}, CURRENT_TIMESTAMP()),
                          QUERY_ID, NULL)) AS RECENT_READS,
       COUNT(DISTINCT IFF(QUERY_START_TIME <  DATEADD('day', -{recent}, CURRENT_TIMESTAMP()),
                          QUERY_ID, NULL)) AS PRIOR_READS,
       MAX(QUERY_START_TIME) AS LAST_READ
FROM product_reads
GROUP BY DATA_PRODUCT
ORDER BY TOTAL_READS DESC
LIMIT 500
"""


def cost_truth(days: int = 30, company: str = "ALL") -> str:
    """One non-additive inventory of billed, metered, measured and allocated credits."""
    horizon = bounded_days(days, 400)
    company_value = str(company or "ALL").upper()
    fact_scope = (
        "" if company_value == "ALL"
        else f"AND UPPER(COMPANY) = {sql_literal(company_value, 40)}"
    )
    return f"""
WITH billed AS (
    SELECT SUM(CREDITS_BILLED) AS CREDITS
    FROM {core_object('FACT_METERING_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
), metered AS (
    SELECT SUM(CREDITS_TOTAL) AS CREDITS
    FROM {core_object('FACT_WAREHOUSE_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) {fact_scope}
), measured AS (
    SELECT SUM(CREDITS) AS CREDITS
    FROM {core_object('FACT_OBJECT_COST_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) {fact_scope}
      AND COST_ARM LIKE 'QUERY_COMPUTE%'
), allocated AS (
    SELECT SUM(ALLOC_CREDITS) AS CREDITS
    FROM {core_object('MART_COST_ALLOCATION_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) {fact_scope}
      AND DIMENSION = 'USER'
)
SELECT 'BILLED' AS BASIS, 'Account billed credits' AS METRIC, CREDITS,
       'ACCOUNT (global Company filter does not apply)' AS FILTER_SCOPE,
       'FACT_METERING_DAILY; cloud-services adjustment and all services' AS SOURCE,
       'Invoice/rate-card truth; do not add to rows below' AS RECONCILES_TO
FROM billed
UNION ALL
SELECT 'METERED', 'Warehouse usage credits', CREDITS,
       {sql_literal(company_value, 40)}, 'FACT_WAREHOUSE_DAILY; warehouse compute only',
       'Operational warehouse usage; excludes serverless and AI'
FROM metered
UNION ALL
SELECT 'MEASURED', 'Object-attributed query credits', CREDITS,
       {sql_literal(company_value, 40)}, 'FACT_OBJECT_COST_DAILY query arms',
       'Query activity only; excludes idle and unattributed non-query services'
FROM measured
UNION ALL
SELECT 'ALLOCATED', 'User-attributed warehouse credits', CREDITS,
       {sql_literal(company_value, 40)}, 'MART_COST_ALLOCATION_DAILY USER dimension',
       'Allocation of warehouse credits; compare coverage to METERED, never add it'
FROM allocated
ORDER BY CASE BASIS WHEN 'BILLED' THEN 0 WHEN 'METERED' THEN 1
                    WHEN 'MEASURED' THEN 2 ELSE 3 END
"""


def _entity_metric_nonwarehouse(kind: str, literal: str, horizon: int) -> str:
    if kind in ("DATABASE", "OBJECT"):
        predicate = (
            f"UPPER(SPLIT_PART(OBJECT_FQN, '.', 1)) = {literal}"
            if kind == "DATABASE"
            else f"UPPER(OBJECT_FQN) = {literal}"
        )
        return f"""
SELECT METRIC, VALUE, UNIT, BASIS, AS_OF
FROM (
    SELECT 'Measured credits' METRIC, SUM(CREDITS)::FLOAT VALUE, 'credits' UNIT,
           'MEASURED' BASIS, MAX(DAY)::TIMESTAMP_NTZ AS AS_OF
    FROM {core_object('FACT_OBJECT_COST_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) AND {predicate}
    UNION ALL
    SELECT 'Costed objects', COUNT(DISTINCT OBJECT_FQN)::FLOAT, 'count',
           'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('FACT_OBJECT_COST_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) AND {predicate}
    UNION ALL
    SELECT 'Maintenance credits',
           SUM(IFF(COST_ARM NOT LIKE 'QUERY_COMPUTE%', CREDITS, 0))::FLOAT,
           'credits', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('FACT_OBJECT_COST_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE()) AND {predicate}
)
WHERE AS_OF IS NOT NULL
ORDER BY METRIC
"""
    if kind == "TASK":
        return f"""
SELECT METRIC, VALUE, UNIT, BASIS, AS_OF
FROM (
    SELECT 'Runs' METRIC, SUM(RUNS)::FLOAT VALUE, 'count' UNIT,
           'MEASURED' BASIS, MAX(LAST_COMPLETED)::TIMESTAMP_NTZ AS AS_OF
    FROM {core_object('MART_TASK_NODE_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TASK_NAME) = {literal}
    UNION ALL
    SELECT 'Failures', SUM(FAILED)::FLOAT, 'count', 'MEASURED',
           MAX(LAST_COMPLETED)::TIMESTAMP_NTZ
    FROM {core_object('MART_TASK_NODE_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TASK_NAME) = {literal}
    UNION ALL
    SELECT 'P95 dispatch queue', MAX(P95_QUEUE_SEC)::FLOAT, 'seconds', 'MEASURED',
           MAX(LAST_COMPLETED)::TIMESTAMP_NTZ
    FROM {core_object('MART_TASK_NODE_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TASK_NAME) = {literal}
    UNION ALL
    SELECT 'P95 execution', MAX(P95_EXEC_SEC)::FLOAT, 'seconds', 'MEASURED',
           MAX(LAST_COMPLETED)::TIMESTAMP_NTZ
    FROM {core_object('MART_TASK_NODE_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TASK_NAME) = {literal}
)
WHERE AS_OF IS NOT NULL
ORDER BY METRIC
"""
    if kind == "QUERY_FINGERPRINT":
        return f"""
SELECT METRIC, VALUE, UNIT, BASIS, AS_OF
FROM (
    SELECT 'Runs' METRIC, SUM(RUNS)::FLOAT VALUE, 'count' UNIT,
           'MEASURED' BASIS, MAX(DAY)::TIMESTAMP_NTZ AS AS_OF
    FROM {core_object('MART_QUERY_FAMILY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(TO_VARCHAR(QUERY_HASH)) = {literal}
    UNION ALL
    SELECT 'Failures', SUM(FAILS)::FLOAT, 'count', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_QUERY_FAMILY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(TO_VARCHAR(QUERY_HASH)) = {literal}
    UNION ALL
    SELECT 'Total execution', SUM(TOTAL_EXEC_SEC)::FLOAT, 'seconds', 'MEASURED',
           MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_QUERY_FAMILY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(TO_VARCHAR(QUERY_HASH)) = {literal}
    UNION ALL
    SELECT 'P95 runtime', MAX(P95_S)::FLOAT, 'seconds', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_QUERY_FAMILY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(TO_VARCHAR(QUERY_HASH)) = {literal}
    UNION ALL
    SELECT 'Users', MAX(USERS)::FLOAT, 'count', 'MEASURED', MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_QUERY_FAMILY_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND UPPER(TO_VARCHAR(QUERY_HASH)) = {literal}
)
WHERE AS_OF IS NOT NULL
ORDER BY METRIC
"""
    dimension = "USER" if kind == "USER" else "ROLE"
    return f"""
SELECT METRIC, VALUE, UNIT, BASIS, AS_OF
FROM (
    SELECT 'Allocated credits' METRIC, SUM(ALLOC_CREDITS)::FLOAT VALUE,
           'credits' UNIT, 'ALLOCATED' BASIS, MAX(DAY)::TIMESTAMP_NTZ AS AS_OF
    FROM {core_object('MART_COST_ALLOCATION_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND DIMENSION = {sql_literal(dimension)} AND UPPER(KEY_NAME) = {literal}
    UNION ALL
    SELECT 'Execution', SUM(EXEC_SEC)::FLOAT, 'seconds', 'ALLOCATED',
           MAX(DAY)::TIMESTAMP_NTZ
    FROM {core_object('MART_COST_ALLOCATION_DAILY')}
    WHERE DAY >= DATEADD('day', -{horizon}, CURRENT_DATE())
      AND DIMENSION = {sql_literal(dimension)} AND UPPER(KEY_NAME) = {literal}
)
WHERE AS_OF IS NOT NULL
ORDER BY METRIC
"""
