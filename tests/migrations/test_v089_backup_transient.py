"""V089 locks: SP_BACKUP_OPERATOR_TABLES clones to a TRANSIENT backup target.

The proc cloned each operator table to a PERMANENT ``*_BAK_LAST`` snapshot, but a
transient source (ALERT_EVENTS, ACTION_QUEUE, ...) cannot be cloned into a
permanent object — so those backups failed every run (owner error log 2026-08-17,
clone_failed x3). V089 re-derives the proc from its V075 base, changing ONLY the
clone target to TRANSIENT. Proc swap; no data reload, no new objects."""

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
_V089 = (_MIG / "V089__backup_transient_clone.sql").read_text(encoding="utf-8")

# The exact text V089 inserts before the clone DDL (kept byte-for-byte in sync
# with outputs/gen_v089.py so the reverse-derivation check is exact).
_ADDED = (
    "            -- V089: TRANSIENT target -- a transient source (ALERT_EVENTS,\n"
    "            -- ACTION_QUEUE, ...) cannot clone into a PERMANENT table\n"
    "            -- (\"Transient object cannot be cloned to a permanent object\"),\n"
    "            -- which failed those backups every run. TRANSIENT works for both\n"
    "            -- transient and permanent sources and needs no Fail-safe.\n"
)


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(\).*?\n\$\$;\n",
        text, re.S,
    ).group(0)


def test_v089_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v089.py")],
        env={**os.environ, "V089_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V089


def test_v089_is_one_guarded_proc_swap():
    assert "EXCEPTION (-20089" in _V089 and "IF (v < 88) THEN" in _V089
    assert "SELECT 89 AS VERSION" in _V089 and "WHERE VERSION = 89)" in _V089
    assert _V089.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TASK" not in _V089 and "CREATE WAREHOUSE" not in _V089
    assert "RESOURCE MONITOR" not in _V089


def test_v089_clone_target_is_transient():
    proc = _proc(_V089, "SP_BACKUP_OPERATOR_TABLES")
    # the TRANSIENT clone is present, the old permanent form is gone.
    assert "CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname" in proc
    assert "CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.' || :tname" not in proc
    # the full operator-table list + error-capture arm survive unchanged.
    assert "'ALERT_EVENTS'" in proc and "'ACTION_QUEUE'" in proc
    assert "'clone_failed'" in proc and "RETURN 'cloned ' || :done" in proc


def test_v089_leaves_the_proc_otherwise_at_its_v075_base():
    # Removing the inserted comment reproduces the V075 proc byte-for-byte, except
    # the one word TRANSIENT — the ONLY functional change vs the base.
    base = _proc((_MIG / "V075__security_operating_model.sql").read_text(encoding="utf-8"),
                 "SP_BACKUP_OPERATOR_TABLES")
    derived = _proc(_V089, "SP_BACKUP_OPERATOR_TABLES")
    assert _ADDED in derived
    normalized = derived.replace(_ADDED, "").replace(
        "CREATE OR REPLACE TRANSIENT TABLE", "CREATE OR REPLACE TABLE")
    assert normalized == base


def test_v089_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V089):
        sqlglot.parse(statement, dialect="snowflake")
