#!/usr/bin/env python3
"""Forward-generate V094: fix the FACT_QUERY_HOURLY boundary-hour duplicate.

SP_LOAD_QH_EXTRACT's FACT_QUERY_HOURLY refresh (V062) bounds its DELETE on the
hour-truncated column HOUR_TS against a NON-truncated instant
`DATEADD('hour', -48, CURRENT_TIMESTAMP())`, while the paired INSERT groups
DATE_TRUNC('hour', START_TIME) filtered by the same non-truncated instant. The
boundary hour H0 = DATE_TRUNC('hour', now-48h) is < the bound, so each hourly run
FAILS to delete the already-complete H0 row yet INSERTS a fresh PARTIAL H0 row
(covering only [:MM, :00)). Once the bound advances past H0 the pair is never
revisited, so two rows accumulate at the same
(HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY) grain, permanently,
for every hour older than ~48h. Any reader that SUMs the grain over a window >=2 days
double-counts the post-:MM slice of each stale hour (~2x QUERY_COUNT / FAILED_COUNT /
ELAPSED_SEC_SUM / QUEUED_SEC_SUM / SPILL_REMOTE_GB; P95 is MAX-based, unaffected).

The sibling arm SP_LOAD_OPS_DIAG (same file, B10 fix) already truncates BOTH bounds to
the hour, proving the intended pattern — the FACT_QUERY_HOURLY arm was the one the B10
clamp missed.

Two enumerated edits, re-derived byte-identically from the LATEST SP_LOAD_QH_EXTRACT
(V062 — later migrations re-derive only SP_NIGHTLY_RECONCILE / SP_LOAD_MARTS_V27):
  1. DELETE bound: HOUR_TS >= DATEADD(...) -> HOUR_TS >= DATE_TRUNC('hour', DATEADD(...))
  2. INSERT bound: START_TIME >= DATEADD(...) -> START_TIME >= DATE_TRUNC('hour', DATEADD(...))
so the boundary hour is deleted AND fully rebuilt each run. The watermark first-run
fallback (also DATEADD('hour', -48, ...)) is deliberately UNTOUCHED.

Plus a one-time dedup of the rows the bug already left: keep the highest-QUERY_COUNT
row per grain (the complete hour is a superset of the partial slice) via an
INSERT OVERWRITE ... QUALIFY ROW_NUMBER() (the standard Snowflake in-place dedup;
the SELECT is evaluated against the pre-overwrite snapshot). SP_NIGHTLY_RECONCILE does
not rebuild FACT_QUERY_HOURLY, so without this the duplicates linger until the 90-day
purge.

Procedure re-derivation + one-time data cleanup: no schema change, no new object. Owner
applies in Snowsight after V093 (no backfill needed — the next hourly run self-heals the
recent window and the dedup clears the historical rows). This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V062__loader_robustness_alert_split_webhook.sql"

# --- enumerated edit 1: DELETE bound truncated to the hour ------------------------
OLD_DELETE = "     WHERE HOUR_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());"
NEW_DELETE = "     WHERE HOUR_TS >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()));"

# --- enumerated edit 2: INSERT bound truncated to the hour (GROUP BY pins the arm) -
OLD_INSERT = ("    WHERE START_TIME >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
              "    GROUP BY 1, 2, 3, 4, 5;")
NEW_INSERT = ("    WHERE START_TIME >= DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))\n"
              "    GROUP BY 1, 2, 3, 4, 5;")


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_LOAD_QH_EXTRACT")

for old in (OLD_DELETE, OLD_INSERT):
    assert proc.count(old) == 1, f"expected exactly 1 occurrence of: {old!r}"
proc = proc.replace(OLD_DELETE, NEW_DELETE).replace(OLD_INSERT, NEW_INSERT)
assert NEW_DELETE in proc and NEW_INSERT in proc
# the watermark first-run fallback uses the SAME instant but must stay UNTOUCHED
assert "DATEADD('hour', -48, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ" in proc
# both boundary bounds are now hour-truncated; no raw (untruncated) bound remains on
# the two facts arms (the only remaining raw -48h use is the ::TIMESTAMP_NTZ fallback)
assert proc.count("DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))") == 2
# untouched anchors: the extract arm + isolation structure carry over from V062
for anchor in (
    "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
    "SOURCE = 'QH_EXTRACT'",
    "ok := TRUE;",
    "APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95)",
    "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)",
):
    assert anchor in proc, anchor

out = f"""-- V094__fact_query_hourly_boundary_dedupe.sql
--
-- Fix a permanent FACT_QUERY_HOURLY duplicate. SP_LOAD_QH_EXTRACT's hourly refresh
-- (V062) DELETEs on the hour-truncated column HOUR_TS against a NON-truncated instant
-- but re-INSERTs DATE_TRUNC('hour', START_TIME) filtered by that same instant, so the
-- boundary hour H0 is never deleted yet is re-inserted PARTIAL each run -- two rows
-- accumulate per grain for every hour older than ~48h, and readers that SUM the grain
-- over a window >=2 days double-count (~2x QUERY_COUNT / FAILED_COUNT / ELAPSED /
-- QUEUED / SPILL). The sibling SP_LOAD_OPS_DIAG already truncates both bounds (the B10
-- fix); this arm was missed.
--
-- Re-derives SP_LOAD_QH_EXTRACT from V062 byte-identically plus two edits: the DELETE
-- and INSERT bounds are truncated to the hour (DATE_TRUNC('hour', DATEADD(...))), so
-- the boundary hour is deleted AND fully rebuilt each run. The watermark first-run
-- fallback is deliberately unchanged. Then a one-time dedup keeps the highest-
-- QUERY_COUNT row per grain (the complete hour is a superset of the partial slice) --
-- SP_NIGHTLY_RECONCILE does not rebuild this fact, so the historical duplicates need an
-- explicit cleanup.
--
-- Procedure re-derivation + one-time data cleanup: no schema change, no new object, no
-- backfill. Owner applies in Snowsight after V093. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20094, 'V094 requires V093 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 93) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
-- One-time dedup of the rows the boundary-hour bug already left: keep the highest-
-- QUERY_COUNT row per grain (the complete hour dominates the partial slice), break ties
-- on the newest LOAD_TS. INSERT OVERWRITE reads the pre-overwrite snapshot, so the
-- self-referential dedup is safe (the standard Snowflake pattern). No-op once clean.
INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
    (HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
     FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB, LOAD_TS)
SELECT HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY, QUERY_COUNT,
       FAILED_COUNT, ELAPSED_SEC_SUM, P95_ELAPSED_SEC, QUEUED_SEC_SUM, SPILL_REMOTE_GB, LOAD_TS
FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY
    ORDER BY QUERY_COUNT DESC, LOAD_TS DESC) = 1;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 94 AS VERSION,
       'FACT_QUERY_HOURLY boundary-hour dedupe: SP_LOAD_QH_EXTRACT re-derived from V062 so the FACT_QUERY_HOURLY DELETE and INSERT bounds are both hour-truncated (DATE_TRUNC(hour, DATEADD(hour,-48,...))) like the sibling SP_LOAD_OPS_DIAG, so the boundary hour is deleted and fully rebuilt each run instead of leaving a permanent partial duplicate that doubled multi-day query facts. Plus a one-time INSERT OVERWRITE dedup keeping the highest-QUERY_COUNT row per grain. Proc + data cleanup, no schema change, no backfill.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 94);
"""

# --- self-assertions -------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE VIEW" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert out.count("INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY") == 1
assert "QUALIFY ROW_NUMBER() OVER (" in out
assert "EXCEPTION (-20094" in out and "IF (v < 93) THEN" in out
assert "SELECT 94 AS VERSION" in out and "WHERE VERSION = 94)" in out

target = Path(os.environ.get("V094_OUT")
              or (MIG / "V094__fact_query_hourly_boundary_dedupe.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
