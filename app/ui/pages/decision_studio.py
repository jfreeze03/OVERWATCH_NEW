"""Proof page (v4.597, Option C — the former Decision Studio): does OVERWATCH pay for itself, what
each verified saving rests on, and the priced pipeline ahead.

Two READ-ONLY sections — Proof (the merged Scorecard + ROI with per-item evidence) and Pipeline
(addressable $ + queued work + a measured-default projection). The section BODIES live in
app/ui/decision_studio.py; this module is the page shell (header, page-open verdict, primary section
bar, scope contract) and dispatches into them. Both module paths and the ``decision_section`` key are
kept this release. The rest of Decision Studio moved to its natural homes (Portfolio → Operations ▸
Optimize, SLOs → Operations ▸ Pipeline SLA, Cost Truth → Cost ▸ Spend & Attribution); old links remap
through navigate.LEGACY_TARGETS. No write path, so EXECUTIVE can open it; cross-links are
profile-gated (state.can_open)."""

from __future__ import annotations

from app.core.errors import safe_page
from app.core.state import filters
from app.logic.formulas import safe_float
from app.ui.components import (
    lazy_sections,
    load_settings,
    page_header,
    page_verdict_line,
    section_filter_contract,
    since_last_visit_opener,
    stashed_counts,
)
from app.ui.decision_studio import (
    _pipeline_tab,
    _proof_tab,
    decision_verdict,
    reset_proof_memo,
)

_PAGE = "Proof"


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company, days = f["company"], f["days"]
    bounds = f["bounds"]
    page_header(
        "Proof",
        "Does OVERWATCH pay for itself, what each saving rests on, and what's ahead.",
        icon_name="target",
        scope_note=f"{f['company']} · {f['window_label']}",
    )
    rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
    # Per-render: the page-open verdict and the section both call _proof_signals; reset here so they
    # share ONE computation this render. The Pipeline projection is a fragment, but it never calls
    # _proof_signals (its measured defaults are passed in), so a fragment-only rerun — which skips this
    # reset — never reads the memo. The run() cache stays the freshness authority across renders.
    reset_proof_memo()
    # Wave 2 #8: the page-open "should I worry?" line — the prove-it verdict hoisted above the
    # section bar so it reads before any section is opened. Reuses the Proof reads (cache-shared)
    # and renders nothing until the ledger is set up.
    _verdict = decision_verdict(rate)
    if _verdict:
        page_verdict_line(_verdict)
    # C18: "since your last visit" opener — renders nothing mid-session or anonymous.
    since_last_visit_opener(_PAGE, f["company"])
    section = lazy_sections(
        ["Proof", "Pipeline"],
        key="decision_section",
        counts=stashed_counts(_PAGE) or None,
    )
    # #13: each section declares which of the page filters it actually honors, instead of one
    # blanket contract that overclaims.
    _contracts = {
        "Proof": {"applies": (),
                  "note": "Account-wide proof (ledger, run cost, acceptance, alert precision, per-item "
                          "evidence); the page Company/Window do not apply."},
        "Pipeline": {"applies": ("company", "days"),
                     "note": "Addressable $ scopes to Company and Window (unread maintenance, when counted, "
                             "is its last 30 complete days); queued work is every open Action Center item for "
                             "the Company (not windowed); the measured slider defaults (acceptance, "
                             "realization) are account-wide."},
    }
    section_filter_contract(f, **_contracts[section])
    if section == "Proof":
        _proof_tab(rate)
    else:
        _pipeline_tab(company, days, rate, bounds=bounds)
