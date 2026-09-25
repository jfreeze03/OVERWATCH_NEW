"""V149 locks: add SESSION_ID + IS_CLIENT_GENERATED_STATEMENT to OW_QH_EXTRACT.

Foundation for cloud-services driver/application attribution. V149 ALTERs the two columns
onto the single-scan staging copy and re-derives SP_LOAD_QH_EXTRACT so the extract fills them.
DERIVED FROM V094 -- the CURRENT definition (the loader was re-derived across
V041/V042/V055/V056/V062/V094); an older base would silently drop the intervening changes.
Byte-locked to outputs/gen_v149.py; the proc is byte-identical to V094 apart from the two
appended columns.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V149 = (_MIG / "V149__qh_extract_session_client_columns.sql").read_text(encoding="utf-8")
_V094 = (_MIG / "V094__fact_query_hourly_boundary_dedupe.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    m = re.search(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_QH_EXTRACT\(.*?\$\$;\n",
        text, re.S)
    assert m, "SP_LOAD_QH_EXTRACT not found"
    return m.group(0)


def test_v149_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v149.py")],
        env={**os.environ, "V149_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V149, (
        "V149 drifted from its forward-generation — edit outputs/gen_v149.py, "
        "not the .sql, then regenerate."
    )


def test_v149_guarded_and_versioned():
    assert "EXCEPTION (-20149" in _V149 and "IF (v < 148) THEN" in _V149
    assert "SELECT 149 AS VERSION" in _V149 and "WHERE VERSION = 149)" in _V149


def test_v149_adds_the_two_columns_to_the_extract():
    for col, typ in (("SESSION_ID", "NUMBER"), ("IS_CLIENT_GENERATED_STATEMENT", "BOOLEAN")):
        assert (f"ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN IF NOT EXISTS {col} {typ};"
                in _V149), col
    assert _V149.count("ALTER TABLE DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT ADD COLUMN") == 2
    # a single guarded proc re-derivation, no view/task
    assert _V149.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE VIEW" not in _V149 and "CREATE TASK" not in _V149


def test_v149_proc_is_byte_identical_to_v094_apart_from_the_two_columns():
    p149 = _proc(_V149)
    # both columns land once in the INSERT list and once in the SELECT
    assert p149.count("SESSION_ID, IS_CLIENT_GENERATED_STATEMENT") == 2
    # normalize the two appended columns back to V094's form; the proc bodies must then match
    norm = p149.replace(
        "         CREDITS_USED_CLOUD_SERVICES, SESSION_ID, IS_CLIENT_GENERATED_STATEMENT)\n",
        "         CREDITS_USED_CLOUD_SERVICES)\n",
    ).replace(
        "           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0),\n"
        "           SESSION_ID, IS_CLIENT_GENERATED_STATEMENT\n"
        "    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
        "           LEFT(QUERY_TEXT, 200), COALESCE(CREDITS_USED_CLOUD_SERVICES, 0)\n"
        "    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
    )
    assert norm == _proc(_V094), "V149 changed the proc beyond the two appended columns"


def test_v149_reloads_the_extract_immediately():
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);" in _V149


def test_validate_and_docs_track_v149():
    val = _read("snowflake/validate.sql")
    assert "V001..V158 applied" in val and "VERSION BETWEEN 1 AND 158) = 158" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V149__qh_extract_session_client_columns.sql" in _read(rel)


def test_v149_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 149 in _EXPECTED_MIGRATIONS


def test_v149_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V149):
        sqlglot.parse(statement, dialect="snowflake")
