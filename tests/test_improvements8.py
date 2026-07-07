"""Remediation engine, schedule advisor, contract planner, new panels."""

import pytest

from app.data import insights_sql, mart_sql, ops_sql, security_sql
from app.logic import contract_planner, remediation


def test_auto_suspend_fix_clamps_and_validates():
    assert remediation.auto_suspend_fix("wh_alfa_etl", 60) == \
        "ALTER WAREHOUSE WH_ALFA_ETL SET AUTO_SUSPEND = 60;"
    assert "AUTO_SUSPEND = 30" in remediation.auto_suspend_fix("W", 5)   # floor
    with pytest.raises(ValueError):
        remediation.auto_suspend_fix("WH; DROP TABLE X")


def test_resize_and_retention_fixes():
    assert "WAREHOUSE_SIZE = 'SMALL'" in remediation.resize_fix("WH_A", "small")
    with pytest.raises(ValueError):
        remediation.resize_fix("WH_A", "GIGANTIC")
    stmt = remediation.retention_fix("db1", "raw", "events", 400)
    assert "DB1.RAW.EVENTS" in stmt and "DATA_RETENTION_TIME_IN_DAYS = 90" in stmt


def test_suspend_schedule_pair():
    s = remediation.suspend_schedule("WH_TRXS_TRANSFORM", 20, 6)
    assert "OVERWATCH_SUSPEND_WH_TRXS_TRANSFORM" in s
    assert "OVERWATCH_RESUME_WH_TRXS_TRANSFORM" in s
    assert "CRON 0 20 * * 1-5" in s and "CRON 0 6 * * 1-5" in s
    assert "RESUME IF SUSPENDED" in s
    with pytest.raises(ValueError):
        remediation.suspend_schedule("WH", 5, 5)


def test_quiet_window_wraps_midnight():
    hours = [{"HOUR_OF_DAY": h,
              "AVG_QUERIES": 0 if (h >= 21 or h < 5) else 50,
              "AVG_CREDITS": 0.4} for h in range(24)]
    win = remediation.propose_quiet_window(hours)
    assert win is not None
    assert win["start"] == 21 and win["end"] == 5 and win["hours"] == 8
    # busy around the clock -> no proposal
    busy = [{"HOUR_OF_DAY": h, "AVG_QUERIES": 50, "AVG_CREDITS": 1} for h in range(24)]
    assert remediation.propose_quiet_window(busy) is None


def test_savings_estimate_monthlyizes():
    assert remediation.monthly_savings_estimate(30, 30, 3.68) == pytest.approx(110.4)
    assert remediation.monthly_savings_estimate(-5, 30, 3.68) == 0.0


def test_contract_planner_scenarios():
    rows = contract_planner.plan_scenarios(100.0, 12, 15.0, remaining_usd=0)
    flat = next(r for r in rows if r["GROWTH"] == "+0%")
    assert flat["TERM_CONSUMPTION_USD"] == pytest.approx(100 * 30.44 * 12, rel=0.01)
    assert flat["RECOMMENDED_COMMIT_USD"] == pytest.approx(flat["TERM_CONSUMPTION_USD"] * 1.15, rel=0.01)
    assert flat["CURRENT_CONTRACT_EXHAUSTED"] == "n/a"
    up = next(r for r in rows if r["GROWTH"] == "+25%")
    assert up["DAILY_BURN_USD"] == pytest.approx(125.0)


def test_new_builders_sources():
    hs = insights_sql.warehouse_hourly_activity(14, "ALFA")
    assert "WAREHOUSE_METERING_HISTORY" in hs and "FACT_QUERY_HOURLY" in hs
    dt = ops_sql.dynamic_table_health(7)
    assert "DYNAMIC_TABLE_REFRESH_HISTORY" in dt and "'FAILED'" in dt
    assert "LIMIT" in ops_sql.show_streams_sql()      # keeps the row-cap rewrite away
    tc = security_sql.trust_center_findings()
    assert "TRUST_CENTER.FINDINGS" in tc and "QUALIFY" in tc
    assert "ALERT_ROUTES" in mart_sql.alert_routes()
    assert "REMEDIATION_LOG" in mart_sql.remediation_log(50)
