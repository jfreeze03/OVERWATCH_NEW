"""Decision Studio page (rec8): the planning workbench, promoted from a Control
Room section to its own Analyze page so the daily-triage console and the weekly
planning studio no longer share a roof. The section BODIES live in
app/ui/decision_studio.py; this module is the page shell (header, primary section
bar, scope contract) and dispatches into them. Cross-jumps into Entity 360 stay
pointed at Control Room, where Entity 360 remains."""

from __future__ import annotations

from app.core.errors import safe_page
from app.core.state import filters
from app.logic.formulas import safe_float
from app.ui.components import (
    lazy_sections,
    load_settings,
    page_header,
    section_filter_contract,
    since_last_visit_opener,
    stashed_counts,
)
from app.ui.decision_studio import (
    _cost_truth,
    _experiments,
    _portfolio,
    _products,
    _roi,
    _scenarios,
    _scorecard,
    _slos,
)

_PAGE = "Decision Studio"


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company, days = f["company"], f["days"]
    page_header(
        "Decision Studio",
        "Plan the work: portfolio, objectives, product economics, cost truth, scenarios, experiments.",
        icon_name="target",
        scope_note=f"{f['company']} · {f['window_label']}",
    )
    # C18: "since your last visit" opener — renders nothing mid-session or anonymous.
    since_last_visit_opener(_PAGE, f["company"])
    section = lazy_sections(
        ["Scorecard", "ROI", "Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios", "Experiments"],
        key="decision_section",
        counts=stashed_counts(_PAGE) or None,
    )
    # #13: each section declares which of the page filters it actually honors, instead
    # of one blanket "Company + Window" contract that overclaimed for the sections that
    # ignore them (SLO objectives carry their own windows; experiments are account-wide;
    # Scenarios scopes by Company only, with the horizon chosen in-panel).
    _contracts = {
        "Scorecard": {"applies": (),
                      "note": "The prove-it scorecard (ROI, realization, acceptance, alert precision, "
                              "evidence) is account-wide; the page Company/Window do not apply."},
        "ROI": {"applies": (),
                "note": "The savings ledger (verified $, realization, run-rate) is account-wide; "
                        "the page Company/Window do not apply."},
        "Portfolio": {"applies": ("company", "days"),
                      "note": "Recurring-query portfolio scoped to Company and Window."},
        "SLOs": {"applies": (),
                 "note": "Objectives evaluate against their own configured windows; the page Company/Window do not apply."},
        "Products": {"applies": ("company", "days"),
                     "note": "Data-product economics scoped to Company and Window."},
        "Cost Truth": {"applies": ("company", "days"),
                       "note": "Billed/metered/measured/allocated inventory scoped to Company and Window."},
        "Scenarios": {"applies": ("company",),
                      "note": "Scenario projections scope to Company; the horizon is chosen inside the panel."},
        "Experiments": {"applies": (),
                        "note": "Experiment records are account-wide; the page Company/Window do not apply."},
    }
    section_filter_contract(f, **_contracts[section])
    rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
    if section == "Scorecard":
        _scorecard(company, rate)
    elif section == "ROI":
        _roi(company)
    elif section == "Portfolio":
        _portfolio(company, days, rate)
    elif section == "SLOs":
        _slos()
    elif section == "Products":
        _products(company, days, rate)
    elif section == "Cost Truth":
        _cost_truth(company, days)
    elif section == "Scenarios":
        _scenarios(company)
    else:
        _experiments()
