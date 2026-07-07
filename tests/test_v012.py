"""V012: routing, anomaly sweep, remediation log, DT failures."""

from pathlib import Path

_V012 = (Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
         / "V012__routing_anomaly_remediation.sql").read_text(encoding="utf-8")


def test_v012_objects_and_rules():
    for marker in ("ALERT_ROUTES", "REMEDIATION_LOG", "SP_ANOMALY_SWEEP",
                   "TASK_ANOMALY_SWEEP", "'COST_ANOMALY_SWEEP'", "'PIPE_DT_FAILURES'"):
        assert marker in _V012, marker
    assert "SELECT 12 AS VERSION" in _V012


def test_v012_notify_routes_and_falls_back_cleanly():
    assert "FOR rec IN c1 DO" in _V012
    assert ":r_integration" in _V012                 # bound local, not raw record ref
    assert "route_send_failed" in _V012              # one bad route never blocks others
    assert "'OVERWATCH_WEBHOOK'" in _V012            # default route seeded
    # severity gates use rank comparison, portable across editions
    assert "WHEN 'CRITICAL' THEN 4" in _V012


def test_v012_sweep_is_robust_z_on_facts():
    assert "0.6745" in _V012                         # Iglewicz-Hoaglin, matches app logic
    assert "FACT_WAREHOUSE_DAILY" in _V012 and "FACT_METERING_DAILY" in _V012
    assert "MEDIAN(ABS(s.CREDITS - m.MED))" in _V012
    assert "l.MAD > 0" in _V012                      # constant series never alerts
    assert "DYNAMIC_TABLE_REFRESH_HISTORY" in _V012
    assert "dynamic_tables_unavailable" in _V012     # guarded block
