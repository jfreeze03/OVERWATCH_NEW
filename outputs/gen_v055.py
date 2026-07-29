#!/usr/bin/env python3
"""Forward-generate V055__cloud_services_breakdown.sql.

Owner ask 2026-07-28: a cloud-services alert (COST_CLOUD_SVC_RATIO) sits >21%
for two warehouses; drill into the *components* driving the cloud-services
credits so the cause can be lowered.

The Spend page already shows the per-warehouse ratio, the rebate, compile-heavy
families, and CS-by-statement-type (live QUERY_HISTORY). What is missing is a
persisted, shape/user-grain breakdown: which exact query shapes and which
users/roles burn the cloud-services credits (metadata storms of tiny queries
never surface in the compile-heavy view). This migration persists per-query
cloud-services credits into a mart so those views are mart-backed (no new live
scan) and gain history.

What ships:
  1. OW_QH_EXTRACT gains CREDITS_USED_CLOUD_SERVICES (additive) — the extract
     already scans QUERY_HISTORY once, so this is a free column, no new scan.
  2. SP_LOAD_QH_EXTRACT re-derived (from V042) to (a) populate the new column
     and (b) cascade a guarded CALL into the new mart loader (isolated arm —
     its failure never breaks the extract or the watermark).
  3. MART_CLOUD_SVC_DAILY — CS credits at (day, company, warehouse, user, role,
     query_type, parameterized_hash) grain, CS>0 only (this is the "what drives
     the ratio" lens; compute-only queries are out of scope).
  4. SP_LOAD_CLOUD_SVC_MART — MERGEs the trailing 3 days from the extract
     (accumulates history like FACT_QUERY_DAILY), no QUERY_HISTORY rescan.
  5. SP_PURGE_FACTS re-derived (from V054) to retention-trim the new mart on the
     daily-fact floor.

Derivation law:
  - SP_LOAD_QH_EXTRACT from V042 (its CURRENT definition) + two column edits +
    one guarded cascade CALL.
  - SP_PURGE_FACTS from V054 (its CURRENT definition) + one retention DELETE.
tests/test_v055_cloud_services.py re-derives and byte-compares.
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
        assert n == 1, f"{name}: needle x{n}: {old[:80]!r}"
        body = body.replace(old, new)
    return body


# --- extract proc: add the CS column to the fill + cascade the mart loader ----
extract = apply(extract_proc("V042__codex_r22.sql", "SP_LOAD_QH_EXTRACT"), [
    # (a) INSERT column list
    ("         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT)",
     "         PERCENTAGE_SCANNED_FROM_CACHE, QUERY_TAG, QUERY_PARAMETERIZED_HASH, QUERY_TEXT,\n"
     "         CREDITS_USED_CLOUD_SERVICES)"),
    # (b) SELECT list
    ("           LEFT(QUERY_TEXT, 200)\n    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n    WHERE START_TIME >= :lo;",
     "           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)\n"
     "    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n    WHERE START_TIME >= :lo;"),
    # (c) cascade the cloud-services mart loader as an isolated arm
    ("            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_DAILY - extract unaffected', CURRENT_ROLE();\n"
     "    END;\n\n"
     "    -- R5: advance the watermark; R6: loader-owned freshness — ONLY when the",
     "            SELECT 'ExtractLoader', 'fact_load_failed', :emsg, 'FACT_QUERY_DAILY - extract unaffected', CURRENT_ROLE();\n"
     "    END;\n\n"
     "    -- V055: cloud-services breakdown mart, from the extract just filled.\n"
     "    -- Isolated (V017): its failure must not break the extract or the\n"
     "    -- watermark — consumers keep the previous mart fill.\n"
     "    BEGIN\n"
     "        CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();\n"
     "    EXCEPTION\n"
     "        WHEN OTHER THEN\n"
     "            emsg := SQLERRM;\n"
     "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
     "            SELECT 'ExtractLoader', 'cloud_svc_mart_failed', :emsg, 'MART_CLOUD_SVC_DAILY - extract unaffected', CURRENT_ROLE();\n"
     "    END;\n\n"
     "    -- R5: advance the watermark; R6: loader-owned freshness — ONLY when the"),
], "extract")

# --- purge proc: retention-trim the new mart on the daily-fact floor ----------
purge = apply(extract_proc("V054__exec_board_window_history.sql", "SP_PURGE_FACTS"), [
    ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY\n"
     "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
     "    total := total + SQLROWCOUNT;\n\n"
     "    RETURN 'purged ' || :total || ' row(s)';",
     "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY\n"
     "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
     "    total := total + SQLROWCOUNT;\n"
     "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY\n"
     "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
     "    total := total + SQLROWCOUNT;\n\n"
     "    RETURN 'purged ' || :total || ' row(s)';"),
], "purge")

out = f"""-- V055__cloud_services_breakdown.sql — persist per-query cloud-services credits
-- so the alert (COST_CLOUD_SVC_RATIO) becomes self-explaining.
--
--   Owner ask 2026-07-28: the cloud-services ratio sits >21% on two warehouses;
--   drill into the components (which query shapes / users burn the credits) to
--   lower the cost. The Spend page already shows the ratio, the rebate, and the
--   compile-heavy / by-type views (live). Missing is a persisted shape+user
--   breakdown — metadata storms of tiny queries never surface in compile-heavy.
--
--   1. OW_QH_EXTRACT gains CREDITS_USED_CLOUD_SERVICES (additive; the extract
--      already scans QUERY_HISTORY once, so no new scan).
--   2. SP_LOAD_QH_EXTRACT re-derived (V042) to fill the column and cascade an
--      isolated CALL into the mart loader.
--   3. MART_CLOUD_SVC_DAILY — CS credits at day/company/warehouse/user/role/
--      query_type/parameterized_hash grain, CS>0 only.
--   4. SP_LOAD_CLOUD_SVC_MART — MERGE trailing 3 days from the extract.
--   5. SP_PURGE_FACTS re-derived (V054) to trim the new mart.
--
--   Additive columns + one new table/proc + two proc swaps. Apply AFTER V054.
--   Idempotent; safe to re-run.
--
-- Derivation law: SP_LOAD_QH_EXTRACT from V042 + column/cascade edits;
-- SP_PURGE_FACTS from V054 + one retention DELETE; the test re-derives and
-- byte-compares. MART_CLOUD_SVC_DAILY + SP_LOAD_CLOUD_SVC_MART are hand-authored.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20055, 'V055 requires V054 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 54) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- 1. Additive column on the QH extract (TRANSIENT; safe to leave on rollback).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
    ADD COLUMN IF NOT EXISTS CREDITS_USED_CLOUD_SERVICES FLOAT;

-- 3. Cloud-services breakdown mart (shape + user grain, CS>0 only).
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY (
    DAY                       DATE          NOT NULL,
    COMPANY                   VARCHAR(50),
    WAREHOUSE_NAME            VARCHAR(300)  NOT NULL,
    USER_NAME                 VARCHAR(300)  NOT NULL,
    ROLE_NAME                 VARCHAR(300)  NOT NULL,
    QUERY_TYPE                VARCHAR(100)  NOT NULL,
    QUERY_PARAMETERIZED_HASH  VARCHAR(64)   NOT NULL,
    SAMPLE_TEXT               VARCHAR(200),
    RUNS                      NUMBER,
    CS_CREDITS                FLOAT,
    EXEC_SEC_SUM              FLOAT,
    COMPILE_SEC_SUM           FLOAT,
    CACHE_PCT_SUM            FLOAT,
    LOAD_TS                   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- 4. Mart loader — from the extract (no QUERY_HISTORY rescan), trailing 3 days,
--    MERGE-accumulated. COMPANY via the warehouse UDF (V030 shape law: the UDF
--    runs on a plain column outside the aggregation).
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY t
    USING (
        SELECT g.DAY,
               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
               g.WAREHOUSE_NAME, g.USER_NAME, g.ROLE_NAME, g.QUERY_TYPE,
               g.QUERY_PARAMETERIZED_HASH, g.SAMPLE_TEXT, g.RUNS, g.CS_CREDITS,
               g.EXEC_SEC_SUM, g.COMPILE_SEC_SUM, g.CACHE_PCT_SUM
        FROM (
            SELECT DATE(START_TIME) AS DAY,
                   COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                   COALESCE(USER_NAME, 'UNKNOWN') AS USER_NAME,
                   COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                   COALESCE(QUERY_TYPE, 'UNKNOWN') AS QUERY_TYPE,
                   COALESCE(QUERY_PARAMETERIZED_HASH, 'n/a') AS QUERY_PARAMETERIZED_HASH,
                   ANY_VALUE(LEFT(QUERY_TEXT, 160)) AS SAMPLE_TEXT,
                   COUNT(*) AS RUNS,
                   SUM(COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)) AS CS_CREDITS,
                   SUM(COALESCE(EXECUTION_TIME, 0)) / 1000 AS EXEC_SEC_SUM,
                   SUM(COALESCE(COMPILATION_TIME, 0)) / 1000 AS COMPILE_SEC_SUM,
                   SUM(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)) AS CACHE_PCT_SUM
            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT
            -- Day-ALIGNED window (CURRENT_DATE, not CURRENT_TIMESTAMP): only
            -- merge whole calendar days that are fully inside the extract's 72h
            -- retention (today + the 2 prior full days). A rolling -3d TIMESTAMP
            -- window would overwrite an aging day with a shrinking PARTIAL count
            -- as the window slid off it, then freeze it partial (no backfill
            -- owns this mart's history). Aligned, a day is only ever written
            -- complete, so it freezes complete when it ages out.
            WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())
              AND COALESCE(CREDITS_USED_CLOUD_SERVICES, 0) > 0
            GROUP BY 1, 2, 3, 4, 5, 6
        ) g
    ) s
    ON  t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
    AND t.USER_NAME = s.USER_NAME AND t.ROLE_NAME = s.ROLE_NAME
    AND t.QUERY_TYPE = s.QUERY_TYPE
    AND t.QUERY_PARAMETERIZED_HASH = s.QUERY_PARAMETERIZED_HASH
    WHEN MATCHED THEN UPDATE SET
        COMPANY = s.COMPANY, SAMPLE_TEXT = s.SAMPLE_TEXT, RUNS = s.RUNS,
        CS_CREDITS = s.CS_CREDITS, EXEC_SEC_SUM = s.EXEC_SEC_SUM,
        COMPILE_SEC_SUM = s.COMPILE_SEC_SUM, CACHE_PCT_SUM = s.CACHE_PCT_SUM,
        LOAD_TS = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (DAY, COMPANY, WAREHOUSE_NAME, USER_NAME, ROLE_NAME, QUERY_TYPE,
         QUERY_PARAMETERIZED_HASH, SAMPLE_TEXT, RUNS, CS_CREDITS, EXEC_SEC_SUM,
         COMPILE_SEC_SUM, CACHE_PCT_SUM)
    VALUES
        (s.DAY, s.COMPANY, s.WAREHOUSE_NAME, s.USER_NAME, s.ROLE_NAME, s.QUERY_TYPE,
         s.QUERY_PARAMETERIZED_HASH, s.SAMPLE_TEXT, s.RUNS, s.CS_CREDITS, s.EXEC_SEC_SUM,
         s.COMPILE_SEC_SUM, s.CACHE_PCT_SUM);
    RETURN 'cloud-svc mart merged (' || :SQLROWCOUNT || ' rows)';
END;
$$;

-- 2. Re-derived extract loader — fills the new column + cascades the mart.
-- >>> derived:SP_LOAD_QH_EXTRACT
{extract}
-- 5. Re-derived purge — trims the new mart on the daily-fact retention floor.
-- >>> derived:SP_PURGE_FACTS
{purge}
-- First fill: refill the trailing 3 days of the extract WITH the CS column, which
-- cascades into MART_CLOUD_SVC_DAILY. The hourly task keeps both current.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 55 AS VERSION,
       'Per-query cloud-services credits persisted (OW_QH_EXTRACT column + MART_CLOUD_SVC_DAILY at shape/user grain) so the cloud-services ratio alert can be drilled to its driving query shapes and users' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 55);
"""
target = Path(os.environ.get("V055_OUT") or (MIG / "V055__cloud_services_breakdown.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
