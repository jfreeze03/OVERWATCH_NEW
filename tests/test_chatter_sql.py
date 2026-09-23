"""Cloud-services chatter-by-application builders (app/data/chatter_sql.py) — Phase 1b.

Live SESSIONS x QUERY_HISTORY attribution of metadata/compile chatter to the client
application/driver. SQL-shape + parse + the reachable-table contract (only QUERY_HISTORY +
SESSIONS, so operations.py's reachable set gains only SESSIONS).
"""

from __future__ import annotations

import datetime as _dt
import re

import pandas as pd
import pytest

from app.data import chatter_sql
from app.logic import cs_driver

sqlglot = pytest.importorskip("sqlglot")


def _tables(sql: str) -> set[str]:
    return set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", sql))


def test_by_application_shape_and_parse():
    sql = chatter_sql.chatter_by_application()
    sqlglot.parse_one(sql, dialect="snowflake")
    for tok in (
        "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q",
        "FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS",
        "CLIENT_ENVIRONMENT",                       # the _APP_EXPR application identifier
        "IS_CLIENT_GENERATED_STATEMENT",            # the live-only client-gen signal
        "q.WAREHOUSE_NAME IS NULL",                 # the chatter predicate (metadata-only)
        "NOT LIKE 'OVERWATCH%'",                    # self-noise exclusion
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY SESSION_ID",  # SESSIONS dedup
        "GROUP BY 1", "HAVING COUNT(*) >= 20", "LIMIT 100",
    ):
        assert tok in sql, tok
    # CS credits are surfaced as gross usage (no rate math in SQL)
    assert "CREDITS_USED_CLOUD_SERVICES" in sql and "3.68" not in sql


def test_scope_filters_are_applied_and_escaped():
    sql = chatter_sql.chatter_by_application(30, warehouse_contains="WH_ALFA", user_contains="SVC")
    assert "q.WAREHOUSE_NAME ILIKE '%WH_ALFA%'" in sql
    assert "q.USER_NAME ILIKE '%SVC%'" in sql
    # an apostrophe in the filter is escaped, not interpolated raw
    assert "''" in chatter_sql.chatter_by_application(30, user_contains="o'brien")


def test_bounds_window_and_padded_sessions():
    bounds = (_dt.date(2026, 8, 1), _dt.date(2026, 9, 1))
    sql = chatter_sql.chatter_by_application(30, bounds=bounds)
    assert "q.START_TIME >= '2026-08-01' AND q.START_TIME < '2026-09-01'" in sql
    # SESSIONS scanned 7d wider so an in-window query keeps its application
    assert "CREATED_ON >= DATEADD('day', -7, '2026-08-01')" in sql


def test_families_for_application_shape_and_feeds_cs_driver():
    sql = chatter_sql.chatter_families_for_application("DBeaver")
    sqlglot.parse_one(sql, dialect="snowflake")
    assert "COALESCE(s.APPLICATION, '(unknown)') = 'DBeaver'" in sql
    assert "q.QUERY_PARAMETERIZED_HASH IS NOT NULL" in sql
    # output columns are exactly what cs_driver.classify_families reads
    for col in ("SAMPLE_TEXT", "QUERY_TYPE", "RUNS", "AVG_COMPILE_S", "AVG_TOTAL_S", "COMPILE_PCT"):
        assert col in sql, col


def test_application_literal_is_escaped():
    sql = chatter_sql.chatter_families_for_application("Ma'am Tool")
    assert "'Ma''am Tool'" in sql  # sql_literal doubles the quote


def test_both_builders_reach_only_query_history_and_sessions():
    for sql in (chatter_sql.chatter_by_application(),
                chatter_sql.chatter_families_for_application("x")):
        assert _tables(sql) == {"QUERY_HISTORY", "SESSIONS"}


def test_family_builder_columns_classify():
    # a frame with the family builder's output columns runs cleanly through the Phase-0 classifier
    df = pd.DataFrame([
        {"QUERY_PARAMETERIZED_HASH": "h1", "SAMPLE_TEXT": "show /* JDBC:DatabaseMetaData.getTables() */",
         "QUERY_TYPE": "SHOW", "RUNS": 40, "AVG_COMPILE_S": 0.5, "AVG_TOTAL_S": 0.6,
         "COMPILE_PCT": 79.0, "CS_CREDITS": 0.01},
    ])
    out = cs_driver.classify_families(df)
    assert out.iloc[0]["DRIVER_CLASS"] == cs_driver.JDBC_ODBC_DISCOVERY
    assert out.iloc[0]["RESIZE_VERDICT"] == cs_driver.RESIZE_NOT_INDICATED
