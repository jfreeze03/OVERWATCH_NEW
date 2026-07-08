"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import streamlit as st

from app.config import OPERATOR_PROFILES, resolve_role_profile
from app.core.query import run
from app.core.session import current_role
from app.core.state import filters
from app.data import cost_sql
from app.logic.formulas import safe_float
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    page_header,
    section_header,
    styled_table,
)

_PAGE = "Cost & Contract"

_SERVICE_CATEGORY = {
    "WAREHOUSE_METERING": "Warehouse",
    "WAREHOUSE_METERING_READER": "Warehouse (reader)",
    "SNOWPIPE": "Serverless", "SNOWPIPE_STREAMING": "Serverless",
    "SERVERLESS_TASK": "Serverless", "SERVERLESS_ALERTS": "Serverless",
    "AUTOMATIC_CLUSTERING": "Serverless", "MATERIALIZED_VIEW": "Serverless",
    "SEARCH_OPTIMIZATION": "Serverless", "QUERY_ACCELERATION": "Serverless",
    "SNOWPARK_CONTAINER_SERVICES": "Serverless", "COPY_FILES": "Serverless",
    "REPLICATION": "Replication", "STORAGE": "Storage",
}


from app.ui.pages.cost_parts.ai_chargeback import (  # noqa: E402
    _ai_users_tab,
    _chargeback_tab,
    _cortex_storage_tab,
)
from app.ui.pages.cost_parts.contract import _contract_tab  # noqa: E402
from app.ui.pages.cost_parts.optimize import _optimization_tab, _savings_tab  # noqa: E402
from app.ui.pages.cost_parts.spend import _attribution_tab, _categorize, _spend_tab  # noqa: E402,F401
from app.ui.pages.cost_parts.unit_costs import _unit_costs_tab  # noqa: E402


def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    page_header("Cost & Contract",
                "Where the money goes, whether the contract holds, and what savings are proven.",
                scope_note=f"{f['company']} · last {f['days']} days", icon_name="cost")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    # Four grouped sections instead of eight pills (CoCo density fix): each
    # group renders its related sub-panels under labeled section headers.
    section = lazy_sections(
        ["Spend & Attribution", "Contract & Forecast", "Chargeback & AI",
         "Unit costs", "Optimization & Savings"], key="cost_section")
    if section == "Spend & Attribution":
        section_header("Spend", "info", "spend")
        _spend_tab(f["company"], f["days"], rate, ai_rate)
        st.divider()
        section_header("Attribution", "info", "chargeback")
        _attribution_tab(f["company"], f["days"], rate, f["database"], f["schema_contains"])
        st.divider()
        section_header("Query-tag governance", "info", "chargeback")
        st.caption("Chargeback precision is capped by tag coverage — untagged execution "
                   "time can only be allocated, never attributed.")
        tags_res = run(cost_sql.tag_coverage(f["days"], f["company"]), page=_PAGE,
                       key=f"tagcov_{f['company']}_{f['days']}", tier="historical",
                       source="QUERY_HISTORY (exec-time-weighted tag coverage)")
        if guard(tags_res, "No workloads above the 60s floor in this window."):
            tdf_g = tags_res.df.copy()
            total_exec = float(tdf_g["EXEC_SEC"].sum())
            untagged = float(tdf_g["UNTAGGED_EXEC_SEC"].sum())
            kpi_row([
                {"label": "Tagged share (exec-time)",
                 "value": f"{(1 - untagged / total_exec) * 100 if total_exec else 100:,.1f}%",
                 "severity": "ok" if total_exec and untagged / total_exec < 0.3 else "warn"},
                {"label": "Top untagged user",
                 "value": str(tdf_g.iloc[0]["USER_NAME"]) if len(tdf_g) else "n/a",
                 "delta": f"{float(tdf_g.iloc[0]['UNTAGGED_EXEC_SEC']) / 3600:,.1f}h untagged" if len(tdf_g) else None,
                 "delta_color": "off"},
            ])
            styled_table(tdf_g, height=260, column_config={
                "TAGGED_PCT": st.column_config.NumberColumn("Tagged %", format="%.1f%%")})
            st.caption("Fix at the source: set QUERY_TAG in the tool/session that runs the "
                       "workload; the scoreboard moves within a day.")
    elif section == "Contract & Forecast":
        section_header("Contract pacing & renewal planner", "info", "contract")
        _contract_tab(settings)
    elif section == "Chargeback & AI":
        section_header("Department chargeback", "info", "chargeback")
        _chargeback_tab(f["company"], f["days"], rate, is_operator)
        st.divider()
        section_header("Cortex & storage", "info", "cost")
        _cortex_storage_tab(f["company"], f["days"], ai_rate, settings)
        st.divider()
        section_header("AI users", "info", "operations")
        _ai_users_tab(f["company"], f["days"], ai_rate, settings, is_operator)
    elif section == "Unit costs":
        section_header("Unit costs — one query, one call, one AI request", "info", "cost")
        _unit_costs_tab(f, rate, ai_rate)
    else:
        section_header("Optimization", "info", "optimize")
        _optimization_tab(f["company"], f["days"], rate, settings, is_operator)
        st.divider()
        section_header("Savings ledger", "ok", "cost")
        _savings_tab()
