"""Perf: Operations Queries live path serves the window summary + failure taxonomy from ONE
QUERY_HISTORY scan (ops_sql.queries_health_bundle via GROUPING SETS), instead of three scans
of the same window (summary + top-N + failures). top-N stays its own scan (row-level grain
can't share the GROUP BY). See ops_sql.queries_health_bundle / operations._split_queries_health.
(perf audit 2026-09-02)
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest
import sqlglot

from app.data import ops_sql


def _named(sql: str) -> set[str]:
    return {c for c in sqlglot.parse_one(sql, read="snowflake").named_selects if c != "*"}


def test_bundle_is_one_scan_grouping_sets_and_parses():
    for comp in ("ALFA", "Trexis", "UNKNOWN", "ALL"):
        sql = ops_sql.queries_health_bundle(30, comp, schema_contains="PUBLIC")
        sqlglot.parse_one(sql, read="snowflake")
        # exactly ONE QUERY_HISTORY reference — the CTE is referenced once, so it scans once.
        assert sql.count("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY") == 1
        assert "GROUPING SETS ((), (FAIL_CODE, FAIL_MSG))" in sql
        assert "GROUPING_ID(FAIL_CODE, FAIL_MSG) AS GRP" in sql
    # clamps to the live horizon and honors the bounded 'Last month' window
    assert "-90," in ops_sql.queries_health_bundle(9999, "ALFA")
    sqlglot.parse_one(
        ops_sql.queries_health_bundle(30, "ALFA", bounds=(datetime.date(2026, 8, 1), datetime.date(2026, 9, 1))),
        read="snowflake")


def test_bundle_scopes_identically_to_the_three_builders():
    # same _query_scope => same company axis (COMPANY_FOR_WAREHOUSE, C10) + same filters
    b = ops_sql.queries_health_bundle(30, "Trexis", warehouse_contains="LOAD", schema_contains="PUBLIC")
    assert "COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME) = 'Trexis'" in b
    assert "WAREHOUSE_NAME ILIKE '%LOAD%'" in b and "SCHEMA_NAME ILIKE '%PUBLIC%'" in b


def test_split_reconstructs_the_two_panel_shapes():
    df = pd.DataFrame([
        {"GRP": 3, "ERROR_CODE": None, "ERROR_MESSAGE": None, "QUERY_COUNT": 100, "FAILED_COUNT": 7,
         "P95_ELAPSED_SEC": 12.5, "QUEUED_SEC": 3.0, "SPILL_REMOTE_GB": 0.2,
         "FAILURES": 7, "USERS_AFFECTED": 3, "LAST_SEEN": "2026-08-15"},
        {"GRP": 0, "ERROR_CODE": None, "ERROR_MESSAGE": None, "QUERY_COUNT": 93, "FAILED_COUNT": 0,
         "P95_ELAPSED_SEC": 1, "QUEUED_SEC": 0, "SPILL_REMOTE_GB": 0,
         "FAILURES": 0, "USERS_AFFECTED": 0, "LAST_SEEN": None},                     # success group -> dropped
        {"GRP": 0, "ERROR_CODE": "000630", "ERROR_MESSAGE": "canceled", "QUERY_COUNT": 5, "FAILED_COUNT": 5,
         "P95_ELAPSED_SEC": 1, "QUEUED_SEC": 0, "SPILL_REMOTE_GB": 0,
         "FAILURES": 5, "USERS_AFFECTED": 2, "LAST_SEEN": "2026-08-14"},
        {"GRP": 0, "ERROR_CODE": "000904", "ERROR_MESSAGE": "invalid identifier", "QUERY_COUNT": 2, "FAILED_COUNT": 2,
         "P95_ELAPSED_SEC": 1, "QUEUED_SEC": 0, "SPILL_REMOTE_GB": 0,
         "FAILURES": 2, "USERS_AFFECTED": 1, "LAST_SEEN": "2026-08-13"},
    ])
    summary, fails = ops_sql.split_health_bundle(df)
    assert list(summary.columns) == ops_sql.QUERY_SUMMARY_COLS and len(summary) == 1
    assert int(summary.iloc[0]["QUERY_COUNT"]) == 100 and int(summary.iloc[0]["FAILED_COUNT"]) == 7
    assert list(fails.columns) == ops_sql.QUERY_FAILS_COLS
    assert len(fails) == 2                                        # the success (FAILURES=0) group is dropped
    assert list(fails["FAILURES"]) == [5, 2]                      # sorted worst-first


def test_split_is_empty_safe():
    summary, fails = ops_sql.split_health_bundle(pd.DataFrame())
    assert len(summary) == 1 and int(summary.iloc[0]["QUERY_COUNT"]) == 0   # renders "Queries: 0", never crashes
    assert fails.empty and list(fails.columns) == ops_sql.QUERY_FAILS_COLS


def test_split_columns_match_the_original_builders():
    """The split must feed the SAME columns the summary KPI and failures table already index."""
    assert set(ops_sql.QUERY_SUMMARY_COLS) <= _named(ops_sql.query_window_summary(30, "ALFA"))
    assert set(ops_sql.QUERY_FAILS_COLS) <= _named(ops_sql.failures_by_error(30, "ALFA"))


# --- live-path wiring: rendered Operations serves summary+fails from the one bundle scan -----
st = pytest.importorskip("streamlit")
from packaging.version import parse as _parse_version  # noqa: E402

_APPTEST_OK = _parse_version(st.__version__) >= _parse_version("1.55.0")


@pytest.mark.skipif(not _APPTEST_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_live_path_serves_summary_and_failures_from_one_bundle_scan():
    import usage_sim
    # schema filter + a specific company => the _use_diag False live path (the old 3-scan case)
    report = usage_sim.simulate(
        pages=["Operations"],
        scopes={"schema_filter": {"flt_company": "ALFA", "flt_schema_contains": "PUBLIC"}},
        measure_rerun=False)
    flow = report["flows"][0]
    assert not flow["error"], flow["error"]
    keys = {str(r.get("key")) for r in usage_sim._LEDGER if r.get("key")}
    assert "health" in keys                                       # the one-scan bundle fired
    # neither the separate summary nor the separate failures live scan runs any more
    assert not any(k.startswith("q_summary") for k in keys)
    assert not any(k.startswith("q_fails") for k in keys)
