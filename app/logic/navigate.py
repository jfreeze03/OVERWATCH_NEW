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
    # V163 (Next-Fifty #37a / #44b): the per-user AI table and the Trust Center tab (the COST family default,
    # Spend & Attribution, is the wrong page for a per-user runaway).
    "COST_AI_USER_RUNAWAY": ("Cost Intelligence", "Chargeback & AI"),
    "SEC_TRUST_REGRESSION": ("Security", "Trust Center"),
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
    # R2-088: the DQ_* and WH_CHANGE_* families have no family default, so these landed on Overview, which
    # has no panel for any of them. Each goes where its playbook's step 1 (and its own DETAIL) sends the user.
    "DQ_BREACH": ("Operations", "Pipeline SLA"),           # Data checks > Row-volume anomalies
    "DQ_RECON_ERROR": ("Operations", "Pipeline SLA"),      # Data checks > Reconciliation errors
    "DQ_SCHEMA_DRIFT": ("Security", "Changes"),            # Who changed what (DDL/DCL)
    "WH_CHANGE_REGRESSION": ("Operations", "Change impact"),   # Warehouse setting changes
    # R2-091: the BUDGET / TASK family defaults never matched a rule id (these are COST_* / PIPE_*), so
    # they took the COST / PIPE default. The contract runway and the org per-account spend both live on
    # Contract & Forecast; a task-failure alert's panels are on Operations > Tasks; a PUBLIC grant's evidence
    # (Recent grant changes) is on Security > Changes, like SEC_ADMIN_GRANT's. COST_BUDGET_PACE and
    # COST_FORECAST_BREACH keep the COST default: Contract & Forecast has no monthly-budget content.
    "COST_CONTRACT_BREACH": ("Cost Intelligence", "Contract & Forecast"),
    "COST_ORG_ACCOUNT_CREEP": ("Cost Intelligence", "Contract & Forecast"),
    "PIPE_TASK_FAILURES": ("Operations", "Tasks"),
    "SEC_NEW_EXPOSURE": ("Security", "Changes"),
}

_FAMILY_DEFAULTS = (
    ("COST", ("Cost Intelligence", "Spend & Attribution")),
    ("PERF", ("Operations", "Queries")),
    ("PIPE", ("Operations", "Pipeline SLA")),
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


def inline_fix_warehouse(rule_id: str, text: str = "", detail: str = "") -> str:
    """The warehouse an inline fix should target, or '' when not applicable. ``text`` is the event TITLE and
    ``detail`` its DETAIL (see rule_warehouse)."""
    rid = str(rule_id or "").strip().upper()
    if rid not in INLINE_FIX_RULES:
        return ""
    return rule_warehouse(rid, text, detail)


def fix_target(rule_id: str, text: str = "", detail: str = "") -> dict | None:
    """Like investigation_target but lands where the FIX is generated."""
    rid = str(rule_id or "").strip().upper()
    if rid not in FIX_TARGETS:
        return None
    page, section = FIX_TARGETS[rid]
    return {"page": page, "section": section,
            "filters": investigation_target(rid, text, detail)["filters"]}


_WH_RE = re.compile(r"\bWH_[A-Z0-9_]+\b")
_DB_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\.([A-Z][A-Z0-9_]{2,})\.")
# An unquoted Snowflake identifier (app.core.sqlsafe's _IDENT_RE): the only warehouse name a title token may be.
_IDENT_RE = re.compile(r"^[A-Z_][A-Z0-9_$]{0,254}$")

# R2-092: the warehouse-led raisers write the warehouse name FIRST in the title, with no prefix rule, so
# BLCOMPUTE_WH or COMPUTE_WH never matched _WH_RE (a '_' before 'WH' is no word boundary) and the drawer lost
# the re-check, the inline fix, warehouse-scoped evidence and the Investigate filter without a word. The name
# is read from each raiser's fixed title shape instead (anchored, so a DETAIL can never supply it); an
# unparseable or legacy title falls back to _WH_RE.
_TITLE_WAREHOUSE_RES: dict[str, re.Pattern[str]] = {
    "COST_WH_DAILY_CREDITS": re.compile(r"^\s*(\S+) used \d"),                # [02] '<WH> used N credits on D'
    "PERF_QUEUED_MINUTES": re.compile(r"^\s*(\S+) queued \d"),                # [04] '<WH> queued N min in 24h'
    "PERF_SPILL_GB": re.compile(r"^\s*(\S+) spilled \d"),                     # [05] '<WH> spilled N GB remote ...'
    "COST_IDLE_OPPORTUNITY": re.compile(r"^\s*(\S+) idle waste ~\$"),         # [24] '<WH> idle waste ~$N/mo: ...'
    "COST_SLEEP_POLLING": re.compile(r"^\s*(\S+) sleep polling ~\$"),         # V160 ('No warehouse ...' never matches)
    # V150 SP_ANOMALY_SWEEP: SERIES = 'WAREHOUSE <name>' or 'SERVICE <type>' ('spent' = the pre-V076 wording)
    "COST_ANOMALY_SWEEP": re.compile(r"^\s*WAREHOUSE (\S+) (?:spent|spiked to|collapsed to) \d"),
    "COST_CLOUD_SVC_ANOMALY": re.compile(r"^\s*CLOUD SVC (\S+) cloud-services (?:spiked|collapsed) to "),
    "WH_CHANGE_REGRESSION": re.compile(r"^\s*Warehouse (\S+) regressed after "),   # SP_WAREHOUSE_CHANGE_SCAN
}
# Shaped rules whose every title the shape does not match names NO warehouse: SP_ANOMALY_SWEEP's SERVICE series
# (AUTO_CLUSTERING, the WAREHOUSE_METERING aggregate; every sweep title since V012 is SERIES-led) and V160's
# 'No warehouse' sleep-polling series (whose tail is a user name). Neither falls back to a WH_* token.
_TITLE_SHAPE_ONLY_RULES = frozenset({"COST_ANOMALY_SWEEP", "COST_SLEEP_POLLING"})
# Free LLM text appended to an event's DETAIL: the sweep's pre-explain (' | AI: ', V016..V150) and the drawer's
# saved hypothesis (' | AI hypothesis: ', alerts.py). Both are appended after the raiser's own DETAIL, so
# everything from the first marker on is model output, never a source for an entity filter or a fix target.
_AI_TAIL_RE = re.compile(r"\s\|\sAI(?: hypothesis)?:\s")


def _raiser_text(text: str) -> str:
    """``text`` up to the first appended AI narrative (the raiser-written part only)."""
    raw = str(text or "")
    m = _AI_TAIL_RE.search(raw)
    return raw[:m.start()] if m else raw


def rule_warehouse(rule_id: str, text: str = "", detail: str = "") -> str:
    """The warehouse an alert names (upper case, as Snowflake stores an unquoted name), or ''. ``text`` is the
    event TITLE and ``detail`` its DETAIL (an older caller may still pass 'TITLE DETAIL' as ``text``).

    A rule whose raiser leads its title with the warehouse (_TITLE_WAREHOUSE_RES) is read from that position
    and must be an unquoted (upper-case) identifier as Snowflake stores it: a quoted name (lower case,
    punctuation) or the cloud-services NULL series ('NONE') yields '' rather than a guess at a different
    warehouse. A shaped rule never reads its DETAIL: a title in an older shape falls back to the first WH_*
    token in the TITLE only, and a _TITLE_SHAPE_ONLY_RULES title the shape does not match (a SERVICE sweep
    series, the 'No warehouse' poller) names none. Any other rule takes the first WH_* token in the title or
    DETAIL. An appended AI narrative (_AI_TAIL_RE) is never read: the sweep's ' | AI: ' text once made an
    AUTO_CLUSTERING spike offer a closed-loop fix on a warehouse the model happened to mention."""
    rid = str(rule_id or "").strip().upper()
    raw = _raiser_text(text)
    shape = _TITLE_WAREHOUSE_RES.get(rid)
    if shape is not None:
        m = shape.match(raw)
        if m:
            name = m.group(1)
            if not _IDENT_RE.match(name) or (rid == "COST_CLOUD_SVC_ANOMALY" and name == "NONE"):
                return ""
            return name
        if rid in _TITLE_SHAPE_ONLY_RULES:
            return ""
        wh = _WH_RE.search(raw.upper())
        return wh.group(0) if wh else ""
    wh = _WH_RE.search(f"{raw} {_raiser_text(detail)}".upper())
    return wh.group(0) if wh else ""


# Account-wide self-watch rules whose text carries file names and raw loader error messages, not
# entities: OPS_PIPELINE_DEGRADED's STALE leg ends "snowflake/loader_chain_check.sql." (read as database
# LOADER_CHAIN_CHECK) and its ERR leg embeds a free-text SQLERRM ("Object 'DBA_MAINT_DB.OVERWATCH.X' does
# not exist" -> database DBA_MAINT_DB; a 'WH_...' name -> warehouse). A sticky top-bar filter taken from
# that text would scope every later page to a bogus or OVERWATCH-only entity, so Investigate applies none.
# V162: the two identity alerts carry user names (often dotted, e.g. first.last.name), client IPs and raw
# login error text -- never a warehouse or database -- so they get no entity filter either.
# V163: COST_AI_USER_RUNAWAY titles lead with a user name and SEC_TRUST_REGRESSION with a scanner name, neither
# a warehouse or database (a dotted user name reads as DB.SCHEMA.), so Investigate applies no filter.
# R2-037: every other arm whose TITLE leads with a user name gets the same carve-out -- SEC_CRED_EXPIRY ([10]
# '<USER> <type> '<NAME>' expires ...'), SEC_NEW_ADMIN_NETWORK ([18] '<USER> logged in from new network <IP>')
# and SEC_FAILED_LOGINS (daily [07] '<USER> had N failed logins on D'); first.last.name read as database FIRST.
# tests/test_r2_alerts_logic.py scans the current raiser bodies (a user-name column concatenated anywhere in a
# TITLE or DETAIL) so a new user-led arm cannot slip past.
# The list stays explicit: SEC_NEW_EXPOSURE and SEC_POSTURE_METRIC can carry real object names.
# DQ_RECON_ERROR's text is a count plus reconciliation METRIC names (never an entity), so a dotted metric
# name must not become a database filter either.
_NO_ENTITY_FILTER_RULES = frozenset({
    "OPS_PIPELINE_DEGRADED",
    "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT",
    "COST_AI_USER_RUNAWAY", "SEC_TRUST_REGRESSION",
    "SEC_CRED_EXPIRY", "SEC_NEW_ADMIN_NETWORK", "SEC_FAILED_LOGINS",
    "DQ_RECON_ERROR",
})
# Rules whose title-led warehouse filter is real but whose text carries no database: COST_SLEEP_POLLING (V160)
# ends its title and DETAIL with the poller's OWNER_HINT ('User <USER_NAME>' or '<APP> · <USER_NAME>'), so
# 'User first.last.name' read as database FIRST. They keep the warehouse filter and never get a database one.
_NO_DATABASE_FILTER_RULES = frozenset({
    "COST_SLEEP_POLLING",
})


def investigation_target(rule_id: str, text: str = "", detail: str = "") -> dict:
    """-> {"page": str, "section": str, "filters": {...}} for one event. ``text`` is the event TITLE and
    ``detail`` its DETAIL (an older caller may pass 'TITLE DETAIL' as ``text``); an appended AI narrative in
    either is never read for a filter."""
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
    wh = rule_warehouse(rid, str(text or ""), str(detail or ""))   # R2-092: a warehouse-led title by position
    if wh:
        filters["warehouse_contains"] = wh
    if rid not in _NO_DATABASE_FILTER_RULES:
        db = _DB_RE.search(f"{_raiser_text(text)} {_raiser_text(detail)}".upper())
        if db and db.group(1) not in ("SNOWFLAKE",):
            filters["database"] = db.group(1)
    return {"page": page, "section": section, "filters": filters}
