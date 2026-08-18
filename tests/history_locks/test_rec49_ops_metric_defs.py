"""rec#49: ops/security metric-definition consistency.

Three sub-fixes: (1) the failure taxonomy is aligned — BOTH the live query summary
and the mart FAILED_COUNT count EXECUTION_STATUS <> 'SUCCESS' (resolved in V062,
NOT ='FAIL' — the rec's suggested direction would have broken that parity);
(2) the Operations fail-rate tile uses the 2% materiality threshold; (3) the
ops-diag hourly windows are day-aligned like the summary.
"""

from pathlib import Path

from app.data import mart27_sql, ops_sql

_ROOT = Path(__file__).resolve().parents[2]
_OPS = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")


def test_ops_diag_hourly_windows_are_day_aligned_like_the_summary():
    for sql in (mart27_sql.role_hourly(7), mart27_sql.schema_hourly(7)):
        assert "CURRENT_TIMESTAMP" not in sql
        assert "HOUR_TS >= DATEADD('day', -7, CURRENT_DATE())" in sql
    # the live summary they must line up with is day-aligned (CURRENT_DATE) too
    assert "CURRENT_DATE" in ops_sql.query_window_summary(7)


def test_operations_fail_rate_tile_uses_the_2pct_threshold():
    # warn only above 2% (matching Control Room's > 0.02 and the platform score),
    # not on any single failed query
    assert "fail_pct > 2.0" in _OPS
    assert '"severity": "warn" if failed else' not in _OPS   # the old any-failure warn is gone


def test_failure_taxonomy_parity_is_non_success_on_both_sides():
    # sub-fix 1 was already resolved in V062: mart FAILED_COUNT and the live summary
    # both count <> 'SUCCESS'. Lock the parity so neither side drifts to ='FAIL'.
    assert "SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT" in ops_sql.query_window_summary(7)
    v062 = (_ROOT / "snowflake" / "migrations"
            / "V062__loader_robustness_alert_split_webhook.sql").read_text(encoding="utf-8")
    assert "SUM(IFF(EXECUTION_STATUS <> 'SUCCESS', 1, 0)) AS FAILED_COUNT" in v062
