"""Operations-layer bug-hunt #3 locks (2026-08-30, v4.361.0). App-side only.

Third adversarial pass (6 finders). Seven confirmed; two refuted (the family cache-basis was already
fixed by the v4.360.0 live-route; the optimization-triage mis-rank is unreachable since a >50GB scan
implies >100 partitions on internal tables). Fixes:
  - [MED] lock_wait_spikes summed yesterday+today into "last day" vs a per-single-day baseline.
  - [MED] task_freshness_status flagged weekday-only / business-hours crons Stale every idle boundary.
  - [MED] result_cache_daily company-scope dropped NULL-warehouse cache hits -> HIT_PCT ~0.
  - [MED] volume_deltas flagged a benign weekend day-of-week gap as a 100% FAILED drop.
  - [LOW] proc_sla_rollup ranked 100%-failing procs at 0, truncating them out of the panel.
  - [LOW] product_row_volume LIMIT 8000 rows truncated alphabetically-late tables' series.
  - [LOW] change-impact "Changes tracked" KPI read len(df) (LIMIT 200), undercounting.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

from app.data import change_impact_sql, dq_sql, mart27_sql, ops_sql
from app.logic import insights

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- MED: lock_wait_spikes "last day" is a single complete date ---------------------
def test_lock_wait_spikes_last_day_is_one_complete_date():
    sql = mart27_sql.lock_wait_spikes("ALFA")
    # yesterday only (= today-1), matching the per-single-day PRIOR_DAILY_AVG baseline
    assert "c.DAY = DATEADD('day', -1, CURRENT_DATE())" in sql
    assert "c.DAY >= DATEADD('day', -1, CURRENT_DATE())" not in sql   # the 2-day span is gone
    assert "c.DAY < DATEADD('day', -1, CURRENT_DATE())" in sql        # prior window intact
    sqlglot.parse(sql, dialect="snowflake")


# ---- MED: task freshness judges silence against the p90 longest normal gap ----------
def test_task_freshness_sla_carries_long_gap_signal():
    sql = ops_sql.task_freshness_sla(14, "ALFA")
    assert "APPROX_PERCENTILE(IFF(GAP_MIN > 0, GAP_MIN, NULL), 0.9) AS LONG_GAP_MIN" in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_task_freshness_suppresses_scheduled_idle_but_catches_real_stops():
    df = pd.DataFrame([
        # weekday-only cron on Monday morning: silent since Friday (~4200m), within its longest
        # NORMAL gap (the weekend, ~4320m) -> On-time, NOT a false Stale.
        {"TASK_NAME": "WEEKDAY_OK", "MEDIAN_GAP_MIN": 1440, "LONG_GAP_MIN": 4320,
         "MINS_SINCE_SUCCESS": 4200},
        # weekday cron genuinely stopped: silent far past 2x its longest gap -> Stale/High.
        {"TASK_NAME": "WEEKDAY_STOPPED", "MEDIAN_GAP_MIN": 1440, "LONG_GAP_MIN": 4320,
         "MINS_SINCE_SUCCESS": 10000},
        # uniform hourly cron (long == median): 200m silent >= 2x60 -> Stale (behavior unchanged).
        {"TASK_NAME": "HOURLY", "MEDIAN_GAP_MIN": 60, "LONG_GAP_MIN": 60, "MINS_SINCE_SUCCESS": 200},
    ])
    out = insights.task_freshness_status(df).set_index("TASK_NAME")
    assert out.loc["WEEKDAY_OK", "STATUS"] == "On-time"
    assert out.loc["WEEKDAY_STOPPED", "STATUS"] == "Stale"
    assert out.loc["HOURLY", "STATUS"] == "Stale"


def test_task_freshness_old_shape_frame_falls_back_to_median():
    # a frame without LONG_GAP_MIN behaves as before (yardstick = median): 200m >= 2x60 -> Stale
    df = pd.DataFrame([{"TASK_NAME": "T", "MEDIAN_GAP_MIN": 60, "MINS_SINCE_SUCCESS": 200}])
    assert insights.task_freshness_status(df).iloc[0]["STATUS"] == "Stale"


# ---- MED: result-cache hit % is account-wide (zero-scan hits have no warehouse) ------
def test_result_cache_daily_is_account_wide():
    scoped = ops_sql.result_cache_daily(30, "ALFA")
    account = ops_sql.result_cache_daily(30, "ALL")
    # a company arg must NOT introduce a warehouse-company predicate that drops NULL-warehouse hits
    assert "COMPANY_FOR_WAREHOUSE" not in scoped
    assert scoped == account   # company is accepted but not applied
    sqlglot.parse(scoped, dialect="snowflake")


# ---- MED: volume_deltas suppresses a weekday-off day rather than flagging FAILED -----
def test_volume_deltas_suppresses_day_of_week_gap():
    sql = ops_sql.volume_deltas()
    assert "AS SAME_DOW_ROWS" in sql
    assert "DATEADD('day', -8, CURRENT_DATE())" in sql   # same weekday last week
    assert "WHEN Y_ROWS = 0 AND SAME_DOW_ROWS = 0 THEN 'NORMAL'" in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- LOW: proc_sla_rollup surfaces 100%-failing procs before the LIMIT --------------
def test_proc_sla_rollup_ranks_fully_failing_procs_first():
    sql = ops_sql.proc_sla_rollup(7, "ALFA")
    assert "ORDER BY CASE WHEN FAIL_PCT = 100 THEN 1 ELSE 0 END DESC" in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- LOW: product_row_volume caps TABLES, not rows ----------------------------------
def test_product_row_volume_caps_tables_not_rows():
    sql = dq_sql.product_row_volume(28)
    assert "QUALIFY DENSE_RANK() OVER (ORDER BY m.FQN) <= 600" in sql
    assert "LIMIT 8000" not in sql   # no mid-series row cut
    sqlglot.parse(sql, dialect="snowflake")


# ---- LOW: change-impact "Changes tracked" reads the untruncated total ---------------
def test_change_registry_carries_untruncated_total():
    sql = change_impact_sql.change_registry(90, "ALFA")
    assert "COUNT(*) OVER () AS TOTAL_CHANGES" in sql
    assert "LIMIT 200" in sql   # display list still capped
    sqlglot.parse(sql, dialect="snowflake")
    ops = _src("app/ui/pages/operations.py")
    assert 'int(df["TOTAL_CHANGES"].iloc[0])' in ops
    assert '"value": f"{len(df)}"' not in ops.split("Changes tracked", 1)[1][:200]
