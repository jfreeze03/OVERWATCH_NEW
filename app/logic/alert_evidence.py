"""Per-alert evidence planning: pick the RIGHT evidence for each alert family.

The Alerts "Explain with AI" flow used ONE query-elapsed-by-warehouse pack for
every COST_*/PERF_* alert, so a cloud-services-ratio or Cortex-spend alert got
handed query-latency rows — evidence unrelated to the metric that actually
fired, which made the grounded AI summary read as off-topic ("data that has
nothing to do with the selected issue").

This module resolves, from the alert's rule + title + detail, which evidence
SHAPE explains it and the parameters that scope that evidence. The SQL for each
kind lives in app.data.alert_evidence_sql; the matching prompt framing lives in
app.logic.ai_prompts.alert_evidence_prompt.

Pure: string/regex parsing only. Tested in tests/test_alert_evidence.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WH_RE = re.compile(r"\bWH_[A-Z0-9_]+\b")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SERVICE_AFTER_RE = re.compile(r"\bSERVICE\s+([A-Z0-9_]+)")
_LEADING_TOKEN_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\b")

# Human labels for the caption over the "Assemble evidence" button — they tell
# the DBA which evidence the AI will be grounded in BEFORE they spend credits.
_KIND_LABEL = {
    "cloud_svc": "cloud-services credits by query shape",
    "cortex": "AI/Cortex spend by day",
    "metering_service": "this service's daily credits",
    "query_family": "this query family's latency history",
    "queueing": "this warehouse's queueing by hour",
    "generic": "query families by elapsed hours",
}


@dataclass(frozen=True)
class EvidencePlan:
    """Which evidence shape explains one alert, and how to scope it."""

    kind: str            # cloud_svc | cortex | metering_service | query_family | queueing | generic
    window_label: str    # human window for the caption, e.g. "last 7 days"
    days: int = 7
    warehouse: str = ""
    service: str = ""
    day: str = ""
    family_text: str = ""

    @property
    def label(self) -> str:
        return _KIND_LABEL.get(self.kind, _KIND_LABEL["generic"])

    @property
    def scope_note(self) -> str:
        bits = []
        if self.warehouse:
            bits.append(f"warehouse {self.warehouse}")
        if self.service:
            bits.append(f"service {self.service}")
        if self.family_text:
            bits.append(f'family "{self.family_text[:44]}"')
        return " · ".join(bits)


def _day_from(title: str, raised_at: str) -> str:
    match = _DAY_RE.search(str(title or ""))
    return match.group(0) if match else str(raised_at or "")[:10]


def _family_text(title: str) -> str:
    """Extract the statement sample from a fingerprint-drift title.

    "Query family p95 39.5s -> 170.7s: CALL X.Y.Z(...)..." -> "CALL X.Y.Z(..."
    """
    text = str(title or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    # drop the trailing truncation marks the raiser adds
    return text.strip().strip(".…").strip()


def plan_for_alert(rule_id: str, title: str, detail: str = "",
                   raised_at: str = "") -> EvidencePlan | None:
    """Resolve the evidence plan for one alert, or None when no grounded pack
    fits (the AI-explain affordance is then withheld rather than showing
    off-topic evidence)."""
    rid = str(rule_id or "").strip().upper()
    title = str(title or "")
    detail = str(detail or "")
    both = f"{title} {detail}".upper()
    wh_match = _WH_RE.search(both)
    warehouse = wh_match.group(0) if wh_match else ""
    day = _day_from(title, raised_at)

    if rid == "COST_CLOUD_SVC_RATIO":
        # The ratio is driven by query SHAPES on the warehouse (compile/metadata
        # overhead) — without a warehouse there is nothing honest to scope to.
        if not warehouse:
            return None
        return EvidencePlan("cloud_svc", "last 7 days", days=7, warehouse=warehouse)

    if rid == "COST_AI_CREEP":
        return EvidencePlan("cortex", "this week vs prior 7 days", days=14)

    if rid == "COST_SERVERLESS_CREEP":
        match = _LEADING_TOKEN_RE.search(title.upper())
        service = match.group(1) if match else ""
        if not service:
            return None
        return EvidencePlan("metering_service", "this week vs prior 7 days",
                            days=14, service=service)

    if rid == "COST_ANOMALY_SWEEP":
        match = _SERVICE_AFTER_RE.search(both)
        service = match.group(1) if match else ""
        if not service:
            return None
        return EvidencePlan("metering_service", f"{day} in 30-day context",
                            days=30, service=service, day=day)

    if rid == "PERF_FINGERPRINT_DRIFT":
        family = _family_text(title)
        if len(family) < 8:
            return None
        return EvidencePlan("query_family", "last 14 days", days=14,
                            warehouse=warehouse, family_text=family)

    if rid == "PERF_QUEUED_MINUTES":
        if not warehouse:
            return None
        return EvidencePlan("queueing", "last 7 days", days=7, warehouse=warehouse, day=day)

    if rid.startswith(("COST_", "PERF_")):
        # A query-latency-shaped anomaly we don't have a bespoke pack for: the
        # original query-families-by-elapsed pack is the right generic evidence.
        return EvidencePlan("generic", f"{day} vs prior 7 days", days=7,
                            warehouse=warehouse, day=day)

    return None
