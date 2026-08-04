"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import streamlit as st

from app.core.query import run, run_batch
from app.core.session import is_operator as _is_operator
from app.core.state import filters
from app.data import cost_sql, mart27_sql, mart_sql
from app.logic.directory import resolve_display
from app.logic.formulas import humanize_duration, safe_float
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    page_header,
    result_caption,
    run_mart_first,
    section_filter_contract,
    section_header,
    styled_table,
    user_display_map,
    with_user_names,
)

_PAGE = "Cost & Contract"


from app.ui.pages.cost_parts.ai_chargeback import (  # noqa: E402
    _ai_users_tab,
    _chargeback_tab,
    _cortex_spend_tab,
)
from app.ui.pages.cost_parts.contract import _contract_tab  # noqa: E402
from app.ui.pages.cost_parts.optimize import _optimization_tab, _savings_tab  # noqa: E402
from app.ui.pages.cost_parts.spend import (  # noqa: E402,F401
    _attribution_tab,
    _categorize,
    _spend_attr_recent_jobs,
    _spend_tab,
    _storage_tab,
)
from app.ui.pages.cost_parts.unit_costs import _unit_costs_tab  # noqa: E402


def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    page_header("Cost & Contract",
                "Where the money goes, whether the contract holds, and what savings are proven.",
                scope_note=f"{f['company']} · last {f['days']} days", icon_name="cost")
    # #3: operator gating from the VIEWER identity + allowlist, not CURRENT_ROLE().
    is_operator = _is_operator()
    # Six grouped sections instead of eight pills (CoCo density fix): each
    # group renders its related sub-panels under labeled section headers.
    section = lazy_sections(
        ["Spend & Attribution", "Contract & Forecast", "Chargeback & AI",
         "Unit costs", "Compare", "Optimization & Savings"], key="cost_section")
    _contracts = {
        "Spend & Attribution": {
            "applies": ("company", "days"),
            "partial": ("database", "schema_contains"),
            "note": "Database and Schema apply only to object-grain attribution panels.",
        },
        "Contract & Forecast": {
            "applies": (),
            "note": "Account-wide contract calendar and renewal assumptions.",
        },
        "Chargeback & AI": {
            "applies": ("days",),
            "partial": ("company", "database", "schema_contains"),
            "note": "Company shapes chargeback/AI users; Cortex service totals remain account-wide.",
        },
        "Unit costs": {
            "applies": ("company", "days", "database", "schema_contains"),
            "note": "Each metric retains its declared measured or allocated grain.",
        },
        "Compare": {
            "applies": ("company",),
            "note": "Panel-local periods replace the global Window.",
        },
        "Optimization & Savings": {
            "applies": (),
            "partial": ("company", "days"),
            "note": "Savings verification and ledger totals are account-wide where labeled.",
        },
    }
    section_filter_contract(f, **_contracts[section])
    if section == "Spend & Attribution":
        # perf #15: submit the four INDEPENDENT recent mart reads that gate the
        # eager Spend + Attribution paint as ONE parallel batch instead of four
        # serial round-trips. Each panel still falls back to its own mart/live
        # read if the batch is unavailable (run_batch -> None) or a member misses
        # (a None/empty prefetch triggers that panel's existing fallback).
        _pf = run_batch(_spend_attr_recent_jobs(f["company"], f["days"]),
                        page=_PAGE, tier="hourly") or {}
        section_header("Spend", "info", "spend")
        _spend_tab(f["company"], f["days"], rate, ai_rate, f["database"],
                   metering_res=_pf.get("metering"), csr_res=_pf.get("csr"))
        st.divider()
        section_header("Attribution", "info", "chargeback")
        _attribution_tab(f["company"], f["days"], rate, f["database"], f["schema_contains"],
                         wh_res=_pf.get("wh"), daily_res=_pf.get("daily"))
        st.divider()
        # perf #15: Storage (3 reads) + Unmapped (1 read) are below-fold detail;
        # gate both behind ONE toggle so the default first paint pays only the
        # Spend/Attribution batch. A toggle (not st.expander) is required — an
        # expander still executes its body every rerun and would not defer them.
        if st.toggle("Load storage & unmapped-entity detail", key="cost_spend_detail",
                     help="Storage economics (3 reads) and the unmapped-entity check "
                          "(1 read), loaded on demand — kept off the default first paint."):
            section_header("Storage", "info", "cost")
            _storage_tab(f["company"], f["days"], settings)
            st.divider()
            section_header("Unmapped entities", "warn", "chargeback")
            st.caption("V044: entities with no company evidence classify UNKNOWN instead of "
                       "silently billing ALFA. Empty is the goal state.")
            unm = run(mart_sql.unmapped_entities(f["days"]), page=_PAGE,
                      key=f"unmapped_{f['days']}", tier="hourly",
                      source="FACT_WAREHOUSE_DAILY + FACT_QUERY_SCHEMA_HOURLY + FACT_LOGIN_DAILY (COMPANY='UNKNOWN')")
            if unm.ok and unm.empty:
                st.success("Every entity in the window carries company evidence — nothing is billed blind.")
            elif guard(unm, ""):
                kpi_row([{"label": "Unmapped entities", "value": f"{len(unm.df)}", "delta_color": "inverse",
                          "help": "Facts re-stamp trailing 3 days nightly; older rows keep their "
                                  "original company until a backfill re-run."}])
                styled_table(unm.df, height=240)
                st.caption("Classify explicitly, then the next loader pass re-stamps: "
                           "`INSERT INTO DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE (SCOPE_TYPE, PATTERN, COMPANY) "
                           "VALUES ('WAREHOUSE'|'DATABASE'|'USER_OVERRIDE', '<NAME>', 'ALFA'|'Trexis');`")
                result_caption(unm)
    elif section == "Contract & Forecast":
        section_header("Contract pacing & renewal planner", "info", "contract")
        _contract_tab(settings)
    elif section == "Chargeback & AI":
        section_header("Department chargeback", "info", "chargeback")
        _chargeback_tab(f["company"], f["days"], rate, is_operator)
        st.divider()
        section_header("Query-tag governance", "info", "chargeback")
        st.caption("Chargeback precision is capped by tag coverage — untagged execution "
                   "time can only be allocated, never attributed.")
        # #36: MART_TAG_COVERAGE_DAILY is user x day grain and carries no
        # DATABASE_NAME/SCHEMA_NAME column, so it cannot honor the active
        # Database/Schema filter — served mart-first, a scoped chargeback screen
        # silently widened to company-wide. When a db/schema scope is set, read
        # the db/schema-predicated live QUERY_HISTORY path (which does carry those
        # columns) instead; with no scope, keep the cheap mart-first read.
        _tag_db = f["database"]
        _tag_sc = f["schema_contains"]
        if str(_tag_db).strip() or str(_tag_sc).strip():
            tags_res = run(
                cost_sql.tag_coverage(f["days"], f["company"], database=_tag_db,
                                      schema_contains=_tag_sc),
                page=_PAGE, key=f"tagcov_live_{f['company']}_{f['days']}_{_tag_db}_{_tag_sc}",
                tier="historical",
                source="QUERY_HISTORY (exec-time-weighted, db/schema-scoped live)")
            st.caption("Scoped to the active Database/Schema filter via the live "
                       "QUERY_HISTORY path (the tag-coverage mart is user-grain and carries "
                       "no object columns); the live window clamps to 90 days.")
        else:
            tags_res = run_mart_first(
                mart27_sql.tag_coverage_daily(f["days"], f["company"]),
                cost_sql.tag_coverage(f["days"], f["company"]),
                page=_PAGE, key=f"tagcov_{f['company']}_{f['days']}",
                mart_source="MART_TAG_COVERAGE_DAILY (mart, loaded hourly)",
                live_source="QUERY_HISTORY (exec-time-weighted, live fallback)")
        if guard(tags_res, "No workloads above the 60s floor in this window."):
            tdf_g = tags_res.df.copy()
            total_exec = float(tdf_g["EXEC_SEC"].sum())
            untagged = float(tdf_g["UNTAGGED_EXEC_SEC"].sum())
            kpi_row([
                {"label": "Tagged share (exec-time)",
                 "value": f"{(1 - untagged / total_exec) * 100 if total_exec else 100:,.1f}%",
                 "severity": "ok" if total_exec and untagged / total_exec < 0.3 else "warn"},
                {"label": "Top untagged user",
                 "value": (resolve_display(tdf_g.iloc[0]["USER_NAME"], user_display_map(_PAGE))
                           if len(tdf_g) else "n/a"),
                 "delta": (f"{humanize_duration(tdf_g.iloc[0]['UNTAGGED_EXEC_SEC'], 's')} untagged"
                           if len(tdf_g) else None),
                 "delta_color": "off"},
            ])
            styled_table(with_user_names(tdf_g, _PAGE), height=260, column_config={
                "TAGGED_PCT": st.column_config.NumberColumn("Tagged %", format="%.1f%%")})
            st.caption("Fix at the source: set QUERY_TAG in the tool/session that runs the "
                       "workload; the scoreboard moves within a day.")
        st.divider()
        section_header("Cortex / AI spend", "info", "cost")
        _cortex_spend_tab(f["days"], ai_rate)
        st.divider()
        section_header("AI users", "info", "operations")
        # r22 #14: the exact Cortex Code user scan is the heaviest read in
        # this group — it runs only when asked, like the other deep scans.
        from app.ui.components import toggle_cost_hint
        st.caption(toggle_cost_hint("cortex_users"))
        if st.toggle("Load AI user attribution (exact live token metering)",
                     key="ai_users_scan",
                     help="Runs the per-user Cortex Code scans; the rest of "
                          "this section stays cheap without it."):
            _ai_users_tab(f["company"], f["days"], ai_rate, settings, is_operator)
    elif section == "Unit costs":
        section_header("Unit costs — one query, one call, one AI request", "info", "cost")
        _unit_costs_tab(f, rate, ai_rate)
    elif section == "Compare":
        section_header("Compare — period vs period", "info", "cost")
        from app.ui.pages.cost_parts.compare import _compare_tab
        _compare_tab(f["company"], rate, ai_rate)
    else:
        section_header("Optimization", "info", "optimize")
        _optimization_tab(f["company"], f["days"], rate, settings, is_operator)
        # rec2: the savings ledger belongs to the inner "Remediation & ledger" pill
        # (set by _optimization_tab's nested lazy_sections) — render it only there,
        # so the other subgroups don't pay for its read.
        if st.session_state.get("opt_section") == "Remediation & ledger":
            st.divider()
            section_header("Savings ledger", "ok", "cost")
            _savings_tab()
