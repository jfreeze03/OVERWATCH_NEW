"""Locks for V049 write-target attribution (r28+ queue, owner go 2026-07-15).

Write-only ETL (COPY INTO, INSERT..VALUES, CTAS from constants) reads no base
table, so V048 parked its credits in QUERY_COMPUTE_RESIDUAL. V049 folds
ACCESS_HISTORY.OBJECTS_MODIFIED into the equal split so loads attribute to the
tables they build; the residual shrinks to no-read-no-write compute.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_V49 = (_ROOT / "snowflake" / "migrations" / "V049__write_target_attribution.sql").read_text(encoding="utf-8")


def test_v049_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v049.py")],
                       env={**os.environ, "V049_OUT": str(out)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V49, (
        "V049 drifted from its forward-generation — edit outputs/gen_v049.py, "
        "regenerate, never hand-edit the migration.")


def test_v049_guard_version_house_rules():
    assert "EXCEPTION (-20049" in _V49 and "RAISE not_ready;" in _V49
    assert "IF (v < 48) THEN" in _V49 and "SELECT 49 AS VERSION" in _V49


def test_v049_is_a_proc_swap_only():
    # No new objects: the ledger table and daily task are V048's. A stray
    # CREATE TABLE/TASK here would need teardown coverage and a backfill story.
    assert _V49.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TABLE" not in _V49 and "CREATE TRANSIENT TABLE" not in _V49
    assert "CREATE TASK" not in _V49
    # ...and the working window is re-attributed under the new split.
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);" in _V49


def test_v049_split_and_residual_agree_on_writes():
    """OBJECTS_MODIFIED must appear in BOTH CTEs — the split (dedup) and the
    residual guard (obj_q). In only the first, write-only credits double-count
    (split AND residual); in only the second, they vanish entirely. Additivity
    is the ledger's whole contract."""
    assert _V49.count("LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED)") == 2
    assert _V49.count("LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED)") == 2
    # the split dedupes ACROSS the read/write union: one share per object even
    # when one query both reads and writes it
    assert "SELECT DISTINCT QUERY_ID, OBJECT_FQN, OBJECT_DOMAIN" in _V49
    # both arms keep the domain gate — writes to stages/pipes are not the
    # table-grain ledger's business
    assert _V49.count("IN ('Table', 'Materialized view')") == 4


def test_v049_keeps_v048_invariants():
    # The derivation must not regress the two V048 lessons:
    # residual carries UNKNOWN (not NULL — additivity of per-company sums)...
    assert "'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)" in _V49
    assert "'QUERY_COMPUTE_RESIDUAL', NULL" not in _V49
    # ...and QAS stays inside measured compute.
    assert "CREDITS_USED_QUERY_ACCELERATION" in _V49
    # equal split unchanged: credits divided by the object count
    assert "qa.CREDITS / c.N" in _V49


def test_v049_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V49):
        sqlglot.parse(stmt, dialect="snowflake")
