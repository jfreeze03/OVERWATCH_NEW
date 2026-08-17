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
         "IDLE_CREDITS", "IDLE_PCT", "IDLE_USD", "PROJECTED_MONTHLY_IDLE_USD",
         "AUTO_SUSPEND", "AUTO_SUSPEND_KNOWN", "ACTION_STATUS", "ACTIONABLE",
         "ACTIONABLE_MONTHLY_USD", "SAVINGS_CONFIDENCE"],
    )
    return _assemble(
        f"Idle warehouse analysis for {company}, last {window_days} days. IDLE_* = credits billed in "
        "hour slices where zero queries ran on that warehouse.",
        evidence,
        "Recommend auto-suspend only where ACTIONABLE=True. Use ACTIONABLE_MONTHLY_USD as the "
        "timer-change estimate; never substitute gross projected idle. For VERIFY SETTING rows, "
        "request metadata verification. For ALREADY TUNED rows, investigate cadence, scheduling, "
        "consolidation, or retirement instead of recommending a longer timer.",
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


def anomaly_explain_prompt(title: str, detail: str, evidence: pd.DataFrame,
                           ddl_count: int, window_label: str) -> str:
    """Grounded 'why did this spike?' prompt: evidence rows only, no
    invitation to speculate beyond them."""
    rows = _serialize_rows(
        evidence,
        ["SAMPLE_TEXT", "WAREHOUSE_NAME", "RUNS_DAY", "ELAPSED_H_DAY", "ELAPSED_H_PRIOR_AVG"],
        max_rows=15,
    )
    return (
        "You are a Snowflake cost analyst. An automated sweep raised this anomaly:\n"
        f"ALERT: {str(title)[:300]}\n"
        f"DETAIL: {str(detail)[:500]}\n\n"
        f"Query families by elapsed hours on the anomalous day vs their prior-7-day average ({window_label}):\n"
        f"{rows}\n\n"
        f"DDL statements in the same window: {int(ddl_count) if ddl_count is not None and int(ddl_count) >= 0 else 'not counted'}\n\n"
        "Using ONLY the evidence above: (1) name the 1-2 most likely drivers with the "
        "numbers that support them, (2) state what to check next, (3) say 'evidence is "
        "inconclusive' if the rows do not explain the spike. Max 150 words. Never invent "
        "queries, warehouses, or numbers not shown."
    )


# Per-alert-family evidence shaping. Each entry names the evidence columns to
# serialize and the one-line framing that tells the model what the rows MEAN —
# so a cloud-services-ratio alert is explained with cloud-services credits, a
# Cortex-spend alert with daily Cortex credits, etc. (See app.logic.alert_evidence.)
_EVIDENCE_COLUMNS: dict[str, list[str]] = {
    "cloud_svc": ["SAMPLE_TEXT", "QUERY_TYPE", "RUNS", "CS_CREDITS",
                  "CS_PER_1K_RUNS", "AVG_EXEC_S", "AVG_CACHE_PCT"],
    "cortex": ["DAY", "SERVICE_TYPE", "CREDITS_BILLED"],
    "metering_service": ["DAY", "SERVICE_TYPE", "CREDITS_USED",
                         "CREDITS_COMPUTE", "CREDITS_CLOUD_SERVICES"],
    "query_family": ["DAY", "RUNS", "P50_SEC", "P95_SEC", "WAREHOUSE_NAME", "SAMPLE_TEXT"],
    "queueing": ["HOUR_TS", "RUNS", "QUEUED_MIN", "P95_SEC"],
    "generic": ["SAMPLE_TEXT", "WAREHOUSE_NAME", "RUNS_DAY", "ELAPSED_H_DAY", "ELAPSED_H_PRIOR_AVG"],
}

_EVIDENCE_FRAMING: dict[str, str] = {
    "cloud_svc": ("Top query shapes by cloud-services credits on the warehouse "
                  "(higher CS_PER_1K_RUNS = more metadata/compile overhead per run)"),
    "cortex": "Daily AI/Cortex billed credits by service type",
    "metering_service": "Daily billed credits for this service type",
    "query_family": "This query family's daily run count and p50/p95 latency in seconds",
    "queueing": "The warehouse's worst queueing hours (queued minutes and p95 latency)",
    "generic": ("Query families by elapsed hours on the anomalous day vs their "
                "prior-7-day average"),
}


def alert_evidence_prompt(kind: str, title: str, detail: str,
                          evidence: pd.DataFrame, window_label: str) -> str:
    """Grounded 'explain this alert' prompt whose evidence framing matches the
    alert family (kind). Evidence rows only — no invitation to speculate beyond
    them. See app.logic.alert_evidence for how kind/columns are resolved."""
    columns = _EVIDENCE_COLUMNS.get(kind, _EVIDENCE_COLUMNS["generic"])
    framing = _EVIDENCE_FRAMING.get(kind, _EVIDENCE_FRAMING["generic"])
    rows = _serialize_rows(evidence, columns, max_rows=20)
    return (
        "You are a Snowflake cost & performance analyst. An automated sweep raised this alert:\n"
        f"ALERT: {str(title)[:300]}\n"
        f"DETAIL: {str(detail)[:500]}\n\n"
        f"{framing} ({window_label}):\n"
        f"{rows}\n\n"
        "Using ONLY the evidence above: (1) name the 1-2 most likely drivers with the "
        "numbers that support them, (2) state what to check or change next, (3) say "
        "'evidence is inconclusive' if the rows do not explain the alert. Max 150 words. "
        "Never invent queries, warehouses, services, or numbers not shown."
    )
