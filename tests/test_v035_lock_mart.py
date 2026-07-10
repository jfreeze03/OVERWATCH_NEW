"""Locks for V035 — the lock-wait mart (v4.22.0, live finding 2026-07-10).

Joe's own Heaviest-queries panel showed the lock-contention live reads
scanning 46-56 GB at 74-259s each — LOCK_WAIT_HISTORY is enormous in this
account. The daily task pays that scan once on a 3-day increment; page
views read the mart. Company derives from the object's database, outside
the aggregation (V030 shape law)."""

from __future__ import annotations

from pathlib import Path

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V035__lock_wait_mart.sql").read_text(encoding="utf-8")


def test_v035_guard_version_and_pieces():
    assert "EXCEPTION (-20035" in _MIG
    assert "RAISE not_ready;" in _MIG                               # declared, then raised


def test_guards_declare_their_exceptions_everywhere():
    """Live failure 2026-07-10 (owner-diagnosed): RAISE only accepts a
    DECLAREd exception name — the inline RAISE EXCEPTION (code, msg) form
    is invalid scripting and the sqlglot gate cannot see $$ bodies."""
    for p in sorted((_ROOT / "snowflake" / "migrations").glob("V0*.sql")):
        assert "RAISE EXCEPTION (" not in p.read_text(encoding="utf-8"), p.name
    assert "IF (v < 34) THEN" in _MIG
    assert "SELECT 35 AS VERSION" in _MIG
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_LOCK_WAIT_MART(45);" in _MIG   # one-time backfill


def test_v035_loader_obeys_the_v030_shape_law():
    body = _MIG.split("SP_LOAD_LOCK_WAIT_MART", 1)[1]
    assert "COMPANY_FOR_DATABASE(g.DATABASE_NAME)" in body          # UDF outside the aggregation
    assert "COMPANY_FOR_DATABASE(MAX(" not in _MIG                  # never the V029 mistake
    assert "-1 * :DAYS_BACK" in body                                # increment is a parameter
    assert "COUNT_IF(ACQUIRED_AT IS NULL) AS NEVER_ACQUIRED" in body  # live semantics preserved
    assert "MERGE INTO" in body                                     # idempotent on the day grain


def test_v035_task_chain_freshness_and_teardown():
    assert "TASK_LOAD_DAILY SUSPEND" in _MIG
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY" in _MIG
    tail = _MIG.split("TASK_LOCK_WAIT_DAILY RESUME", 1)[1]
    assert "TASK_LOAD_MARTS_V27_DAILY RESUME" in tail               # sibling resumes
    assert "TASK_LOAD_DAILY RESUME" in tail
    view = _MIG.split("MART_SOURCE_FRESHNESS AS", 1)[1].split("ALTER TASK", 1)[0]
    assert view.count("UNION ALL") == 17                            # 18th arm
    assert "'MART_LOCK_WAIT_DAILY'" in view
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY;" in teardown
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOCK_WAIT_DAILY;" in teardown


def test_reader_is_qualified_scoped_and_adopted():
    sql = mart27_sql.lock_wait_daily(14, "ALFA")
    assert "SUM(c.WAIT_EVENTS)" in sql and "SUM(WAIT_EVENTS)" not in sql  # alias-shadow rule
    assert "(c.COMPANY = 'ALFA' OR UPPER(c.COMPANY) = 'ALL')" in sql      # triage-filter law
    assert "NEVER_ACQUIRED DESC" in sql                                   # ranking preserved
    assert "''" in mart27_sql.lock_wait_daily(7, "x'y")                   # injection-safe
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "mart27_sql.lock_wait_daily(min(days, 14), company)" in ops
    assert "ops_sql.lock_contention(min(days, 14))" in ops                # live fallback kept
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "mart27_sql.lock_wait_daily" in canary
