"""P2-17/18: DT pilot, forecast engines, backups."""

from datetime import date
from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic.forecast import month_end_projection

_ROOT = Path(__file__).resolve().parents[1]
_V015 = (_ROOT / "snowflake" / "migrations" / "V015__pilot_and_backups.sql").read_text(encoding="utf-8")


def _daily(n=28, base=100.0, weekend=20.0):
    rows = []
    today = date(2026, 7, 15)
    for i in range(n, 0, -1):
        d = pd.Timestamp(today) - pd.Timedelta(days=i)
        usd = weekend if d.weekday() >= 5 else base
        rows.append({"DAY": d.date(), "USD": usd})
    return pd.DataFrame(rows), today


def test_seasonal_engine_projects_per_weekday():
    daily, today = _daily()
    linear = month_end_projection(daily, today, engine="linear")
    seasonal = month_end_projection(daily, today, engine="seasonal")
    assert linear.ok and seasonal.ok
    assert "Linear engine" in linear.basis and "Seasonal engine" in seasonal.basis
    # weekday/weekend split: seasonal projection differs from flat average
    assert seasonal.projected_usd != linear.projected_usd
    # July 16-31 2026: 12 weekdays x100 + 4 weekend x20 = 1280
    assert abs(seasonal.projected_usd - seasonal.mtd_usd - 1280) < 1
    # tight residuals -> tight band
    assert seasonal.high_usd - seasonal.low_usd < linear.high_usd - linear.low_usd


def test_seasonal_falls_back_when_thin():
    daily, today = _daily(n=10)
    thin = month_end_projection(daily, today, engine="seasonal")
    assert thin.ok and "Linear engine" in thin.basis  # <14 points -> linear


def test_ml_forecast_reader():
    sql = mart_sql.ml_forecast_daily()
    assert "FORECAST_ML_DAILY" in sql and "FORECAST_CREDITS" in sql
    assert "TS::DATE > CURRENT_DATE()" in sql


def test_v015_dt_pilot_and_backups():
    assert "CREATE DYNAMIC TABLE IF NOT EXISTS" in _V015
    assert "MART_SPEND_ROLLUP_DT" in _V015
    assert "SET CHANGE_TRACKING = TRUE" in _V015       # DT prerequisite on OUR fact
    assert "FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY" in _V015
    assert "SNOWFLAKE.ACCOUNT_USAGE" not in _V015      # DTs cannot source share views
    assert "SP_BACKUP_OPERATOR_TABLES" in _V015 and "TASK_BACKUP_OPERATOR" in _V015
    assert "_BAK_LAST CLONE" in _V015
    assert "clone_failed" in _V015                     # per-table isolation
    for t in ("'SETTINGS'", "'ALERT_CONFIG'", "'SAVINGS_LEDGER'", "'USER_PREFS'"):
        assert t in _V015, t
    assert "SELECT 15 AS VERSION" in _V015


def test_ml_option_script_is_self_contained():
    script = (_ROOT / "snowflake" / "ml_forecast_option.sql").read_text(encoding="utf-8")
    assert "SNOWFLAKE.ML.FORECAST" in script
    assert "FORECAST_ML_DAILY" in script
    assert "TASK_REFRESH_ML_FORECAST" in script
    assert "-- ALTER TASK" in script                   # created suspended, opt-in resume
