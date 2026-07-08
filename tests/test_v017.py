"""V017: scan v7 isolation, deploy stage, render SLA, version guard."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_V017 = (_ROOT / "snowflake" / "migrations" / "V017__hardening_v7.sql").read_text(encoding="utf-8")

_ALL_RULES = (
    "COST_DAILY_CREDITS", "COST_WH_DAILY_CREDITS", "COST_BUDGET_PACE",
    "COST_FORECAST_BREACH", "COST_CLOUD_SVC_RATIO", "COST_STORAGE_SURGE",
    "COST_SERVERLESS_CREEP", "COST_CONTRACT_BREACH", "COST_DEPT_BUDGET_PACE",
    "PERF_QUERY_FAIL_PCT", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB",
    "PIPE_TASK_FAILURES", "PIPE_COPY_FAILURES",
    "SEC_FAILED_LOGINS", "SEC_CRED_EXPIRY", "SEC_BREAK_GLASS_USE",
)


def test_v7_every_rule_isolated_and_preserved():
    for rule in _ALL_RULES:
        assert rule in _V017, rule
    # one isolated INSERT + one exception handler per block (the 18th mention
    # of the token lives inside the OPS_SCAN_DEGRADED detail text)
    assert _V017.count("'rule_block_failed', :emsg") == len(_ALL_RULES)
    assert _V017.count(") b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)") == len(_ALL_RULES)
    assert "'OPS_SCAN_DEGRADED'" in _V017
    assert "alert scan v7 complete" in _V017
    # budget blocks kept their mtd CTE
    assert _V017.count("mtd AS (") >= 2


def test_v7_dedupe_semantics_preserved():
    assert _V017.count("WHERE e.DEDUPE_KEY = b.DEDUPE_KEY") == len(_ALL_RULES)


def test_version_guard_and_stage():
    assert "RAISE not_ready" in _V017
    assert "SCHEMA_VERSION < 16" in _V017
    assert "CREATE STAGE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE" in _V017
    assert "DIRECTORY = (ENABLE = TRUE)" in _V017
    yml = (_ROOT / "snowflake.yml").read_text(encoding="utf-8")
    assert "stage: DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE" in yml


def test_render_sla_and_usage_retention():
    assert "'OPS_SLOW_RENDER'" in _V017
    assert "APPROX_PERCENTILE(RENDER_MS, 0.95)" in _V017
    assert "HAVING COUNT(*) >= 20" in _V017            # sample floor
    assert "render_check_unavailable" in _V017          # guarded
    assert "ADD COLUMN IF NOT EXISTS RENDER_MS" in _V017
    assert "APP_USAGE_RETENTION_DAYS" in _V017
    assert "usage_days := GREATEST(usage_days, 90)" in _V017
    assert "SELECT 17 AS VERSION" in _V017


def test_fix_targets_and_render_capture():
    from app.logic.navigate import fix_target

    t = fix_target("COST_CLOUD_SVC_RATIO", "WH_TRXS_TRANSFORM ratio 31%")
    assert t == {"page": "Cost & Contract", "section": "Optimization & Savings",
                 "filters": {"warehouse_contains": "WH_TRXS_TRANSFORM"}}
    assert fix_target("SEC_CRED_EXPIRY") is None        # no mechanical fix

    main_src = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "_render_started = time.perf_counter()" in main_src
    assert "RENDER_MS" in main_src
    alerts_src = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert "Generate fix →" in alerts_src
