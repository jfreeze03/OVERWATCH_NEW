"""Locks for the V025 differentiators: day replay, contract steering, blast
radius, object TCO, pattern pricing, fire drill, tag governance, restatements."""

from __future__ import annotations

import pandas as pd
import pytest

from app.data import cost_sql, mart_sql, ops_sql, security_sql
from app.data.common import day_literal
from app.data.insights_sql import table_tco
from app.logic.drill import drill_report
from app.logic.replay import replay_headlines
from app.logic.sizing import price_per_run_bounds
from app.logic.steering import steering_plan

# ---------------------------------------------------------------------------
# day_literal: the only gate between a date picker and SQL
# ---------------------------------------------------------------------------


def test_day_literal_accepts_iso_and_date():
    from datetime import date

    assert day_literal("2026-07-01") == "'2026-07-01'::DATE"
    assert day_literal(date(2026, 7, 1)) == "'2026-07-01'::DATE"


def test_day_literal_rejects_hostile():
    with pytest.raises(ValueError):
        day_literal("2026-07-01'; DROP TABLE X--")


def test_day_builders_embed_validated_literal():
    for builder in (mart_sql.day_spend_movers, mart_sql.day_activity,
                    mart_sql.day_task_failures, mart_sql.day_alerts,
                    security_sql.day_ddl, security_sql.day_grants):
        sql = builder("2026-07-01")
        assert "'2026-07-01'::DATE" in sql
        with pytest.raises(ValueError):
            builder("bad'day")


# ---------------------------------------------------------------------------
# Replay narrative
# ---------------------------------------------------------------------------


def _movers(delta):
    return pd.DataFrame([{"WAREHOUSE_NAME": "WH_X", "CREDITS_TOTAL": 50.0 + delta,
                          "BASELINE_CREDITS": 50.0, "DELTA_CREDITS": delta}])


def test_replay_orders_worst_first():
    heads = replay_headlines(_movers(30.0), None, ddl_count=2, grants_count=1,
                             task_failures=3, critical_alerts=1, rate_usd=2.0)
    assert heads[0]["severity"] == "bad"
    sev = [h["severity"] for h in heads]
    assert sev == sorted(sev, key=lambda x: {"bad": 0, "warn": 1, "info": 2, "ok": 3}[x])


def test_replay_quiet_day_is_empty():
    assert replay_headlines(None, None, 0, 0, 0, 0, 2.0) == []


def test_replay_down_mover_is_ok_severity():
    heads = replay_headlines(_movers(-30.0), None, 0, 0, 0, 0, 2.0)
    assert heads and heads[-1]["severity"] == "ok"


# ---------------------------------------------------------------------------
# Steering plan
# ---------------------------------------------------------------------------


def test_steering_overage_math():
    plan = steering_plan(projected_term_credits=1100, contract_credits=1000,
                         days_remaining=50, rate_usd=2.0,
                         levers_monthly_usd={"idle": 90.0})
    assert plan["ok"] and plan["gap_usd"] == 200  # 100 cr * $2
    assert plan["needed_per_day_usd"] == 4.0
    assert plan["covered_per_day_usd"] == 3.0
    assert plan["coverage_pct"] == 75.0
    assert "75%" in plan["verdict"]


def test_steering_on_track():
    plan = steering_plan(projected_term_credits=900, contract_credits=1000,
                         days_remaining=50, rate_usd=2.0, levers_monthly_usd={})
    assert plan["ok"] and plan["gap_usd"] == 0 and "On track" in plan["verdict"]


def test_steering_declines_unconfigured():
    assert not steering_plan(projected_term_credits=1, contract_credits=0,
                             days_remaining=10, rate_usd=2.0,
                             levers_monthly_usd={})["ok"]


# ---------------------------------------------------------------------------
# Drill report
# ---------------------------------------------------------------------------


def _drills(rows):
    return pd.DataFrame(rows)


def test_drill_streak_counts_consecutive_passes():
    df = _drills([
        {"RAISED_AT": "2026-07-01 09:01", "NOTIFIED_AT": "2026-07-01 09:02", "ACK_AT": "2026-07-01 09:11"},
        {"RAISED_AT": "2026-06-01", "NOTIFIED_AT": "2026-06-01 09:02", "ACK_AT": "2026-06-01 09:30"},
        {"RAISED_AT": "2026-05-01", "NOTIFIED_AT": None, "ACK_AT": None},
        {"RAISED_AT": "2026-04-01", "NOTIFIED_AT": "2026-04-01 09:00", "ACK_AT": "2026-04-01 09:05"},
    ])
    report = drill_report(df)
    assert report["streak_months"] == 2  # broken by May
    assert report["last"]["delivered"] and report["last"]["acked"]
    assert report["last"]["mtta_min"] == 10.0


def test_drill_no_history():
    assert drill_report(None) == {"ran": False, "streak_months": 0}


# ---------------------------------------------------------------------------
# Pattern pricing + blast radius + TCO + coverage + restatements
# ---------------------------------------------------------------------------


def test_price_bounds_match_simulator_assumptions():
    b = price_per_run_bounds(allocated_credits=100.0, runs=50, rate_usd=2.0, size_delta=-1)
    assert b["per_run_now_usd"] == 4.0
    assert b["per_run_low_usd"] == 2.0 and b["per_run_high_usd"] == 4.0
    same = price_per_run_bounds(100.0, 50, 2.0, 0)
    assert same["per_run_low_usd"] == same["per_run_high_usd"] == 4.0


def test_blast_radius_identifier_safety():
    sql = ops_sql.warehouse_blast_radius("WH_TRXS_LOAD", 7)
    assert "'WH_TRXS_LOAD'" in sql and "QUERY_TAG" in sql
    with pytest.raises(ValueError):
        ops_sql.warehouse_blast_radius("WH; DROP TABLE X", 7)


def test_table_tco_identifier_safety():
    sql = table_tco("DB1", "SCH", "T1", 30)
    assert "'DB1.SCH.T1'" in sql and "OBJECTS_MODIFIED" in sql
    with pytest.raises(ValueError):
        table_tco("DB1", "SCH", "T1; DROP", 30)


def test_tag_coverage_shape():
    sql = cost_sql.tag_coverage(7, "Trexis")
    assert "QUERY_TAG" in sql and "UNTAGGED_EXEC_SEC" in sql and "WH_TRXS_LOAD" in sql


def test_restatements_shape():
    sql = mart_sql.metering_restatements(9999)
    assert "RESTATED_HOURS_AFTER_CLOSE" in sql and "-180," in sql
    assert ">= 48" in sql
