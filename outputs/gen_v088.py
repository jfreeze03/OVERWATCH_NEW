#!/usr/bin/env python3
"""Forward-generate V088: broaden the Security "Change Risk" ETL exclusion.

V080 stopped the change-risk queue flooding on routine ETL truncate-and-reload by
excluding DESTRUCTIVE events from a FIXED list of 18 service roles. On this
account the flood persisted (owner 2026-08-17): DESTRUCTIVE DROP/TRUNCATE rows on
ALFA_EDW_PRD.PUBLIC and DBA_MAINT_DB.PUBLIC, plus NULL-database "DROP" rows, still
zeroed the CHANGE RISK domain score -- i.e. the roles actually driving it were not
in that fixed list.

Fix: re-derive V_SECURITY_EXCEPTION_QUEUE (from the V075 base, same as V080) with
a BROADER CHANGE RISK exclusion:
  * role PATTERN, not a fixed list -- every known ETL/service role follows the
    Terraform service-account convention (TF_*, non-interactive automation), so
    match TF_* instead of enumerating environments/engines that keep changing.
  * OVERWATCH's own DBA_MAINT_DB -- the app's operational database churns on its
    own object refreshes; that is not a security event.

Deliberately still scoped, exactly as V080:
  * CHANGE_KIND = 'DESTRUCTIVE' only -> GRANT / REVOKE / POLICY by these same
    roles is STILL surfaced (privilege escalation is real signal).
  * NULL-safe via COALESCE -> a DESTRUCTIVE event with an unattributed (NULL) role
    on a non-app database still surfaces.
  * Rows are NOT deleted -- they stay in FACT_SECURITY_CHANGE for audit; only the
    exception QUEUE (and the domain score that reads it) stop counting them.

View-only: no data reload, no new objects, no app version bump. Owner applies in
Snowsight after V087. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V075__security_operating_model.sql"

OLD_WHERE = ("    WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) "
             "AND RISK_SCORE >= 70")
# COALESCE(...) keeps the exclusion NULL-safe; a DESTRUCTIVE event with an
# unattributed (NULL) ROLE_NAME on a non-app database is not TF_* / DBA_MAINT_DB,
# so it still surfaces. 'TF~_%' ESCAPE '~' matches a literal "TF_" prefix (the ~
# escape avoids backslash-in-SQL-string semantics). No ';' anywhere in the comment
# block -- a ';' inside a view comment fools naive statement splitters.
NEW_WHERE = (
    OLD_WHERE + "\n"
    "      -- V088: V080's fixed 18-role list missed the roles driving the flood. The\n"
    "      -- owner diagnostic (2026-08-17, Security change-risk noise panel) confirmed\n"
    "      -- 94% of DESTRUCTIVE events are TF_* roles, 0% unattributed, ~0% on the app\n"
    "      -- DB -- so match the Terraform service-role convention (TF_*, non-interactive\n"
    "      -- automation) instead of a fixed list, and drop the app's own\n"
    "      -- DBA_MAINT_DB.PUBLIC scratch churn. The DBA_MAINT_DB carve-out is narrowed\n"
    "      -- to PUBLIC so a DROP/TRUNCATE of the OVERWATCH audit tables stays visible.\n"
    "      -- Still DESTRUCTIVE-only and NULL-safe: GRANT/REVOKE/POLICY by these roles\n"
    "      -- and any non-TF role's DROP still surface.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND (\n"
    "          UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'\n"
    "          OR (UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'\n"
    "              AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC')))"
)


def extract_view(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.{name} AS.*?;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


view = extract_view(BASE.read_text(encoding="utf-8"), "V_SECURITY_EXCEPTION_QUEUE")
assert view.count(OLD_WHERE) == 1, f"expected 1 CHANGE RISK WHERE, got {view.count(OLD_WHERE)}"
view = view.replace(OLD_WHERE, NEW_WHERE)
assert NEW_WHERE in view
assert view.count("CHANGE_KIND = 'DESTRUCTIVE' AND (") == 1
assert "LIKE 'TF~_%' ESCAPE '~'" in view
assert "= 'DBA_MAINT_DB'\n" in view and "AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC')))" in view
# Untouched anchors: the other queue arms and the final join survive.
for anchor in ("'CHANGE RISK'", "'TRUST CENTER'", "'IDENTITY'", "'PRIVILEGE'",
               "FROM candidates c", "COALESCE(a.ACTION_STATUS, 'UNTRACKED') AS STATUS"):
    assert anchor in view, anchor

out = f"""-- V088__security_change_risk_etl_exclusion_broadened.sql
--
-- Broaden V080's Security "Change Risk" ETL exclusion. V080 excluded DESTRUCTIVE
-- (DROP/TRUNCATE) events from a FIXED list of 18 service roles, but on this
-- account the flood persisted (owner 2026-08-17) -- the roles driving it were not
-- in that list, and OVERWATCH's own DBA_MAINT_DB churn plus NULL-database drops
-- kept the CHANGE RISK domain score at 0/100.
--
-- Re-derives V_SECURITY_EXCEPTION_QUEUE (from the V075 base, same as V080) with a
-- BROADER CHANGE RISK exclusion: match the Terraform service-role convention
-- (TF_*, non-interactive automation) instead of a fixed list, and drop the app's
-- own DBA_MAINT_DB. Still DESTRUCTIVE-only and NULL-safe; GRANT/REVOKE/POLICY by
-- those roles and any human role's DROP still surface. Rows stay in
-- FACT_SECURITY_CHANGE for audit -- only the QUEUE and its domain score change.
--
-- View-only: no data reload, no new objects, no app version bump. Supersedes V080
-- (later CREATE OR REPLACE wins). Owner applies in Snowsight after V087. This file
-- never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20088, 'V088 requires V087 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 87) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (CHANGE RISK arm excludes TF_* / DBA_MAINT_DB DESTRUCTIVE, from V075)
{view}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 88 AS VERSION,
       'Security change-risk ETL exclusion broadened (owner diagnostic 2026-08-17 confirmed 94% of DESTRUCTIVE events are TF_* roles, 0% unattributed): V_SECURITY_EXCEPTION_QUEUE CHANGE RISK arm now excludes DESTRUCTIVE (DROP/TRUNCATE) events by any Terraform service role (TF_*, generalizing V080 fixed list) and on OVERWATCH own DBA_MAINT_DB.PUBLIC (narrowed to PUBLIC so audit-table drops stay visible), so the persistent routine-ETL flood stops zeroing the domain score. Still DESTRUCTIVE-only and NULL-safe; GRANT/REVOKE/POLICY and any non-TF role DROP still surface; rows stay in FACT_SECURITY_CHANGE for audit. View-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 88);
"""

assert out.count("CREATE OR REPLACE VIEW") == 1
assert "CREATE OR REPLACE PROCEDURE" not in out
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20088" in out and "IF (v < 87) THEN" in out
assert "SELECT 88 AS VERSION" in out and "WHERE VERSION = 88)" in out
assert out.count("CHANGE_KIND = 'DESTRUCTIVE' AND (") == 1  # exactly one exclusion, on CHANGE RISK

target = Path(os.environ.get("V088_OUT")
              or (MIG / "V088__security_change_risk_etl_exclusion_broadened.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
