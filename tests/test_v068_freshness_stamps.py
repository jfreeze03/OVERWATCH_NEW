"""Locks for V068 — standalone-mart freshness stamps (live-screenshot finding).

Two loaders with their own tasks (lock-wait V035, pattern-cost V036/37/47) were missed in
the V041 stamp handoff, so their SOURCE_FRESHNESS_STATE rows froze at apply time — the
Brief card showed MART_LOCK_WAIT_DAILY 448.5h stale while the loader ran fine. Both are
re-derived from their latest defs with the standard V041-R6 freshness MERGE, stamping
LAST_LOAD_TS as a RUN timestamp so zero-event windows read fresh (no-news != no-load).

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V68 = (_MIG / "V068__standalone_mart_freshness_stamps.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v068_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v068.py")],
                       env={**os.environ, "V068_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V68, (
        "V068 drifted from outputs/gen_v068.py — regenerate, never hand-edit.")


def test_v068_guard_version_and_heal():
    assert "EXCEPTION (-20068" in _V68 and "IF (v < 67) THEN" in _V68
    assert "SELECT 68 AS VERSION" in _V68 and "WHERE VERSION = 68)" in _V68
    assert _V68.count("CREATE OR REPLACE PROCEDURE") == 2
    assert "CREATE TABLE" not in _V68 and "CREATE TASK" not in _V68
    # the tail heals both frozen rows immediately (cheap 3-day windows)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_LOCK_WAIT_MART(3);" in _V68
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(3);" in _V68


def test_both_loaders_gain_a_run_stamp():
    for name, source in (("SP_LOAD_LOCK_WAIT_MART", "MART_LOCK_WAIT_DAILY"),
                         ("SP_LOAD_PATTERN_COST", "MART_PATTERN_COST_DAILY")):
        body = _proc(_V68, name)
        assert body.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE") == 1, name
        assert f"'{source}' AS SOURCE_NAME" in body, name
        # RUN stamp — CURRENT_TIMESTAMP(), NOT MAX(LOAD_TS) of the mart: an account with
        # zero lock waits / no repeated patterns must read FRESH, not stale-forever.
        assert "CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS" in body, name
        assert "MAX(LOAD_TS) AS LAST_LOAD_TS" not in body, name   # the row-stamp anti-pattern
        # stamp precedes the single RETURN; the V041-R6 columns are all fed
        assert body.index("SOURCE_FRESHNESS_STATE") < body.index("RETURN 'OK';"), name
        assert "GENERATION = COALESCE(t.GENERATION, 0) + 1" in body, name


def test_v047_qas_credits_survive_the_rederivation():
    # the derived pattern-cost proc must carry V047's Query Acceleration credits through
    assert "CREDITS_USED_QUERY_ACCELERATION" in _proc(_V68, "SP_LOAD_PATTERN_COST")


def test_v068_in_migration_registry():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 68 in _EXPECTED_MIGRATIONS
