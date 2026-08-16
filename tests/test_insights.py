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
    lambda: insights_sql.release_task_compare("2026-07-01", 7, "ALFA"),
    lambda: insights_sql.detect_release_days(90, "ALFA"),
    lambda: insights_sql.task_failure_details(7, "ALFA"),
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


def test_active_hours_use_a_span_join_not_the_start_hour():
    """D2: a query that starts at 10:59 and runs 3h must mark hours 11 and 12
    active too — the start-hour DISTINCT booked them as idle."""
    for sql in (insights_sql.idle_warehouse_analysis(30, "ALFA"),
                insights_sql.warehouse_sizing_profile(30, "ALFA")):
        assert "q_spans" in sql and "GENERATOR(ROWCOUNT =>" in sql
        assert "DATEADD('hour', g.SEQ, s.H0) <= s.H1" in sql
        # the naive start-hour set is gone from both builders
        assert "SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME)" not in sql


def test_sizing_profile_splits_provisioning_out_of_queueing():
    """D2: provisioning time is a warehouse waking up (a suspend-timer signal),
    not concurrency pressure — sizing up to 'fix' it buys nothing."""
    sql = insights_sql.warehouse_sizing_profile(30, "ALFA")
    assert "SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000.0 AS QUEUED_SEC" in sql
    assert "QUEUED_PROVISIONING_SEC" in sql
    assert "QUEUED_OVERLOAD_TIME, 0) + COALESCE(QUEUED_PROVISIONING_TIME" not in sql


def test_repeat_fingerprints_rank_credit_weighted_and_weight_cache_by_bytes():
    sql = insights_sql.repeat_query_fingerprints(7, "ALFA")
    assert "EST_CREDITS" in sql and "WAREHOUSE_SIZE" in sql
    assert "ORDER BY EST_CREDITS * (1 - AVG_CACHE_PCT / 100) DESC" in sql
    # cache % is BYTES_SCANNED-weighted and ignores zero-scan (result-cache) runs
    assert "IFF(COALESCE(BYTES_SCANNED, 0) > 0, BYTES_SCANNED, 0)" in sql
    assert "AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)) * 100" not in sql


def test_patterns_hash_filter_sits_outside_the_denominator():
    """C3 / attribution law: the hash filter picks DISPLAY rows only. Inside the
    q CTE it also shrank the warehouse-hour denominator, inflating every $."""
    sql = insights_sql.expensive_patterns_usd(7, "ALFA", 30)
    head, tail = sql.split("SELECT\n    q.PATTERN_HASH", 1)
    assert "QUERY_PARAMETERIZED_HASH IS NOT NULL" not in head   # not in q/t/m
    assert "WHERE q.PATTERN_HASH IS NOT NULL" in tail


def test_hourly_activity_averages_over_calendar_days():
    """B1: AVG_CREDITS is per calendar day of the window; a per-METERED-day
    average monetized x30 calendar days overstated the schedule saving ~3.5x."""
    sql = insights_sql.warehouse_hourly_activity(14, "ALFA")
    assert "ROUND(m.CR / 14, 3) AS AVG_CREDITS" in sql
    assert "ROUND(COALESCE(q.QC, 0) / 14, 1) AS AVG_QUERIES" in sql
    assert "NULLIF(m.DAYS_SEEN, 0)" not in sql
    assert "DAYS_METERED" in sql          # kept for transparency


def test_storage_builders_carry_current_retention():
    """B4: the retention-fix estimate needs the CURRENT retention to say how
    much of the Time Travel a shorter window actually releases."""
    for sql in (insights_sql.storage_waste("ALFA"), insights_sql.storage_reclaim("ALFA")):
        assert "RETENTION_DAYS" in sql and "ACCOUNT_USAGE.TABLES" in sql
        assert "RETENTION_KNOWN" in sql
        assert "COALESCE(rt.RETENTION_DAYS, 0)" not in sql


def test_reclaim_joins_reads_by_object_id():
    """D4: joining ACCESS_HISTORY by assembled name made every quoted/mixed-case
    identifier look NEVER_READ — a 'safe to archive' verdict on a hot table."""
    sql = insights_sql.storage_reclaim("ALFA")
    assert 'f.value:"objectId"::NUMBER AS TABLE_ID' in sql
    assert "LEFT JOIN reads r ON r.TABLE_ID = m.ID" in sql
    assert "r.FQN = m.TABLE_CATALOG" not in sql


def test_storage_growth_exposes_a_regression_slope():
    sql = insights_sql.storage_growth_by_database(30, "ALFA")
    assert "REGR_SLOPE(DB_BYTES" in sql and "DAYS_OBSERVED" in sql


def test_release_compare_validates_date():
    with pytest.raises(ValueError):
        insights_sql.release_query_compare("07/01/2026", 7)
    with pytest.raises(ValueError):
        insights_sql.release_task_compare("2026-07-01'; DROP TABLE x;--", 7)


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


def test_idle_advisor_haircuts_the_unavoidable_resume_tail():
    """B3: 100% of idle is not recoverable — Snowflake bills a 60s minimum per
    resume, so one target-suspend tail per ACTIVE metered hour is deducted."""
    df = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA", "METERED_HOURS": 100,
        "IDLE_HOURS": 60, "TOTAL_CREDITS": 100.0, "IDLE_CREDITS": 40.0,
    }])
    row = insights.idle_advisor(df, credit_rate_usd=3.68, window_days=30).iloc[0]
    # 40 active hours x 60s x (100 credits / 100 hours) = 0.667 idle credits are tail
    tail_credits = 40 * (insights.IDLE_RESUME_TAIL_SEC / 3600.0) * 1.0
    assert row["RECOVERABLE_IDLE_USD"] == round((40.0 - tail_credits) * 3.68, 2)
    assert row["RECOVERABLE_IDLE_USD"] < row["IDLE_USD"]          # never the gross number
    assert row["RECOVERABLE_MONTHLY_USD"] <= row["PROJECTED_MONTHLY_IDLE_USD"]


def test_idle_advisor_recoverable_never_goes_negative():
    df = pd.DataFrame([{"WAREHOUSE_NAME": "W", "COMPANY": "ALFA", "METERED_HOURS": 100,
                        "IDLE_HOURS": 1, "TOTAL_CREDITS": 500.0, "IDLE_CREDITS": 0.2}])
    row = insights.idle_advisor(df, 3.68, 30).iloc[0]
    assert row["RECOVERABLE_IDLE_USD"] == 0.0


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


def test_repeat_gate_is_window_relative():
    """D3: the gate is 0.5h of compute per 30 DAYS. The same fingerprint must
    not become a candidate merely because the window picker moved."""
    row = {"FINGERPRINT": "a", "RUNS": 100, "TOTAL_ELAPSED_HOURS": 0.6,
           "AVG_CACHE_PCT": 5.0, "TOTAL_TB_SCANNED": 1.0}
    df = pd.DataFrame([row])
    assert bool(insights.flag_repeat_candidates(df, 30).iloc[0]["CANDIDATE"])
    # same 0.6h spread over a YEAR is 0.05h/30d — not a materialization target
    assert not bool(insights.flag_repeat_candidates(df, 365).iloc[0]["CANDIDATE"])
    # and over 3 days it is 6h/30d — clearly one
    assert bool(insights.flag_repeat_candidates(df, 3).iloc[0]["CANDIDATE"])


def test_repeat_gate_requires_recurring_runs_per_30_days():
    row = {"FINGERPRINT": "monthly", "RUNS": 12, "TOTAL_ELAPSED_HOURS": 120.0,
           "AVG_CACHE_PCT": 0.0, "TOTAL_TB_SCANNED": 12.0, "EST_CREDITS": 100.0}
    result = insights.flag_repeat_candidates(pd.DataFrame([row]), 365).iloc[0]
    assert result["RUNS_PER_30D"] == 1.0
    assert result["HOURS_PER_30D"] > insights.REPEAT_MIN_ELAPSED_HOURS
    assert not bool(result["CANDIDATE"])
    assert insights.repeat_min_runs(7) == 3
    assert insights.repeat_min_runs(30) == 10
    assert insights.repeat_min_runs(365) == 122


def test_repeat_candidates_rank_by_money_not_hours():
    """D3: an X-Small hour and a 4X-Large hour differ 128x in money, and an
    already-cached family has nothing left to reclaim."""
    df = pd.DataFrame([
        {"FINGERPRINT": "long_but_tiny", "RUNS": 10, "TOTAL_ELAPSED_HOURS": 40.0,
         "AVG_CACHE_PCT": 0.0, "TOTAL_TB_SCANNED": 0.1, "EST_CREDITS": 2.0},
        {"FINGERPRINT": "short_but_huge", "RUNS": 10, "TOTAL_ELAPSED_HOURS": 2.0,
         "AVG_CACHE_PCT": 0.0, "TOTAL_TB_SCANNED": 5.0, "EST_CREDITS": 200.0},
        {"FINGERPRINT": "huge_but_cached", "RUNS": 10, "TOTAL_ELAPSED_HOURS": 30.0,
         "AVG_CACHE_PCT": 99.0, "TOTAL_TB_SCANNED": 5.0, "EST_CREDITS": 150.0},
    ])
    ranked = list(insights.flag_repeat_candidates(df, 30)["FINGERPRINT"])
    assert ranked[0] == "short_but_huge"
    assert ranked.index("huge_but_cached") > ranked.index("long_but_tiny")


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
    assert row["GROWTH_TB_30D"] == 1.0        # no slope column -> endpoint basis
    assert row["GROWTH_USD_30D"] == 23.0
    assert row["TREND_BASIS"] == "endpoints"
    # E6 (audit 2026-07-31): failsafe is billed ALONGSIDE the database bytes, so
    # the share is of the TOTAL (0.5 / 2.5), not of the database alone (0.5 / 2.0
    # = 25%, which could and did exceed 100% on failsafe-heavy databases).
    assert row["FAILSAFE_SHARE_PCT"] == 20.0


def test_storage_movers_failsafe_share_cannot_exceed_100():
    tb = 1024.0**4
    df = pd.DataFrame([{"DATABASE_NAME": "D", "COMPANY": "ALFA", "FIRST_BYTES": 1.0 * tb,
                        "LAST_BYTES": 1.0 * tb, "FAILSAFE_BYTES": 9.0 * tb, "SPAN_DAYS": 30}])
    row = insights.storage_movers(df, usd_per_tb_month=23.0).iloc[0]
    assert 0 <= row["FAILSAFE_SHARE_PCT"] <= 100


def test_storage_movers_prefer_slope_and_flag_short_series():
    """D4: the x30 projection follows the regression slope over observed days,
    and a series too short to trend is flagged rather than projected as fact."""
    tb = 1024.0**4
    df = pd.DataFrame([
        # endpoints say +9 TB (a one-day spike on the last day); the slope over
        # 30 observed days says +0.1 TB/day = 3 TB/30d.
        {"DATABASE_NAME": "SPIKE", "COMPANY": "ALFA", "FIRST_BYTES": 1.0 * tb,
         "LAST_BYTES": 10.0 * tb, "FAILSAFE_BYTES": 0.0, "SPAN_DAYS": 30,
         "DAYS_OBSERVED": 30, "SLOPE_BYTES_PER_DAY": 0.1 * tb},
        {"DATABASE_NAME": "NEW", "COMPANY": "ALFA", "FIRST_BYTES": 1.0 * tb,
         "LAST_BYTES": 2.0 * tb, "FAILSAFE_BYTES": 0.0, "SPAN_DAYS": 2,
         "DAYS_OBSERVED": 3, "SLOPE_BYTES_PER_DAY": 0.5 * tb},
    ])
    out = insights.storage_movers(df, usd_per_tb_month=23.0).set_index("DATABASE_NAME")
    assert out.loc["SPIKE", "GROWTH_TB_30D"] == 3.0          # not the 9 TB endpoint jump
    assert out.loc["SPIKE", "TREND_BASIS"] == "regression"
    assert not bool(out.loc["SPIKE", "LOW_CONFIDENCE"])
    assert bool(out.loc["NEW", "LOW_CONFIDENCE"])            # 3 observed days
    assert bool(out.loc["SPIKE", "PROJECTABLE"])
    assert out.loc["NEW", "PROJECTABLE_GROWTH_USD_30D"] == 0.0
    # ordering is by projected dollars (what the chart's top-10 cut means)
    assert insights.storage_movers(df, 23.0)["DATABASE_NAME"].iloc[0] == "NEW"


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


def test_task_release_deltas_flags_regressions():
    df = pd.DataFrame([
        {"DATABASE_NAME": "DB", "TASK_NAME": "T1", "PERIOD": "BEFORE", "RUNS": 10, "FAILED": 0, "AVG_SEC": 100},
        {"DATABASE_NAME": "DB", "TASK_NAME": "T1", "PERIOD": "AFTER", "RUNS": 10, "FAILED": 3, "AVG_SEC": 100},
        {"DATABASE_NAME": "DB", "TASK_NAME": "T2", "PERIOD": "BEFORE", "RUNS": 10, "FAILED": 0, "AVG_SEC": 100},
        {"DATABASE_NAME": "DB", "TASK_NAME": "T2", "PERIOD": "AFTER", "RUNS": 10, "FAILED": 0, "AVG_SEC": 101},
    ])
    out = insights.task_release_deltas(df)
    t1 = out[out["TASK_NAME"] == "T1"].iloc[0]
    t2 = out[out["TASK_NAME"] == "T2"].iloc[0]
    assert bool(t1["GOT_WORSE"]) and t1["NEW_FAILURES"] == 3
    assert not bool(t2["GOT_WORSE"])
    assert out.iloc[0]["TASK_NAME"] == "T1"  # regressions first


# ---- 4b. Release auto-detect (O16) --------------------------------------------

def test_detect_release_days_scans_ddl_excludes_noise_and_scopes():
    sql = insights_sql.detect_release_days(90, "ALFA")
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in sql
    # the deploy signal is CREATE/ALTER/DROP DDL
    for frag in ("QUERY_TYPE ILIKE 'CREATE%'", "QUERY_TYPE ILIKE 'ALTER%'",
                 "QUERY_TYPE ILIKE 'DROP%'"):
        assert frag in sql
    # CTAS / session ops are high-volume non-deploys and must not swamp the count
    assert "CREATE_TABLE_AS_SELECT" in sql and "ALTER_SESSION" in sql
    # the app's own maintenance DDL is excluded by its query tag
    assert "NOT LIKE 'OVERWATCH%'" in sql
    # company-scoped by the touched database (database_clause form)
    assert "LIKE 'ALFA%'" in sql
    assert "GROUP BY 1" in sql and "DDL_COUNT" in sql


def test_detect_release_days_clamped_to_180():
    sql = insights_sql.detect_release_days(9999, "ALL")
    assert "-180," in sql.replace(" ", "")


def test_rank_release_candidates_keeps_biggest_days_recent_first():
    df = pd.DataFrame([
        {"DAY": "2026-08-01", "DDL_COUNT": 40, "TOP_ACTOR": "DEPLOYER"},
        {"DAY": "2026-08-10", "DDL_COUNT": 5, "TOP_ACTOR": "X"},
        {"DAY": "2026-08-05", "DDL_COUNT": 60, "TOP_ACTOR": "Y"},
        {"DAY": "2026-08-09", "DDL_COUNT": 0, "TOP_ACTOR": "Z"},   # no DDL -> dropped
    ])
    out = insights.rank_release_candidates(df, limit=2)
    # top-2 by count are 08-05 (60) and 08-01 (40); shown newest-first so the
    # default pick is the latest notable deploy
    assert list(out["DAY"]) == ["2026-08-05", "2026-08-01"]


def test_rank_release_candidates_normalises_day_and_handles_empty():
    assert insights.rank_release_candidates(pd.DataFrame()).empty
    # a timestamp DAY normalises to YYYY-MM-DD, the exact shape the release
    # readers validate before embedding — round-trips without raising
    df = pd.DataFrame([{"DAY": pd.Timestamp("2026-08-03 12:00:00"), "DDL_COUNT": 3}])
    out = insights.rank_release_candidates(df)
    assert out.iloc[0]["DAY"] == "2026-08-03"
    insights_sql.release_query_compare(out.iloc[0]["DAY"], 7, "ALFA")  # no ValueError


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


def test_error_classification_covers_live_account_top_fallthroughs():
    # v4.154: the exact messages that made "Other" the top family (73%) in the
    # live account. Each must land in its new family, not "Other" — and not get
    # stolen by an earlier pattern.
    assert insights.classify_task_error(
        "There is already a live version. Please commit it first."
    ) == "Concurrency / live version"
    assert insights.classify_task_error(
        "Cannot perform SELECT. This session does not have a current database. "
        "Call 'USE DATABASE', or use a qualified name."
    ) == "Session not set up"
    assert insights.classify_task_error(
        "Role hierarchy data is not yet available in Md Fetch"
    ) == "Metadata not ready"


def test_failure_timeline_marks_root_vs_cascade():
    df = pd.DataFrame([
        {"TASK_NAME": "CHILD", "GRAPH_RUN_GROUP_ID": "g1",
         "QUERY_START_TIME": "2026-07-07 02:10:00", "ERROR_MESSAGE": "upstream failed"},
        {"TASK_NAME": "ROOT", "GRAPH_RUN_GROUP_ID": "g1",
         "QUERY_START_TIME": "2026-07-07 02:00:00", "ERROR_MESSAGE": "timeout"},
    ])
    out = insights.build_failure_timeline(df)
    root = out[out["TASK_NAME"] == "ROOT"].iloc[0]
    child = out[out["TASK_NAME"] == "CHILD"].iloc[0]
    assert root["ROLE_IN_GRAPH"] == "Root cause"
    assert child["ROLE_IN_GRAPH"] == "Cascade"
    assert root["ERROR_FAMILY"] == "Timeout / cancelled"


# ---- 7. dormant users -----------------------------------------------------------------

def test_dormant_severity():
    df = pd.DataFrame([
        {"USER_NAME": "A", "DAYS_DORMANT": 400, "ROLE_COUNT": 1},
        {"USER_NAME": "B", "DAYS_DORMANT": 100, "ROLE_COUNT": 2},
        {"USER_NAME": "C", "DAYS_DORMANT": 95, "ROLE_COUNT": 8},
    ])
    out = insights.dormant_severity(df)
    assert list(out["SEVERITY"]) == ["High", "Medium", "High"]


def test_reawakening_severity():
    # Sec5: dormant-then-active ranked by silence length + roles held.
    df = pd.DataFrame([
        {"USER_NAME": "A", "GAP_DAYS": 400, "ROLE_COUNT": 1},   # long silence -> High
        {"USER_NAME": "B", "GAP_DAYS": 100, "ROLE_COUNT": 2},   # 90-179d -> Medium
        {"USER_NAME": "C", "GAP_DAYS": 60, "ROLE_COUNT": 8},    # many roles -> High
        {"USER_NAME": "D", "GAP_DAYS": 50, "ROLE_COUNT": 1},    # short + few roles -> Low
    ])
    assert list(insights.reawakening_severity(df)["SEVERITY"]) == ["High", "Medium", "High", "Low"]
    assert insights.reawakening_severity(pd.DataFrame()).empty
