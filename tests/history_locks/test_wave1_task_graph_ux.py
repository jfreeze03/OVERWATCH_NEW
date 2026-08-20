"""Wave 1 locks: coherent task graphs, scannable scope, and shell accessibility."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import ops_sql
from app.logic.task_graph import inspect_task_graph, parse_task_predecessors
from app.ui.charts import _task_dag_markup
from app.ui.components import filter_contract_text

_ROOT = Path(__file__).resolve().parents[2]


def _src(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _graph_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "TASK_FQN": "DB.OPS.ROOT",
                "PREDECESSORS": [],
                "STATE": "started",
                "WAREHOUSE_NAME": "ETL_WH",
                "SCHEDULE": "USING CRON 0 * * * * UTC",
                "RECENT_FAILURES": 0,
            },
            {
                "TASK_FQN": "DB.OPS.LOAD_STAGE",
                "PREDECESSORS": '["DB.OPS.ROOT"]',
                "STATE": "started",
                "WAREHOUSE_NAME": "ETL_WH",
                "SCHEDULE": None,
                "RECENT_FAILURES": 1,
            },
            {
                "TASK_FQN": "DB.OPS.PUBLISH",
                "PREDECESSORS": ['"DB"."OPS"."LOAD_STAGE"'],
                "STATE": "suspended",
                "WAREHOUSE_NAME": None,
                "SCHEDULE": None,
                "RECENT_FAILURES": 0,
            },
        ]
    )


def test_task_graph_sql_selects_one_complete_current_snapshot():
    sql = ops_sql.task_graph_nodes("root-id", 7)
    assert "SNOWFLAKE.ACCOUNT_USAGE.TASK_VERSIONS" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.TASKS" in sql
    assert "WAREHOUSE AS WAREHOUSE_NAME" in sql
    assert "DELETED IS NULL" in sql
    assert "PARTITION BY ROOT_TASK_ID" in sql
    assert "g.ROOT_TASK_ID = v.ROOT_TASK_ID" in sql
    assert "g.GRAPH_VERSION = v.GRAPH_VERSION" in sql
    assert "v.ROOT_TASK_ID = 'root-id'" in sql
    assert "v.GRAPH_VERSION = 7" in sql
    assert "COUNT(*) OVER () AS SNAPSHOT_NODE_COUNT" in sql
    assert "LIMIT 300" not in sql.upper()


def test_task_graph_sql_escapes_root_and_rejects_bad_versions():
    sql = ops_sql.task_graph_nodes("root'id", 2)
    assert "'root''id'" in sql
    try:
        ops_sql.task_graph_nodes("root", -1)
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("negative graph versions must be rejected")


def test_task_graph_parser_normalizes_snowflake_array_representations():
    assert parse_task_predecessors(None) == ()
    assert parse_task_predecessors('["DB.OPS.ROOT"]') == ("DB.OPS.ROOT",)
    assert parse_task_predecessors(['"DB"."OPS"."ROOT"']) == (
        '"DB"."OPS"."ROOT"',
    )
    frame = _graph_frame()
    shape = inspect_task_graph(frame["TASK_FQN"], frame["PREDECESSORS"])
    assert shape.edges == (
        ("DB.OPS.LOAD_STAGE", "DB.OPS.PUBLISH"),
        ("DB.OPS.ROOT", "DB.OPS.LOAD_STAGE"),
    )
    assert dict(shape.levels) == {
        "DB.OPS.ROOT": 0,
        "DB.OPS.LOAD_STAGE": 1,
        "DB.OPS.PUBLISH": 2,
    }
    assert not shape.missing_predecessors
    assert not shape.cyclic_nodes


def test_task_graph_integrity_reports_every_unsafe_shape():
    missing = inspect_task_graph(["A", "B"], [[], ["MISSING"]])
    duplicate = inspect_task_graph(["A", "A"], [[], []])
    cyclic = inspect_task_graph(["A", "B"], [["B"], ["A"]])
    assert missing.missing_predecessors == ("MISSING",)
    assert duplicate.duplicate_nodes == ("A",)
    assert cyclic.cyclic_nodes == ("A", "B")


def test_task_graph_workbench_has_dependency_free_pan_zoom_and_exports():
    frame = _graph_frame()
    shape = inspect_task_graph(frame["TASK_FQN"], frame["PREDECESSORS"])
    markup = _task_dag_markup(frame, shape, height=640)
    for control in ("zoomIn", "zoomOut", "fit", "full", "download"):
        assert f'id="{control}"' in markup
    assert 'role="toolbar"' in markup
    assert 'svg id="dag" tabindex="0" role="img"' in markup
    assert "requestFullscreen" in markup
    assert 'addEventListener("wheel"' in markup
    assert 'addEventListener("pointermove"' in markup
    assert 'link.download = "overwatch-task-graph.svg"' in markup
    assert "Math.max(0.002" in markup
    assert "<script src" not in markup and "https://" not in markup


def test_operations_refuses_partial_or_invalid_graphs_before_rendering():
    source = _src("app/ui/pages/operations.py")
    sql = ops_sql.task_graph_nodes()
    for column in ("TASK_FQN", "PREDECESSORS", "STATE", "RECENT_FAILURES"):
        assert f'frame["{column}"]' in source
        assert column in sql
    assert "max_rows=2_000" in source
    assert "graph.truncated or expected != len(frame)" in source
    assert "shape.missing_predecessors" in source
    assert "shape.duplicate_nodes" in source
    assert "shape.cyclic_nodes" in source
    assert "charts.interactive_task_dag" in source
    assert "st.graphviz_chart" in source
    assert 'nested_sections(\n        ["Health", "SLA", "Graph", "Runs"]' in source


def test_overview_orders_economics_risk_actions_drivers_then_context():
    source = _src("app/ui/pages/overview.py")
    ordered = (
        'section_header("Company economics"',
        'section_header("Account risk & contract"',
        'section_header("Top actions")',
        'section_header("Top cost drivers")',
        'section_header("Monthly spend by warehouse")',
        'section_header("Spend trend")',
    )
    positions = [source.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    assert source.count("section_filter_contract(") >= len(ordered)
    risk_contract = source.split('section_header("Account risk & contract"', 1)[1]
    risk_contract = risk_contract.split("kpi_row(account_kpis)", 1)[0]
    assert 'applies=(),\n        partial=("company",)' in risk_contract


def test_filter_contract_names_applied_partial_and_ignored_dimensions():
    text = filter_contract_text(
        {
            "company": "ALFA",
            "days": 30,
            "database": "ALFA_EDW_PRD",
            "warehouse_contains": "ETL",
            "user_contains": "DBA",
        },
        applies=("company", "days"),
        partial=("database",),
        note="Fixed grain.",
    )
    assert "Applies:" in text and "Company" in text and "Window" in text
    assert "Panel-dependent: Database" in text
    assert "Active but ignored:" in text
    assert "Warehouse" in text and "User" in text
    assert text.endswith("Fixed grain.")


def test_shell_semantics_wrapping_and_runtime_target_are_pinned():
    main = _src("app/main.py")
    components = _src("app/ui/components.py")
    icons = _src("app/ui/icons.py")
    theme = _src("app/theme.py")
    environment = _src("environment.yml")
    assert "def _health_strip(" not in main
    assert "def _persistent_status_bar(" in main
    assert '"target": _target(' in main
    assert "request_navigation(page, section)" in components
    assert '<h1>{html.escape(title)}</h1>' in components
    assert '<h2 class="ow-section' in components
    assert '<span class="ow-section__title">' in components
    assert 'aria-hidden="true" focusable="false"' in icons
    assert 'div[data-testid="stButtonGroup"] div[data-baseweb="button-group"]' in theme
    assert "flex-wrap:wrap !important" in theme
    assert 'button:focus-visible' in theme
    assert "streamlit=1.52.2" in environment
