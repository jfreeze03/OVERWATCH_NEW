"""Lock for V047: Query Acceleration in the pattern-cost mart (Codex item 4)."""
import re
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_V47 = (_ROOT/"snowflake"/"migrations"/"V047__pattern_cost_qas.sql").read_text(encoding="utf-8")


def test_v047_guard_and_version():
    assert "EXCEPTION (-20047" in _V47 and "RAISE not_ready;" in _V47 and "RAISE EXCEPTION (" not in _V47
    assert "IF (v < 46) THEN" in _V47 and "SELECT 47 AS VERSION" in _V47


def test_v047_adds_query_acceleration():
    assert "CREDITS_ATTRIBUTED_COMPUTE + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)" in _V47
    assert "SP_LOAD_PATTERN_COST" in _V47 and "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(90)" in _V47


def test_v047_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V47):
        sqlglot.parse(stmt, dialect="snowflake")


def test_validate_gate_at_v047():
    val=(_ROOT/"snowflake"/"validate.sql").read_text(encoding="utf-8")
    m=re.search(r"V001\.\.V(\d+) applied", val)
    assert m and int(m.group(1)) >= 47


def test_v047_preserves_v037_grain_not_v036():
    # V047 must be re-derived from the CURRENT schema (V037: DATABASE_NAME grain,
    # USERS_HLL sketch), not the pre-V037 V036 body (USERS NUMBER, no DATABASE_NAME).
    # Regressing the grain makes the MERGE reference a dropped USERS column.
    assert "m.DATABASE_NAME" in _V47 and "t.DATABASE_NAME = s.DATABASE_NAME" in _V47
    assert "HLL_ACCUMULATE(q.USER_NAME)" in _V47 and "HLL_COMBINE(m.USERS_HLL)" in _V47
    assert "t.USERS_HLL = s.USERS_HLL" in _V47
    assert "AS USERS\n" not in _V47 and "t.USERS =" not in _V47   # the V036 column is gone
