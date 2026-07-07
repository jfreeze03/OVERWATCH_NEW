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


def test_emergency_builders_validated():
    import pytest as _pt

    from app.logic import remediation as r

    assert r.suspend_warehouse("wh_trxs_transform") == "ALTER WAREHOUSE WH_TRXS_TRANSFORM SUSPEND;"
    assert "RESUME IF SUSPENDED" in r.resume_warehouse("WH_A")
    assert "STATEMENT_TIMEOUT_IN_SECONDS = 3600" in r.statement_timeout_fix("WH_A", 3600)
    assert "MIN_CLUSTER_COUNT = 1 MAX_CLUSTER_COUNT = 3" in r.cluster_range_fix("WH_A", 1, 3)
    assert r.cluster_range_fix("WH_A", 5, 2).count("5") >= 2  # hi floored to lo
    with _pt.raises(ValueError):
        r.scaling_policy_fix("WH_A", "TURBO")
    assert "CREDIT_QUOTA = 30" in r.resource_monitor_quota("OVERWATCH_RM", 30)
    assert "PIPE_EXECUTION_PAUSED = TRUE" in r.pause_pipe("DB1", "RAW", "MY_PIPE")
    assert r.suspend_task_fqn("db1", "raw", "t1") == "ALTER TASK DB1.RAW.T1 SUSPEND;"
    assert "SET DISABLED = TRUE" in r.disable_user("BADUSER")
    assert r.cortex_allowlist("None") == "ALTER ACCOUNT SET CORTEX_MODELS_ALLOWLIST = 'None';"
    assert "llama3.1-8b,mistral-7b" in r.cortex_allowlist("llama3.1-8b,mistral-7b")
    with _pt.raises(ValueError):
        r.cortex_allowlist("bad'; DROP--")
    with _pt.raises(ValueError):
        r.suspend_warehouse("WH; DROP")


def test_runbook_complete_and_selfcontained():
    from pathlib import Path

    rb = (Path(__file__).resolve().parents[1] / "RUNBOOK.md").read_text(encoding="utf-8")
    for section in ("Ten-minute orientation", "Scheduled automation", "Calculated scores",
                    "Forecast engines", "AI engines", "Emergency levers", "Alert engine reference",
                    "Fallback matrix", "Troubleshooting", "Disaster recovery", "Glossary",
                    "Settings reference", "Object inventory"):
        assert section in rb, section
    for rule in ("COST_CONTRACT_BREACH", "PERF_FINGERPRINT_DRIFT", "SEC_CRED_EXPIRY",
                 "CORTEX_MODELS_ALLOWLIST", "TASK_BACKUP_OPERATOR"):
        assert rule in rb, rule
    assert "old app" not in rb.lower()
