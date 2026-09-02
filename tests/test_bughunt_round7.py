"""Regression locks for the round-7 bug hunt (v4.422.0)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

import app.ui.pages.ask as ask_mod
from app.core.result import QueryResult
from app.logic.ai_prompts import MAX_PROMPT_CHARS, alert_evidence_prompt
from app.logic.ask.types import AnswerResult

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- AIP-1 (MED): _ai_phrasing rejects a SQL-NULL/"None"/"nan" Cortex result ---------
def _grounded() -> AnswerResult:
    return AnswerResult(intent="spend", headline="USER_A spent 900 credits (60% of spend).",
                        bullets=[], confidence="grounded")


def test_ai_phrasing_rejects_null_and_placeholder_results(monkeypatch):
    def fake_run(sql, **kw):
        return QueryResult(df=pd.DataFrame({"TXT": [fake_run.val]}), ok=True)
    monkeypatch.setattr(ask_mod, "run", fake_run)

    for bad in (None, float("nan"), "None", "nan", "  ", "NULL"):
        fake_run.val = bad
        assert ask_mod._ai_phrasing(_grounded(), "llama3.1-8b") is None, f"should reject {bad!r}"
    # a real, number-preserving rephrase still passes
    fake_run.val = "USER_A used 900 credits — 60% of spend."
    assert ask_mod._ai_phrasing(_grounded(), "llama3.1-8b") is not None


# --- AIP-2 (MED): alert_evidence_prompt keeps its instructions ahead of the evidence
def test_alert_evidence_prompt_instructions_survive_a_wide_pack():
    # 20 wide cloud_svc rows + a near-max detail would push a trailing instruction block
    # past cortex_complete's front-truncation; instructions-first + budget-trim prevents that.
    df = pd.DataFrame({
        "SAMPLE_TEXT": ["SELECT " + "x" * 200] * 20, "QUERY_TYPE": ["SELECT"] * 20,
        "RUNS": [4000] * 20, "CS_CREDITS": [12.3] * 20, "CS_PER_1K_RUNS": [3.1] * 20,
        "AVG_EXEC_S": [0.2] * 20, "AVG_CACHE_PCT": [10] * 20,
    })
    prompt = alert_evidence_prompt("cloud_svc", "T" * 300, "D" * 500, df, "last 7 days")
    assert len(prompt) <= MAX_PROMPT_CHARS               # never exceeds the cortex_complete cap
    assert "Never invent queries, warehouses, services, or numbers not shown." in prompt
    assert "evidence is inconclusive" in prompt
    # instructions come BEFORE the evidence rows (so a front-truncation can't drop them)
    assert prompt.index("Never invent") < prompt.index("- SAMPLE_TEXT=")


# --- AIP-3 (LOW): the AI panel displays the model that will actually run ------------
def test_ai_panel_normalizes_model_for_display():
    ap = _src("app/ui/ai_panel.py")
    assert "from app.core.ai import cortex_complete, normalize_model" in ap
    assert 'normalize_model(settings.get("CORTEX_MODEL") or "llama3.1-8b")' in ap


# --- NP-1 (LOW): the Cortex Code spend KPI no longer overclaims "Exact" -------------
def test_cortex_spend_help_does_not_overclaim_exact():
    aic = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert '"help": f"Exact token credits x ${ai_rate:.2f}/credit."' not in aic
    assert "may differ by a few cents from the Cost page" in aic


# --- scope-arm-1 (MED): schema-filtered summary keeps the warehouse-company axis -----
def test_schema_summary_gated_on_all_company():
    ops = _src("app/ui/pages/operations.py")
    assert 'elif not wh_filter and not user_filter and str(company).upper() in ("ALL", ""):' in ops
    cr = _src("app/ui/pages/control_room.py")
    assert 'elif str(company).upper() in ("ALL", ""):' in cr
