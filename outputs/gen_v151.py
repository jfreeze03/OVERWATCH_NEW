#!/usr/bin/env python3
"""Forward-generate V151: Terraform-role identity / policy drops re-surface in CHANGE RISK.

Next-Fifty rank 8 (confirmed defect). V088 excluded EVERY CHANGE_KIND='DESTRUCTIVE' row by a
Terraform service role (TF_*) or on the app scratch schema DBA_MAINT_DB.PUBLIC, to stop the
truncate-and-reload flood zeroing the CHANGE RISK domain score. But SP_LOAD_SECURITY_FACTS (V105,
its current definer) classifies with the DROP%/TRUNCATE% test BEFORE the %POLICY%/%USER% test, so
DROP_USER, DROP_ROLE and every DROP ... POLICY land in DESTRUCTIVE too -- and V088 hid them all,
contrary to its own comment ("GRANT/REVOKE/POLICY by these roles ... still surface").

Fix: re-derive V_SECURITY_EXCEPTION_QUEUE from V088 (its current definition; V088 itself
re-derived from V075 and deliberately superseded V080's fixed role list), byte-identical except
the CHANGE RISK exclusion block. A TF_* / DBA_MAINT_DB.PUBLIC DESTRUCTIVE row is now KEPT when
  * its QUERY_TYPE names a USER / ROLE / POLICY object (ILIKE ANY), or
  * its whitespace-normalized, upper-cased statement preview OPENS with one of 17 keyword-anchored
    DROP openers: USER, ROLE, DATABASE ROLE, APPLICATION ROLE, and 13 named POLICY kinds
    (MASKING, ROW ACCESS, NETWORK, PASSWORD, SESSION, AUTHENTICATION, AGGREGATION, PROJECTION,
    JOIN, PACKAGES, PRIVACY, STORAGE LIFECYCLE, BACKUP -- decision O-1).

Two deliberate deviations from the rec text (correctness, not taste):
  * NO bare ``QUERY_PREVIEW ILIKE '%POLICY%'`` -- this is an insurance EDW (FACT_POLICY-style
    tables), so a TF_* TRUNCATE of FACT_POLICY would re-flood the queue. Openers are anchored.
  * NO ``DATABASE_NAME IS NOT NULL`` -- DATABASE_NAME is the SESSION database, not the dropped
    object's, and NULL-database drops were part of the flood V088 was written to suppress.

The app mirror of both keep lists lives in app/data/security_sql.py
(CHANGE_RISK_KEEP_QUERY_TYPES / CHANGE_RISK_KEEP_PREVIEWS); the V151 migration test locks the two
copies together. This generator stays self-contained (no app import), per house style.

View-only: no data reload, no new objects, no tail CALL. Rows were never deleted from
FACT_SECURITY_CHANGE, so the 7-day queue window corrects the moment the view is replaced.
Owner applies in Snowsight after V150. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V088__security_change_risk_etl_exclusion_broadened.sql"

# V088's appended CHANGE RISK exclusion block, byte-for-byte (== V088:79-91, and
# tests/migrations/test_v088_change_risk_exclusion_broadened.py::_ADDED).
OLD_BLOCK = (
    "\n"
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

# The keep lists (app mirror: security_sql.CHANGE_RISK_KEEP_QUERY_TYPES / _PREVIEWS).
KEEP_QUERY_TYPES = ("%USER%", "%ROLE%", "%POLICY%")
KEEP_PREVIEWS = (
    "DROP USER %", "DROP ROLE %", "DROP DATABASE ROLE %", "DROP APPLICATION ROLE %",
    "DROP MASKING POLICY %", "DROP ROW ACCESS POLICY %", "DROP NETWORK POLICY %",
    "DROP PASSWORD POLICY %", "DROP SESSION POLICY %", "DROP AUTHENTICATION POLICY %",
    "DROP AGGREGATION POLICY %", "DROP PROJECTION POLICY %", "DROP JOIN POLICY %",
    "DROP PACKAGES POLICY %", "DROP PRIVACY POLICY %", "DROP STORAGE LIFECYCLE POLICY %",
    "DROP BACKUP POLICY %",
)

# Notes on the predicate:
#  * '[[:space:]]+' is a POSIX class -- no backslash inside a single-quoted SQL string (the same
#    reason V088 uses the ~ ESCAPE). House precedent: app/data/change_impact_sql.py REGEXP_REPLACE.
#  * LTRIM runs AFTER the collapse (LTRIM strips blanks only), so a leading newline/tab is handled.
#  * No pattern contains '_', so no ESCAPE is needed on either list.
#  * No ';' anywhere in the block, and no quote character in its comment lines (both asserted):
#    a ';' inside a view comment fools naive statement splitters.
NEW_BLOCK = (
    "\n"
    "      -- V088 excluded DESTRUCTIVE (DROP/TRUNCATE) rows by Terraform service roles\n"
    "      -- (TF_*) and on the app scratch schema DBA_MAINT_DB.PUBLIC -- the owner\n"
    "      -- diagnostic (2026-08-17) put 94% of the flood on TF_* roles.\n"
    "      -- V151: that exclusion also hid every TF_* DROP USER / DROP ROLE / DROP POLICY,\n"
    "      -- because the loader files DROP_USER and DROP_ROLE under DESTRUCTIVE (its DROP\n"
    "      -- test runs before its USER test). Identity and governance deletions are not\n"
    "      -- truncate-and-reload noise, so a row is now kept whenever its QUERY_TYPE names\n"
    "      -- a USER / ROLE / POLICY object or its statement opens DROP USER, DROP ROLE,\n"
    "      -- DROP DATABASE ROLE, DROP APPLICATION ROLE or DROP (kind) POLICY. The preview\n"
    "      -- test is keyword-anchored, never a bare POLICY substring, so a TF_* TRUNCATE of\n"
    "      -- an insurance FACT_POLICY table stays excluded. Table/schema drops by TF_*\n"
    "      -- roles and the DBA_MAINT_DB.PUBLIC churn stay excluded, NULL-safe as before.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND (\n"
    "          UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'\n"
    "          OR (UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'\n"
    "              AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC'))\n"
    "          AND NOT (COALESCE(QUERY_TYPE, '') ILIKE ANY ('%USER%', '%ROLE%', '%POLICY%')\n"
    "              OR LTRIM(REGEXP_REPLACE(UPPER(COALESCE(QUERY_PREVIEW, '')), '[[:space:]]+', ' '))\n"
    "                 LIKE ANY ('DROP USER %', 'DROP ROLE %', 'DROP DATABASE ROLE %',\n"
    "                           'DROP APPLICATION ROLE %', 'DROP MASKING POLICY %',\n"
    "                           'DROP ROW ACCESS POLICY %', 'DROP NETWORK POLICY %',\n"
    "                           'DROP PASSWORD POLICY %', 'DROP SESSION POLICY %',\n"
    "                           'DROP AUTHENTICATION POLICY %', 'DROP AGGREGATION POLICY %',\n"
    "                           'DROP PROJECTION POLICY %', 'DROP JOIN POLICY %',\n"
    "                           'DROP PACKAGES POLICY %', 'DROP PRIVACY POLICY %',\n"
    "                           'DROP STORAGE LIFECYCLE POLICY %', 'DROP BACKUP POLICY %')))"
)


def extract_view(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.{name} AS.*?;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 view, got {len(matches)}"
    return matches[0]


# --- block self-checks -------------------------------------------------------------------------
assert len(KEEP_QUERY_TYPES) == 3 and len(KEEP_PREVIEWS) == 17, "4 identity + 13 policy openers"
assert len(set(KEEP_PREVIEWS)) == len(KEEP_PREVIEWS)
_types_sql = "ILIKE ANY (" + ", ".join(f"'{p}'" for p in KEEP_QUERY_TYPES) + ")"
assert NEW_BLOCK.count(_types_sql) == 1, "QUERY_TYPE keep list drifted from KEEP_QUERY_TYPES"
_prev_sql = re.search(r"LIKE ANY \(([^)]*)\)\)\)$", NEW_BLOCK, re.S)
assert _prev_sql, "preview LIKE ANY list not found at the block tail"
assert tuple(re.findall(r"'([^']*)'", _prev_sql.group(1))) == KEEP_PREVIEWS, "preview keep list drifted"
assert all("_" not in p for p in KEEP_QUERY_TYPES + KEEP_PREVIEWS), "a '_' pattern needs an ESCAPE"
assert NEW_BLOCK.count("'%POLICY%'") == 1, "no bare POLICY substring outside the QUERY_TYPE list"
assert "QUERY_PREVIEW, '')) ILIKE" not in NEW_BLOCK and "IS NOT NULL" not in NEW_BLOCK
assert ";" not in NEW_BLOCK
assert "'" not in "".join(ln for ln in NEW_BLOCK.splitlines() if ln.strip().startswith("--"))
# The V088 ETL scope (TF_* pattern + NULL-safe DBA_MAINT_DB.PUBLIC carve-out) is carried verbatim.
_V088_SCOPE = OLD_BLOCK[OLD_BLOCK.index("      AND NOT (CHANGE_KIND"):]
assert NEW_BLOCK.count(_V088_SCOPE[:-1]) == 1, "V088 TF_* / PUBLIC scope must be carried byte-for-byte"

view = extract_view(BASE.read_text(encoding="utf-8"), "V_SECURITY_EXCEPTION_QUEUE")
assert view.count(OLD_BLOCK) == 1, f"expected 1 V088 exclusion block, got {view.count(OLD_BLOCK)}"
assert view.count("\n), open_actions AS (") == 1
assert view.index(OLD_BLOCK) + len(OLD_BLOCK) == view.index("\n), open_actions AS ("), \
    "the V088 block must end exactly at the candidates CTE boundary"
view = view.replace(OLD_BLOCK, NEW_BLOCK, 1)

# post-conditions: exactly the one exclusion changed, every other arm survives
assert view.count("CHANGE_KIND = 'DESTRUCTIVE' AND (") == 1
assert view.count("WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) AND RISK_SCORE >= 70") == 1
for anchor in ("'IDENTITY' AS DOMAIN", "'PRIVILEGE', 'ALL'", "'TRUST CENTER', 'ALL'",
               "'CHANGE RISK', COMPANY", "FROM candidates c",
               "COALESCE(a.ACTION_STATUS, 'UNTRACKED') AS STATUS"):
    assert view.count(anchor) == 1, anchor
assert "COALESCE(ROLE_NAME, '') IN (" not in view, "V080's fixed role list must not come back"

out = f"""-- V151__security_change_risk_identity_policy_drops.sql
--
-- Stop hiding Terraform-role DROP USER / DROP ROLE / DROP POLICY from the Security
-- exception queue (Next-Fifty rank 8). V088 excluded EVERY CHANGE_KIND=DESTRUCTIVE
-- row by a TF_* role (or on DBA_MAINT_DB.PUBLIC) to stop the truncate-and-reload
-- flood -- but SP_LOAD_SECURITY_FACTS (V105) files DROP_USER / DROP_ROLE / DROP
-- POLICY under DESTRUCTIVE (its DROP test precedes its USER / POLICY tests), so a
-- TF_* role deleting a user, a role or a masking / row-access / network policy never
-- reached the queue or the CHANGE RISK domain score, contrary to the V088 comment.
--
-- Re-derives V_SECURITY_EXCEPTION_QUEUE from V088 (its current definition; V088
-- itself re-derived from V075 and superseded V080), byte-identical except the
-- CHANGE RISK exclusion block: a row whose QUERY_TYPE names a USER / ROLE / POLICY
-- object, or whose whitespace-normalized statement opens with DROP USER, DROP ROLE,
-- DROP DATABASE ROLE, DROP APPLICATION ROLE or DROP (kind) POLICY (17 keyword-
-- anchored openers), is no longer excluded. TF_* table / schema drops, TRUNCATEs
-- and the DBA_MAINT_DB.PUBLIC churn stay excluded, NULL-safe as before. There is
-- deliberately no bare POLICY substring test (a TF_* TRUNCATE of an insurance
-- FACT_POLICY table stays excluded) and no DATABASE_NAME IS NOT NULL gate
-- (DATABASE_NAME is the session database, and NULL-database drops were flood).
--
-- Posture impact (intentional): every surfaced row is CRITICAL (DROP base score 90)
-- and costs the CHANGE RISK domain 25 points, so 1 surfaced row in 7 days reads
-- Watch (75) and 2 read Act (50), which turns the Security page verdict bad.
--
-- Rows were never deleted from FACT_SECURITY_CHANGE, so the fix is retroactive over
-- the 7-day queue window. View-only: no data reload, no new objects, no tail
-- procedure run. Owner applies in Snowsight after V150. This file never runs from
-- the app. Rollback: re-run the V088 view statement (V088:34-108).

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20151, 'V151 requires V150 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 150) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V088; CHANGE RISK exclusion keeps TF_* identity / policy drops)
{view}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 151 AS VERSION,
       'Security change-risk identity/policy drops un-hidden (Next-Fifty rank 8): V_SECURITY_EXCEPTION_QUEUE re-derived from V088 (byte-identical except the CHANGE RISK exclusion) so the TF_* / DBA_MAINT_DB.PUBLIC DESTRUCTIVE exclusion no longer swallows DROP USER / DROP ROLE / DROP DATABASE ROLE / DROP APPLICATION ROLE / DROP (kind) POLICY -- a row is kept when QUERY_TYPE names USER/ROLE/POLICY or the whitespace-normalized statement opens with one of those DROP keywords (keyword-anchored, so a TF_* TRUNCATE of a FACT_POLICY table stays excluded; no DATABASE_NAME IS NOT NULL gate, since NULL-database drops were part of the flood). The V105 classifier files DROP_USER/DROP_ROLE as DESTRUCTIVE (DROP tested before USER), which is why V088 hid them. Rows were always kept in FACT_SECURITY_CHANGE, so the 7-day queue window corrects immediately. View-only, no reload.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 151);
"""

assert out.count("CREATE OR REPLACE VIEW") == 1
assert "CREATE OR REPLACE PROCEDURE" not in out
assert "CREATE TABLE" not in out and "CREATE TASK" not in out and "ALTER TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "DELETE FROM" not in out and "CALL " not in out
assert "EXCEPTION (-20151" in out and "IF (v < 150) THEN" in out
assert "SELECT 151 AS VERSION" in out and "WHERE VERSION = 151)" in out
assert out.count("CHANGE_KIND = 'DESTRUCTIVE' AND (") == 1  # exactly one exclusion, on CHANGE RISK
assert out.count("-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V088;") == 1

target = Path(os.environ.get("V151_OUT")
              or (MIG / "V151__security_change_risk_identity_policy_drops.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
