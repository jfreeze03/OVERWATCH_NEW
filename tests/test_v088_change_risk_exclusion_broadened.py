"""V088 locks: the Security change-risk queue exclusion is broadened to a role
PATTERN plus the app's own database.

V080 excluded DESTRUCTIVE (DROP/TRUNCATE) events from a fixed 18-role list; the
flood persisted (owner 2026-08-17) because the driving roles weren't in it. V088
re-derives V_SECURITY_EXCEPTION_QUEUE (from the V075 base) matching the Terraform
service-role convention (TF_*) instead, and also drops OVERWATCH's own
DBA_MAINT_DB churn. Still CHANGE_KIND='DESTRUCTIVE' only and NULL-safe; rows stay
in FACT_SECURITY_CHANGE for audit. View-only, no data reload, no new objects."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V088 = (_MIG / "V088__security_change_risk_etl_exclusion_broadened.sql").read_text(encoding="utf-8")

# The exact text V088 appends after the CHANGE RISK WHERE (kept byte-for-byte in
# sync with outputs/gen_v088.py so the reverse-derivation check is exact).
_ADDED = (
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


def _view(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.{name} AS.*?;\n",
        text, re.S,
    ).group(0)


def test_v088_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v088.py")],
        env={**os.environ, "V088_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V088


def test_v088_is_one_guarded_view_swap():
    assert "EXCEPTION (-20088" in _V088 and "IF (v < 87) THEN" in _V088
    assert "SELECT 88 AS VERSION" in _V088 and "WHERE VERSION = 88)" in _V088
    assert _V088.count("CREATE OR REPLACE VIEW") == 1
    assert "CREATE OR REPLACE PROCEDURE" not in _V088
    assert "CREATE TABLE" not in _V088 and "CREATE TASK" not in _V088
    assert "CREATE WAREHOUSE" not in _V088 and "RESOURCE MONITOR" not in _V088


def test_v088_excludes_tf_pattern_and_app_db_scoped_to_destructive():
    view = _view(_V088, "V_SECURITY_EXCEPTION_QUEUE")
    # Exactly one exclusion, on the CHANGE RISK arm, scoped to DESTRUCTIVE.
    assert view.count("CHANGE_KIND = 'DESTRUCTIVE' AND (") == 1
    # Pattern-based, not the old fixed list: TF_* (literal underscore via ~ escape).
    # The diagnostic ground-truthed 94% of DESTRUCTIVE events to TF_* roles.
    assert "LIKE 'TF~_%' ESCAPE '~'" in view
    # The app-DB carve-out is narrowed to DBA_MAINT_DB.PUBLIC so a DROP/TRUNCATE of
    # OVERWATCH's own OVERWATCH-schema audit tables stays VISIBLE (reviewer fix).
    assert "UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'" in view
    assert "AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC')))" in view
    # NULL-safe: COALESCE keeps an unattributed (NULL-role) non-app DROP surfaced;
    # the bare `ROLE_NAME LIKE ...` form would go NULL and silently drop it.
    assert "AND ROLE_NAME LIKE" not in view
    # The V080 fixed-list form is gone (superseded), and no stray role literals remain.
    assert "COALESCE(ROLE_NAME, '') IN (" not in view
    assert "TF_SFR_PRD_GLUE" not in view
    # The other queue arms are untouched — GRANT/REVOKE/POLICY signal still flows.
    for arm in ("'CHANGE RISK'", "'TRUST CENTER'", "'IDENTITY'", "'PRIVILEGE'"):
        assert arm in view


def test_v088_leaves_the_view_otherwise_at_its_v075_base():
    # Removing the appended exclusion block reproduces the V075 view byte-for-byte
    # — the ONLY change vs the base is the broadened CHANGE RISK exclusion.
    base_view = _view((_MIG / "V075__security_operating_model.sql").read_text(encoding="utf-8"),
                      "V_SECURITY_EXCEPTION_QUEUE")
    derived = _view(_V088, "V_SECURITY_EXCEPTION_QUEUE")
    assert _ADDED in derived
    assert derived.replace(_ADDED, "") == base_view


def test_v088_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V088):
        sqlglot.parse(statement, dialect="snowflake")
