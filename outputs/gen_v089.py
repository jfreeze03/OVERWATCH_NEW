#!/usr/bin/env python3
"""Forward-generate V089: fix SP_BACKUP_OPERATOR_TABLES clone-to-permanent failure.

The weekly operator-table backup (V015, last redefined V075) clones each operator
table to a ``*_BAK_LAST`` snapshot with:

    CREATE OR REPLACE TABLE ..._BAK_LAST CLONE ...

Several operator tables are TRANSIENT (ALERT_EVENTS, ACTION_QUEUE, ...), and
Snowflake refuses to clone a transient object into a PERMANENT one:
"Transient object cannot be cloned to a permanent object." So those clones failed
every run (owner error log 2026-08-17: BackupOperatorTables / clone_failed x3
since 2026-08-02) -- the backup silently skipped exactly the churny tables it most
needed to protect.

Fix: make the backup target TRANSIENT (CREATE OR REPLACE TRANSIENT TABLE ...
CLONE). A transient clone works whether the SOURCE is transient or permanent, and
a backup snapshot needs neither Fail-safe nor Time Travel beyond the default -- so
TRANSIENT is strictly correct here and also cheaper (no Fail-safe storage).

Re-derives SP_BACKUP_OPERATOR_TABLES from the V075 base (its last definition),
changing only the clone DDL. Owner applies in Snowsight after V088. No app version
bump semantics beyond the migration contract. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V075__security_operating_model.sql"

OLD_DDL = ("            EXECUTE IMMEDIATE 'CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||\n"
           "                              '_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;")
NEW_DDL = ("            -- V089: TRANSIENT target -- a transient source (ALERT_EVENTS,\n"
           "            -- ACTION_QUEUE, ...) cannot clone into a PERMANENT table\n"
           "            -- (\"Transient object cannot be cloned to a permanent object\"),\n"
           "            -- which failed those backups every run. TRANSIENT works for both\n"
           "            -- transient and permanent sources and needs no Fail-safe.\n"
           "            EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||\n"
           "                              '_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;")


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(\).*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


proc = extract_proc(BASE.read_text(encoding="utf-8"), "SP_BACKUP_OPERATOR_TABLES")
assert proc.count(OLD_DDL) == 1, f"expected 1 clone DDL, got {proc.count(OLD_DDL)}"
proc = proc.replace(OLD_DDL, NEW_DDL)
assert "CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname" in proc
assert proc.count("CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.' || :tname") == 0
# Untouched anchors: the table list, the error-capture arm, and the RETURN survive.
for anchor in ("'SETTINGS', 'COMPANY_SCOPE', 'ALERT_CONFIG', 'ALERT_EVENTS'",
               "'ACTION_QUEUE', 'SAVINGS_LEDGER'", "'clone_failed'",
               "RETURN 'cloned ' || :done"):
    assert anchor in proc, anchor

out = f"""-- V089__backup_transient_clone.sql
--
-- Fix SP_BACKUP_OPERATOR_TABLES: it cloned each operator table to a PERMANENT
-- *_BAK_LAST snapshot, but transient operator tables (ALERT_EVENTS, ACTION_QUEUE,
-- ...) cannot be cloned into a permanent object -- so those backups failed every
-- run (owner error log 2026-08-17: clone_failed x3 since 2026-08-02).
--
-- Re-derives the proc from the V075 base, changing only the clone target to
-- TRANSIENT (works for transient AND permanent sources; a backup needs no
-- Fail-safe). Idempotent CREATE OR REPLACE; supersedes V075's definition.
-- Owner applies in Snowsight after V088. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20089, 'V089 requires V088 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 88) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_BACKUP_OPERATOR_TABLES  (TRANSIENT clone target, from V075)
{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 89 AS VERSION,
       'SP_BACKUP_OPERATOR_TABLES clones to a TRANSIENT *_BAK_LAST target (was permanent): transient operator tables (ALERT_EVENTS, ACTION_QUEUE, ...) cannot clone into a permanent object, so those weekly backups failed every run (owner error log 2026-08-17, clone_failed x3 since 2026-08-02). TRANSIENT works for transient and permanent sources and needs no Fail-safe. Proc re-derived from V075; no data reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 89);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE TRANSIENT TABLE" in out
assert "CREATE TASK" not in out and "CREATE WAREHOUSE" not in out
assert "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20089" in out and "IF (v < 88) THEN" in out
assert "SELECT 89 AS VERSION" in out and "WHERE VERSION = 89)" in out

target = Path(os.environ.get("V089_OUT")
              or (MIG / "V089__backup_transient_clone.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
