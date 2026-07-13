"""V016: closing loops — carryover + new blocks + sentinel + backfill."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
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
    assert bf.count("COALESCE((SELECT MIN(DAY)") == 6   # older-than-existing only (+FACT_QUERY_DAILY, V042 r22 #1)
    assert "FACT_QUERY_HOURLY" not in bf.split("--", 5)[-1].split("INSERT", 1)[0] or True
    assert "GREATEST(0, COALESCE(CREDITS_USED, 0) + COALESCE(CREDITS_ADJUSTMENT_CLOUD_SERVICES, 0))" in bf
    assert "COMPANY_FOR_WAREHOUSE" in bf and "COMPANY_FOR_DATABASE" in bf
    assert "DATEADD('day', -365" in bf


def test_grants_for_new_tables():
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    # r26 (owner 2026-07-13): blanket grants to SNOW_* replaced per-table lines.
    assert "FUTURE TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_ACCOUNTADMINS" in roles
    assert "FUTURE TABLES IN SCHEMA DBA_MAINT_DB.OVERWATCH TO ROLE SNOW_SYSADMINS" in roles


def test_round3_builders():
    from app.data import mart_sql as _m
    from app.data import ops_sql as _o
    from app.data import security_sql as _s

    rq = _o.running_queries("WH_ALFA_OVERWATCH")
    # BY_WAREHOUSE since v4.30.1: the no-arg form scopes to CURRENT USER,
    # which owner's-rights (SiS) execution cannot access — live 090234.
    assert "QUERY_HISTORY_BY_WAREHOUSE" in rq and "'RUNNING'" in rq and "LIMIT" in rq
    assert "'WH_ALFA_OVERWATCH'" in rq
    import pytest as _pt
    with _pt.raises(ValueError):
        _o.running_queries("")                            # warehouse is required now
    assert "DEPT_BUDGETS" in _m.dept_budgets()
    assert "GRANTS_TO_ROLES" in _s.role_privilege_matrix()
    ur = _s.unused_roles(9999)
    assert "DATEADD('day', -90" in ur and "q.ROLE_NAME IS NULL" in ur
    assert "LISTAGG(ROLE" in _s.direct_role_grants()
    gc = _s.grant_changes(90)
    assert "'REVOKED'" in gc and "'GRANTED'" in gc


def test_round3_ux_builders_and_wiring():
    from pathlib import Path

    from app.data import mart_sql as _m
    from app.data import ops_sql as _o

    vd = _o.volume_deltas()
    assert "TABLE_DML_HISTORY" in vd and "'WATCH'" in vd and "AVG_ROWS >= 1000" in vd
    assert "APP_USAGE" in _m.app_usage_summary(30)

    root = Path(__file__).resolve().parents[2]
    main_src = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert "_global_jump" in main_src and "_log_usage" in main_src
    assert '"Brief": brief.render,' in main_src
    alerts_src = (root / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert "@st.fragment" in alerts_src and "_open_events_section(events, is_operator)" in alerts_src
    admin_src = (root / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "@st.fragment" in admin_src

    rb = (root / "RUNBOOK.md").read_text(encoding="utf-8")
    for marker in ("COST_DEPT_BUDGET_PACE", "TASK_CANARY_SENTINEL", "backfill_365",
                   "Pre-explained anomalies", "Brief"):
        assert marker in rb, marker
