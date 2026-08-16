#!/usr/bin/env python3
"""Forward-generate V082: regrain MART_QUERY_FAMILY_DAILY to carry COMPANY.

Decision Studio review, finding #3 (company-scope leaks). The query-family mart is
the ONLY loader arm with no company attribution: its grain is (DAY, QUERY_HASH),
DATABASE_NAME/SCHEMA_NAME are ANY_VALUE representatives, and a family that ran under
multiple companies collapses to one account-wide row. The app then fakes company
scope off that representative DATABASE_NAME (workbench_sql.workload_portfolio joins
on it; mart27_sql.family_* use companies.database_clause on it) — a lossy heuristic
that drops or mixes a company's families.

Fix: re-derive SP_LOAD_MARTS_V27 (from V078, its current owner) with the query-family
arm regrained to per-company row grain:
  * add COMPANY = COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) (bare — the UDF self-defaults
    residual/NULL warehouses to 'UNKNOWN' and never returns NULL, the house idiom;
    see FACT_WAREHOUSE_DAILY V062:1050),
  * GROUP BY 1, 2, 3 (DAY, QUERY_HASH, COMPANY) so RUNS/FAILS/USERS/WAREHOUSES become
    per-company,
  * QUALIFY PARTITION BY DAY, COMPANY (each company keeps its own top-2000/day, not a
    shared budget one company can crowd out),
  * MERGE ON + t.COMPANY = s.COMPANY, and COMPANY added to the INSERT column/values.
Everything else in the ~790-line proc is byte-identical to V078 (single-point swap).

Plus: ALTER the mart to add COMPANY (nullable, additive like V060's TOTAL_ELAPSED_SEC),
and a one-time DELETE of the mart — the grain changed, so old (DAY, QUERY_HASH) rows
(COMPANY NULL) can't be MERGE-matched by the new per-company rows and would double-count
under any GROUP BY reader. The mart is fully rebuildable from OW_QH_EXTRACT; the OWNER
re-runs the backfill after applying (SP_LOAD_QH_EXTRACT(N) then SP_LOAD_MARTS_V27('HOURLY',
N) — the qfam arm runs under HOURLY scope). Until then the family boards read empty /
fall back to their live twins.

No alert scan reads this mart (verified), so there is no alert-layer ripple.
SP_NIGHTLY_RECONCILE deletes by DAY only (grain-insensitive) and is untouched. Owner
applies in Snowsight after V081; the app rewire (workload_portfolio + family_* now scope
on the real COMPANY column) ships with it. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V078__ai_usage_ts_cast.sql"

# The current query-family MERGE (V078 lines 153-190), verbatim. Replaced wholesale.
OLD_QFAM = """            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DATE(START_TIME) AS DAY,
                       QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY 1, 2
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);"""

# Regrained to per-company grain. Deltas: +COMPANY select item (position 3), GROUP BY
# 1,2,3, QUALIFY PARTITION BY DAY,COMPANY, MERGE ON + t.COMPANY, +COMPANY in the INSERT.
# COMPANY is a match key so it is NOT in the MATCHED UPDATE SET.
NEW_QFAM = """            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY t
            USING (
                SELECT DAY,
                       QUERY_HASH,
                       COMPANY,
                       ANY_VALUE(LEFT(QUERY_TEXT, 200)) AS SAMPLE_TEXT,
                       COUNT(*) AS RUNS,
                       COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS,
                       COUNT(DISTINCT USER_NAME) AS USERS,
                       COUNT(DISTINCT WAREHOUSE_NAME) AS WAREHOUSES,
                       ANY_VALUE(DATABASE_NAME) AS DATABASE_NAME,
                       ANY_VALUE(SCHEMA_NAME) AS SCHEMA_NAME,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS TOTAL_EXEC_SEC,
                       ROUND(SUM(COALESCE(TOTAL_ELAPSED_TIME, 0)) / 1000, 1) AS TOTAL_ELAPSED_SEC,
                       ROUND(MEDIAN(TOTAL_ELAPSED_TIME) / 1000, 2) AS MEDIAN_S,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 2) AS P95_S,
                       ROUND(AVG(COALESCE(COMPILATION_TIME, 0)), 1) AS COMPILE_MS_AVG,
                       ROUND(AVG(COALESCE(BYTES_SCANNED, 0)) / POWER(1024, 3), 3) AS GB_SCANNED_AVG,
                       ROUND(AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)), 2) AS CACHE_PCT_AVG,
                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS
                FROM (
                    -- V082: derive COMPANY per row FIRST (UDF outside the aggregation, the
                    -- V029 shape law), so the outer GROUP BY keys on a plain column and never
                    -- on the correlated-subquery UDF directly -- grouping BY that UDF is the
                    -- exact shape that logged mart_load_failed every hour after V027 (V029).
                    SELECT DATE(START_TIME) AS DAY,
                           QUERY_PARAMETERIZED_HASH AS QUERY_HASH,
                           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY,
                           QUERY_TEXT, EXECUTION_STATUS, USER_NAME, WAREHOUSE_NAME,
                           DATABASE_NAME, SCHEMA_NAME, EXECUTION_TIME, TOTAL_ELAPSED_TIME,
                           COMPILATION_TIME, BYTES_SCANNED, PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG
                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
                    WHERE START_TIME >= GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)
                      AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                )
                GROUP BY DAY, QUERY_HASH, COMPANY
                QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY, COMPANY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000
            ) s
            ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY
            WHEN MATCHED THEN UPDATE SET
                SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS, FAILS = s.FAILS, USERS = s.USERS,
                WAREHOUSES = s.WAREHOUSES, DATABASE_NAME = s.DATABASE_NAME, SCHEMA_NAME = s.SCHEMA_NAME,
                TOTAL_EXEC_SEC = s.TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC = s.TOTAL_ELAPSED_SEC, MEDIAN_S = s.MEDIAN_S, P95_S = s.P95_S,
                COMPILE_MS_AVG = s.COMPILE_MS_AVG, GB_SCANNED_AVG = s.GB_SCANNED_AVG,
                CACHE_PCT_AVG = s.CACHE_PCT_AVG, TAGGED_RUNS = s.TAGGED_RUNS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, QUERY_HASH, COMPANY, SAMPLE_TEXT, RUNS, FAILS, USERS, WAREHOUSES, DATABASE_NAME, SCHEMA_NAME,
                 TOTAL_EXEC_SEC, TOTAL_ELAPSED_SEC, MEDIAN_S, P95_S, COMPILE_MS_AVG, GB_SCANNED_AVG, CACHE_PCT_AVG, TAGGED_RUNS)
            VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.SAMPLE_TEXT, s.RUNS, s.FAILS, s.USERS, s.WAREHOUSES, s.DATABASE_NAME,
                    s.SCHEMA_NAME, s.TOTAL_EXEC_SEC, s.TOTAL_ELAPSED_SEC, s.MEDIAN_S, s.P95_S, s.COMPILE_MS_AVG, s.GB_SCANNED_AVG,
                    s.CACHE_PCT_AVG, s.TAGGED_RUNS);"""

base = BASE.read_text(encoding="utf-8")

# Extract the whole SP_LOAD_MARTS_V27 proc (CREATE OR REPLACE PROCEDURE ... $$;).
proc_match = re.search(
    r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_MARTS_V27.*?\n\$\$;\n",
    base, re.S)
assert proc_match, "SP_LOAD_MARTS_V27 proc not found in V078"
proc = proc_match.group(0)

assert proc.count(OLD_QFAM) == 1, f"expected exactly 1 qfam MERGE, got {proc.count(OLD_QFAM)}"
proc = proc.replace(OLD_QFAM, NEW_QFAM)
assert NEW_QFAM in proc and OLD_QFAM not in proc

out = f"""-- V082__query_family_company_regrain.sql
--
-- Regrain MART_QUERY_FAMILY_DAILY to carry COMPANY (Decision Studio review, #3).
--
-- The query-family mart was the only loader arm with no company attribution: grain
-- (DAY, QUERY_HASH), DATABASE_NAME an ANY_VALUE representative, so a family that ran
-- under multiple companies collapsed to one account-wide row. The app then faked
-- company scope off that representative DATABASE_NAME (workload_portfolio join,
-- family_* database_clause) -- a lossy heuristic that dropped or mixed a company's
-- families. This re-derives SP_LOAD_MARTS_V27 (from V078) with the query-family arm
-- regrained to per-company rows: COMPANY = COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME),
-- GROUP BY DAY, QUERY_HASH, COMPANY, QUALIFY top-2000 PARTITION BY DAY, COMPANY, and
-- the MERGE keyed on (DAY, QUERY_HASH, COMPANY). All other arms are byte-identical to
-- V078.
--
-- The mart gains a COMPANY column and is CLEARED once: the grain changed, so old
-- (DAY, QUERY_HASH) rows (COMPANY NULL) can't be MERGE-matched by the new per-company
-- rows and would double-count under any GROUP BY reader. The mart is fully rebuildable
-- from OW_QH_EXTRACT. OWNER: after applying, re-run the backfill so the mart repopulates
-- at the new grain -- CALL SP_LOAD_QH_EXTRACT(90) then CALL SP_LOAD_MARTS_V27('HOURLY',
-- 90) (the qfam arm runs under HOURLY scope; widen to 365 for full history). Until then
-- the family boards read empty or fall back to their live QUERY_HISTORY twins.
--
-- No alert scan reads this mart, so there is no alert-layer ripple; SP_NIGHTLY_RECONCILE
-- deletes by DAY only and is untouched.
--
-- Two disclosed notes (skeptic review): (1) ALL-view MAX(USERS)/MAX(WAREHOUSES) in the
-- family boards become per-company peaks and understate a cross-company family's true
-- distinct counts (already a peak-day proxy; the materialization gate keys on
-- elapsed/runs/cache, not these). (2) workload_portfolio joins the family mart on
-- f.COMPANY unconditionally with no live fallback, so between this DELETE and the owner's
-- backfill its family metrics read as no-evidence (routed to the VALIDATE lane by
-- EVIDENCE_COVERAGE), and if the app is deployed before V082 is applied that board errors
-- on the missing column -- so APPLY V082 BEFORE DEPLOYING the app. The two family panels
-- use run_mart_first and fall back to their live twins in both windows.
--
-- Owner applies in Snowsight after V081; the app rewire (workload_portfolio + family_*
-- scope on the real COMPANY column) ships with it. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20082, 'V082 requires V081 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 81) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Additive column (nullable, like V060's TOTAL_ELAPSED_SEC); the regrained loader
-- always stamps it via COMPANY_FOR_WAREHOUSE (never NULL in practice).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
    ADD COLUMN IF NOT EXISTS COMPANY VARCHAR(40);

-- One-time grain reset: clear the account-grain rows so the per-company loader +
-- backfill rebuild the mart cleanly. Rebuildable from OW_QH_EXTRACT (see header).
-- Gated on V082 not yet recorded, so a re-apply never wipes an already-backfilled mart.
DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 82);

-- >>> derived:SP_LOAD_MARTS_V27  (query-family arm regrained to per-COMPANY grain, from V078)
{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 82 AS VERSION,
       'Query-family company regrain: MART_QUERY_FAMILY_DAILY gains COMPANY (COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)) and is regrained to (DAY, QUERY_HASH, COMPANY) -- top-2000/day now per company, MERGE keyed on +COMPANY -- so the app scopes query families on the real company instead of the lossy ANY_VALUE(DATABASE_NAME) heuristic (workload_portfolio + family_* rewired). Mart cleared once for the grain change; owner re-runs SP_LOAD_QH_EXTRACT + SP_LOAD_MARTS_V27(HOURLY) backfill. All other loader arms byte-identical to V078.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 82);
"""

# --- self-assertions -------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE VIEW" not in out
assert out.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27") == 1
assert "EXCEPTION (-20082" in out and "IF (v < 81) THEN" in out
assert "SELECT 82 AS VERSION" in out and "WHERE VERSION = 82)" in out
# schema + one-time reset (the DELETE is guarded so a re-apply can't wipe a backfill)
assert "ADD COLUMN IF NOT EXISTS COMPANY VARCHAR(40)" in out
assert ("DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_QUERY_FAMILY_DAILY\n"
        "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 82);") in out
# the regrained qfam arm (per-company). COMPANY is derived per-row in an INNER subquery
# and the OUTER GROUP BY keys on the plain column -- never GROUP BY the correlated UDF
# (the V029 mart_load_failed shape).
assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) AS COMPANY" in out
assert "GROUP BY DAY, QUERY_HASH, COMPANY" in out   # outer groups on the plain column
# (other sibling arms legitimately still use positional GROUP BY — the OLD_QFAM-not-in /
# NEW_QFAM-in checks above already prove the qfam arm specifically no longer groups by the UDF)
assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY DAY, COMPANY ORDER BY TOTAL_EXEC_SEC DESC) <= 2000" in out
assert "ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY" in out
# the old account-grain qfam block is fully gone; the regrained one replaced it
# (other sibling arms legitimately still use GROUP BY 1, 2 — don't over-assert on that)
assert OLD_QFAM not in out
assert NEW_QFAM in out
assert "PARTITION BY DAY ORDER BY TOTAL_EXEC_SEC" not in out   # qfam was the only such QUALIFY
# the sibling arms survived the extraction (spot-check the warehouse-eff arm)
assert "loaded := loaded || 'wh_eff ';" in out and "loaded := loaded || 'qfam ';" in out
assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t" in out  # freshness leg kept

target = Path(os.environ.get("V082_OUT") or (MIG / "V082__query_family_company_regrain.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
