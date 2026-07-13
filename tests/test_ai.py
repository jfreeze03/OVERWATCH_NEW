"""Tests for the AI evaluation layer (prompt grounding + model validation)."""

import pandas as pd

from app.core.ai import MAX_PROMPT_CHARS as EXEC_CAP
from app.core.ai import normalize_model
from app.logic.ai_prompts import (
    MAX_PROMPT_CHARS,
    MAX_ROWS,
    idle_warehouse_prompt,
    release_compare_prompt,
)


def _big_advisor(n: int) -> pd.DataFrame:
    return pd.DataFrame([{
        "WAREHOUSE_NAME": f"WH_ALFA_{i}", "COMPANY": "ALFA", "METERED_HOURS": 100,
        "IDLE_HOURS": 60, "TOTAL_CREDITS": 50.0, "IDLE_CREDITS": 30.0,
        "IDLE_PCT": 60.0, "IDLE_USD": 110.4, "PROJECTED_MONTHLY_IDLE_USD": 473.1,
        "NOTE": "x" * 500,
    } for i in range(n)])


def test_prompts_cap_length_on_oversized_evidence():
    prompt = idle_warehouse_prompt(_big_advisor(200), "ALFA", 7)
    assert len(prompt) <= MAX_PROMPT_CHARS  # hard-truncated


def test_prompts_cap_rows_with_marker():
    prompt = idle_warehouse_prompt(_big_advisor(MAX_ROWS + 5), "ALFA", 7)
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert "(+5 more rows not shown)" in prompt


def test_empty_evidence_is_explicit():
    prompt = idle_warehouse_prompt(pd.DataFrame(), "ALFA", 7)
    assert "(no rows)" in prompt


def test_idle_prompt_carries_dollars():
    advisor = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_ALFA_QUERY", "COMPANY": "ALFA", "METERED_HOURS": 100,
        "IDLE_HOURS": 60, "TOTAL_CREDITS": 50.0, "IDLE_CREDITS": 30.0,
        "IDLE_PCT": 60.0, "IDLE_USD": 110.4, "PROJECTED_MONTHLY_IDLE_USD": 473.1,
    }])
    prompt = idle_warehouse_prompt(advisor, "ALFA", 7)
    assert "WH_ALFA_QUERY" in prompt and "473.1" in prompt


def test_release_prompt_grounds_query_verdicts():
    verdicts = [{"Metric": "Failure %", "Before": 1.0, "After": 3.0, "Delta %": 200.0, "Verdict": "Worse"}]
    prompt = release_compare_prompt(verdicts, "2026-07-01", 3)
    assert "Failure %" in prompt and "2026-07-01" in prompt
    assert "QUERY HEALTH" in prompt
    # r26: task-deltas section removed with task monitoring (owner call)
    assert "PER-TASK" not in prompt


def test_model_name_validation():
    assert normalize_model("llama3.1-8b") == "llama3.1-8b"
    assert normalize_model("MISTRAL-LARGE2") == "mistral-large2"
    assert normalize_model("bad model'; DROP") == "llama3.1-8b"
    assert normalize_model("") == "llama3.1-8b"
    assert EXEC_CAP == MAX_PROMPT_CHARS
