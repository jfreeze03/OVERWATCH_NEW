"""Grounded prompt builders for Cortex-backed evaluations.

Contract (the rebuild's honesty rules applied to AI):
- Prompts embed ONLY the evidence rows the page already computed, serialized
  compactly with hard row/char caps — the model never sees or invents data
  the DBA hasn't seen.
- The instructions forbid invented numbers and demand actionable, ranked
  recommendations with the evidence row each one cites.
- Pure module: no Streamlit, no Snowflake; fully unit-testable.
"""

from __future__ import annotations

import pandas as pd

MAX_ROWS = 25
MAX_PROMPT_CHARS = 6000

_SYSTEM_RULES = (
    "You are a senior Snowflake DBA advisor. Use ONLY the evidence rows provided — "
    "never invent numbers, objects, or causes that are not in the data. "
    "Return: (1) a one-sentence overall assessment, (2) a ranked, numbered list of "
    "recommended actions (max 5), each citing the evidence row it is based on and, "
    "when relevant, the exact Snowflake statement to investigate or fix, "
    "(3) anything in the data that looks contradictory or needs a human check. "
    "Be specific and brief; no preamble."
)


def _serialize_rows(df: pd.DataFrame, columns: list[str], max_rows: int = MAX_ROWS) -> str:
    """Compact, deterministic row serialization for prompt grounding."""
    if df is None or df.empty:
        return "(no rows)"
    keep = [c for c in columns if c in df.columns]
    view = df[keep].head(max_rows)
    lines = []
    for _, row in view.iterrows():
        parts = [f"{col}={str(row[col])[:120]}" for col in keep]
        lines.append("- " + "; ".join(parts))
    if len(df) > max_rows:
        lines.append(f"- (+{len(df) - max_rows} more rows not shown)")
    return "\n".join(lines)


def _assemble(context: str, evidence: str, question: str) -> str:
    prompt = (
        f"{_SYSTEM_RULES}\n\n"
        f"CONTEXT: {context}\n\n"
        f"EVIDENCE ROWS:\n{evidence}\n\n"
        f"TASK: {question}"
    )
    return prompt[:MAX_PROMPT_CHARS]


def task_failure_prompt(timeline: pd.DataFrame, company: str, window_days: int = 7) -> str:
    evidence = _serialize_rows(
        timeline,
        ["QUERY_START_TIME", "ROLE_IN_GRAPH", "ERROR_FAMILY", "DATABASE_NAME",
         "SCHEMA_NAME", "TASK_NAME", "RUN_SEC", "ERROR_MESSAGE"],
    )
    return _assemble(
        f"Snowflake task failures for company scope {company}, last {window_days} days. "
        "ROLE_IN_GRAPH=Root cause means first failure in its task-graph run; Cascade rows are downstream.",
        evidence,
        "Diagnose the most likely root causes and recommend fixes, prioritizing Root cause rows and "
        "repeat offenders. Group by database where it clarifies ownership.",
    )


def idle_warehouse_prompt(advisor: pd.DataFrame, company: str, window_days: int) -> str:
    evidence = _serialize_rows(
        advisor,
        ["WAREHOUSE_NAME", "COMPANY", "METERED_HOURS", "IDLE_HOURS", "TOTAL_CREDITS",
         "IDLE_CREDITS", "IDLE_PCT", "IDLE_USD", "PROJECTED_MONTHLY_IDLE_USD"],
    )
    return _assemble(
        f"Idle warehouse analysis for {company}, last {window_days} days. IDLE_* = credits billed in "
        "hour slices where zero queries ran on that warehouse.",
        evidence,
        "Recommend auto-suspend or consolidation changes per warehouse, estimate the monthly saving "
        "from the data, and flag any warehouse where the idle pattern suggests a scheduling gap instead.",
    )


def release_compare_prompt(verdicts: list[dict], task_deltas: pd.DataFrame,
                           release_date: str, window_days: int) -> str:
    verdict_lines = "\n".join(
        f"- {v.get('Metric')}: before={v.get('Before')} after={v.get('After')} "
        f"delta={v.get('Delta %')}% verdict={v.get('Verdict')}"
        for v in (verdicts or [])
    ) or "(no rows)"
    task_lines = _serialize_rows(
        task_deltas,
        ["DATABASE_NAME", "TASK_NAME", "FAILED_BEFORE", "FAILED_AFTER",
         "NEW_FAILURES", "AVG_SEC_BEFORE", "AVG_SEC_AFTER", "RUNTIME_DELTA_PCT", "GOT_WORSE"],
    )
    evidence = f"QUERY HEALTH:\n{verdict_lines}\n\nPER-TASK DELTAS:\n{task_lines}"
    return _assemble(
        f"Release comparison around {release_date}: {window_days} days before vs after.",
        evidence,
        "Judge whether the release degraded the platform, name the specific tasks/metrics driving "
        "that judgment, and recommend what to roll back, re-test, or monitor next.",
    )
