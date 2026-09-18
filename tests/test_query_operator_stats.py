"""Locks for the QOIE Slice 2 operator-profile readers (ops_sql).

operator_stats_summary / operator_problem_board / operator_anatomy read the V143 COLLECTOR
MART (FACT_QUERY_OPERATOR_STATS_DAILY), NOT a live ACCOUNT_USAGE scan — operator stats have no
bulk ACCOUNT_USAGE view. These locks pin the load-bearing "mart-first, no live scan" contract
(which is what keeps operations.py's ACCOUNT_USAGE perf budget and its test_v451_trust reachable
pin unchanged), the COMPANY scope axis, the two pathology definitions, and the anatomy drill.
"""

from __future__ import annotations

import pytest

from app.data import ops_sql


def test_operator_readers_are_mart_first_no_account_usage():
    """The whole point of Slice 2's reader: it reads the pre-collected fact, never
    SNOWFLAKE.ACCOUNT_USAGE. If any of these grew a live ACCOUNT_USAGE scan, operations.py's
    perf budget (42) and its test_v451_trust reachable-table pin would BOTH have to change —
    this lock fails first so that can't happen silently."""
    sqls = [
        ops_sql.operator_stats_summary(7),
        ops_sql.operator_problem_board(7),
        ops_sql.operator_anatomy("01ab-2cd"),
    ]
    for sql in sqls:
        assert "DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY" in sql
        assert "ACCOUNT_USAGE" not in sql, "Slice-2 reader must stay mart-first (no live scan)"


def test_summary_emits_the_kpi_columns():
    sql = ops_sql.operator_stats_summary(30)
    for col in ("QUERIES_PROFILED", "EXPLODING_JOIN_OPS", "SPILL_OPS",
                "MAX_ROW_MULTIPLE", "TOTAL_REMOTE_SPILL_GB", "NEWEST_QUERY_DAY"):
        assert f"AS {col}" in sql, col


def test_company_scope_is_live_and_quoted():
    """A named company narrows to its COMPANY partition (a plain equality on the pre-stamped
    column); ALL is account-wide (no COMPANY predicate). Mirrors the mart-scope convention."""
    all_sql = ops_sql.operator_problem_board(7, "ALL")
    alfa_sql = ops_sql.operator_problem_board(7, "ALFA")
    assert "COMPANY =" not in all_sql
    assert "COMPANY = 'ALFA'" in alfa_sql
    assert all_sql != alfa_sql


def test_board_tags_both_pathologies_with_the_exploding_floor():
    sql = ops_sql.operator_problem_board(7)
    assert "'Exploding join'" in sql and "'Memory spill'" in sql
    # exploding join needs BOTH a large multiple AND a materially large result, so a 10x on a
    # handful of rows is not flagged
    assert "ROW_MULTIPLE >= 10" in sql
    assert "OUTPUT_ROWS, 0) >= 1000000" in sql
    assert "REMOTE_SPILL_GB, 0) > 0" in sql
    # top-N PER pathology (not top-N overall — else spill could crowd out every join)
    assert "PARTITION BY PATHOLOGY" in sql and "ROW_NUMBER() OVER" in sql
    # cross-link key to Slice 1 is carried
    assert "QUERY_PARAMETERIZED_HASH AS FINGERPRINT" in sql


def test_board_limit_is_clamped():
    assert "<= 50" in ops_sql.operator_problem_board(7)              # default
    assert "<= 200" in ops_sql.operator_problem_board(7, limit=9999)  # clamped up-bound
    assert "<= 1" in ops_sql.operator_problem_board(7, limit=0)       # clamped low-bound


def test_anatomy_is_per_query_ordered_by_time_share():
    sql = ops_sql.operator_anatomy("01b2c3d4-dead-beef")
    assert "WHERE QUERY_ID = '01b2c3d4-dead-beef'" in sql            # sql_literal-quoted id
    # TIME_SHARE_PCT = a SCALE-INVARIANT per-operator share (raw OP_TIME_PCT / SUM over the
    # query x 100) so the profile reads right whether the collector stored 0-1 or 0-100.
    assert "SUM(OP_TIME_PCT) OVER ()" in sql and "AS TIME_SHARE_PCT" in sql
    assert "NULLIF(SUM(OP_TIME_PCT) OVER (), 0)" in sql              # all-zero/null -> NULL, not fake 0
    assert "ORDER BY TIME_SHARE_PCT DESC" in sql
    assert "PARENT_OPERATOR_ID" in sql                               # the tree edge


def test_anatomy_quotes_a_hostile_query_id():
    # query_id comes from a collected board row (a UUID), but the builder still quotes it —
    # a stray apostrophe must stay inside the literal, never break out of the WHERE.
    sql = ops_sql.operator_anatomy("x' OR '1'='1")
    assert "x'' OR ''1''=''1" in sql


def test_slice2_builders_parse_under_snowflake_dialect():
    sqlglot = pytest.importorskip("sqlglot")
    for sql in (ops_sql.operator_stats_summary(7),
                ops_sql.operator_problem_board(7, "ALFA"),
                ops_sql.operator_anatomy("abc-123")):
        sqlglot.parse_one(sql, dialect="snowflake")
