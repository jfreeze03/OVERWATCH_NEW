#!/usr/bin/env python3
"""Forward-generate V060__family_elapsed_queued_alert_guard.sql — triage LOWs + alert guard.

Three loader/alert correctness items from the 2026-07-29 metrics triage and its
verification round (docs/reviews/METRICS_TRIAGE_2026-07-29.md):

#5  MART_QUERY_FAMILY_DAILY has no wall-clock column, so the compile-heavy reader
    divided total compile time by EXECUTION-only time and could print
    COMPILE_PCT > 100%. V060 adds TOTAL_ELAPSED_SEC (NUMBER(18,1), additive) and
    the qfam arm now stores ROUND(SUM(TOTAL_ELAPSED_TIME)/1000, 1). The app
    reader (mart27_sql.family_compile_heavy) switches to
    COALESCE(TOTAL_ELAPSED_SEC, TOTAL_EXEC_SEC) so pre-V060 rows (never
    re-loaded beyond the trailing 2 days) degrade to the old behavior instead
    of dropping out.

#11 FACT_QUERY_SCHEMA_HOURLY's loader summed QUEUED_OVERLOAD_TIME only, while
    the query-window contract and the sibling FACT_QUERY_HOURLY include
    OVERLOAD + PROVISIONING — a schema-filtered Queued KPI silently dropped
    provisioning-queue seconds. The arm now matches the hourly-fact expression
    exactly (OW_QH_EXTRACT already carries QUEUED_PROVISIONING_TIME).

GUARD (verify round, 4.68.0): SP_ALERT_SCAN's COST_CLOUD_SVC_RATIO arm read
    WAREHOUSE_METERING_HISTORY without WAREHOUSE_ID > 0, so the
    CLOUD_SERVICES_ONLY pseudo-warehouse (ratio = 100%) could fire the alert on
    a busy metadata day. The arm gains the same guard every fact writer and the
    live CS-ratio builder already use.

Derivation law: SP_LOAD_MARTS_V27 re-derived from its CURRENT (V059) definition
(preserving V057's FAIL fixes, V058's arm [6b], V059's ROOT_QUERY_ID rollup);
SP_ALERT_SCAN re-derived from its CURRENT (V056) definition.
tests/test_v060_family_elapsed.py byte-compares both.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    matches = pat.findall(text)
    assert matches, (path, name)
    return matches[-1]


def apply(body: str, edits: list[tuple[str, str]], name: str) -> str:
    for old, new in edits:
        n = body.count(old)
        assert n == 1, f"{name}: needle x{n}: {old[:70]!r}"
        body = body.replace(old, new)
    return body


# --- #5 + #11: SP_LOAD_MARTS_V27 (from V059) --------------------------------
marts = apply(extract_proc("V059__task_graph_root_credits.sql", "SP_LOAD_MARTS_V27"), [
    # #5 qfam arm: store wall-clock seconds beside exec seconds
    ("ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,",
     "ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,\n"
     "                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,"),
    ("TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,",
     "TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, "
     "MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,"),
    ("TOTAL_EXEC_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)",
     "TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)"),
    ("s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,",
     "s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,"),
    # #11 schema-hour arm: queued = overload + provisioning (the FACT_QUERY_HOURLY
    # convention; the query-window contract's Queued KPI expects both)
    ("ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC,",
     "ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,"),
], "SP_LOAD_MARTS_V27")

# guardrails: prior releases' fixes survive intact
assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4, "V057 FAIL fix must survive"
assert "EXECUTION_STATUS = 'FAILED'" not in marts
assert marts.count("loaded := loaded || 'task_node ';") == 1, "V058 arm [6b] must survive"
assert marts.count("a ON a.ROOT_ID = h.QUERY_ID") == 1, "V059 ROOT rollup must survive"
assert marts.count("AS TOTAL_ELAPSED_SEC,") == 1
assert marts.count("QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,") == 1
assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts


# --- alert guard: SP_ALERT_SCAN (from V056, its latest definition) ----------
alert = apply(extract_proc("V056__loader_reconcile_alert_fixes.sql", "SP_ALERT_SCAN"), [
    ("FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY\n"
     "            WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())",
     "FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY\n"
     "            WHERE START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n"
     "              AND WAREHOUSE_ID > 0"),
], "SP_ALERT_SCAN")

assert alert.count("AND WAREHOUSE_ID > 0") == 1
# V056's alert fixes survive
assert "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')" in alert
assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(w.WAREHOUSE_NAME)" in alert


out = f"""-- V060__family_elapsed_queued_alert_guard.sql — triage #5/#11 + CS-alert guard.
--
--   #5  MART_QUERY_FAMILY_DAILY gains TOTAL_ELAPSED_SEC (wall-clock) so the
--       compile-heavy reader stops dividing compile time by execution-only time
--       (COMPILE_PCT could exceed 100%). Additive column; the qfam arm stores it;
--       the app reader COALESCEs to TOTAL_EXEC_SEC for pre-V060 rows.
--   #11 FACT_QUERY_SCHEMA_HOURLY's QUEUED_SEC now includes provisioning-queue
--       time (OVERLOAD + PROVISIONING), matching FACT_QUERY_HOURLY and the
--       query-window contract — a schema filter no longer under-reports Queued.
--   GUARD COST_CLOUD_SVC_RATIO's metering subquery gains WAREHOUSE_ID > 0 so the
--       CLOUD_SERVICES_ONLY pseudo-warehouse (ratio = 100%) can never fire the
--       alert (parity with the live CS-ratio builder and every fact writer).
--
--   Two proc re-derivations + one additive column. No new object, no task-graph
--   change. Idempotent; safe to re-run. Apply AFTER V059.
--
--   Forward loads self-correct on the next hourly cycle. History note: only the
--   trailing ~2 days of the qfam/schema marts ever re-load, so older rows keep
--   NULL TOTAL_ELAPSED_SEC (reader degrades to exec-time) and overload-only
--   QUEUED_SEC; a wider re-run needs a fresh extract backfill first.
--
-- Derivation law: SP_LOAD_MARTS_V27 re-derived from its CURRENT (V059)
-- definition, SP_ALERT_SCAN from its CURRENT (V056) definition, + the
-- enumerated edits; the test byte-compares both.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20060, 'V060 requires V059 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 59) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- #5: additive wall-clock column (NULL for history; reader degrades honestly).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
    ADD COLUMN IF NOT EXISTS TOTAL_ELAPSED_SEC NUMBER(18,1);

-- >>> derived:SP_LOAD_MARTS_V27
{marts}
-- >>> derived:SP_ALERT_SCAN
{alert}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 60 AS VERSION,
       'Triage #5/#11 + alert guard: MART_QUERY_FAMILY_DAILY gains TOTAL_ELAPSED_SEC (compile-heavy COMPILE_PCT bounded 0-100), FACT_QUERY_SCHEMA_HOURLY queued includes provisioning time, COST_CLOUD_SVC_RATIO alert excludes the CLOUD_SERVICES_ONLY pseudo-warehouse (WAREHOUSE_ID > 0)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 60);
"""
target = Path(os.environ.get("V060_OUT") or (MIG / "V060__family_elapsed_queued_alert_guard.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
