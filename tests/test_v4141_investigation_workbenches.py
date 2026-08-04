"""v4.141 locks for universal investigation and task-DAG diagnostics."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import ops_sql, workbench_sql
from app.logic.task_graph import analyze_task_run, compare_task_versions, inspect_task_graph
from app.ui import charts

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_task_run_diagnostics_find_longest_dependency_path() -> None:
    topology = pd.DataFrame(
        {
            "TASK_FQN": ["DB.S.A", "DB.S.B", "DB.S.C", "DB.S.D"],
            "PREDECESSORS": [[], ["DB.S.A"], ["DB.S.A"], ["DB.S.B", "DB.S.C"]],
        }
    )
    run = pd.DataFrame(
        {
            "TASK_FQN": ["DB.S.A", "DB.S.B", "DB.S.C", "DB.S.D"],
            "RUN_SEC": [1.0, 5.0, 2.0, 3.0],
            "QUEUE_SEC": [0.0, 1.0, 0.0, 0.0],
            "EXEC_SEC": [1.0, 4.0, 2.0, 3.0],
            "STATE": ["SUCCEEDED"] * 4,
        }
    )
    shape = inspect_task_graph(topology["TASK_FQN"], topology["PREDECESSORS"])

    result = analyze_task_run(run, shape).set_index("TASK_FQN")

    assert result["CRITICAL_PATH"].to_dict() == {
        "DB.S.A": True,
        "DB.S.B": True,
        "DB.S.C": False,
        "DB.S.D": True,
    }
    assert result["PATH_SEC"].to_dict() == {
        "DB.S.A": 1.0,
        "DB.S.B": 6.0,
        "DB.S.C": 3.0,
        "DB.S.D": 9.0,
    }
    assert result["DOWNSTREAM_TASKS"].to_dict() == {
        "DB.S.A": 3,
        "DB.S.B": 1,
        "DB.S.C": 1,
        "DB.S.D": 0,
    }


def test_task_version_diff_catches_added_removed_and_rewired() -> None:
    before = pd.DataFrame(
        {
            "TASK_FQN": ["DB.S.A", "DB.S.B", "DB.S.OLD"],
            "PREDECESSORS": [[], ["DB.S.A"], ["DB.S.B"]],
        }
    )
    after = pd.DataFrame(
        {
            "TASK_FQN": ["DB.S.A", "DB.S.B", "DB.S.NEW"],
            "PREDECESSORS": [[], [], ["DB.S.B"]],
        }
    )

    result = compare_task_versions(before, after).set_index("TASK_FQN")

    assert result.loc["DB.S.B", "CHANGE"] == "DEPENDENCIES_CHANGED"
    assert result.loc["DB.S.NEW", "CHANGE"] == "ADDED"
    assert result.loc["DB.S.OLD", "CHANGE"] == "REMOVED"
    assert "DB.S.A" not in result.index


@pytest.mark.parametrize(
    "builder",
    (
        lambda: ops_sql.task_graph_recent_runs("root'1", 999, 999),
        lambda: ops_sql.task_graph_run_nodes("root'1", "run'1", 999),
        lambda: ops_sql.task_graph_versions("root'1", 999),
        lambda: ops_sql.task_graph_version_nodes("root'1", 12),
    ),
)
def test_task_graph_investigation_sql_parses_prunes_and_escapes(builder) -> None:
    statement = builder()
    sqlglot.parse(statement, dialect="snowflake")
    assert "root'1" not in statement and "run'1" not in statement
    if "TASK_HISTORY" in statement:
        assert "SCHEDULED_TIME >= DATEADD('day', -14" in statement


@pytest.mark.parametrize(
    "kind",
    workbench_sql.ENTITY_METRIC_TYPES,
)
def test_every_entity_metric_snapshot_parses(kind: str) -> None:
    statement = workbench_sql.entity_metric_snapshot(kind, "O'Brien", 30)
    sqlglot.parse(statement, dialect="snowflake")
    assert "O'Brien" not in statement
    assert "METRIC" in statement and "BASIS" in statement and "AS_OF" in statement


def test_universal_search_and_exact_destination_contexts_are_wired() -> None:
    main = _source("app/main.py")
    operations = _source("app/ui/pages/operations.py")
    alerts = _source("app/ui/pages/alerts.py")
    control = _source("app/ui/pages/control_room.py")
    assert "investigation_target(target_kind, target_value)" in main
    assert "target.page not in pages" in main
    assert 'navigation_context().get("query_id")' in operations
    assert 'navigation_context().get("event_id")' in alerts
    assert 'navigation_context().get("incident_id")' in control


def test_task_graph_ui_has_run_diff_search_and_semantic_zoom() -> None:
    operations = _source("app/ui/pages/operations.py")
    charts = _source("app/ui/charts.py")
    for label in ("Run analyzer", "Version compare", "Critical path"):
        assert label.lower() in operations.lower()
    for contract in (
        'id="taskSearch"',
        'classList.toggle("compact"',
        'classList.toggle("tiny"',
        "focusNode",
        "critical-path",
    ):
        assert contract in charts


def test_fingerprint_profile_and_operational_replay_are_wired() -> None:
    optimize = _source("app/ui/pages/cost_parts/optimize.py")
    control = _source("app/ui/pages/control_room.py")
    charts = _source("app/ui/charts.py")
    assert '"entity_type": "QUERY_FINGERPRINT"' in optimize
    assert "selectable_nav_table(" in optimize
    assert "charts.operational_replay(tdf)" in control
    assert "selection_interval" in charts and "alt.vconcat(focus, overview" in charts


def test_operational_replay_compiles_real_lane_data(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda chart, **_kwargs: rendered.append(chart))
    frame = pd.DataFrame(
        {
            "AT": pd.to_datetime(["2026-08-03 09:00", "2026-08-03 09:12"]),
            "EVENT_TYPE": ["Alert", "Task failure"],
            "SEVERITY": ["CRITICAL", "HIGH"],
            "LABEL": ["Spend spike", "Loader failed"],
            "REF_ID": ["event-1", "query-1"],
        }
    )

    charts.operational_replay(frame)

    assert len(rendered) == 1
    spec = rendered[0].to_dict()
    assert "vconcat" in spec and len(spec["vconcat"]) == 2
