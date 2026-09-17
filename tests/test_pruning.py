"""Locks for the per-table at-rest pruning advisor (TABLE_PRUNING_HISTORY).

Builder ops_sql.table_pruning_candidates + logic insights.poor_pruning_summary — the
"which TABLES to cluster" complement of the per-query poor_pruning_queries panel.
"""

from __future__ import annotations

import pandas as pd

from app.data import ops_sql
from app.logic.insights import poor_pruning_summary


def test_builder_reads_table_pruning_history_with_the_real_columns():
    sql = ops_sql.table_pruning_candidates(30)
    assert "SNOWFLAKE.ACCOUNT_USAGE.TABLE_PRUNING_HISTORY" in sql
    # the confirmed time column is START_TIME — NOT the sibling view's INTERVAL_START_TIME
    assert "START_TIME" in sql and "INTERVAL_START_TIME" not in sql
    # efficiency = pruned / (scanned + pruned); there is NO PARTITIONS_TOTAL column
    assert "PARTITIONS_PRUNED" in sql and "PARTITIONS_SCANNED" in sql
    assert "PARTITIONS_TOTAL" not in sql
    assert "GREATEST(" in sql                       # divide-by-zero guard on the denominator
    assert "AS PRUNE_PCT" in sql
    # worst pruning first + TWO floors: activity (>=1000 considered) AND size
    # (>=100 partitions/scan) so a small full-scanned table isn't a false candidate
    assert "ORDER BY PRUNE_PCT ASC" in sql
    assert ">= 1000" in sql
    assert "/ GREATEST(SUM(NUM_SCANS), 1) >= 100" in sql
    assert "TABLE_FQN" in sql


def test_builder_is_company_scoped_like_the_rest_of_the_tab():
    # ALL = no company predicate; a real company narrows by database (matches the tab)
    assert ops_sql.table_pruning_candidates(30, "ALL") != ops_sql.table_pruning_candidates(30, "ALFA")
    assert "DATABASE_NAME" in ops_sql.table_pruning_candidates(30, "ALFA")


def test_builder_database_and_schema_filters_are_injection_safe():
    sql = ops_sql.table_pruning_candidates(7, database="my_db", schema_contains="stag")
    assert "UPPER(DATABASE_NAME) = 'MY_DB'" in sql   # sql_literal + upper-cased
    assert "SCHEMA_NAME" in sql and "stag" in sql.lower()
    # a quote in the filter must be escaped, never break out of the literal
    # (the database value is upper-cased before it is quoted)
    hostile = ops_sql.table_pruning_candidates(7, database="x' OR '1'='1")
    assert "'X'' OR ''1''=''1'" in hostile


def _cand():
    return pd.DataFrame([
        {"TABLE_FQN": "DB.SC.BIG", "NUM_SCANS": 500, "PARTITIONS_SCANNED": 9000,
         "PARTITIONS_PRUNED": 100, "PRUNE_PCT": 1.1},        # worst
        {"TABLE_FQN": "DB.SC.MED", "NUM_SCANS": 80, "PARTITIONS_SCANNED": 2000,
         "PARTITIONS_PRUNED": 1500, "PRUNE_PCT": 42.9},
    ])


def test_summary_names_the_worst_pruned_table():
    s = poor_pruning_summary(_cand())
    assert s["n_candidates"] == 2
    assert s["worst_table"] == "DB.SC.BIG"
    assert s["worst_prune_pct"] == 1.1
    assert s["total_partitions_scanned"] == 11000.0


def test_summary_picks_worst_even_if_frame_unsorted():
    s = poor_pruning_summary(_cand().iloc[::-1].reset_index(drop=True))
    assert s["worst_table"] == "DB.SC.BIG" and s["worst_prune_pct"] == 1.1


def test_summary_empty_and_missing_columns():
    z = poor_pruning_summary(pd.DataFrame())
    assert z["n_candidates"] == 0 and z["worst_prune_pct"] is None
    assert poor_pruning_summary(None)["n_candidates"] == 0
    assert poor_pruning_summary(pd.DataFrame([{"X": 1}]))["n_candidates"] == 0
