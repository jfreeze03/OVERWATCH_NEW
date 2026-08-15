"""V074 locks for the persistent operating and investigation workbenches."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.data import workbench_sql
from app.logic.workbench import (
    action_summary,
    action_transition_sql,
    confidence_state,
    create_action_sql,
    entity_catalog_merge_sql,
    investigation_target,
    watchlist_sql,
)

sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = (
    _ROOT / "snowflake" / "migrations" / "V074__operating_workbench_foundation.sql"
).read_text(encoding="utf-8")


def test_v074_is_guarded_additive_and_versioned() -> None:
    assert "EXCEPTION (-20074" in _MIGRATION
    assert "IF (v < 73) THEN" in _MIGRATION
    assert "SELECT 74 AS VERSION" in _MIGRATION
    assert "WHERE VERSION = 74)" in _MIGRATION
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ACTION_LIFECYCLE" in _MIGRATION
    assert "BEGIN TRANSACTION" in _MIGRATION and "ROLLBACK" in _MIGRATION
    assert "REQUEST_KEY" in _MIGRATION and "DUPLICATE: request already applied" in _MIGRATION
    assert "CREATE TASK" not in _MIGRATION
    for name in (
        "ACTION_ACTIVITY",
        "EVIDENCE_LINKS",
        "ENTITY_CATALOG",
        "USER_WATCHLIST",
        "OPTIMIZATION_EXPERIMENTS",
        "SLO_OBJECTIVES",
    ):
        assert f"CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.{name}" in _MIGRATION


def test_v074_plain_sql_parses() -> None:
    from tests.test_migrations_parse import _plain_statements

    for statement in _plain_statements(_MIGRATION):
        sqlglot.parse(statement, dialect="snowflake")


@pytest.mark.parametrize(
    ("name", "builder"),
    (
        ("action_center", lambda: workbench_sql.action_center("ALFA", True)),
        ("action_activity", lambda: workbench_sql.action_activity("A'1")),
        ("evidence_links", lambda: workbench_sql.evidence_links("ACTION", "A'1")),
        ("entity_catalog", lambda: workbench_sql.entity_catalog("TASK", "ETL'ROOT")),
        ("entity_record", lambda: workbench_sql.entity_record("TASK", "DB.S.T")),
        ("related_actions", lambda: workbench_sql.related_actions("TASK", "DB.S.T")),
        ("related_remediations", lambda: workbench_sql.related_remediations("DB.S.T")),
        ("related_savings", lambda: workbench_sql.related_savings("DB.S.T")),
        ("watchlist", lambda: workbench_sql.watchlist("joe@example.com")),
        ("experiments", lambda: workbench_sql.experiments("RUNNING", "TASK", "DB.S.T")),
        ("slo_objectives", lambda: workbench_sql.slo_objectives(True, "TASK", "DB.S.T")),
    ),
)
def test_workbench_read_builders_parse_and_escape(name: str, builder) -> None:
    statement = builder()
    assert statement.strip(), name
    sqlglot.parse(statement, dialect="snowflake")
    assert "A'1" not in statement and "ETL'ROOT" not in statement


def test_action_summary_uses_real_non_empty_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.logic.workbench.account_today", lambda: date(2026, 8, 3))
    frame = pd.DataFrame(
        {
            "ACTION_ID": ["a", "b", "c", "d"],
            "STATUS": ["OPEN", "IN_PROGRESS", "DONE", "OPEN"],
            "SEVERITY": ["CRITICAL", "HIGH", "LOW", "MEDIUM"],
            "DUE_DATE": ["2026-08-02", "2026-08-04", "2026-08-01", None],
            "OWNER": [None, "DBA", "DBA", "UNASSIGNED"],
            "ESTIMATED_USD": [125.5, 74.5, 999.0, None],
        }
    )

    assert action_summary(frame) == {
        "open": 3.0,
        "critical_high": 2.0,
        "overdue": 1.0,
        "unassigned": 2.0,
        "estimated_usd": 200.0,
    }


def test_workbench_write_builders_validate_and_escape() -> None:
    transition = action_transition_sql(
        "A'1", status="IN_PROGRESS", owner="O'Brien", note="checked ' twice",
        request_key="request-1",
    )
    created = create_action_sql(
        title="Tune O'Brien workload", detail="Measured", company="ALFA",
        severity="HIGH", owner="DBA", due_date="2026-08-10", source="test",
        entity_type="TASK", entity_key="DB.S.T", confidence=1.5,
        estimated_usd=-1,
    )
    merged = entity_catalog_merge_sql(
        entity_type="TASK", entity_key="DB.S.T", label="Loader", company="ALFA",
        team="Data", owner="O'Brien", steward="", on_call="", criticality="HIGH",
        data_product="Finance", slo_name="Daily", notes="owner's task", actor="Joe",
    )
    watched = watchlist_sql("O'Brien", "TASK", "DB.S.T")

    for statement in (transition, created, merged, watched):
        sqlglot.parse(statement, dialect="snowflake")
        assert "O'Brien" not in statement
    assert "IN_PROGRESS" in transition and "request-1" in transition
    assert "'DB.S.T', 1.0, 0.0" in created
    with pytest.raises(ValueError):
        action_transition_sql("a", status="SURPRISE")


def test_confidence_and_universal_search_contracts() -> None:
    assert confidence_state(0.9)["state"] == "ok"
    assert confidence_state(0.7)["state"] == "warn"
    assert confidence_state(0.2)["state"] == "bad"
    assert confidence_state(0.9, stale=True)["label"] == "Stale evidence"
    assert confidence_state(0.9, coverage_pct=40)["label"] == "Low coverage"
    assert investigation_target("Query ID", "01abc").context == {"query_id": "01abc"}
    entity = investigation_target("Warehouse", "WH_ETL")
    assert (entity.page, entity.section) == ("Control Room", "Entity 360")
    assert entity.context == {"entity_type": "WAREHOUSE", "entity_key": "WH_ETL"}


def test_action_center_and_exact_navigation_are_wired() -> None:
    control = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(
        encoding="utf-8"
    )
    overview = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(
        encoding="utf-8"
    )
    state = (_ROOT / "app" / "core" / "state.py").read_text(encoding="utf-8")
    assert '"Action Center"' in control and "render_action_center(company)" in control
    assert 'context={"action_id": _action_id} if _action_id else {}' in overview
    assert 'st.session_state["_ow_nav_context"]' in state


def test_v074_coverage_surfaces_move_in_lockstep() -> None:
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    deploy = (_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for name in (
        "ACTION_ACTIVITY",
        "EVIDENCE_LINKS",
        "ENTITY_CATALOG",
        "USER_WATCHLIST",
        "OPTIMIZATION_EXPERIMENTS",
        "SLO_OBJECTIVES",
    ):
        assert name in teardown
    assert "V001..V080 applied" in validate
    assert "V074__operating_workbench_foundation.sql" in deploy
