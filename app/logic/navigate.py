"""Alert -> investigation deep-link targets. Pure module.

Given a rule id and the event text, decide which page + section answers
"why did this fire?" and which filters to pre-apply (warehouse / database
extracted from the event title). The UI layer performs the navigation.
"""

from __future__ import annotations

import re

# page label -> the lazy_sections widget key on that page
PAGE_SECTION_KEYS = {
    "Control Room": "cr_section",
    "Cost Intelligence": "cost_section",
    "Operations": "ops_section",
    # v4.597 (Option C): the former Decision Studio page, renamed; the widget key is kept.
    "Proof": "decision_section",
    "Security": "sec_section",
    "Alerts": "alerts_section",
    "Admin": "adm_section",
}

# page label -> its ordered section labels. The Jump-to command palette (rec4)
# and the deep-link resolver enumerate a page's sections from here instead of
# hard-coding them. tests/test_section_labels.py keeps this in lock-step with
# each page's own lazy_sections() call, so a rename in one place fails CI until
# the other matches (the house drift-guard pattern, cf. the alert-rule test).
PAGE_SECTION_LABELS = {
    "Control Room": ["Action Center", "Pulse", "Incidents & triage", "Timeline & movers",
                     "Freshness & replay", "Entity 360"],
    "Cost Intelligence": ["Spend & Attribution", "Contract & Forecast", "Chargeback & AI",
                         "Unit costs", "Compare", "Optimization & Savings"],
    "Operations": ["Queries", "Tasks", "Warehouses", "Optimize", "Change impact",
                   "Pipeline SLA", "Release compare", "Emergency"],
    "Proof": ["Proof", "Pipeline"],
    "Alerts": ["Open events", "Rules", "History", "Native delivery"],
    "Security": ["Decision queue", "Access", "AI guardrails", "Changes", "Clients", "Egress", "Exposure", "Least privilege", "Trust Center"],
    "Admin": ["Settings", "Migrations & freshness", "Setup progress", "Metrics",
              "App self-cost", "Performance", "Canary", "Errors & telemetry"],
}

# v4.597 (Option C): Decision Studio became "Proof" (Proof · Pipeline) and its other sections moved to
# their natural homes. Old links — a saved DEFAULT_VIEW, a hard-coded request_navigation, a
# ``?page=decision-studio[&section=<slug>]`` deep link — are remapped through this ONE pure table
# (app/core/state.py applies it in request_navigation, consume_pending_navigation and requested_page),
# so a stale target lands on the new home instead of silently clamping to a first section.
LEGACY_PAGE = "Decision Studio"
LEGACY_SECTIONS = ("Scorecard", "ROI", "Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios",
                   "Experiments")
LEGACY_TARGETS: dict[tuple[str, str], tuple[str, str]] = {
    (LEGACY_PAGE, "Scorecard"): ("Proof", "Proof"),
    (LEGACY_PAGE, "ROI"): ("Proof", "Proof"),
    (LEGACY_PAGE, "Experiments"): ("Proof", "Proof"),     # retired UI; its savings are Proof evidence rows
    (LEGACY_PAGE, "Products"): ("Proof", "Proof"),        # hidden this release
    (LEGACY_PAGE, "Scenarios"): ("Proof", "Pipeline"),
    (LEGACY_PAGE, "Portfolio"): ("Operations", "Optimize"),
    (LEGACY_PAGE, "SLOs"): ("Operations", "Pipeline SLA"),
    (LEGACY_PAGE, "Cost Truth"): ("Cost Intelligence", "Spend & Attribution"),
    # rec8's older remap: Decision Studio was once a Control Room section (its Portfolio led).
    ("Control Room", LEGACY_PAGE): ("Operations", "Optimize"),
}
LEGACY_FALLBACK = ("Proof", "")


def remap_legacy_target(page: str, section: str = "") -> tuple[str, str]:
    """A retired (page, section) -> its v4.597 home. A bare or unknown-section Decision Studio lands
    on Proof (keeping a section only when it is a real Proof section); anything else passes through
    unchanged."""
    page, section = str(page or ""), str(section or "")
    hit = LEGACY_TARGETS.get((page, section))
    if hit is not None:
        return hit
    if page == LEGACY_PAGE:
        return ("Proof", section if section in PAGE_SECTION_LABELS["Proof"] else "")
    return (page, section)


def _slug(label: str) -> str:
    """The ?page= / ?section= slug (mirrors components._section_slug and state.remember_page)."""
    return str(label).lower().replace("&", "and").replace(" ", "-")


def legacy_deep_link(page_slug: str, section_slug: str,
                     valid_pages: tuple[str, ...] | list[str]) -> tuple[str, str] | None:
    """Resolve a retired ``?page=decision-studio[&section=<old slug>]`` deep link to (page, section),
    or None when the slug is not the retired page. The target must be one this viewer's profile
    offers: an off-profile target (EXECUTIVE sent to Operations) falls back to Proof, and when even
    Proof is off-profile the link resolves to nothing (the caller keeps its normal landing)."""
    if str(page_slug or "").strip().lower() != _slug(LEGACY_PAGE):
        return None
    want = str(section_slug or "").strip().lower()
    old = next((label for label in LEGACY_SECTIONS if _slug(label) == want), "")
    target = remap_legacy_target(LEGACY_PAGE, old)
    allowed = tuple(valid_pages)
    if target[0] in allowed:
        return target
    return LEGACY_FALLBACK if LEGACY_FALLBACK[0] in allowed else None


_RULE_TARGETS = {
    "PERF_CHANGE_REGRESSION": ("Operations", "Change impact"),
    # COST_CLOUD_SVC_RATIO is no longer raised (V157: its config row and scan arm are gone; the per-warehouse
    # cloud-services baseline rule of V150 covers the signal). Its entries here, in FIX_TARGETS and in
    # INLINE_FIX_RULES stay only so the drawer still routes its historical events -- the house pattern the
    # break-glass rule's entry below follows.
    "COST_CLOUD_SVC_RATIO": ("Cost Intelligence", "Spend & Attribution"),
    "COST_STORAGE_SURGE": ("Cost Intelligence", "Optimization & Savings"),
    "COST_SERVERLESS_CREEP": ("Cost Intelligence", "Spend & Attribution"),
    "COST_ANOMALY_SWEEP": ("Cost Intelligence", "Spend & Attribution"),
    "COST_DEPT_BUDGET_PACE": ("Cost Intelligence", "Chargeback & AI"),
    "PIPE_COPY_FAILURES": ("Operations", "Pipeline SLA"),
    "PIPE_DT_FAILURES": ("Operations", "Pipeline SLA"),
    "SEC_CRED_EXPIRY": ("Security", "Access"),
    "SEC_BREAK_GLASS_USE": ("Security", "Changes"),
    # V162 (Next-Fifty #39): the grant evidence (*Recent grant changes*) lives on Security > Changes;
    # SEC_LOGIN_TAKEOVER keeps the SEC family default, Security > Access (*Account-takeover candidates*).
    "SEC_ADMIN_GRANT": ("Security", "Changes"),
    # V157: the freshness board lives on Control Room (an EXECUTIVE viewer, who has no Control Room,
    # is clamped to Overview by request_navigation like every other unreachable target).
    "OPS_PIPELINE_DEGRADED": ("Control Room", "Freshness & replay"),
    "COST_IDLE_OPPORTUNITY": ("Cost Intelligence", "Optimization & Savings"),
    # V160: the billed-family panel (Cloud-services health) lives on Spend & Attribution.
    "COST_SLEEP_POLLING": ("Cost Intelligence", "Spend & Attribution"),
}

_FAMILY_DEFAULTS = (
    ("BUDGET", ("Cost Intelligence", "Contract & Forecast")),
    ("COST", ("Cost Intelligence", "Spend & Attribution")),
    ("PERF", ("Operations", "Queries")),
    ("PIPE", ("Operations", "Pipeline SLA")),
    ("TASK", ("Operations", "Tasks")),
    ("SEC", ("Security", "Access")),
)

# Rules with a mechanical fix: the drawer offers "Generate fix ->" landing on
# the remediation/optimization surface with the event's filters applied.
FIX_TARGETS = {
    "COST_CLOUD_SVC_RATIO": ("Cost Intelligence", "Optimization & Savings"),
    "COST_WH_DAILY_CREDITS": ("Cost Intelligence", "Optimization & Savings"),
    "COST_ANOMALY_SWEEP": ("Cost Intelligence", "Optimization & Savings"),
    "COST_STORAGE_SURGE": ("Cost Intelligence", "Optimization & Savings"),
    "PERF_QUEUED_MINUTES": ("Cost Intelligence", "Optimization & Savings"),
    "PERF_SPILL_GB": ("Cost Intelligence", "Optimization & Savings"),
    "COST_IDLE_OPPORTUNITY": ("Cost Intelligence", "Optimization & Savings"),
}


# Warehouse-lever rules: the drawer generates the fix INLINE (no navigation)
# because the target and the statement are both unambiguous.
INLINE_FIX_RULES = ("COST_CLOUD_SVC_RATIO", "COST_WH_DAILY_CREDITS",
                    "COST_ANOMALY_SWEEP", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB",
                    "COST_IDLE_OPPORTUNITY")


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

# Account-wide self-watch rules whose text carries file names and raw loader error messages, not
# entities: OPS_PIPELINE_DEGRADED's STALE leg ends "snowflake/loader_chain_check.sql." (read as database
# LOADER_CHAIN_CHECK) and its ERR leg embeds a free-text SQLERRM ("Object 'DBA_MAINT_DB.OVERWATCH.X' does
# not exist" -> database DBA_MAINT_DB; a 'WH_...' name -> warehouse). A sticky top-bar filter taken from
# that text would scope every later page to a bogus or OVERWATCH-only entity, so Investigate applies none.
# V162: the two identity alerts carry user names (often dotted, e.g. first.last.name), client IPs and raw
# login error text -- never a warehouse or database -- so they get no entity filter either.
_NO_ENTITY_FILTER_RULES = frozenset({
    "OPS_PIPELINE_DEGRADED",
    "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT",
})


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
    if rid in _NO_ENTITY_FILTER_RULES:
        return {"page": page, "section": section, "filters": {}}
    filters: dict = {}
    upper = str(text or "").upper()
    wh = _WH_RE.search(upper)
    if wh:
        filters["warehouse_contains"] = wh.group(0)
    db = _DB_RE.search(upper)
    if db and db.group(1) not in ("SNOWFLAKE",):
        filters["database"] = db.group(1)
    return {"page": page, "section": section, "filters": filters}
