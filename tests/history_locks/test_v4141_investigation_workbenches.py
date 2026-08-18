"""v4.141 locks for universal investigation and task-DAG diagnostics."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import ops_sql, workbench_sql
from app.logic.task_graph import analyze_task_run, compare_task_versions, inspect_task_graph
from app.ui import charts

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]


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
    assert "charts.operational_replay(tdf" in control   # wired (CR9: now carries a credit overlay)
    assert "credits=" in control                          # CR9: hourly spend passed to the overlay
    assert "selection_interval" in charts and "alt.vconcat(focus, overview" in charts
    assert "alt.vconcat(spend, focus" in charts           # CR9: spend panel layered above events


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


def test_operational_replay_overlays_credits_on_a_shared_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    # CR9: a supplied credit series adds a spend panel that shares ONE explicit
    # x-domain with the events, so the two align on first paint without a brush.
    import json

    rendered = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda chart, **_kwargs: rendered.append(chart))
    frame = pd.DataFrame(
        {
            "AT": pd.to_datetime(["2026-08-03 08:00", "2026-08-03 12:00"]),  # wide event span
            "EVENT_TYPE": ["Alert", "Task failure"],
            "SEVERITY": ["CRITICAL", "HIGH"],
            "LABEL": ["Spend spike", "Loader failed"],
            "REF_ID": ["event-1", "query-1"],
        }
    )
    credits = pd.DataFrame(  # narrower credit span — must be widened to the union
        {"HOUR_TS": pd.to_datetime(["2026-08-03 09:00", "2026-08-03 10:00"]), "USD": [12.5, 41.0]}
    )
    charts.operational_replay(frame, credits=credits)
    spec = json.dumps(rendered[0].to_dict())
    assert "USD" in spec                                        # spend overlay present
    # both panels pin to the union extent [08:00, 12:00] — the fix for the
    # empty-brush independent-domain misalignment the review caught
    assert "2026-08-03T08:00:00" in spec and "2026-08-03T12:00:00" in spec

    # an empty / column-less credit frame overlays nothing (no USD) and never crashes
    rendered.clear()
    charts.operational_replay(frame, credits=pd.DataFrame())
    assert "USD" not in json.dumps(rendered[0].to_dict())


def test_spend_trend_click_returns_the_selected_day(monkeypatch: pytest.MonkeyPatch) -> None:
    # UI23: a selected bar returns that day's ISO date on EVERY run while it stays
    # selected (persistent, so the inline breakdown does not flicker away), and
    # None once the selection is cleared.
    monkeypatch.setattr(charts.st, "caption", lambda *a, **k: None)
    monkeypatch.setattr(charts.st, "altair_chart",
                        lambda *a, **k: {"selection": {"pt": [{"DayStr": "2026-08-15"}]}})
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-13", "2026-08-14", "2026-08-15"]),
                       "USD": [100.0, 120.0, 200.0]})
    assert charts.spend_trend(df, key="wh") == "2026-08-15"   # selected
    assert charts.spend_trend(df, key="wh") == "2026-08-15"   # persists across reruns
    monkeypatch.setattr(charts.st, "altair_chart", lambda *a, **k: {"selection": {"pt": []}})
    assert charts.spend_trend(df, key="wh") is None            # cleared -> breakdown disappears


def test_spend_trend_without_key_stays_non_clickable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(charts.st, "caption", lambda *a, **k: None)
    seen = {"on_select": 0}
    monkeypatch.setattr(charts.st, "altair_chart",
                        lambda *a, **k: seen.__setitem__("on_select", seen["on_select"] + ("on_select" in k)))
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-14", "2026-08-15"]), "USD": [1.0, 2.0]})
    assert charts.spend_trend(df, key=None) is None
    assert seen["on_select"] == 0                              # no on_select wiring without a key


def test_spend_trend_overlays_flagged_day_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    # UI15/Ov5: markers add a dashed-rule layer carrying the flagged day + label.
    import json

    rendered = []
    monkeypatch.setattr(charts.st, "caption", lambda *a, **k: None)
    monkeypatch.setattr(charts.st, "altair_chart", lambda c, **k: rendered.append(c))
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-13", "2026-08-14", "2026-08-15"]),
                       "USD": [100.0, 120.0, 900.0]})
    markers = pd.DataFrame({"DAY": ["2026-08-15"], "LABEL": ["WH_A, WH_B"]})
    charts.spend_trend(df, markers=markers)
    assert "WH_A, WH_B" in json.dumps(rendered[0].to_dict())   # marker label overlaid
    # no markers -> no marker layer
    rendered.clear()
    charts.spend_trend(df)
    assert "WH_A, WH_B" not in json.dumps(rendered[0].to_dict())


def test_spend_trend_click_survives_the_marker_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    # UI15 x UI23: markers make the chart multi-layer (trend + rule). The click
    # drill must still BUILD (add_params must not throw on the layered chart) and
    # return the selected day — Operations Warehouses passes both key and markers.
    monkeypatch.setattr(charts.st, "caption", lambda *a, **k: None)
    monkeypatch.setattr(
        charts.st, "altair_chart",
        lambda *a, **k: {"selection": {"pt": [{"DayStr": "2026-08-15"}]}} if "on_select" in k else None)
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-14", "2026-08-15"]), "USD": [1.0, 900.0]})
    markers = pd.DataFrame({"DAY": ["2026-08-15"], "LABEL": ["WH_A"]})
    assert charts.spend_trend(df, key="wh", markers=markers) == "2026-08-15"


def test_incident_gantt_renders_lifecycle_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    # CR5: detected->resolved span bars, colored by severity.
    import json

    rendered = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda c, **k: rendered.append(c))
    monkeypatch.setattr(charts, "_empty_note", lambda *a, **k: None)
    df = pd.DataFrame({
        "INCIDENT_ID": ["i1", "i2"],
        "TITLE": ["Loader stall", "Spend spike"],
        "SEVERITY": ["CRITICAL", "HIGH"],
        "STATUS": ["RESOLVED", "OPEN"],
        "STARTED": pd.to_datetime(["2026-08-14 01:00", "2026-08-15 09:00"]),
        "ENDED": pd.to_datetime(["2026-08-14 03:30", "2026-08-15 12:00"]),
        "DURATION_MIN": [150, 180],
    })
    charts.incident_gantt(df)
    spec = json.dumps(rendered[0].to_dict())
    assert '"x2"' in spec                              # a span bar (start -> end), not a point
    assert "Loader stall" in spec and "SEVERITY" in spec
    rendered.clear()
    charts.incident_gantt(pd.DataFrame())              # empty -> nothing rendered, no crash
    assert rendered == []
