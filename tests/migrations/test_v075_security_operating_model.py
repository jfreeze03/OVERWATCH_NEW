"""V075 security operating-model and app-performance regression locks."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import app.core.query as query
from app.core.result import QueryResult
from app.data import mart_sql, security_sql
from app.logic.security import (
    domain_posture,
    escalation_flags,
    fact_coverage_complete,
    grant_anomaly_flags,
)
from app.ui.security_center import _cell_text

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (_ROOT / "snowflake/migrations/V075__security_operating_model.sql").read_text(
    encoding="utf-8"
)


def _read(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_v075_is_guarded_versioned_and_never_provisions_compute() -> None:
    assert "EXCEPTION (-20075" in _MIGRATION
    assert "IF (v < 74)" in _MIGRATION
    assert "SELECT 75," in _MIGRATION
    assert "WHERE VERSION = 75" in _MIGRATION
    assert "CREATE WAREHOUSE" not in _MIGRATION
    assert "ALTER WAREHOUSE" not in _MIGRATION
    assert "RESOURCE_MONITOR" not in _MIGRATION


def test_v075_materializes_bounded_evidence_and_serializes_its_task() -> None:
    for name in (
        "FACT_SECURITY_LOGIN_DAILY",
        "FACT_SECURITY_CHANGE",
        "SECURITY_TRUST_SNAPSHOT",
        "SP_LOAD_SECURITY_FACTS",
        "V_SECURITY_TRUST_DELTA",
        "V_SECURITY_EXCEPTION_QUEUE",
    ):
        assert name in _MIGRATION
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_MARTS_V27_HOURLY" in _MIGRATION
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(3)" in _MIGRATION
    assert "IF (applied = 0 OR fact_rows = 0)" in _MIGRATION
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(90)" in _MIGRATION
    assert "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT" in _MIGRATION
    assert "IF (d <= 3)" in _MIGRATION
    assert _MIGRATION.count("DATEADD('day', -:d, CURRENT_DATE())") >= 5
    assert "WHERE DAY < DATEADD('day', -180, CURRENT_DATE())" in _MIGRATION
    assert "WHERE DAY < DATEADD('day', -400, CURRENT_DATE())" in _MIGRATION
    assert "WITH current_findings AS" in _MIGRATION
    assert "SELECT CURRENT_DATE(), p.SCANNER_ID" in _MIGRATION
    assert "TOTAL_AT_RISK_COUNT, SCANNED_AT" in _MIGRATION


def test_v075_is_read_only_posture_with_no_write_surfaces() -> None:
    # v4.146.0 trimmed Security to read-only posture. The policy-editor and
    # access-review write surfaces must NOT be created by this migration.
    for gone in (
        "SECURITY_IDENTITY_POLICY",
        "SECURITY_EGRESS_POLICY",
        "SECURITY_CLIENT_POLICY",
        "ACCESS_REVIEW_CAMPAIGNS",
        "ACCESS_REVIEW_ITEMS",
        "ACCESS_REVIEW_DECISION_LOG",
        "SP_CREATE_ACCESS_REVIEW",
        "SP_ACCESS_REVIEW_DECIDE",
        "'ACCESS REVIEW'",  # the exception-queue arm that read the campaign table
    ):
        assert gone not in _MIGRATION, f"{gone} should have been removed from V075"
    # The kept exception-queue view still carries its evidence-backed arms.
    assert "'CHANGE RISK'" in _MIGRATION
    assert "'TRUST CENTER'" in _MIGRATION
    # SP_BACKUP_OPERATOR_TABLES stops at the V074 operator tables.
    backup = _MIGRATION.split("SP_BACKUP_OPERATOR_TABLES", 1)[1].split("$$;", 1)[0]
    assert "'SLO_OBJECTIVES'" in backup
    assert "SECURITY_IDENTITY_POLICY" not in backup


def test_security_domain_scores_require_proven_coverage() -> None:
    exceptions = pd.DataFrame(
        {
            "DOMAIN": ["IDENTITY", "DATA MOVEMENT"],
            "SEVERITY": ["HIGH", "CRITICAL"],
            "IMPACT_COUNT": [2, 50],
        }
    )
    coverage = pd.DataFrame(
        {
            "DOMAIN": ["IDENTITY", "DATA MOVEMENT"],
            "COVERAGE": ["COMPLETE", "ON_DEMAND"],
            "NEWEST": ["2026-08-04", None],
        }
    )
    by_domain = {item.domain: item for item in domain_posture(exceptions, coverage)}
    assert by_domain["IDENTITY"].score == 76
    assert by_domain["IDENTITY"].findings == 2
    assert by_domain["IDENTITY"].state == "Watch"
    assert by_domain["DATA MOVEMENT"].score is None
    assert by_domain["DATA MOVEMENT"].state == "On demand"

    # A domain reporting NOT_CONFIGURED coverage scores None and reads "Not
    # configured". Kept as a defensive mapping even though, after the access-
    # review surface was removed, no live coverage source emits NOT_CONFIGURED.
    not_configured = pd.DataFrame(
        {"DOMAIN": ["TRUST CENTER"], "COVERAGE": ["NOT_CONFIGURED"], "NEWEST": [None]}
    )
    review = {item.domain: item for item in domain_posture(pd.DataFrame(), not_configured)}[
        "TRUST CENTER"
    ]
    assert review.score is None
    assert review.state == "Not configured"


def test_nullable_security_cells_never_become_fake_drill_ids() -> None:
    assert _cell_text(None) == ""
    assert _cell_text(float("nan")) == ""
    assert _cell_text(pd.NA, "fallback") == "fallback"
    assert _cell_text(" QID-1 ") == "QID-1"


def test_fact_coverage_requires_span_and_freshness(monkeypatch) -> None:
    monkeypatch.setattr("app.logic.security.account_today", lambda: pd.Timestamp("2026-08-04").date())
    complete = QueryResult(
        df=pd.DataFrame({"COVERAGE_DAYS": [30], "LAST_DAY": ["2026-08-03"]}), ok=True
    )
    stale = QueryResult(
        df=pd.DataFrame({"COVERAGE_DAYS": [30], "LAST_DAY": ["2026-07-30"]}), ok=True
    )
    thin = QueryResult(
        df=pd.DataFrame({"COVERAGE_DAYS": [3], "LAST_DAY": ["2026-08-04"]}), ok=True
    )
    ninety = QueryResult(
        df=pd.DataFrame({"COVERAGE_DAYS": [90], "LAST_DAY": ["2026-08-04"]}), ok=True
    )
    assert fact_coverage_complete(complete, 30)
    assert not fact_coverage_complete(stale, 30)
    assert not fact_coverage_complete(thin, 30)
    assert fact_coverage_complete(ninety, 90)
    assert not fact_coverage_complete(complete, 90)


def test_effective_access_exposes_admin_reach() -> None:
    # Sec2: the recursive path reader must publish whether it inherits an admin
    # role, so the escalation scorer can flag self-escalation surfaces.
    sql = security_sql.effective_access("ALFA")
    assert "AS REACHES_ADMIN" in sql
    assert "'ACCOUNTADMIN'" in sql and "'SECURITYADMIN'" in sql
    assert "AS RISK_SCORE" in sql


def test_escalation_flags_scores_self_escalation_paths() -> None:
    # The base is the OBJECT-privilege exposure (ownership*10 + sensitive*20), NOT the
    # RISK_SCORE column — RISK_SCORE also weights manage grants, so reusing it would
    # double-count manage against the self-escalation bump (bug-hunt 2026-08-29).
    frame = pd.DataFrame(
        {
            "USER_NAME": ["A", "A", "B", "C"],
            "OWNERSHIP_GRANTS": [3, 0, 1, 0],
            "SENSITIVE_PRIVILEGES": [0, 1, 0, 0],
            "DEPTH": [3, 1, 0, 0],
            "MANAGE_GRANTS": [2, 0, 0, 1],
            "REACHES_ADMIN": [True, True, False, False],
        }
    )
    scored = escalation_flags(frame)
    # MANAGE GRANTS on the effective role IS the self-escalation privilege.
    # Row 0: reaches admin AND manages grants -> flagged.
    assert bool(scored.loc[0, "SELF_ESCALATION"]) is True
    # Row 1: reaches admin but holds no MANAGE GRANTS -> cannot self-grant here.
    assert bool(scored.loc[1, "SELF_ESCALATION"]) is False
    # Row 3: never reaches admin today, but MANAGE GRANTS lets it grant itself
    # admin -> flagged. (The bug this replaces AND-ed the two facts row-wise and
    # would have missed this genuine escalation surface.)
    assert bool(scored.loc[3, "SELF_ESCALATION"]) is True
    # Row 0: object 30 + depth 12 + admin 25 + self-escalate 40 = 107 -> clipped 100.
    assert int(scored.loc[0, "ESCALATION_SCORE"]) == 100
    assert int(scored.loc[0, "ESCALATION_SCORE"]) > int(scored.loc[1, "ESCALATION_SCORE"])
    # Double-count guard (bug-hunt): row 3 is a pure MANAGE-GRANTS-only path (no
    # object privileges, no depth, no admin reach) -> exactly the +40 self-escalation
    # bump, NOT 40 + a second manage weight. Was 65 when RISK_SCORE (which weights
    # MANAGE_GRANTS*25) was summed with the bump.
    assert int(scored.loc[3, "ESCALATION_SCORE"]) == 40
    # A pathless frame returns the columns without raising.
    empty = escalation_flags(pd.DataFrame())
    for col in ("REACHES_ADMIN", "SELF_ESCALATION", "ESCALATION_SCORE"):
        assert col in empty.columns


def test_security_center_wires_escalation_scoring() -> None:
    center = _read("app/ui/security_center.py")
    assert "escalation_flags(result.df" in center
    assert "Can self-escalate to admin" in center
    assert 'MAX_ESCALATION=("ESCALATION_SCORE", "max")' in center
    assert 'SELF_ESCALATE=("SELF_ESCALATION", "any")' in center


def test_admin_grant_context_reads_admin_grants_with_time_context() -> None:
    sqlglot = pytest.importorskip("sqlglot")
    sql = security_sql.admin_grant_context(90, "ALFA")
    sqlglot.parse(sql, dialect="snowflake")
    for col in ("ADMIN_ROLE", "USER_NAME", "GRANTED_AT", "PRIOR_GRANTS",
                "DOW_ISO", "HOUR_OF_DAY"):
        assert col in sql, col
    assert "SNOW_ACCOUNTADMINS" in sql and "ACCOUNTADMIN" in sql
    # First-time detection reads the SAME grant history (retains revoked rows),
    # so a re-grant is not miscounted as a first-ever elevation.
    assert "p.CREATED_ON < g.CREATED_ON" in sql
    assert "COMPANY_FOR_USER" in sql and "'ALFA'" in sql   # company-scoped
    # Account-wide scope carries no user filter.
    assert "COMPANY_FOR_USER" not in security_sql.admin_grant_context(90, "ALL")


def test_grant_anomaly_flags_scores_first_time_and_off_hours() -> None:
    frame = pd.DataFrame(
        {
            "ADMIN_ROLE": ["ACCOUNTADMIN"] * 4,
            "USER_NAME": ["A", "B", "C", "D"],
            "PRIOR_GRANTS": [0, 3, 2, 0],
            "DOW_ISO": [3, 6, 2, 2],       # 6 = Saturday
            "HOUR_OF_DAY": [10, 14, 3, 11],  # 3 = pre-business
        }
    )
    flagged = grant_anomaly_flags(frame)
    # A: first-ever grant on a weekday afternoon -> FIRST_TIME (HIGH), anomalous.
    assert bool(flagged.loc[0, "FIRST_TIME"]) is True
    assert flagged.loc[0, "SEVERITY"] == "HIGH"
    # B: renewal (prior grants) but on a Saturday -> off-hours only (MEDIUM).
    assert bool(flagged.loc[1, "FIRST_TIME"]) is False
    assert bool(flagged.loc[1, "OFF_HOURS"]) is True
    assert flagged.loc[1, "SEVERITY"] == "MEDIUM"
    # C: renewal at 3am weekday -> off-hours (MEDIUM), not first-time.
    assert bool(flagged.loc[2, "OFF_HOURS"]) is True
    assert flagged.loc[2, "SEVERITY"] == "MEDIUM"
    # D: first-ever during business hours -> HIGH regardless of clock.
    assert flagged.loc[3, "SEVERITY"] == "HIGH"
    # Empty frame returns the columns without raising.
    empty = grant_anomaly_flags(pd.DataFrame())
    for col in ("FIRST_TIME", "OFF_HOURS", "ANOMALY", "SEVERITY", "REASON"):
        assert col in empty.columns


def test_security_center_wires_admin_grant_timing() -> None:
    center = _read("app/ui/security_center.py")
    page = _read("app/ui/pages/security.py")
    assert "grant_anomaly_flags(result.df" in center
    assert "First-ever elevations" in center
    assert "render_admin_grant_anomalies(company)" in page


def test_new_readers_parse_and_preserve_expected_shapes() -> None:
    sqlglot = pytest.importorskip("sqlglot")
    builders = (
        security_sql.security_exception_queue("ALFA"),
        security_sql.security_domain_coverage(),
        security_sql.trust_center_delta(),
        security_sql.login_fact_coverage(30),
        security_sql.security_login_fact_coverage(30),
        security_sql.failed_logins_fact(7, "ALFA"),
        security_sql.failed_login_reasons_fact(7, "ALFA"),
        security_sql.new_network_logins_fact(7, "ALFA"),
        security_sql.recent_ddl_changes_fact(7, "ALFA"),
        security_sql.admin_role_activity_fact(7, "ALFA"),
        security_sql.effective_access("ALFA"),
        security_sql.egress_baseline(7),
        mart_sql.app_performance_slo(7),
    )
    for statement in builders:
        sqlglot.parse(statement, dialect="snowflake")
    assert "QUERY_ID" in security_sql.recent_ddl_changes_fact(7, "ALFA")
    assert "TARGET_LOCATION" in security_sql.unload_activity(7, "ALFA")
    fact_changes = security_sql.recent_ddl_changes_fact(7, "ALFA")
    assert "COMPANY_FOR_USER(g.USER_NAME) = 'ALFA'" in fact_changes
    assert "g.DATABASE_NAME ILIKE 'ALFA%'" in fact_changes
    assert "THEN 'NOT_APPLICABLE'" in fact_changes
    for changes in (fact_changes, security_sql.recent_ddl_changes(7, "ALFA")):
        assert "EXISTS (" not in changes
        assert "LEFT JOIN DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r" in changes
        assert "QUALIFY ROW_NUMBER() OVER" in changes
        assert "ABS(DATEDIFF('hour', r.CHANGE_SEEN_AT, g.LAST_CHANGE)) <= 24" in changes
        assert changes.index("GROUP BY 1, 2, 3, 4, 5, 6") < changes.index("COMPANY_FOR_USER")
    queue = security_sql.security_exception_queue("ALFA")
    assert "ACTOR_COMPANY" in queue and "OBJECT_COMPANY" in queue
    assert "UPPER(COALESCE(ACTOR_COMPANY, '')) = 'ALFA'" in queue
    for scoped in (
        security_sql.admin_role_holders("ALFA"),
        security_sql.admin_role_activity(7, "ALFA"),
        security_sql.new_network_logins(7, "ALFA"),
        security_sql.new_network_logins_fact(7, "ALFA"),
    ):
        assert "COMPANY_FOR_USER" in scoped
        assert "'ALFA'" in scoped
    perf = mart_sql.app_performance_slo(7)
    assert "RENDER_SAMPLES < 20" in perf
    assert "NOT STARTSWITH(QUERY_KEY, 'batch_wall:')" in perf
    assert "SUM(IFF(NOT OK, 1.0 / COALESCE(SAMPLE_PROB, 1.0), 0))" in perf
    domain_sql = security_sql.security_domain_coverage()
    # v4.146.0 removed the access-review write surface, so the domain-coverage
    # builder no longer probes the removed ACCESS_REVIEW_CAMPAIGNS table.
    assert "ACCESS REVIEW" not in domain_sql
    assert "ACCESS_REVIEW_CAMPAIGNS" not in domain_sql
    assert domain_sql.count("SNAPSHOT_TS >= DATEADD('hour', -3, CURRENT_TIMESTAMP())") == 2


def test_large_batches_are_executed_in_waves_of_four(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake(chunk: tuple, tier: str, page: str, *, timeout_s=None) -> tuple:  # #58: signature parity
        calls.append(chunk)
        return tuple(pd.DataFrame({"VALUE": [value]}) for value in chunk)

    monkeypatch.setattr(query, "_execute_batch", fake)
    frames = query._execute_batch_bounded(tuple(range(10)), "recent", "Security")
    assert [len(chunk) for chunk in calls] == [4, 4, 2]
    assert [int(frame.iloc[0]["VALUE"]) for frame in frames] == list(range(10))


def test_bounded_batch_preserves_partial_indices_and_finishes_later_waves(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake(chunk: tuple, tier: str, page: str, *, timeout_s=None) -> tuple:  # #58: signature parity
        calls.append(chunk)
        if chunk[0] == 0:
            raise query._BatchPartial(
                {
                    0: pd.DataFrame({"VALUE": [0]}),
                    2: pd.DataFrame({"VALUE": [2]}),
                },
                {1: RuntimeError("member failed")},
                {3},
            )
        return tuple(pd.DataFrame({"VALUE": [value]}) for value in chunk)

    monkeypatch.setattr(query, "_execute_batch", fake)
    with pytest.raises(query._BatchPartial) as caught:
        query._execute_batch_bounded(tuple(range(6)), "recent", "Security")
    assert calls == [(0, 1, 2, 3), (4, 5)]
    assert set(caught.value.frames) == {0, 2, 4, 5}
    assert set(caught.value.errors) == {1}
    assert caught.value.pending == {3}


def test_batch_member_cache_reuses_unchanged_siblings(monkeypatch) -> None:
    import streamlit as st

    calls: list[tuple] = []

    def fake_fetch(sqls: tuple, scope: str, page: str) -> tuple:
        calls.append(sqls)
        return tuple(pd.DataFrame({"SQL": [statement]}) for statement in sqls)

    query._batch_member_cache_clear()
    st.session_state["_ow_refresh_salt"] = "v075-cache-test"
    st.session_state["_ow_current_role"] = "ROLE_A"
    st.session_state["_ow_batch_quarantine"] = {"salt": "v075-cache-test", "keys": set()}
    monkeypatch.setitem(query._BATCH_FETCHERS, "recent", fake_fetch)
    monkeypatch.setattr(query, "_telemetry", lambda *args, **kwargs: None)
    monkeypatch.setattr(query, "record_error", lambda *args, **kwargs: None)
    first = query.run_batch(
        [{"key": "a", "sql": "SELECT 1"}, {"key": "b", "sql": "SELECT 2"}],
        page="Security",
        tier="recent",
    )
    second = query.run_batch(
        [{"key": "a", "sql": "SELECT 1"}, {"key": "c", "sql": "SELECT 3"}],
        page="Security",
        tier="recent",
    )
    assert second is not None
    second["a"].df.iloc[0, 0] = "MUTATED"
    third = query.run_batch(
        [{"key": "a", "sql": "SELECT 1"}], page="Security", tier="recent"
    )
    st.session_state["_ow_current_role"] = "ROLE_B"
    fourth = query.run_batch(
        [{"key": "a", "sql": "SELECT 1"}], page="Security", tier="recent"
    )
    assert first is not None and third is not None and fourth is not None
    assert [len(call) for call in calls] == [2, 1, 1]
    assert first["a"].df.iloc[0]["SQL"].startswith("SELECT 1")
    assert third["a"].df.iloc[0]["SQL"].startswith("SELECT 1")
    assert third["a"].df.iloc[0]["SQL"] != "MUTATED"
    query._batch_member_cache_clear()


def test_security_page_wires_decisions_drills_and_fact_fallbacks() -> None:
    page = _read("app/ui/pages/security.py")
    center = _read("app/ui/security_center.py")
    assert page.index("render_security_overview") < page.index("_governance_score_panel()")
    assert "security_login_fact_coverage" in page and "fact_coverage_complete" in page
    assert "fact_coverage_complete(security_coverage, 90)" in page
    assert "security_login_fact_coverage(90)" in page
    assert "admin_role_holders(company)" in page
    assert "new_network_logins_fact(days, company)" in page
    assert "_domain_covered(coverage, \"TRUST CENTER\")" in page
    assert 'active = pd.to_numeric(fdf["CURRENT_COUNT"]' in page
    assert "stable_batch = run_batch" in page and "batch = run_batch" in page
    assert page.count("snowsight_profile_column") >= 3
    assert "Show posture trend (90d)" in page
    assert "Account-wide governance sheets:" in page
    assert "with st.expander(\"Posture trend" not in page
    for marker in (
        "render_security_overview",
        "render_effective_access",
        '"Control Room", "Action Center"',
        '"Control Room", "Entity 360"',
        "No exceptions are queued from the evidence that resolved",
        'frame["SCOPE"] = frame.apply(_scope_label, axis=1)',
    ):
        assert marker in page + center


def test_deploy_and_rebuild_surfaces_track_v075() -> None:
    assert 'APP_VERSION = "4.390.0"' in _read("app/config.py")
    assert "## 4.146.0 - Security page trimmed to read-only posture" in _read(
        "CHANGELOG.md"
    )
    assert 'APP_WAREHOUSE = "WH_ALFA_ADMIN"' in _read("app/config.py")
    assert "query_warehouse: WH_ALFA_ADMIN" in _read("snowflake.yml")
    roles = _read("snowflake/roles.sql")
    assert roles.count("GRANT USAGE ON WAREHOUSE WH_ALFA_ADMIN") == 2
    assert "WH_OVERWATCH_APP" not in roles
    # v4.146.0 removed the access-review write surface, so its append-only seal
    # went with it; the platform audit tables stay sealed.
    assert roles.count(
        "REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT"
    ) == 2
    assert "ACCESS_REVIEW_DECISION_LOG" not in roles
    assert "V001..V117 applied" in _read("snowflake/validate.sql")
    assert "SP_LOAD_SECURITY_FACTS(90)" in _read("snowflake/backfill_365.sql")
    teardown = _read("snowflake/teardown.sql")
    for name in (
        "TASK_LOAD_SECURITY_FACTS",
        "FACT_SECURITY_LOGIN_DAILY",
        "SECURITY_TRUST_SNAPSHOT",
    ):
        assert name in teardown
    # The policy/review write tables were removed (v4.146.0) — teardown no
    # longer references them.
    assert "SECURITY_IDENTITY_POLICY" not in teardown
    assert "ACCESS_REVIEW_DECISION_LOG" not in teardown
    for active_surface in (
        "app/config.py",
        "app/data/mart_sql.py",
        "app/ui/pages/admin.py",
        "app/ui/pages/brief.py",
        "snowflake.yml",
        "snowflake/migrations/V075__security_operating_model.sql",
        "snowflake/roles.sql",
        "snowflake/teardown.sql",
        "snowflake/validate.sql",
        "DEPLOYMENT.md",
        "README.md",
        "docs/FULL_REBUILD.md",
    ):
        assert "WH_OVERWATCH_APP" not in _read(active_surface)
    assert "V075__security_operating_model.sql" in _read("DEPLOYMENT.md")
    assert "V075__security_operating_model.sql" in _read("README.md")


def test_hidden_expander_reads_are_now_explicitly_gated() -> None:
    admin = _read("app/ui/pages/admin.py")
    optimize = _read("app/ui/pages/cost_parts/optimize.py")
    assert 'st.toggle("Diagnose stale sources"' in admin
    assert 'with st.expander("Why stale?' not in admin
    what_if = optimize.split("def _whatif_panel", 1)[1].split("\ndef ", 1)[0]
    assert "whatif_live_settings" in what_if
    assert "if load_settings else None" in what_if
