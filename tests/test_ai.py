"""Tests for the AI evaluation layer (prompt grounding + model validation)."""

import pandas as pd

from app.core.ai import MAX_PROMPT_CHARS as EXEC_CAP
from app.core.ai import normalize_model
from app.logic.ai_prompts import (
    MAX_PROMPT_CHARS,
    MAX_ROWS,
    idle_warehouse_prompt,
    release_compare_prompt,
    task_failure_prompt,
)


def _timeline(n=3) -> pd.DataFrame:
    return pd.DataFrame([{
        "QUERY_START_TIME": "2026-07-07 02:00:00", "ROLE_IN_GRAPH": "Root cause",
        "ERROR_FAMILY": "Timeout / cancelled", "DATABASE_NAME": "ALFA_EDW_PROD",
        "SCHEMA_NAME": "DW", "TASK_NAME": f"LOAD_{i}", "RUN_SEC": 120,
        "ERROR_MESSAGE": "Statement reached its statement or warehouse timeout",
    } for i in range(n)])


def test_task_failure_prompt_grounds_evidence_and_rules():
    prompt = task_failure_prompt(_timeline(), "ALFA")
    assert "ALFA_EDW_PROD" in prompt and "LOAD_0" in prompt
    assert "Root cause" in prompt
    assert "never invent numbers" in prompt
    assert "EVIDENCE ROWS:" in prompt


def test_prompts_cap_length_on_oversized_evidence():
    big = pd.concat([_timeline(1)] * 200, ignore_index=True)
    big["ERROR_MESSAGE"] = "x" * 500
    prompt = task_failure_prompt(big, "ALFA")
    assert len(prompt) == MAX_PROMPT_CHARS  # hard-truncated


def test_prompts_cap_rows_with_marker():
    big = pd.concat([_timeline(1)] * (MAX_ROWS + 5), ignore_index=True)
    big["ERROR_MESSAGE"] = "short"
    prompt = task_failure_prompt(big, "ALFA")
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert "(+5 more rows not shown)" in prompt


def test_empty_evidence_is_explicit():
    prompt = task_failure_prompt(pd.DataFrame(), "ALFA")
    assert "(no rows)" in prompt


def test_idle_prompt_carries_dollars():
    advisor = pd.DataFrame([{
        "WAREHOUSE_NAME": "WH_ALFA_QUERY", "COMPANY": "ALFA", "METERED_HOURS": 100,
        "IDLE_HOURS": 60, "TOTAL_CREDITS": 50.0, "IDLE_CREDITS": 30.0,
        "IDLE_PCT": 60.0, "IDLE_USD": 110.4, "PROJECTED_MONTHLY_IDLE_USD": 473.1,
    }])
    prompt = idle_warehouse_prompt(advisor, "ALFA", 7)
    assert "WH_ALFA_QUERY" in prompt and "473.1" in prompt


def test_release_prompt_includes_both_sections():
    verdicts = [{"Metric": "Failure %", "Before": 1.0, "After": 3.0, "Delta %": 200.0, "Verdict": "Worse"}]
    deltas = pd.DataFrame([{
        "DATABASE_NAME": "TRXS_EDW_PRD", "TASK_NAME": "T1", "FAILED_BEFORE": 0,
        "FAILED_AFTER": 3, "NEW_FAILURES": 3, "AVG_SEC_BEFORE": 10,
        "AVG_SEC_AFTER": 12, "RUNTIME_DELTA_PCT": 20.0, "GOT_WORSE": True,
    }])
    prompt = release_compare_prompt(verdicts, deltas, "2026-07-01", 3)
    assert "QUERY HEALTH:" in prompt and "PER-TASK DELTAS:" in prompt
    assert "TRXS_EDW_PRD" in prompt and "Worse" in prompt and "2026-07-01" in prompt


def test_model_name_validation():
    assert normalize_model("llama3.1-8b") == "llama3.1-8b"
    assert normalize_model("MISTRAL-LARGE2") == "mistral-large2"
    assert normalize_model("bad model'; DROP") == "llama3.1-8b"
    assert normalize_model("") == "llama3.1-8b"
    assert EXEC_CAP == MAX_PROMPT_CHARS
