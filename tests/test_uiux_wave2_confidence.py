"""UI/UX master list — Wave 2 confidence-encoding unification (F60).

A 0-1 confidence was shown three ways across the decision workbenches: a
ProgressColumn bar (portfolio, action center via decision_rows), a raw float
column (Decision Studio scenarios table), and a chips+caption badge (Entity 360,
a single value). F60 unifies the TABLE encoding on one shared ProgressColumn
(`confidence_progress_column`); single-value surfaces keep `confidence_badge`.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.ui import components

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_f60_progress_column_helper_is_a_0_1_bar():
    col = components.confidence_progress_column("Confidence (evidence)", "help text")
    assert col["type_config"]["type"] == "progress"
    assert col["type_config"]["min_value"] == 0 and col["type_config"]["max_value"] == 1
    assert col["label"] == "Confidence (evidence)"


def test_f60_decision_rows_renders_confidence_as_the_shared_bar(monkeypatch):
    captured = {}
    monkeypatch.setattr(components, "selectable_table",
                        lambda view, **k: captured.update(cfg=k.get("column_config")))
    df = pd.DataFrame({"NEXT_MOVE": ["x"], "QUERY_PREVIEW": ["a"], "CONFIDENCE": [0.8]})
    components.decision_rows(df, key="t", decision_col="NEXT_MOVE", why_col="QUERY_PREVIEW",
                             confidence_col="CONFIDENCE", confidence_label="Confidence (evidence)")
    cfg = captured["cfg"] or {}
    assert "Confidence (evidence)" in cfg
    assert cfg["Confidence (evidence)"]["type_config"]["type"] == "progress"


def test_f60_decision_rows_uses_the_shared_helper_not_an_inline_progresscolumn():
    comp = _src("app/ui/components.py")
    body = comp.split("def decision_rows(", 1)[1].split("\ndef ", 1)[0]
    # the inline ProgressColumn is gone — routed through the shared helper
    assert "confidence_progress_column(confidence_label" in body
    assert "st.column_config.ProgressColumn(" not in body


def test_f60_scenarios_table_gets_the_confidence_bar_too():
    studio = _src("app/ui/decision_studio.py")
    assert '"CONFIDENCE"] = confidence_progress_column(' in studio


def test_f60_entity_360_work_table_gets_the_bar_too():
    # review medium: the Entity 360 "Work and outcomes" table showed the SAME
    # authored 0-1 confidence as a raw float — now a bar, completing the sweep.
    workbench = _src("app/ui/workbench.py")
    work = workbench.split("**Work and outcomes**", 1)[1][:400]
    assert '"CONFIDENCE": confidence_progress_column(' in work


def test_f60_authored_confidence_help_is_one_shared_string():
    # review low: the "authored" help is honest about provenance (operator OR
    # recommendation engine — a security finding promoted with RISK_SCORE/100 is not
    # literally operator-authored) and is the SAME string on every surface.
    assert "operator or recommendation engine" in components.AUTHORED_CONFIDENCE_HELP
    assert "AUTHORED_CONFIDENCE_HELP" in _src("app/ui/workbench.py")
    assert "AUTHORED_CONFIDENCE_HELP" in _src("app/ui/decision_studio.py")
    # the old inline help string is no longer duplicated per surface
    assert 'not an evidence heuristic' not in _src("app/ui/decision_studio.py")
