#!/usr/bin/env python3
"""Forward-generate V149: add SESSION_ID + IS_CLIENT_GENERATED_STATEMENT to OW_QH_EXTRACT.

Foundation for cloud-services driver / application attribution (Phase 1 of the Cloud
Services Driver Intelligence work). To attribute metadata chatter to a client application
or driver, a later phase joins ACCOUNT_USAGE.SESSIONS on SESSION_ID; to separate
platform/driver-issued statements from user-authored ones it reads
IS_CLIENT_GENERATED_STATEMENT. Both columns already exist on ACCOUNT_USAGE.QUERY_HISTORY
but the single-scan staging copy OW_QH_EXTRACT does not carry them, so nothing downstream
can see them today.

DERIVATION BASE = V094 (the CURRENT SP_LOAD_QH_EXTRACT), NOT V055: the loader was
re-derived across V041 -> V042 -> V055 -> V056 -> V062 -> V094, so deriving from an older
copy would silently drop the intervening changes (the V047-from-V036 / V148-from-V073
class). This re-derives from V094 byte-identically plus two enumerated edits that append
the two columns to the extract INSERT column list and its SELECT; every other arm
(FACT_QUERY_HOURLY / FACT_QUERY_DAILY refresh, the V017 isolation, the watermark, the
SP_LOAD_CLOUD_SVC_MART cascade) is untouched.

Additive schema change: ALTER OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS (idempotent) for the
two columns, then the re-derived proc. The tail CALL(3) reloads the whole 72h extract so
the columns carry values immediately (a plain watermark-mode hourly run would only fill
them forward). Owner applies in Snowsight after V148. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V094__fact_query_hourly_boundary_dedupe.sql"

# --- enumerated edit 1: append the two columns to the extract INSERT column list ---
OLD_COLS = "         CREDITS_USED_CLOUD_SERVICES)\n    SELECT QUERY_ID"
NEW_COLS = ("         CREDITS_USED_CLOUD_SERVICES, SESSION_ID, IS_CLIENT_GENERATED_STATEMENT)\n"
            "    SELECT QUERY_ID")

# --- enumerated edit 2: append the two columns to the extract SELECT (from QUERY_HISTORY) -
OLD_SEL = ("           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)\n"
           "    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY")
NEW_SEL = ("           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0),\n"
           "           SESSION_ID, IS_CLIENT_GENERATED_STATEMENT\n"
           "    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY")


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_LOAD_QH_EXTRACT")

for old in (OLD_COLS, OLD_SEL):
    assert proc.count(old) == 1, f"expected exactly 1 occurrence of: {old!r}"
proc = proc.replace(OLD_COLS, NEW_COLS).replace(OLD_SEL, NEW_SEL)
assert NEW_COLS in proc and NEW_SEL in proc
# the two columns land in BOTH the INSERT list and the SELECT (once each)
assert proc.count("SESSION_ID, IS_CLIENT_GENERATED_STATEMENT") == 2
# untouched anchors: every other arm carries over from V094 verbatim
for anchor in (
    "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
    "SOURCE = 'QH_EXTRACT'",
    "ok := TRUE;",
    "APPROX_PERCENTILE(TOTAL_ELAPSED_TIME / 1000, 0.95)",
    "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)",
    "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();",
    "DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))",   # the V094 fix survives
):
    assert anchor in proc, anchor

out = f"""-- V149__qh_extract_session_client_columns.sql
--
-- Foundation for cloud-services driver / application attribution (Phase 1). Adds SESSION_ID
-- and IS_CLIENT_GENERATED_STATEMENT to OW_QH_EXTRACT so a later phase can (a) join
-- ACCOUNT_USAGE.SESSIONS on SESSION_ID to attribute metadata chatter to a client
-- application / driver, and (b) separate platform/driver-issued statements from
-- user-authored ones. Both columns already exist on ACCOUNT_USAGE.QUERY_HISTORY; nothing
-- reads them yet (the reader panel is the next app-side step), so this is purely additive.
--
-- Re-derives SP_LOAD_QH_EXTRACT from its CURRENT definition -- V094, NOT V055: the loader
-- was re-derived across V041/V042/V055/V056/V062/V094, so an older base would silently
-- drop the intervening changes -- byte-identically plus two edits appending the two columns
-- to the extract INSERT column list and SELECT. Every other arm is untouched. The tail
-- CALL(3) reloads the 72h extract so the columns carry values immediately.
--
-- ALTER ... ADD COLUMN IF NOT EXISTS is idempotent; the proc re-derivation and CALL are
-- safe to re-run. Owner applies in Snowsight after V148. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20149, 'V149 requires V148 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 148) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Additive columns on the single-scan staging copy (names mirror ACCOUNT_USAGE.QUERY_HISTORY
-- so the SELECT below fills them directly). Idempotent.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS SESSION_ID NUMBER;
ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS IS_CLIENT_GENERATED_STATEMENT BOOLEAN;

{proc}
-- Reload the whole 72h extract so SESSION_ID / IS_CLIENT_GENERATED_STATEMENT carry values
-- immediately (a plain watermark-mode hourly run would only fill them from the watermark
-- forward, leaving the rest of the retention window NULL until it ages out).
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 149 AS VERSION,
       'OW_QH_EXTRACT session/client columns: ADD COLUMN SESSION_ID + IS_CLIENT_GENERATED_STATEMENT (both on ACCOUNT_USAGE.QUERY_HISTORY) to the single-scan staging copy, and re-derive SP_LOAD_QH_EXTRACT from V094 (the current definition; the loader was re-derived across V041/V042/V055/V056/V062/V094) so the extract INSERT/SELECT fill them -- byte-identical otherwise. Foundation for cloud-services driver/application attribution (join ACCOUNT_USAGE.SESSIONS on SESSION_ID; split client-generated vs user statements); nothing reads them yet. Tail CALL(3) reloads the 72h extract so the columns populate immediately. Additive schema change, no backfill.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 149);
"""

# --- self-assertions -------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT" in out
assert out.count("ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS") == 2
assert "SESSION_ID NUMBER;" in out and "IS_CLIENT_GENERATED_STATEMENT BOOLEAN;" in out
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);") == 1
assert "CREATE OR REPLACE VIEW" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20149" in out and "IF (v < 148) THEN" in out
assert "SELECT 149 AS VERSION" in out and "WHERE VERSION = 149)" in out

target = Path(os.environ.get("V149_OUT")
              or (MIG / "V149__qh_extract_session_client_columns.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
