"""V080 locks: the Security change-risk queue excludes ETL-engine DROPs.

Re-derives V_SECURITY_EXCEPTION_QUEUE (from V075) with one addition to the
CHANGE RISK arm: DESTRUCTIVE (DROP/TRUNCATE) events by the three ETL-engine role
families (Glue / Informatica / transform SYSADMIN), across all six environments,
are excluded so routine truncate-and-reload stops flooding the queue and zeroing
the domain score. Scoped to CHANGE_KIND='DESTRUCTIVE' only; rows stay in
FACT_SECURITY_CHANGE for audit. View-only, no data reload, no new objects."""

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
_V080 = (_MIG / "V080__security_change_risk_etl_exclusion.sql").read_text(encoding="utf-8")

_ENVS = ("PRD", "MGM", "SAN", "SEA", "DEV", "PHX")
_TEMPLATES = ("TF_SFR_{}_GLUE", "TF_SFR_{}_INFORMATICA", "TF_O_{}_ALFA_SYSADMIN")
_ROLES = tuple(t.format(env) for t in _TEMPLATES for env in _ENVS)

_OLD_WHERE = ("    WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) "
              "AND RISK_SCORE >= 70")
# The exact text V080 appends after the CHANGE RISK WHERE (kept byte-for-byte in
# sync with outputs/gen_v080.py so the reverse-derivation check is exact).
_role_list = ",\n".join(
    "          " + ", ".join(f"'{r}'" for r in _ROLES[i:i + 3])
    for i in range(0, len(_ROLES), 3)
)
_ADDED = (
    "\n"
    "      -- V080: ETL/service-role truncate-and-reload is not a security event.\n"
    "      -- DESTRUCTIVE by these roles is dropped from the queue, but their\n"
    "      -- GRANT/REVOKE/POLICY changes and any other role's DROP still surface.\n"
    "      -- COALESCE keeps it NULL-safe: an unattributed (NULL-role) DROP surfaces.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE' AND COALESCE(ROLE_NAME, '') IN (\n"
    + _role_list + "))"
)


def _view(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.{name} AS.*?;\n",
        text, re.S,
    ).group(0)


def test_v080_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v080.py")],
        env={**os.environ, "V080_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V080


def test_v080_is_one_guarded_view_swap():
    assert "EXCEPTION (-20080" in _V080 and "IF (v < 79) THEN" in _V080
    assert "SELECT 80 AS VERSION" in _V080 and "WHERE VERSION = 80)" in _V080
    assert _V080.count("CREATE OR REPLACE VIEW") == 1
    assert "CREATE OR REPLACE PROCEDURE" not in _V080
    assert "CREATE TABLE" not in _V080 and "CREATE TASK" not in _V080
    assert "CREATE WAREHOUSE" not in _V080 and "RESOURCE MONITOR" not in _V080


def test_v080_excludes_the_18_etl_roles_scoped_to_destructive():
    view = _view(_V080, "V_SECURITY_EXCEPTION_QUEUE")
    # Exactly one exclusion, on the CHANGE RISK arm, scoped to DESTRUCTIVE.
    # NULL-safe: COALESCE keeps an unattributed (NULL-role) DROP surfaced.
    assert view.count("CHANGE_KIND = 'DESTRUCTIVE' AND COALESCE(ROLE_NAME, '') IN (") == 1
    # Guard the NULL-safety: the bare `ROLE_NAME IN (...)` form would let three-
    # valued logic silently drop a DESTRUCTIVE event with a NULL role.
    assert "AND ROLE_NAME IN (" not in view
    assert len(_ROLES) == 18
    for role in _ROLES:
        assert f"'{role}'" in view, role
    # The SNOW_PRI_GFR service roles were deliberately excluded from the list.
    assert "SNOW_PRI_GFR" not in view
    # The other queue arms are untouched — GRANT/REVOKE/POLICY signal still flows.
    for arm in ("'CHANGE RISK'", "'TRUST CENTER'", "'IDENTITY'", "'PRIVILEGE'"):
        assert arm in view


def test_v080_leaves_the_view_otherwise_at_its_v075_base():
    # Removing the appended exclusion block reproduces the V075 view byte-for-byte
    # — the ONLY change is the ETL-role exclusion.
    base_view = _view((_MIG / "V075__security_operating_model.sql").read_text(encoding="utf-8"),
                      "V_SECURITY_EXCEPTION_QUEUE")
    derived = _view(_V080, "V_SECURITY_EXCEPTION_QUEUE")
    assert _ADDED in derived
    assert derived.replace(_ADDED, "") == base_view


def test_v080_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V080):
        sqlglot.parse(statement, dialect="snowflake")
