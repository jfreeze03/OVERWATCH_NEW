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
    "COST_CLOUD_SVC_RATIO": ("Cost & Contract", "Spend"),
    "COST_STORAGE_SURGE": ("Cost & Contract", "Optimization"),
    "COST_SERVERLESS_CREEP": ("Cost & Contract", "Spend"),
    "COST_ANOMALY_SWEEP": ("Cost & Contract", "Spend"),
    "PIPE_COPY_FAILURES": ("Operations", "Pipeline SLA"),
    "PIPE_DT_FAILURES": ("Operations", "Pipeline SLA"),
    "SEC_CRED_EXPIRY": ("Security", "Access"),
    "SEC_BREAK_GLASS_USE": ("Security", "Changes"),
}

_FAMILY_DEFAULTS = (
    ("BUDGET", ("Cost & Contract", "Contract")),
    ("COST", ("Cost & Contract", "Spend")),
    ("PERF", ("Operations", "Queries")),
    ("PIPE", ("Operations", "Pipeline SLA")),
    ("TASK", ("Operations", "Tasks")),
    ("SEC", ("Security", "Access")),
)

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
