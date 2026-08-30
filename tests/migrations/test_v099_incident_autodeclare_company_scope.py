"""V099 locks: SP_INCIDENT_AUTODECLARE family-open guard scoped by company.

The proc groups per (FAMILY, COMPANY) but its family-already-open guard correlated only on
the family, so a CRITICAL for one company was silently not auto-declared when the other
company had an open incident of the same (company-shared) rule family. V099 re-derives the
proc from V098 with `AND i.COMPANY = c.COMPANY` added to the guard. Byte-locked to
outputs/gen_v099.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V099 = (_MIG / "V099__incident_autodeclare_company_scope.sql").read_text(encoding="utf-8")
_V098 = (_MIG / "V098__incident_autodeclare_relink_guard.sql").read_text(encoding="utf-8")


def test_v099_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v099.py")],
        env={**os.environ, "V099_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V099, (
        "V099 drifted from its forward-generation — edit outputs/gen_v099.py, "
        "not the .sql, then regenerate."
    )


def test_v099_is_one_guarded_proc_redefinition():
    assert "EXCEPTION (-20099" in _V099 and "IF (v < 98) THEN" in _V099
    assert "SELECT 99 AS VERSION" in _V099 and "WHERE VERSION = 99)" in _V099
    assert _V099.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE" in _V099
    assert "CREATE TABLE" not in _V099 and "ALTER TABLE" not in _V099
    assert "CREATE OR REPLACE VIEW" not in _V099 and "CREATE TASK" not in _V099


def test_v099_family_guard_is_scoped_by_company():
    # the fix: the family-already-open guard now correlates on company as well as family
    assert ("          AND i.STATUS IN ('OPEN', 'MITIGATED')\n"
            "          AND i.COMPANY = c.COMPANY\n"
            "          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY") in _V099
    # V098 (base) has the guard WITHOUT the company correlation, confirming V099 supersedes it
    assert "i.COMPANY = c.COMPANY" not in _V098
    assert ("          AND i.STATUS IN ('OPEN', 'MITIGATED')\n"
            "          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY") in _V098
    # untouched anchors: both anti-membership guards survive (crit CTE m + member INSERT m2)
    assert "m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID" in _V099
    assert "m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID" in _V099


def test_v099_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V099):
        sqlglot.parse(statement, dialect="snowflake")
