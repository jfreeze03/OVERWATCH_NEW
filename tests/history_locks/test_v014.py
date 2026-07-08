"""V014 lifecycle hardening: nothing regresses, everything ships."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_V014 = (_ROOT / "snowflake" / "migrations" / "V014__lifecycle_hardening.sql").read_text(encoding="utf-8")


def test_v014_scan_v5_carries_everything():
    """CREATE OR REPLACE must never drop earlier rules (v3+v4 markers)."""
    for marker in ("SEC_CRED_EXPIRY", "COST_CLOUD_SVC_RATIO", "PIPE_COPY_FAILURES",
                   "SEC_BREAK_GLASS_USE", "FACT_METERING_DAILY", "alert scan v5 complete"):
        assert marker in _V014, marker
    assert "'COST_CONTRACT_BREACH'" in _V014
    assert "DATE_TRUNC('week', CURRENT_DATE())" in _V014      # weekly recurrence
    assert "p.DAYS_LEFT <= 14, 'CRITICAL'" in _V014


def test_v014_sweep_v2_carries_everything():
    for marker in ("0.6745", "dynamic_tables_unavailable", "anomaly sweep v2 complete"):
        assert marker in _V014, marker
    assert "'PERF_FINGERPRINT_DRIFT'" in _V014
    assert "DAYOFWEEKISO(CURRENT_DATE()) = 1" in _V014        # Mondays only
    assert "RUNS_RECENT >= 20 AND RUNS_BASE >= 20" in _V014   # sample floors
    assert "f.P95_RECENT_S >= 10" in _V014                    # absolute floor


def test_v014_purge_settings_driven_with_floors():
    assert "SP_PURGE_FACTS" in _V014 and "TASK_PURGE_FACTS" in _V014
    assert "GREATEST(hourly_days, 90)" in _V014
    assert "GREATEST(daily_days, 180)" in _V014
    assert "'FACT_RETENTION_DAYS_HOURLY', '400'" in _V014
    assert "SELECT 14 AS VERSION" in _V014
    for fact in ("FACT_QUERY_HOURLY", "FACT_METERING_DAILY", "FACT_WAREHOUSE_DAILY",
                 "FACT_TASK_DAILY", "FACT_LOGIN_DAILY", "FACT_STORAGE_DAILY"):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{fact}" in _V014, fact


def test_roles_audit_append_only_and_remediation_grant():
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT" in roles
    assert "REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG" in roles
    assert "GRANT INSERT         ON TABLE DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG" in roles
