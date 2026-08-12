"""Locks for V077 cost-by-application ledger (SESSIONS x QUERY_HISTORY x
QUERY_ATTRIBUTION_HISTORY -> FACT_APP_COST_DAILY)."""
from pathlib import Path

import pytest

from app.data import app_cost_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_V77 = (_ROOT / "snowflake" / "migrations" / "V077__app_cost_ledger.sql").read_text(encoding="utf-8")


def test_v077_guard_version_house_rules():
    assert "EXCEPTION (-20077" in _V77 and "RAISE not_ready;" in _V77 and "RAISE EXCEPTION (" not in _V77
    assert "IF (v < 76) THEN" in _V77 and "SELECT 77 AS VERSION" in _V77


def test_v077_fact_proc_task_and_sources():
    assert "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY" in _V77
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST" in _V77
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_APP_COST" in _V77
    for src in ("QUERY_ATTRIBUTION_HISTORY", "QUERY_HISTORY", "SESSIONS"):
        assert src in _V77, src
    assert "CREDITS_USED_QUERY_ACCELERATION" in _V77          # QAS in measured compute
    assert "COMPANY_FOR_WAREHOUSE" in _V77                    # V030 shape law on a plain column
    # self-trimming retention (stays out of the central SP_PURGE_FACTS -> no gen script)
    assert "DATEADD('day', -400, CURRENT_DATE())" in _V77
    # one row per session so the join can't fan out
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID" in _V77


def test_v077_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V77):
        sqlglot.parse(stmt, dialect="snowflake")


def test_app_cost_mart_reader_hits_the_fact_not_account_usage():
    mart = app_cost_sql.app_cost_mart(30, "ALFA")
    assert "FACT_APP_COST_DAILY" in mart
    assert "ACCOUNT_USAGE" not in mart          # the fast path never touches ACCOUNT_USAGE
    sqlglot.parse(mart, dialect="snowflake")


def test_app_cost_live_reader_joins_all_three_and_is_canary_clean():
    live = app_cost_sql.app_cost_live(30, "ALFA")
    for src in ("QUERY_ATTRIBUTION_HISTORY", "QUERY_HISTORY", "SESSIONS"):
        assert src in live, src
    # GET_PATH (not the ':' path variant) keeps it parse-clean for the canary gate.
    assert "GET_PATH(TRY_PARSE_JSON(CLIENT_ENVIRONMENT), 'APPLICATION')" in live
    assert "CREDITS_ATTRIBUTED_COMPUTE" in live and "CREDITS_USED_QUERY_ACCELERATION" in live
    sqlglot.parse(live, dialect="snowflake")


def test_v077_teardown_covered():
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    for obj in ("FACT_APP_COST_DAILY", "SP_LOAD_APP_COST", "TASK_LOAD_APP_COST"):
        assert obj in td, obj
