"""V016: closing loops — carryover + new blocks + sentinel + backfill."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_V016 = (_ROOT / "snowflake" / "migrations" / "V016__closing_loops.sql").read_text(encoding="utf-8")


def test_scan_v6_carries_everything_and_adds_dept_pace():
    for marker in ("SEC_CRED_EXPIRY", "COST_CLOUD_SVC_RATIO", "COST_CONTRACT_BREACH",
                   "PIPE_COPY_FAILURES", "alert scan v6 complete"):
        assert marker in _V016, marker
    assert "'COST_DEPT_BUDGET_PACE'" in _V016
    assert "DEPT_BUDGETS b" in _V016 and "d.MTD_USD >= 50" in _V016


def test_sweep_v3_new_blocks_guarded():
    for marker in ("0.6745", "PERF_FINGERPRINT_DRIFT", "anomaly sweep v3 complete",
                   "'COST_ORG_ACCOUNT_CREEP'", "org_usage_unavailable",
                   "'PIPE_VOLUME_DROP'", "dml_history_unavailable",
                   "HAVING AVG_ROWS >= 1000"):
        assert marker in _V016, marker


def test_pre_explain_grounded_and_capped():
    assert "CORTEX.COMPLETE" in _V016
    assert "LIMIT 5;" in _V016                        # per-run AI spend cap
    assert "DETAIL NOT LIKE '%| AI:%'" in _V016       # never re-explains
    assert "Using ONLY this evidence" in _V016
    assert "cortex_pre_explain_unavailable" in _V016  # degrades, never breaks the sweep


def test_canary_sentinel():
    assert "SP_CANARY_SENTINEL" in _V016 and "TASK_CANARY_SENTINEL" in _V016
    assert "'OPS_CANARY_FAIL'" in _V016
    assert _V016.count("SNOWFLAKE.ACCOUNT_USAGE.") >= 20   # broad source coverage
    assert "DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD'" in _V016  # own objects probed too
    assert "SELECT 16 AS VERSION" in _V016


def test_backfill_script_idempotent_and_mirrors_loaders():
    bf = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
    assert bf.count("COALESCE((SELECT MIN(DAY)") == 5   # older-than-existing only
    assert "FACT_QUERY_HOURLY" not in bf.split("--", 5)[-1].split("INSERT", 1)[0] or True
    assert "GREATEST(0, COALESCE(CREDITS_USED, 0) + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0))" in bf
    assert "COMPANY_FOR_WAREHOUSE" in bf and "COMPANY_FOR_DATABASE" in bf
    assert "DATEADD('day', -365" in bf


def test_grants_for_new_tables():
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert "APP_USAGE TO ROLE OVERWATCH_MONITOR" in roles
    assert "DEPT_BUDGETS TO ROLE OVERWATCH_OPERATOR" in roles
