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

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import pandas as pd

from .formulas import safe_float

if TYPE_CHECKING:
    from .query_advisor import Finding

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
    # The instruction blocks (system rules, context, TASK) must ALWAYS reach the model;
    # only the evidence may be trimmed. Order the fixed blocks first and give the evidence
    # the remaining char budget, so a wide evidence set can never push the TASK past the
    # MAX_PROMPT_CHARS cut (which would leave Cortex with evidence and no instructions —
    # an ungrounded answer). Instruction-first also matches incident_narrative_prompt.
    head = (
        f"{_SYSTEM_RULES}\n\n"
        f"CONTEXT: {context}\n\n"
        f"TASK: {question}\n\n"
        "EVIDENCE ROWS:\n"
    )
    budget = MAX_PROMPT_CHARS - len(head)
    if budget <= 0:
        return head[:MAX_PROMPT_CHARS]
    if len(evidence) > budget:
        marker = "\n- (evidence truncated to fit)"
        # Only append the marker when it actually fits in the budget; otherwise a small
        # budget (< marker length) would push the result past the cap. Belt-and-suspenders
        # final slice keeps the MAX_PROMPT_CHARS contract absolute.
        evidence = (evidence[: budget - len(marker)].rstrip() + marker
                    if budget > len(marker) else evidence[:budget])
    return (head + evidence)[:MAX_PROMPT_CHARS]


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


def query_optimization_prompt(row: Mapping[str, object], findings: Sequence[Finding]) -> str:
    """Ground a Cortex rewrite in ONE query's stats + the deterministic findings.

    The model gets only this query's numbers, the findings already shown to the
    operator, and the query text — never anything the DBA hasn't seen — and is
    told to invent no tables/columns beyond the text.
    """
    stats = "; ".join([
        f"warehouse_size={row.get('WAREHOUSE_SIZE') or '?'}",
        f"elapsed={safe_float(row.get('ELAPSED_SEC')):.1f}s",
        f"compile={safe_float(row.get('COMPILE_SEC')):.1f}s",
        f"queued={safe_float(row.get('QUEUED_SEC')):.1f}s",
        f"gb_scanned={safe_float(row.get('GB_SCANNED')):.1f}",
        f"cache_pct={safe_float(row.get('CACHE_PCT')):.0f}",
        f"remote_spill_gb={safe_float(row.get('REMOTE_SPILL_GB')):.1f}",
        f"partitions_scanned={int(safe_float(row.get('PARTITIONS_SCANNED')))}",
        f"partitions_total={int(safe_float(row.get('PARTITIONS_TOTAL')))}",
        f"rows_produced={int(safe_float(row.get('ROWS_PRODUCED')))}",
    ])
    finds = "\n".join(f"- {f.title}: {f.detail}" for f in findings) or "- (no deterministic findings)"
    qtext = str(row.get("QUERY_TEXT") or "").strip()
    evidence = f"STATS: {stats}\n\nFINDINGS:\n{finds}\n\nQUERY TEXT:\n{qtext}"
    return _assemble(
        "One Snowflake query's execution stats plus the deterministic optimization findings "
        "already computed and shown for it.",
        evidence,
        "Rewrite this SQL to remove the flagged inefficiencies. Explain each change and which "
        "finding it addresses. Do NOT invent tables or columns that are not in the query text; "
        "if a fix needs schema details you cannot see (clustering keys, row counts), say what "
        "you would need instead of guessing.",
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


def _incident_field(incident: object, *keys: str) -> str:
    """First non-empty incident field as a trimmed string, NaN-safe (a pandas
    Series yields NaN for an absent column, and ``bool(NaN)`` is True)."""
    get = getattr(incident, "get", None)
    if get is None:
        return ""
    for key in keys:
        value = get(key)
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


def incident_narrative_prompt(incident: object, hypotheses: list[dict],
                              summary: dict | None = None) -> str:
    """Plain-English root-cause narrative for a declared incident, grounded
    STRICTLY in the ranked hypotheses the auto-investigation already shows
    (app.logic.rca.rank_root_causes). Read-only by construction: the
    instructions forbid remediation SQL — the RCA feature explains, never acts,
    so the shared ``_SYSTEM_RULES`` (which invites a fix statement) is NOT used.

    Empty hypotheses -> '' — the caller offers the panel only when there is a
    ranked field to narrate, so the model is never asked to confabulate a cause
    the deterministic ranker couldn't find."""
    hyps = list(hypotheses or [])
    if not hyps:
        return ""
    title = _incident_field(incident, "TITLE") or "(untitled incident)"
    severity = _incident_field(incident, "SEVERITY") or "unknown"
    status = _incident_field(incident, "STATUS") or "unknown"
    onset = _incident_field(incident, "STARTED_AT", "DETECTED_AT") or "unknown"
    lines = []
    for i, h in enumerate(hyps[:MAX_ROWS], 1):
        band = str(h.get("band") or "LOW")
        htitle = str(h.get("title") or "")[:160]
        lead = str(h.get("lead_text") or "")
        mag = str(h.get("magnitude_text") or "")
        by = str(h.get("changed_by") or "").strip() or "unknown"
        why = str(h.get("why") or "")
        lines.append(f"  {i}. [{band}] {htitle} — {lead}; magnitude: {mag}; "
                     f"changed by: {by}; scoring: {why}")
    prompt = (
        "You are a senior Snowflake DBA assistant helping an on-call engineer "
        "investigate a declared incident. Write a brief plain-English root-cause "
        "narrative for the responder.\n\n"
        "Using ONLY the ranked evidence below:\n"
        "- Lead with the single most likely cause and explain why its timing "
        "(a trigger precedes onset) and magnitude support it.\n"
        "- If the strongest lead is only LOW confidence, say the signals are "
        "inconclusive and name what to check next — do not overstate.\n"
        "- Never invent warehouses, tasks, users, times, or numbers that are "
        "not in the evidence lines.\n"
        "- This is read-only triage: do NOT propose SQL, DDL, or remediation "
        "commands. Explain the cause; do not prescribe the fix.\n"
        "- Plain prose, 3-5 sentences, no headings or markdown.\n\n"
        f"INCIDENT: {title[:200]}\n"
        f"Severity: {severity} · Status: {status} · Onset: {onset}\n\n"
        "RANKED CANDIDATE CAUSES (strongest first):\n"
        + "\n".join(lines)
    )
    assessment = str((summary or {}).get("headline") or "").strip()
    if assessment:
        prompt += f"\n\nDETERMINISTIC LEADING ASSESSMENT: {assessment}"
    return prompt[:MAX_PROMPT_CHARS]


def alert_evidence_prompt(kind: str, title: str, detail: str,
                          evidence: pd.DataFrame, window_label: str) -> str:
    """Grounded 'explain this alert' prompt whose evidence framing matches the
    alert family (kind). Evidence rows only — no invitation to speculate beyond
    them. See app.logic.alert_evidence for how kind/columns are resolved."""
    columns = _EVIDENCE_COLUMNS.get(kind, _EVIDENCE_COLUMNS["generic"])
    framing = _EVIDENCE_FRAMING.get(kind, _EVIDENCE_FRAMING["generic"])
    rows = _serialize_rows(evidence, columns, max_rows=20)
    # AIP-2: instructions FIRST, evidence LAST + trimmed to the remaining budget — mirrors
    # _assemble. cortex_complete slices the prompt to MAX_PROMPT_CHARS from the FRONT, so a
    # wide evidence pack (e.g. 20 cloud_svc rows + a long DETAIL) would otherwise push the
    # trailing anti-fabrication / 150-word instructions past the cut, leaving Cortex evidence
    # with no grounding constraint — the exact ungrounded-answer mode the ordering prevents.
    head = (
        "You are a Snowflake cost & performance analyst. An automated sweep raised this alert:\n"
        f"ALERT: {str(title)[:300]}\n"
        f"DETAIL: {str(detail)[:500]}\n\n"
        "Using ONLY the evidence rows below: (1) name the 1-2 most likely drivers with the "
        "numbers that support them, (2) state what to check or change next, (3) say "
        "'evidence is inconclusive' if the rows do not explain the alert. Max 150 words. "
        "Never invent queries, warehouses, services, or numbers not shown.\n\n"
        f"{framing} ({window_label}):\n"
    )
    budget = MAX_PROMPT_CHARS - len(head)
    if budget <= 0:
        return head[:MAX_PROMPT_CHARS]
    body = rows
    if len(body) > budget:
        marker = "\n- (evidence truncated to fit)"
        body = (body[: budget - len(marker)].rstrip() + marker
                if budget > len(marker) else body[:budget])
    return (head + body)[:MAX_PROMPT_CHARS]
