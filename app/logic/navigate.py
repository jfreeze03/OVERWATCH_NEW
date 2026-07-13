"""Alert -> investigation deep-link targets. Pure module.

Given a rule id and the event text, decide which page + section answers
"why did this fire?" and which filters to pre-apply (warehouse / database
extracted from the event title). The UI layer performs the navigation.
"""

from __future__ import annotations

import re

# page label -> the lazy_sections widget key on that page
PAGE_SECTION_KEYS = {
    "Cost & Contract": "cost_section",
    "Operations": "ops_section",
    "Security": "sec_section",
    "Alerts": "alerts_section",
    "Admin": "adm_section",
}

_RULE_TARGETS = {
    "PERF_CHANGE_REGRESSION": ("Operations", "Change impact"),
    "COST_CLOUD_SVC_RATIO": ("Cost & Contract", "Spend & Attribution"),
    "COST_STORAGE_SURGE": ("Cost & Contract", "Optimization & Savings"),
    "COST_SERVERLESS_CREEP": ("Cost & Contract", "Spend & Attribution"),
    "COST_ANOMALY_SWEEP": ("Cost & Contract", "Spend & Attribution"),
    "COST_DEPT_BUDGET_PACE": ("Cost & Contract", "Chargeback & AI"),
    "PIPE_COPY_FAILURES": ("Operations", "Pipeline SLA"),
    "PIPE_DT_FAILURES": ("Operations", "Pipeline SLA"),
    "SEC_CRED_EXPIRY": ("Security", "Access"),
    "SEC_BREAK_GLASS_USE": ("Security", "Changes"),
}

_FAMILY_DEFAULTS = (
    ("BUDGET", ("Cost & Contract", "Contract & Forecast")),
    ("COST", ("Cost & Contract", "Spend & Attribution")),
    ("PERF", ("Operations", "Queries")),
    ("PIPE", ("Operations", "Pipeline SLA")),
    ("SEC", ("Security", "Access")),
)

# Rules with a mechanical fix: the drawer offers "Generate fix ->" landing on
# the remediation/optimization surface with the event's filters applied.
FIX_TARGETS = {
    "COST_CLOUD_SVC_RATIO": ("Cost & Contract", "Optimization & Savings"),
    "COST_WH_DAILY_CREDITS": ("Cost & Contract", "Optimization & Savings"),
    "COST_ANOMALY_SWEEP": ("Cost & Contract", "Optimization & Savings"),
    "COST_STORAGE_SURGE": ("Cost & Contract", "Optimization & Savings"),
    "PERF_QUEUED_MINUTES": ("Cost & Contract", "Optimization & Savings"),
    "PERF_SPILL_GB": ("Cost & Contract", "Optimization & Savings"),
}


# Warehouse-lever rules: the drawer generates the fix INLINE (no navigation)
# because the target and the statement are both unambiguous.
INLINE_FIX_RULES = ("COST_CLOUD_SVC_RATIO", "COST_WH_DAILY_CREDITS",
                    "COST_ANOMALY_SWEEP", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB")


def inline_fix_warehouse(rule_id: str, text: str = "") -> str:
    """The warehouse an inline fix should target, or '' when not applicable."""
    rid = str(rule_id or "").strip().upper()
    if rid not in INLINE_FIX_RULES:
        return ""
    m = _WH_RE.search(str(text or "").upper())
    return m.group(0) if m else ""


def fix_target(rule_id: str, text: str = "") -> dict | None:
    """Like investigation_target but lands where the FIX is generated."""
    rid = str(rule_id or "").strip().upper()
    if rid not in FIX_TARGETS:
        return None
    page, section = FIX_TARGETS[rid]
    return {"page": page, "section": section,
            "filters": investigation_target(rid, text)["filters"]}


_WH_RE = re.compile(r"\bWH_[A-Z0-9_]+\b")
_DB_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\.([A-Z][A-Z0-9_]{2,})\.")


def investigation_target(rule_id: str, text: str = "") -> dict:
    """-> {"page": str, "section": str, "filters": {...}} for one event."""
    rid = str(rule_id or "").strip().upper()
    page, section = _RULE_TARGETS.get(rid, ("", ""))
    if not page:
        for prefix, target in _FAMILY_DEFAULTS:
            if rid.startswith(prefix):
                page, section = target
                break
        else:
            # OPS_* (canary/render/scan) deliberately lands on Overview: their
            # home is Admin, which non-DBA profiles cannot navigate to.
            page, section = "Overview", ""
    filters: dict = {}
    upper = str(text or "").upper()
    wh = _WH_RE.search(upper)
    if wh:
        filters["warehouse_contains"] = wh.group(0)
    db = _DB_RE.search(upper)
    if db and db.group(1) not in ("SNOWFLAKE",):
        filters["database"] = db.group(1)
    return {"page": page, "section": section, "filters": filters}
