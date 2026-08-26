"""v4.143 locks for exception-first, decision-readable operating surfaces."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.logic.read_models import READ_MODELS, get_contract
from app.ui import components

_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_read_model_contracts_are_complete_unique_and_truthful() -> None:
    assert {contract.key for contract in READ_MODELS} == {
        "action_center",
        "entity_360",
        "task_run",
        "workload_portfolio",
        "slo_cockpit",
        "control_pulse",
    }
    assert get_contract(" ENTITY_360 ").summary_reads == 4
    assert get_contract("action_center").summary_reads == 2
    assert get_contract("workload_portfolio").evidence_reads == 4
    assert get_contract("control_pulse").trigger == "the pulse opens"
    assert get_contract("task_run").trigger == "explicit run-evidence toggle"
    with pytest.raises(KeyError):
        get_contract("made_up")


def test_exception_summary_escapes_content_and_has_a_clean_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: list[str] = []
    fake_st = SimpleNamespace(
        markdown=lambda value, **_kwargs: rendered.append(str(value)),
    )
    monkeypatch.setattr(components, "st", fake_st)

    components.exception_summary(
        [{
            "label": "<script>alert(1)</script>",
            "value": "2 < 3",
            "detail": "Needs A&B",
            "severity": "bad",
        }],
        "clean",
    )
    assert "<script>" not in rendered[-1]
    assert "&lt;script&gt;" in rendered[-1]
    assert "2 &lt; 3" in rendered[-1]
    assert "Needs A&amp;B" in rendered[-1]
    assert "ow-exception--bad" in rendered[-1]

    components.exception_summary([], "No exceptions <today>.")
    assert "ow-exception--ok" in rendered[-1]
    assert "No exceptions &lt;today&gt;." in rendered[-1]


def test_confidence_badge_uses_shared_quality_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown: list[str] = []
    captions: list[str] = []
    fake_st = SimpleNamespace(
        markdown=lambda value, **_kwargs: markdown.append(str(value)),
        caption=lambda value: captions.append(str(value)),
    )
    monkeypatch.setattr(components, "st", fake_st)

    state = components.confidence_badge(0.92, coverage_pct=45)

    assert state["label"] == "Low coverage"
    assert "92% confidence" in markdown[-1]
    assert "45% coverage" in markdown[-1]
    assert "Only 45%" in captions[-1]


def test_decision_rows_preserve_numeric_data_and_sticky_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[pd.DataFrame] = []
    selected: list[int] = []
    fake_st = SimpleNamespace(
        session_state={},
        column_config=SimpleNamespace(
            NumberColumn=lambda label, **kwargs: {"label": label, **kwargs},
            ProgressColumn=lambda label, **kwargs: {"label": label, **kwargs},
        ),
    )
    monkeypatch.setattr(components, "st", fake_st)

    def fake_selectable(frame, **_kwargs):
        captured.append(frame.copy())
        return 1

    monkeypatch.setattr(components, "selectable_table", fake_selectable)
    frame = pd.DataFrame({
        "TITLE": ["Tune A", "Tune B"],
        "DETAIL": ["High queue", "High spill"],
        "USD": [10.5, 20.5],
        "CONFIDENCE": [0.7, 0.9],
        "OWNER": ["A", "B"],
        "STATUS": ["OPEN", "OPEN"],
        "DUE": ["2026-08-04", "2026-08-05"],
        "SEVERITY": ["HIGH", "MEDIUM"],
    })

    kwargs = {
        "key": "decisions",
        "decision_col": "TITLE",
        "why_col": "DETAIL",
        "impact_col": "USD",
        "confidence_col": "CONFIDENCE",
        "owner_col": "OWNER",
        "status_col": "STATUS",
        "next_col": "DUE",
        "context_cols": ("SEVERITY",),
        "on_select": selected.append,
    }
    assert components.decision_rows(frame, **kwargs) == 1
    assert components.decision_rows(frame, **kwargs) == 1

    assert captured[0].columns.tolist() == [
        "Decision", "Why", "Impact", "Confidence", "Owner", "Status", "Next", "SEVERITY",
    ]
    assert pd.api.types.is_float_dtype(captured[0]["Impact"])
    assert pd.api.types.is_float_dtype(captured[0]["Confidence"])
    assert selected == [1]


def test_evidence_gate_discloses_contract_before_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    fake_st = SimpleNamespace(
        caption=lambda value: calls.append(("caption", str(value))),
        toggle=lambda label, **_kwargs: calls.append(("toggle", label)) or True,
    )
    monkeypatch.setattr(components, "st", fake_st)

    assert components.evidence_gate("task_run", key="run", label="Load run") is True
    assert calls[0][0] == "caption" and "Summary:" in calls[0][1]
    assert calls[1] == ("toggle", "Load run")


def test_heavy_evidence_reads_remain_below_explicit_gates() -> None:
    workbench = _source("app/ui/workbench.py")
    entity = workbench.split("def render_entity_360", 1)[1].split("def render_watchlist", 1)[0]
    entity_gate = entity.index('if evidence_gate(\n        "entity_360"')
    for builder in ("related_remediations", "related_savings", "evidence_links"):
        assert entity.index(f"workbench_sql.{builder}") > entity_gate

    operations = _source("app/ui/pages/operations.py")
    analyzer = operations.split("def _task_run_analyzer", 1)[1].split(
        "def _task_version_compare", 1,
    )[0]
    assert analyzer.index('if not evidence_gate(\n        "task_run"') < analyzer.index(
        "ops_sql.task_graph_run_nodes",
    )


def test_exception_first_and_decision_rows_are_wired_at_real_render_shapes() -> None:
    workbench = _source("app/ui/workbench.py")
    action = workbench.split("def render_action_center", 1)[1].split(
        "def _seed_entity_context", 1,
    )[0]
    assert action.index("exception_summary(") < action.index("kpi_row([")
    assert "selection = decision_rows(" in action
    assert 'decision_col="TITLE"' in action and 'why_col="DETAIL"' in action
    assert "confidence_badge(confidence)" in workbench

    studio = _source("app/ui/decision_studio.py")
    portfolio = studio.split("def _portfolio", 1)[1].split("def _slo_editor", 1)[0]
    slos = studio.split("def _slos", 1)[1].split("def _products", 1)[0]
    assert portfolio.index("exception_summary(") < portfolio.index("kpi_row([")
    assert "decision_rows(" in portfolio and 'decision_col="NEXT_MOVE"' in portfolio
    assert slos.index("exception_summary(") < slos.index("kpi_row([")

    control = _source("app/ui/pages/control_room.py")
    # rec8: Decision Studio moved out, so Pulse is now bounded by the next section.
    pulse = control.split('elif section == "Pulse":', 1)[1].split(
        'elif section == "Incidents & triage":', 1,
    )[0]
    assert pulse.index("exception_summary(") < pulse.index("kpi_row([")
    assert 'read_model_caption("control_pulse")' in pulse


def test_v4143_release_metadata_is_current() -> None:
    assert 'APP_VERSION = "4.302.0"' in _source("app/config.py")
    changelog = _source("CHANGELOG.md")
    assert "## 4.143.1 - Snowflake button compatibility hotfix (2026-08-03)" in changelog
    assert "## 4.143.0 - Decision-readable operating surfaces (2026-08-03)" in changelog
