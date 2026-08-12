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
)
from app.ui.decision_studio import (
    _cost_truth,
    _experiments,
    _portfolio,
    _products,
    _scenarios,
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
    section = lazy_sections(
        ["Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios", "Experiments"],
        key="decision_section",
    )
    section_filter_contract(
        f,
        applies=("company", "days"),
        note=("Portfolio, product economics and Cost Truth use Company and Window; SLO "
              "and experiment records keep their own objective windows."),
    )
    rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
    if section == "Portfolio":
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
