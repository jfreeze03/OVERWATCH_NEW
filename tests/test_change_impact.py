"""Change-impact tracking (V010): builders + migration contract."""

from pathlib import Path

import pytest

from app.data import change_impact_sql

_V010 = (Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
         / "V010__change_impact.sql").read_text(encoding="utf-8")


def test_registry_reader_bounded_and_scoped():
    sql = change_impact_sql.change_registry(9999, "Trexis", "TRXS_DW", "STAGING")
    assert "DATEADD('day', -120" in sql          # clamped
    assert "COMPANY = 'Trexis'" in sql
    assert "UPPER(DATABASE_NAME) IN ('TRXS_DW')" in sql  # exact database filter
    assert "SCHEMA_NAME ILIKE '%STAGING%' ESCAPE '~'" in sql
    assert "OBJECT_CHANGE_REGISTRY" in sql and "LIMIT 200" in sql


def test_registry_reader_all_companies_has_no_company_filter():
    sql = change_impact_sql.change_registry(30, "ALL")
    assert "COMPANY =" not in sql
    # schema is always visible as its own column
    assert "SCHEMA_NAME" in sql and "DATABASE_NAME" in sql


def test_run_history_procedure_matches_call_text():
    sql = change_impact_sql.object_run_history("PROCEDURE", "DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN", 28)
    assert "QUERY_TYPE = 'CALL'" in sql
    assert "'SP_ALERT_SCAN('" in sql
    assert "ACCOUNT_USAGE.QUERY_HISTORY" in sql


def test_run_history_task_uses_task_history_equality():
    sql = change_impact_sql.object_run_history("task", "DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY")
    assert "ACCOUNT_USAGE.TASK_HISTORY" in sql
    assert "'DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY'" in sql
    assert "STATE IN ('SUCCEEDED', 'FAILED')" in sql


def test_run_history_rejects_bad_input():
    with pytest.raises(ValueError):
        change_impact_sql.object_run_history("VIEW", "A.B.C")
    with pytest.raises(ValueError):
        change_impact_sql.object_run_history("PROCEDURE", "X'; DROP TABLE Y;--")


def test_v010_registry_distinguishes_schema():
    """Owner requirement: every change must show which schema it came from."""
    assert "DATABASE_NAME     VARCHAR(200)  NOT NULL" in _V010
    assert "SCHEMA_NAME       VARCHAR(200)  NOT NULL" in _V010
    assert "PROCEDURE_SCHEMA AS SCHEMA_NAME" in _V010


def test_v010_seeds_rule_and_scan():
    assert "'PERF_CHANGE_REGRESSION'" in _V010
    assert "SP_CHANGE_IMPACT_SCAN" in _V010
    assert "TASK_CHANGE_IMPACT_SCAN" in _V010
    assert "QUERY_ATTRIBUTION_HISTORY" in _V010
    assert "COALESCE(ROOT_QUERY_ID, QUERY_ID)" in _V010   # children roll up to the CALL
    assert "attribution_unavailable" in _V010             # graceful fallback path
    assert _V010.count("EXCEPTION") >= 2                  # task + attribution guards


def test_v010_baselines_frozen_and_alert_deduped():
    assert "BASELINE_FROM IS NULL" in _V010               # freeze-once marker
    assert "TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)" in _V010  # dedupe on object + change day
    assert "'INSUFFICIENT_AFTER'" in _V010
    assert "SELECT 10 AS VERSION" in _V010
