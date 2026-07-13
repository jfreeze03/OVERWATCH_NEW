"""Tests for the ported insight features (1-7)."""

import re

import pandas as pd
import pytest

from app.data import insights_sql
from app.logic import insights

# ---- SQL invariants ---------------------------------------------------------

@pytest.mark.parametrize("builder", [
    lambda: insights_sql.idle_warehouse_analysis(7, "ALFA"),
    lambda: insights_sql.repeat_query_fingerprints(7, "ALFA"),
    lambda: insights_sql.storage_growth_by_database(30, "ALFA"),
    lambda: insights_sql.release_query_compare("2026-07-01", 7, "ALFA"),
    lambda: insights_sql.dormant_users(90, "ALFA"),
])
def test_every_insight_scan_is_bounded(builder):
    assert re.search(r"DATEADD\('(day|hour)',\s*-?\d+", builder())


def test_idle_analysis_joins_metering_to_query_hours():
    sql = insights_sql.idle_warehouse_analysis(14, "Trexis")
    assert "WAREHOUSE_METERING_HISTORY" in sql and "QUERY_HISTORY" in sql
    assert "IDLE_CREDITS" in sql
    assert re.search(r"\bIN \('WH_TRXS_LOAD'", sql)  # company scoped


def test_repeat_fingerprints_exclude_overwatch_and_clamp_min_runs():
    sql = insights_sql.repeat_query_fingerprints(7, "ALL", min_runs=99999)
    assert "NOT LIKE 'OVERWATCH%'" in sql
    assert "COUNT(*) >= 1000" in sql  # clamped
    assert "QUERY_PARAMETERIZED_HASH" in sql


def test_release_compare_validates_date():
    with pytest.raises(ValueError):
        insights_sql.release_query_compare("07/01/2026", 7)
    with pytest.raises(ValueError):
        insights_sql.release_query_compare("2026-07-01'; DROP TABLE x;--", 7)


def test_release_windows_clamped_to_14_days():
    sql = insights_sql.release_query_compare("2026-07-01", 99, "ALL")
    assert "-14, DATE '2026-07-01'" in sql and "14, DATE '2026-07-01'" in sql


def test_dormant_users_bounds_and_grants_join():
    sql = insights_sql.dormant_users(90, "ALFA")
    assert "GRANTS_TO_USERS" in sql and "LAST_SUCCESS_LOGIN" in sql
    assert "COMPANY_FOR_USER(U.NAME) = 'ALFA'" in sql  # role-based user scope
    sql = insights_sql.dormant_users(5, "ALL")
    assert "-30," in sql.replace(" ", "")  # floor clamp


def test_pipeline_readers_target_overwatch_objects():
    assert "DBA_MAINT_DB.OVERWATCH.PIPELINE_SLA_STATUS" in insights_sql.pipeline_sla_status()
    assert "DBA_MAINT_DB.OVERWATCH.PIPELINE_SLA_CONFIG" in insights_sql.pipeline_sla_config()


# ---- 1. idle advisor ----------------------------------------------------------

def test_idle_advisor_math_and_flag():
    df = pd.DataFrame([
        {"WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA", "METERED_HOURS": 100,
         "IDLE_HOURS": 50, "TOTAL_CREDITS": 100.0, "IDLE_CREDITS": 40.0},
        {"WAREHOUSE_NAME": "WH_B", "COMPANY": "ALFA", "METERED_HOURS": 100,
         "IDLE_HOURS": 2, "TOTAL_CREDITS": 100.0, "IDLE_CREDITS": 0.5},
    ])
    out = insights.idle_advisor(df, credit_rate_usd=3.68, window_days=30)
    top = out.iloc[0]
    assert top["WAREHOUSE_NAME"] == "WH_A"
    assert top["IDLE_PCT"] == 40.0
    assert top["IDLE_USD"] == 147.2
    assert top["PROJECTED_MONTHLY_IDLE_USD"] == 147.2  # 30d window -> same
    assert bool(top["FLAGGED"]) and "AUTO_SUSPEND" in top["RECOMMENDATION"]
    assert not bool(out.iloc[1]["FLAGGED"])


def test_idle_suspend_sql_validates_identifier():
    assert insights.idle_suspend_sql("WH_ALFA_QUERY") == "ALTER WAREHOUSE WH_ALFA_QUERY SET AUTO_SUSPEND = 60;"
    with pytest.raises(ValueError):
        insights.idle_suspend_sql("WH; DROP TABLE X")


# ---- 2. repeat candidates -------------------------------------------------------

def test_repeat_candidates_flag_heavy_cache_poor():
    df = pd.DataFrame([
        {"FINGERPRINT": "a", "RUNS": 200, "TOTAL_ELAPSED_HOURS": 5.0, "AVG_CACHE_PCT": 5.0, "TOTAL_TB_SCANNED": 1.0},
        {"FINGERPRINT": "b", "RUNS": 50, "TOTAL_ELAPSED_HOURS": 3.0, "AVG_CACHE_PCT": 90.0, "TOTAL_TB_SCANNED": 0.2},
        {"FINGERPRINT": "c", "RUNS": 20, "TOTAL_ELAPSED_HOURS": 0.1, "AVG_CACHE_PCT": 0.0, "TOTAL_TB_SCANNED": 0.0},
    ])
    out = insights.flag_repeat_candidates(df)
    assert bool(out[out["FINGERPRINT"] == "a"]["CANDIDATE"].iloc[0])          # heavy + cache-poor
    assert not bool(out[out["FINGERPRINT"] == "b"]["CANDIDATE"].iloc[0])      # cache already works
    assert not bool(out[out["FINGERPRINT"] == "c"]["CANDIDATE"].iloc[0])      # too cheap to matter


# ---- 3. storage movers -----------------------------------------------------------

def test_storage_movers_projection():
    tb = 1024.0**4
    df = pd.DataFrame([{
        "DATABASE_NAME": "ALFA_EDW_PROD", "COMPANY": "ALFA", "FIRST_DAY": "2026-06-01",
        "LAST_DAY": "2026-07-01", "FIRST_BYTES": 1.0 * tb, "LAST_BYTES": 2.0 * tb,
        "FAILSAFE_BYTES": 0.5 * tb, "SPAN_DAYS": 30,
    }])
    out = insights.storage_movers(df, usd_per_tb_month=23.0)
    row = out.iloc[0]
    assert row["CURRENT_TB"] == 2.0
    assert row["GROWTH_TB"] == 1.0
    assert row["GROWTH_TB_30D"] == 1.0
    assert row["GROWTH_USD_30D"] == 23.0
    assert row["FAILSAFE_SHARE_PCT"] == 25.0


# ---- 4. release compare -----------------------------------------------------------

def test_release_verdicts():
    df = pd.DataFrame([
        {"PERIOD": "BEFORE", "QUERY_COUNT": 1000, "FAILED_COUNT": 10,
         "P95_ELAPSED_SEC": 100.0, "QUEUED_SEC": 100.0, "SPILL_REMOTE_GB": 10.0},
        {"PERIOD": "AFTER", "QUERY_COUNT": 1000, "FAILED_COUNT": 30,
         "P95_ELAPSED_SEC": 50.0, "QUEUED_SEC": 105.0, "SPILL_REMOTE_GB": 10.0},
    ])
    rows = {r["Metric"]: r for r in insights.compare_release_periods(df)}
    assert rows["Failure %"]["Verdict"] == "Worse"        # 1% -> 3%
    assert rows["p95 runtime (s)"]["Verdict"] == "Better"  # halved
    assert rows["Queued (s)"]["Verdict"] == "Flat"         # +5% within tolerance
    assert insights.compare_release_periods(pd.DataFrame()) == []


# ---- 5. failure timeline ------------------------------------------------------------

def test_error_classification():
    assert insights.classify_task_error("SQL access control error: Insufficient privileges") == "Permission / auth"
    assert insights.classify_task_error("Object 'X' does not exist or not authorized") == "Permission / auth"
    assert insights.classify_task_error("Table 'F' does not exist") == "Missing object"
    assert insights.classify_task_error("Statement reached its statement or warehouse timeout") == "Timeout / cancelled"
    assert insights.classify_task_error("Numeric value 'x' is not recognized") == "Data quality"
    assert insights.classify_task_error("Syntax error line 1") == "Syntax / SQL"
    assert insights.classify_task_error("") == "No error text"
    assert insights.classify_task_error("weird") == "Other"


# ---- 7. dormant users -----------------------------------------------------------------

def test_dormant_severity():
    df = pd.DataFrame([
        {"USER_NAME": "A", "DAYS_DORMANT": 400, "ROLE_COUNT": 1},
        {"USER_NAME": "B", "DAYS_DORMANT": 100, "ROLE_COUNT": 2},
        {"USER_NAME": "C", "DAYS_DORMANT": 95, "ROLE_COUNT": 8},
    ])
    out = insights.dormant_severity(df)
    assert list(out["SEVERITY"]) == ["High", "Medium", "High"]
