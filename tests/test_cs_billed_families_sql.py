"""Locks for mart_sql.cloud_svc_billed_families (v4.595) — statement families ranked by the
cloud-services credits they BILL, with the per-day marginal billing cap and the sleep flag.

The load-bearing properties: it ranks by CS credits (never compile), the window totals are taken
BEFORE the top-N cut, billing is account-wide under any scope, and it never touches a live view.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
import sqlglot

from app.config import DEFAULT_MAX_ROWS
from app.data import canary, mart_sql
from app.logic import metric_registry as mr
from app.logic.system_wait import SLEEP_SQL_PATTERN

_BOUNDS = (dt.date(2026, 8, 1), dt.date(2026, 9, 1))
_NEEDED = (
    "CS_RANK", "QUERY_PARAMETERIZED_HASH", "QUERY_TYPE", "WAREHOUSE_NAME", "USER_NAME", "ROLE_NAME",
    "USER_TOP_APP", "SAMPLE_TEXT", "SAMPLE_TEXT_ALT", "RUNS", "ACTIVE_DAYS", "AVG_COMPILE_S", "AVG_EXEC_S",
    "AVG_ELAPSED_S", "CS_CREDITS", "BILLED_CS_CREDITS", "CS_SHARE_PCT", "SLEEP_FLAG", "SCOPE_CS_CREDITS",
    "SCOPE_UNMETERED_CS_CREDITS", "LOW_COMPILE_CS_CREDITS_ALL", "SLEEP_FAMILIES_ALL", "SLEEP_CS_CREDITS_ALL",
    "SLEEP_BILLED_CS_CREDITS_ALL", "METERED_CS_CREDITS", "METERED_DAYS", "UNDER_ALLOWANCE_DAYS",
    "LAST_METERED_DAY",
)


def _sql(*a, **kw) -> str:
    return mart_sql.cloud_svc_billed_families(*a, **kw)


def _cte(sql: str, name: str) -> str:
    start = sql.index(f"{name} AS (")
    nxt = re.search(r"\n\),?\n", sql[start:])
    assert nxt is not None
    return sql[start:start + nxt.end()]


@pytest.mark.parametrize("args,kw", [
    ((30, "ALL"), {}), ((30, "ALFA"), {}), ((30, "Trexis"), {}), ((30, "UNKNOWN"), {}),
    ((30, "ALL", "WH_X"), {}), ((30, "ALL"), {"bounds": _BOUNDS}), ((365, "ALFA", "WH_X"), {"bounds": _BOUNDS}),
])
def test_parses_with_every_named_column(args, kw):
    tree = sqlglot.parse_one(_sql(*args, **kw), read="snowflake")
    assert tuple(tree.named_selects) == _NEEDED


def test_mart_only_sources_and_no_rate_math():
    sql = _sql(30, "ALFA")
    for table in ("MART_CLOUD_SVC_DAILY", "FACT_METERING_DAILY", "MART_QUERY_FAMILY_DAILY", "FACT_APP_COST_DAILY"):
        assert table in sql, table
    assert "ACCOUNT_USAGE" not in sql and "3.68" not in sql and "2.20" not in sql


def test_ranks_by_cs_credits_not_compile():
    sql = _sql(30, "ALL")
    assert "ROW_NUMBER() OVER (ORDER BY t.CS_CREDITS DESC" in sql
    assert "ORDER BY r.CS_RANK" in sql
    assert "> 500" not in sql and not re.search(r"ORDER BY[^\n]*COMPILE", sql)


def test_window_totals_are_taken_before_the_top_n_cut():
    sql = _sql(30, "ALL")
    cut = sql.index(f"WHERE r.CS_RANK <= {mart_sql.CS_BILLED_TOP_N} OR r.SLEEP_FLAG = 1")
    for total in ("SUM(t.CS_CREDITS) OVER ()", "SUM(t.SLEEP_FLAG) OVER ()", "SUM(t.UNMETERED_CS_CREDITS) OVER ()"):
        assert sql.index(total) < cut, total
    final = sql[sql.rindex("\nSELECT\n"):sql.index("FROM ranked r")]
    assert "OVER (" not in final                                  # nothing windowed over the capped rows


def test_per_day_marginal_billing_cap():
    sql = _sql(30, "ALL")
    assert "LEAST(fd.CS_CREDITS, GREATEST(0, b.CS_BILLED_DAY))" in sql
    assert "COALESCE(x.CREDITS_CLOUD_SVCS, 0) + COALESCE(x.CREDITS_ADJUSTMENT, 0)" in sql
    assert "COUNT_IF(b.CS_BILLED_DAY <= 0) AS UNDER_ALLOWANCE_DAYS" in sql
    assert "IFF(b.DAY IS NULL, fd.CS_CREDITS, 0)" in sql             # unmetered days stay unpriced
    assert "IFF(b.DAY IS NULL, NULL," in sql
    assert "LEFT JOIN bill b ON b.DAY = fd.DAY" in sql


def test_billing_and_app_hint_are_account_wide_under_any_scope():
    # the panel drills a warehouse with company='ALL' (a warehouse is a complete scope)
    sql = _sql(30, "ALL", "WH_X")
    assert "COMPANY" not in sql
    assert "WAREHOUSE_NAME = 'WH_X'" in _cte(sql, "fd")
    assert "WAREHOUSE_NAME = 'WH_X'" not in _cte(sql, "bill") + _cte(sql, "app") + _cte(sql, "el")
    csql = _sql(30, "ALFA")
    assert csql.count("COMPANY = 'ALFA'") == 2                      # fd + the family mart (el)
    assert "COMPANY" not in _cte(csql, "bill") and "COMPANY" not in _cte(csql, "app")


def test_sleep_flag_uses_the_shared_pattern():
    sql = _sql(30, "ALL")
    assert f"'{SLEEP_SQL_PATTERN}'" in sql
    assert "<> 'n/a'" in sql and "NOT LIKE 'CREATE%'" in sql and "NOT LIKE 'ALTER%'" in sql


def test_warehouse_scope_and_quote_safety():
    sql = _sql(7, "ALL", "WH_X")
    assert sql.count("WAREHOUSE_NAME = 'WH_X'") == 1
    assert "WAREHOUSE_NAME = 'WH''X'" in _sql(7, "ALL", "WH'X")


def test_window_reaches_the_mart_limit_and_honours_bounds():
    sql = _sql(365, "ALL")
    assert "-365," in sql and "-90," not in sql
    bounded = _sql(30, "ALL", bounds=_BOUNDS)
    assert bounded.count("DAY >= '2026-08-01' AND DAY < '2026-09-01'") == 4


def test_row_cap_below_the_run_cap():
    sql = _sql(30, "ALL")
    assert sql.rstrip().endswith(f"LIMIT {mart_sql.CS_BILLED_ROW_CAP}")
    assert mart_sql.CS_BILLED_ROW_CAP < DEFAULT_MAX_ROWS
    assert f"CS_RANK <= {mart_sql.CS_BILLED_TOP_N}" in sql


def test_aggregate_arguments_are_alias_qualified():
    # a bare RUNS once resolved to a SUM(RUNS) AS RUNS alias: "aggregate functions cannot be nested"
    sql = _sql(30, "ALL", bounds=_BOUNDS)
    assert re.search(r"(?:SUM|MIN|MAX|MAX_BY|COUNT_IF)\((?:RUNS|CS_CREDITS|ROLE_NAME|SAMPLE_TEXT|QUERIES)\b",
                     sql) is None


def test_registered_in_canary_and_metric_registry():
    assert "mart.cloud_svc_billed_families" in [name for name, _ in canary.CANARIES]
    m = mr.get("cloud_services_family_billed")
    assert m is not None and m.formula_version == "v4.595"
    sql = _sql(30, "ALL")
    for src in m.required_sources:
        assert src in sql, src


def test_only_complete_metering_days_price_anything():
    # review r1: the newest FACT_METERING_DAILY row is the UTC day in progress at the 06:45 load
    sql = _sql(30, "ALL")
    bill = _cte(sql, "bill")
    assert "x.DAY < (SELECT MAX(z.DAY) FROM" in bill and "FACT_METERING_DAILY" in bill
    bounded = _cte(_sql(30, "ALL", bounds=_BOUNDS), "bill")
    assert "x.DAY < (SELECT MAX(z.DAY) FROM" in bounded      # the whole table's max, not the window's


def test_sleep_total_is_a_per_day_group_cap():
    sql = _sql(30, "ALL")
    assert "LEAST(sd.SLEEP_CS_DAY, GREATEST(0, b.CS_BILLED_DAY))" in sql
    assert "ROUND(ss.SLEEP_BILLED_CS_CREDITS_ALL, 4) AS SLEEP_BILLED_CS_CREDITS_ALL" in sql
    assert "CROSS JOIN sleepsum ss" in sql
    assert "t.BILLED_CS_CREDITS, 0)) OVER ()" not in sql      # never the sum of per-family marginals
