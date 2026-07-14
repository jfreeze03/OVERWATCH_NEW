"""Locks for V048 object-cost ledger (architectural Phase 2, 2026-07-14)."""
from pathlib import Path
import pytest
from app.data import cost_sql
sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_V48 = (_ROOT / "snowflake" / "migrations" / "V048__object_cost_ledger.sql").read_text(encoding="utf-8")


def test_v048_guard_version_house_rules():
    assert "EXCEPTION (-20048" in _V48 and "RAISE not_ready;" in _V48 and "RAISE EXCEPTION (" not in _V48
    assert "IF (v < 47) THEN" in _V48 and "SELECT 48 AS VERSION" in _V48


def test_v048_fact_proc_task_and_arms():
    assert "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY" in _V48
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST" in _V48
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_OBJECT_COST" in _V48
    for arm in ("'CLUSTERING'", "'MV_REFRESH'", "'SERVERLESS_TASK'", "'SNOWPIPE'",
                "'SEARCH_OPT'", "'QUERY_COMPUTE'", "'QUERY_COMPUTE_RESIDUAL'"):
        assert arm in _V48, arm
    # additive split: query credits divided by object count, from ACCESS_HISTORY
    assert "ACCESS_HISTORY" in _V48 and "LATERAL FLATTEN" in _V48
    assert "qa.CREDITS / c.N" in _V48
    assert "CREDITS_USED_QUERY_ACCELERATION" in _V48   # QAS included in measured compute


def test_v048_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V48):
        sqlglot.parse(stmt, dialect="snowflake")


def test_object_cost_readers_parse_and_shape():
    for sql in (cost_sql.object_cost_by_arm(30, "ALFA"), cost_sql.object_cost_top(30, "ALFA", 10)):
        assert "FACT_OBJECT_COST_DAILY" in sql
        sqlglot.parse(sql, dialect="snowflake")
    top = cost_sql.object_cost_top(30, "ALFA")
    assert "QUERY_CREDITS" in top and "MAINTENANCE_CREDITS" in top


def test_object_cost_metrics_registered():
    from app.logic import metric_registry as mr
    keys = {m.key for m in mr.METRICS}
    assert "object_query_cost" in keys and "object_maintenance_cost" in keys


def test_v048_object_fqn_is_null_safe():
    # OBJECT_FQN is NOT NULL and Snowflake '||' yields NULL if ANY operand is
    # NULL. Every direct-arm FQN must COALESCE its name parts, else one NULL
    # component aborts the whole load (regression: SP_LOAD_OBJECT_COST NULL FQN).
    assert "DATABASE_NAME || '.' || SCHEMA_NAME" not in _V48
    assert "COALESCE(DATABASE_NAME, 'UNKNOWN') || '.' || COALESCE(SCHEMA_NAME, 'UNKNOWN')" in _V48
    assert "COALESCE(TABLE_NAME, 'UNKNOWN')" in _V48 and "COALESCE(TASK_NAME, 'UNKNOWN')" in _V48
    assert "COALESCE(PIPE_NAME, 'UNKNOWN_PIPE')" in _V48
