"""v4.142 locks for prioritization, semantic cost bases and closed-loop decisions."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import workbench_sql
from app.logic.decision import prioritize_workloads, scenario_projection, slo_summary
from app.logic.workbench import create_slo_objective_sql, investigation_target
from app.ui import charts

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_portfolio_ranks_impact_confidence_effort_and_next_move() -> None:
    frame = pd.DataFrame(
        {
            "FINGERPRINT": ["hot", "broad", "thin"],
            "RUNS": [300, 60, 2],
            "FAILS": [0, 6, 0],
            "CREDITS": [100, 30, 1],
            "ACTIVE_DAYS": [30, 20, 1],
            "DATABASES": [1, 5, 1],
            "USERS": [2, 20, 1],
            "AVG_CACHE_PCT": [5, 80, 0],
            "P95_SEC": [2, 100, 1],
        }
    )

    result = prioritize_workloads(frame, 3.68, 30).set_index("FINGERPRINT")

    assert result.loc["hot", "LANE"] == "ACT NOW"
    assert result.loc["hot", "NEXT_MOVE"] == "Cache or materialize"
    assert result.loc["broad", "NEXT_MOVE"] == "Stabilize failures"
    assert result.loc["broad", "EFFORT_PROXY"] == "HIGH"
    assert result.loc["thin", "LANE"] == "VALIDATE"
    assert result.loc["hot", "IMPACT_USD_30D"] == 368.0


def test_portfolio_gates_missing_behavioral_evidence() -> None:
    # Decision-Studio #2: a top-priority, high-confidence family whose cache/P95/fails are
    # all NULL (a family-mart join miss) must NOT be coerced to 0 and driven to ACT NOW /
    # "Cache or materialize". The evidence gate forces VALIDATE instead.
    frame = pd.DataFrame(
        {
            "FINGERPRINT": ["blind", "filler"],
            "RUNS": [300, 10],
            "FAILS": [None, 0],
            "CREDITS": [1000, 5],
            "ACTIVE_DAYS": [30, 5],
            "DATABASES": [1, 1],
            "USERS": [2, 1],
            "AVG_CACHE_PCT": [None, 50.0],
            "P95_SEC": [None, 3.0],
        }
    )
    result = prioritize_workloads(frame, 3.68, 30).set_index("FINGERPRINT")
    # blind is the top-priority row with confidence >= 0.65 (so it would be ACT NOW), but
    # it carries NO behavioral evidence — the gate sends it to VALIDATE, not the ACT lane.
    assert result.loc["blind", "CONFIDENCE"] >= 0.65
    assert result.loc["blind", "LANE"] == "VALIDATE"
    assert result.loc["blind", "NEXT_MOVE"] == "Validate evidence"
    assert result.loc["blind", "EVIDENCE_COVERAGE"] == 0.0


def test_scenario_deduplicates_entities_and_separates_closed_work() -> None:
    actions = pd.DataFrame(
        {
            "ACTION_ID": ["a1", "a2", "b1", "closed"],
            "STATUS": ["OPEN", "OPEN", "IN_PROGRESS", "DONE"],
            "SOURCE_ENTITY_TYPE": ["WAREHOUSE"] * 4,
            "SOURCE_ENTITY_KEY": ["WH_A", "WH_A", "WH_B", "WH_C"],
            "CONFIDENCE": [0.9, 0.8, 0.7, 1.0],
            "ESTIMATED_USD": [100, 80, 50, 500],
        }
    )

    result = scenario_projection(
        actions, adoption_pct=50, realization_pct=80, confidence_floor=0.6,
    )

    assert result == {
        "candidates": 2.0,
        "gross_estimate": 150.0,
        "expected_capture": 60.0,
        "low_capture": 45.0,
        "high_capture": 75.0,
    }


def test_slo_summary_keeps_breach_and_missing_evidence_distinct() -> None:
    frame = pd.DataFrame(
        {
            "STATUS": ["MET", "BREACH", "NO_DATA"],
            "BURN_MULTIPLE": [0.4, 3.2, None],
        }
    )
    assert slo_summary(frame) == {
        "total": 3.0,
        "met": 1.0,
        "breach": 1.0,
        "no_data": 1.0,
        "worst_burn": 3.2,
    }


@pytest.mark.parametrize(
    "builder",
    (
        lambda: workbench_sql.workload_portfolio(30, "Trexis"),
        workbench_sql.slo_cockpit,
        lambda: workbench_sql.data_product_economics(30, "Trexis"),
        lambda: workbench_sql.cost_truth(30, "Trexis"),
    ),
)
def test_decision_sql_builders_parse(builder) -> None:
    sqlglot.parse(builder(), dialect="snowflake")


def test_workload_company_scope_applies_to_behavior_and_cost() -> None:
    statement = workbench_sql.workload_portfolio(30, "Trexis")
    assert statement.count("p.COMPANY = 'Trexis'") == 2
    assert "JOIN scoped_families" in statement
    assert "s.DATABASE_NAME = f.DATABASE_NAME" in statement


def test_cost_truth_and_product_economics_refuse_false_additivity() -> None:
    truth = workbench_sql.cost_truth(30, "Trexis")
    products = workbench_sql.data_product_economics(30, "Trexis")
    for basis in ("BILLED", "METERED", "MEASURED", "ALLOCATED"):
        assert f"'{basis}'" in truth
    assert "do not add to rows below" in truth
    assert "MEASURED_OBJECT_CREDITS" in products
    assert "METERED_WAREHOUSE_CREDITS" in products
    assert "MEASURED_OBJECT_CREDITS + METERED_WAREHOUSE_CREDITS" not in products


def test_slo_sql_burns_only_success_rate_objectives() -> None:
    statement = workbench_sql.slo_cockpit()
    assert "o.METRIC_KEY ILIKE '%SUCCESS_PCT'" in statement
    assert "BURN_MULTIPLE" in statement
    assert "TASK_P95_EXEC_SEC" in statement and "QUERY_P95_SEC" in statement


def test_slo_write_validates_metric_and_escapes() -> None:
    statement = create_slo_objective_sql(
        name="O'Brien loader", entity_type="TASK", entity_key="DB.S.T",
        metric_key="TASK_SUCCESS_PCT", comparator=">=", target_value=99.5,
        error_budget_pct=0.5, window_days=30, owner="O'Brien", notes="owner's SLO",
        actor="Joe",
    )
    sqlglot.parse(statement, dialect="snowflake")
    assert "O'Brien" not in statement and "TASK_SUCCESS_PCT" in statement
    with pytest.raises(ValueError):
        create_slo_objective_sql(
            name="bad", entity_type="TASK", entity_key="DB.S.T", metric_key="MADE_UP",
            comparator=">=", target_value=99, error_budget_pct=1, window_days=30,
            owner="", notes="", actor="Joe",
        )


def test_data_product_is_a_first_class_investigation_entity() -> None:
    target = investigation_target("Data product", "Finance")
    assert target.context == {"entity_type": "DATA_PRODUCT", "entity_key": "Finance"}


def test_decision_studio_wires_all_six_views_on_its_page() -> None:
    # rec8: Decision Studio is now its own page; the shell dispatches into the six
    # section bodies that still live in app/ui/decision_studio.py.
    studio = _source("app/ui/decision_studio.py")
    page = _source("app/ui/pages/decision_studio.py")
    for view in ("Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios", "Experiments"):
        assert f'"{view}"' in page
    assert 'lazy_sections(' in page and 'key="decision_section"' in page
    # Control Room no longer carries Decision Studio (Entity 360 stays).
    assert '"Decision Studio"' not in _source("app/ui/pages/control_room.py")
    assert "Verified savings never enter the projection" in studio


def test_workload_portfolio_chart_compiles(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda chart, **_kwargs: rendered.append(chart))
    frame = pd.DataFrame(
        {
            "FINGERPRINT": ["one"],
            "IMPACT_USD_30D": [125.0],
            "CONFIDENCE": [0.9],
            "BLAST_RADIUS": [4],
            "PRIORITY_SCORE": [20.0],
            "LANE": ["ACT NOW"],
            "NEXT_MOVE": ["Cache or materialize"],
        }
    )

    charts.workload_portfolio(frame)

    assert len(rendered) == 1
    spec = rendered[0].to_dict()
    assert spec["encoding"]["x"]["field"] == "IMPACT_USD_30D"
    assert spec["encoding"]["y"]["field"] == "CONFIDENCE"
