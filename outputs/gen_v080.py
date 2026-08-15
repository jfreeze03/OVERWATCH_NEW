#!/usr/bin/env python3
"""Forward-generate V080: stop the Security "Change Risk" queue from flooding on
routine ETL truncate-and-reload DDL.

V075 classifies every DROP/TRUNCATE as a CRITICAL "DESTRUCTIVE" change and the
V_SECURITY_EXCEPTION_QUEUE surfaces it (RISK_SCORE >= 70). But on this account the
volume is automated ETL: a handful of service roles run DROP TABLE IF EXISTS /
TRUNCATE TABLE on EDW tables thousands of times a day (confirmed from
ACCOUNT_USAGE.QUERY_HISTORY, owner 2026-08-15). That noise buried real signal and
forced the domain score to 0/100.

Fix: re-derive V_SECURITY_EXCEPTION_QUEUE (V075) with ONE addition to the CHANGE
RISK arm's WHERE -- exclude DESTRUCTIVE events performed BY the known ETL/service
roles. Deliberately scoped:
  * CHANGE_KIND = 'DESTRUCTIVE' only -> GRANT / REVOKE / POLICY changes by these
    same roles are STILL surfaced (privilege escalation is real signal).
  * By ROLE, not schema -> a DESTRUCTIVE drop by any OTHER (human / interactive)
    role, even in the same EDW schemas, is STILL surfaced.
  * The rows are NOT deleted -- they remain in FACT_SECURITY_CHANGE for
    drill-down and audit; only the exception QUEUE (and the domain score that
    reads it) stop counting them.

The 18 roles are three ETL/service-engine families the owner confirmed (Glue,
Informatica, transform SYSADMIN -- single user each, daily, DROP-IF-EXISTS /
TRUNCATE / DROP TASK), expanded across the six environments. Adding or removing
a role later is a one-line re-derivation of this same view.

View-only: no data reload, no new objects, no app version bump. Owner applies in
Snowsight after V079. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V075__security_operating_model.sql"

# The three ETL-engine role families the owner named (owner 2026-08-15). The
# names carry an ENVIRONMENT token; the same three families run in every
# environment, so expand each across all of them. Generated (not hand-typed) so
# no environment is missed or misspelled. (The lower-volume SNOW_PRI_GFR_* roles
# were deliberately NOT included, per owner scoping.)
ENVIRONMENTS = ("PRD", "MGM", "SAN", "SEA", "DEV", "PHX")
ROLE_TEMPLATES = (
    "TF_SFR_{}_GLUE",
    "TF_SFR_{}_INFORMATICA",
    "TF_O_{}_ALFA_SYSADMIN",
)
ETL_ROLES = tuple(t.format(env) for t in ROLE_TEMPLATES for env in ENVIRONMENTS)

OLD_WHERE = ("    WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) "
             "AND RISK_SCORE >= 70")
# Three roles per line, indented to sit under the IN (.
_role_list = ",\n".join(
    "          " + ", ".join(f"'{r}'" for r in ETL_ROLES[i:i + 3])
    for i in range(0, len(ETL_ROLES), 3)
)
# NOTE: no ';' anywhere in this comment block -- a ';' inside a view comment
# fools naive statement splitters into ending the CREATE VIEW early.
# COALESCE(ROLE_NAME, '') keeps the exclusion NULL-safe: a DESTRUCTIVE event with
# an unattributed (NULL) ROLE_NAME is NOT one of the named engines, so it must
# still surface. Bare `ROLE_NAME IN (...)` would go NULL and the outer NOT(...)
# would silently drop that row (three-valued logic) -- the wrong failure mode for
# a security queue.
NEW_WHERE = (
    OLD_WHERE + "\n"
    "      -- V080: ETL/service-role truncate-and-reload is not a security event.\n"
    "      -- DESTRUCTIVE by these roles is dropped from the queue, but their\n"
    "      -- GRANT/REVOKE/POLICY changes and any other role's DROP still surface.\n"
    "      -- COALESCE keeps it NULL-safe: an unattributed (NULL-role) DROP surfaces.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND COALESCE(ROLE_NAME, '') IN (\n"
    + _role_list + "))"
)


def extract_view(text: str, name: str) -> str:
    # A view is one statement: CREATE OR REPLACE VIEW ... AS <SELECT> ; with no
    # semicolons inside, so the first ';\n' after AS terminates it.
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
assert view.count("CHANGE_KIND = 'DESTRUCTIVE' AND COALESCE(ROLE_NAME, '') IN (") == 1
# Untouched anchors: the other queue arms and the final join survive.
for anchor in ("'CHANGE RISK'", "'TRUST CENTER'", "'IDENTITY'", "'PRIVILEGE'",
               "FROM candidates c", "COALESCE(a.ACTION_STATUS, 'UNTRACKED') AS STATUS"):
    assert anchor in view, anchor
for role in ETL_ROLES:
    assert f"'{role}'" in view, role

out = f"""-- V080__security_change_risk_etl_exclusion.sql
--
-- Stop the Security "Change Risk" queue flooding on routine ETL truncate-and-
-- reload DDL. V075 flags every DROP/TRUNCATE as CRITICAL "DESTRUCTIVE", but on
-- this account that volume is automated ETL: a handful of service roles run
-- DROP TABLE IF EXISTS / TRUNCATE on EDW tables thousands of times a day
-- (confirmed from ACCOUNT_USAGE.QUERY_HISTORY, owner 2026-08-15), which buried
-- real signal and forced the domain score to 0/100.
--
-- Re-derives V_SECURITY_EXCEPTION_QUEUE (from V075) with ONE change: the CHANGE
-- RISK arm excludes DESTRUCTIVE events BY the known ETL/service roles. Scoped so
-- GRANT/REVOKE/POLICY by those roles, and DESTRUCTIVE by any OTHER (human) role,
-- are STILL surfaced; the rows remain in FACT_SECURITY_CHANGE for audit -- only
-- the exception QUEUE (and the domain score that reads it) stop counting them.
-- Adding/removing a role later is a one-line re-derivation of this view.
--
-- View-only: no data reload, no new objects, no app version bump. Fixes both the
-- queue display and the domain score at once (same view). Owner applies in
-- Snowsight after V079. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20080, 'V080 requires V079 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 79) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (CHANGE RISK arm excludes ETL/service-role DESTRUCTIVE, from V075)
{view}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 80 AS VERSION,
       'Security change-risk ETL exclusion: V_SECURITY_EXCEPTION_QUEUE CHANGE RISK arm excludes DESTRUCTIVE (DROP/TRUNCATE) events by the confirmed ETL/service roles (Glue/Informatica/transform + pipeline service accounts) so routine truncate-and-reload stops flooding the queue and zeroing the domain score. GRANT/REVOKE/POLICY by those roles and DESTRUCTIVE by any other role still surface; rows stay in FACT_SECURITY_CHANGE for audit. View-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 80);
"""

assert out.count("CREATE OR REPLACE VIEW") == 1
assert "CREATE OR REPLACE PROCEDURE" not in out
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20080" in out and "IF (v < 79) THEN" in out
assert "SELECT 80 AS VERSION" in out and "WHERE VERSION = 80)" in out
assert out.count("CHANGE_KIND = 'DESTRUCTIVE' AND COALESCE(ROLE_NAME, '') IN (") == 1  # exactly one exclusion, on CHANGE RISK

target = Path(os.environ.get("V080_OUT") or (MIG / "V080__security_change_risk_etl_exclusion.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
