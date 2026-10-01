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

from .navigate import rule_warehouse

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# R2-089: V150 SP_ANOMALY_SWEEP titles are SERIES || ' spiked to ' / ' collapsed to ' (' spent ' before V076),
# where SERIES = 'WAREHOUSE <name>' or 'SERVICE <type>'. Parsed from the TITLE only and anchored: the sweep's
# pre-explain appends ' | AI: <free LLM text>' to DETAIL (and the drawer can append a hypothesis), so a DETAIL
# saying '... the ingestion service running ...' once became a metering plan for service 'RUNNING'. Matched
# case-sensitively on the raw title, exactly as the sweep writes it.
_SWEEP_SERIES_RE = re.compile(r"^\s*(WAREHOUSE|SERVICE)\s+(\S+)\s+(?:spent|spiked to|collapsed to)\s")
_LEADING_TOKEN_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\b")
# R1-052: PERF_FINGERPRINT_DRIFT's DETAIL is 'Hash <QUERY_PARAMETERIZED_HASH> | runs ...' (SP_ANOMALY_SWEEP, V150)
_FAMILY_HASH_RE = re.compile(r"\bHash\s+([0-9A-Fa-f]{16,64})\b")
# V150 SP_SCAN_CLOUD_SVC_ANOMALY titles: 'CLOUD SVC ' || COALESCE(WAREHOUSE_NAME, 'NONE') ||
# ' cloud-services spiked to|collapsed to ' || credits || ' credits on ' || DAY || ' (z=..)'.
_CS_ANOMALY_SERIES_RE = re.compile(r"^\s*CLOUD SVC (.+?) cloud-services (?:spiked|collapsed) to\b",
                                   re.IGNORECASE)

# R2-046 / R2-090: the rules whose condition the generic query-families-by-elapsed pack actually explains --
# warehouse / account credits burned by query families, and remote spill, which is per query family too (the
# WAREHOUSE-series COST_ANOMALY_SWEEP gets the same pack from its own branch in plan_for_alert). An explicit
# allow-list, so a new rule is withheld by default instead of falling into off-topic latency rows.
GENERIC_EVIDENCE_RULES = frozenset({"COST_WH_DAILY_CREDITS", "COST_DAILY_CREDITS", "PERF_SPILL_GB"})

# Human labels for the caption over the "Assemble evidence" button — they tell
# the DBA which evidence the AI will be grounded in BEFORE they spend credits.
_KIND_LABEL = {
    "cloud_svc": "cloud-services credits by query shape",
    "cloud_svc_sleep": "cloud-services credits by query shape (sleep polling)",
    "cortex": "AI/Cortex spend by day",
    "metering_service": "this service's daily credits",
    "query_family": "this query family's latency history",
    "queueing": "this warehouse's queueing by hour",
    "generic": "query families by elapsed hours",
}


@dataclass(frozen=True)
class EvidencePlan:
    """Which evidence shape explains one alert, and how to scope it."""

    kind: str            # cloud_svc | cloud_svc_sleep | cortex | metering_service | query_family | queueing | generic
    window_label: str    # human window for the caption, e.g. "last 7 days"
    days: int = 7
    warehouse: str = ""
    service: str = ""
    day: str = ""
    family_text: str = ""
    family_hash: str = ""   # R1-052: the drifted family's QUERY_PARAMETERIZED_HASH, from the alert DETAIL

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
        if self.family_hash:
            bits.append(f"family hash {self.family_hash}")
        elif self.family_text:
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
    # R2-092: a warehouse-led title (COST_WH_DAILY_CREDITS, PERF_QUEUED_MINUTES, ...) is read by its position,
    # so a warehouse without the WH_ prefix (COMPUTE_WH, BLCOMPUTE_WH) is still scoped; others fall back to a
    # WH_* token anywhere in the title or detail, never in an appended AI narrative (navigate.rule_warehouse,
    # shared with the drawer's re-check, inline fix and Investigate filter so the copies cannot drift).
    warehouse = rule_warehouse(rid, title, detail)
    day = _day_from(title, raised_at)

    if rid in ("COST_CLOUD_SVC_RATIO", "COST_CLOUD_SVC_ANOMALY"):
        # Cloud-services credits are driven by query SHAPES on the warehouse (compile/metadata
        # overhead) — without a warehouse there is nothing honest to scope to. Review R1-110: V157
        # retired the RATIO rule (kept: its closed rows still render) for V150's per-warehouse
        # ANOMALY rule, which fell through to the generic query-latency pack. Its warehouse comes
        # from the fixed title shape, so a name without the WH_ prefix is still scoped, and the
        # NULL-warehouse series ('CLOUD SVC NONE') gets no pack rather than an account-wide one.
        if rid == "COST_CLOUD_SVC_ANOMALY":
            series = _CS_ANOMALY_SERIES_RE.match(title)
            warehouse = series.group(1).strip() if series else ""
            if warehouse.upper() == "NONE":
                warehouse = ""
        if not warehouse:
            return None
        # V150 scores only the last 3 complete days, so the alert's day is inside this window.
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
        series = _SWEEP_SERIES_RE.match(title)
        if series is None:
            return None
        if series.group(1) == "SERVICE":
            return EvidencePlan("metering_service", f"{day} in 30-day context",
                                days=30, service=series.group(2), day=day)
        # R2-089: a WAREHOUSE series had no plan at all (no SERVICE token), so Explain with AI was withheld for
        # every warehouse spike. Its evidence is the generic pack scoped to that warehouse and the title's day --
        # the evidence the sweep's own pre-explain grounds on (V150). An unquotable name gets no pack.
        if not warehouse:
            return None
        return EvidencePlan("generic", f"{day} vs prior 7 days", days=7, warehouse=warehouse, day=day)

    if rid == "PERF_FINGERPRINT_DRIFT":
        family = _family_text(title)
        # R1-052: the raiser groups by QUERY_PARAMETERIZED_HASH and writes it into DETAIL; the title's
        # sample is ONE run's literal text, so match the family by its hash whenever DETAIL carries it.
        hash_match = _FAMILY_HASH_RE.search(detail)
        family_hash = hash_match.group(1) if hash_match else ""
        if len(family) < 8 and not family_hash:
            return None
        return EvidencePlan("query_family", "last 14 days", days=14,
                            warehouse=warehouse, family_text=family, family_hash=family_hash)

    if rid == "PERF_QUEUED_MINUTES":
        if not warehouse:
            return None
        return EvidencePlan("queueing", "last 7 days", days=7, warehouse=warehouse, day=day)

    if rid == "COST_SLEEP_POLLING":
        # V160: a sleep poller bills cloud services on its warehouse -- the per-shape cloud-services pack
        # names its SYSTEM$WAIT families, framed for sleeps (a long wait, not compile/metadata overhead).
        # Without a warehouse there is nothing honest to scope to.
        if not warehouse:
            return None
        return EvidencePlan("cloud_svc_sleep", "last 7 days", days=7, warehouse=warehouse)

    if rid == "COST_IDLE_OPPORTUNITY":
        # V157: idle waste is hours with ZERO queries — the generic query-families-by-elapsed pack
        # would ground the explanation on off-topic latency rows. No bespoke idle pack yet, so the
        # AI-explain affordance is withheld (the alert DETAIL already carries the idle arithmetic).
        return None

    if rid == "COST_AI_USER_RUNAWAY":
        # V163: one user's Cortex Code credits on one day -- query families by elapsed time say nothing about
        # AI credits, and the per-user AI table has no evidence pack yet, so the AI-explain affordance is
        # withheld (the DETAIL carries the z, the user's median and the cap arithmetic).
        return None

    if rid in GENERIC_EVIDENCE_RULES:
        # A query-latency-shaped alert we don't have a bespoke pack for: the
        # original query-families-by-elapsed pack is the right generic evidence.
        return EvidencePlan("generic", f"{day} vs prior 7 days", days=7,
                            warehouse=warehouse, day=day)

    # R2-046 / R2-090: every other rule -- including any rule added later -- gets no pack rather than
    # account-wide query latency for a metric it does not measure (budget-pace and forecast dollars, the
    # contract runway, database bytes, transfer bytes, another org account's spend, a per-company failure
    # rate). The old COST_/PERF_ prefix catch-all did exactly that, and took a contract alert's day from its
    # PROJECTED exhaust date: a future day with no history, so the pack always came back empty.
    return None
