"""Cost & Contract — attribution, contract pacing, Cortex/storage, savings.

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

import streamlit as st

from app.config import core_object
from app.core.query import execute_statement, run, run_batch
from app.core.session import is_operator as _is_operator
from app.core.sqlsafe import sql_literal
from app.core.state import filters
from app.data import cost_sql, mart27_sql, mart_sql
from app.logic.directory import resolve_display
from app.logic.formulas import format_usd, humanize_duration, humanize_minutes_ago, safe_float
from app.logic.verdict import Signal, page_verdict
from app.ui.components import (
    alarm_health,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    page_verdict_line,
    result_caption,
    run_mart_first,
    section_filter_contract,
    section_header,
    selectable_table,
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

# GRAIN (as emitted by mart_sql.unmapped_entities) -> COMPANY_SCOPE.SCOPE_TYPE.
# Users map through USER_OVERRIDE (V044's COMPANY_FOR_USER checks it first).
_SCOPE_FOR_GRAIN = {"WAREHOUSE": "WAREHOUSE", "DATABASE": "DATABASE", "USER": "USER_OVERRIDE"}


def _unmapped_mapper(df, is_operator: bool) -> None:
    """Map an UNKNOWN-company entity to a company (owner ask: "map untapped users
    and backfill"). Builds the idempotent COMPANY_SCOPE upsert for the picked entity;
    an operator applies it in place, otherwise it's copy-paste for Snowsight. The
    next loader pass re-stamps go-forward facts — history needs a backfill re-run."""
    with st.expander("Map an entity to a company"):
        entities = [str(e) for e in df["ENTITY"].dropna().tolist() if str(e).strip()]
        if not entities:
            st.caption("Nothing to map in this window.")
            return
        c1, c2 = st.columns([3, 2])
        with c1:
            pick = st.selectbox("Entity", entities, key="unmap_entity")
        with c2:
            company_choice = st.selectbox("Company", ["ALFA", "Trexis"], key="unmap_company")
        _row = df[df["ENTITY"].astype(str) == str(pick)]
        grain = str(_row.iloc[0]["GRAIN"]).upper() if len(_row) else "WAREHOUSE"
        scope_type = _SCOPE_FOR_GRAIN.get(grain, "USER_OVERRIDE")
        pattern = str(pick).upper()
        note = f"Classified via OVERWATCH ({grain})"
        merge_sql = (
            f"MERGE INTO {core_object('COMPANY_SCOPE')} t\n"
            f"USING (SELECT {sql_literal(scope_type)} AS SCOPE_TYPE, "
            f"{sql_literal(pattern)} AS PATTERN) s\n"
            "ON t.SCOPE_TYPE = s.SCOPE_TYPE AND UPPER(t.PATTERN) = s.PATTERN\n"
            f"WHEN MATCHED THEN UPDATE SET COMPANY = {sql_literal(company_choice)}, "
            f"NOTE = {sql_literal(note)}\n"
            "WHEN NOT MATCHED THEN INSERT (COMPANY, SCOPE_TYPE, PATTERN, NOTE)\n"
            f"VALUES ({sql_literal(company_choice)}, s.SCOPE_TYPE, s.PATTERN, "
            f"{sql_literal(note)});"
        )
        st.code(merge_sql, language="sql")
        st.caption(
            f"Maps **{pick}** ({scope_type}) → **{company_choice}**. Go-forward facts stamp "
            "immediately; the nightly reconcile re-stamps the trailing 3 days. Older history "
            "keeps its original stamp until a full loader backfill re-run.")
        if is_operator and st.button("Apply mapping", key="unmap_apply"):
            ok, msg = execute_statement(merge_sql.replace("\n", " "), page=_PAGE)
            notify(ok, msg if not ok else f"Mapped {pick} → {company_choice}.")
        elif not is_operator:
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS — in-app "
                       "execution needs an admin profile.")


def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    page_header("Cost & Contract",
                "Where the money goes, whether the contract holds, and what savings are proven.",
                scope_note=f"{f['company']} · {f['window_label']}", icon_name="cost")
    # #3: operator gating from the VIEWER identity + allowlist, not CURRENT_ROLE().
    is_operator = _is_operator()
    # Six grouped sections instead of eight pills (CoCo density fix): each
    # group renders its related sub-panels under labeled section headers.
    # CoCo do-first #1: a page-level "should I worry?" opener. Contract runway is the
    # forward-looking cost signal; a cheap recent-tier (cached, mart-first) read.
    _exh = run(mart_sql.contract_exhaustion(), page=_PAGE, key="cost_verdict_exhaustion",
               tier="recent", source="SETTINGS + FACT_METERING_DAILY")
    _vsig = []
    if _exh.usable() and safe_float(_exh.df.iloc[0].get("TOTAL")) > 0:
        _dl = safe_float(_exh.df.iloc[0].get("DAYS_LEFT"), -1.0)
        if 0 <= _dl <= 30:
            _vsig.append(Signal("bad", f"contract runway {_dl:,.0f} days at current burn"))
        elif 0 <= _dl <= 90:
            _vsig.append(Signal("warn", f"contract runway {_dl:,.0f} days at current burn"))
    page_verdict_line(page_verdict(
        _vsig, healthy="contract on track at the current burn — open a section for detail"))
    # Cost3: a "what changed since your last visit" opener from the viewer's own
    # APP_USAGE trail + the alerts/actions raised while they were away.
    from app.core.identity import viewer_name
    from app.logic.actions import since_last_visit_summary
    if viewer_name():
        _slv = run(mart_sql.since_last_visit(f["company"]), page=_PAGE,
                   key="cost_since_last_visit", tier="recent",
                   source="APP_USAGE + ALERT_EVENTS + ACTION_QUEUE")
        if _slv.usable():
            import pandas as pd
            _lvrow = _slv.df.iloc[0]
            if pd.notna(_lvrow.get("LAST_VISIT")):
                _slv_sum = since_last_visit_summary(
                    _lvrow.get("NEW_ALERTS"), _lvrow.get("NEW_CRIT"),
                    _lvrow.get("NEW_HIGH"), _lvrow.get("NEW_ACTIONS"))
                _ago = humanize_minutes_ago(_lvrow.get("MINUTES_AGO"))
                _slv_msg = f"Since your last visit ({_ago}): {_slv_sum['text']}."
                _slv_sev = _slv_sum["severity"]
                if _slv_sev == "bad":
                    st.warning(_slv_msg)
                elif _slv_sev == "warn":
                    st.info(_slv_msg)          # new highs/actions stay visible, not a muted caption
                else:
                    st.caption(_slv_msg)
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
            # v4.157.0: measured_query_costs honors warehouse/user contains too —
            # declare them so the banner stops warning "ignored" where it filters.
            "applies": ("company", "days", "warehouse_contains", "user_contains",
                        "database", "schema_contains"),
            "note": "Each metric retains its declared measured or allocated grain.",
        },
        "Compare": {
            "applies": ("company",),
            "note": "Panel-local periods replace the global Window.",
        },
        "Optimization & Savings": {
            "applies": (),
            # v4.157.0: the expensive/repeat/pruning scan panels honor db + schema
            # when a scan is run — declare them partial so the banner is accurate.
            "partial": ("company", "days", "database", "schema_contains"),
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
        section_header("Spend", "info", "spend", anchor="cost-spend")
        _spend_tab(f["company"], f["days"], rate, ai_rate, f["database"],
                   metering_res=_pf.get("metering"), csr_res=_pf.get("csr"),
                   coco_res=_pf.get("coco"))
        st.divider()
        section_header("Attribution", "info", "chargeback", anchor="cost-attribution")
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
            section_header("Storage", "info", "cost", anchor="cost-storage")
            _storage_tab(f["company"], f["days"], settings)
            st.divider()
            unm = run(mart_sql.unmapped_entities(f["days"]), page=_PAGE,
                      key=f"unmapped_{f['days']}", tier="hourly",
                      source="FACT_WAREHOUSE_DAILY + FACT_QUERY_SCHEMA_HOURLY + FACT_LOGIN_DAILY (COMPANY='UNKNOWN')")
            # C23: "empty is the goal state" — so the header is green when it is.
            section_header("Unmapped entities", alarm_health(unm), "chargeback",
                           anchor="cost-unmapped")
            st.caption("V044: entities with no company evidence classify UNKNOWN instead of "
                       "silently billing ALFA. Empty is the goal state.")
            if unm.ok and unm.empty:
                st.success("Every entity in the window carries company evidence — nothing is billed blind.")
            elif guard(unm, ""):
                import pandas as pd

                # Owner ask ("add costs"): put a dollar figure on the worklist. Only the
                # WAREHOUSE rows carry credits (MEASURE='credits'); DATABASE/USER rows are
                # counts with no direct $, so their Est. $ stays blank.
                _disp = unm.df.copy()
                _credits = pd.to_numeric(_disp["VALUE"], errors="coerce").where(
                    _disp["MEASURE"].astype(str).eq("credits"))
                _disp["EST_USD"] = (_credits * float(rate)).round(2)
                _at_stake = float(_disp["EST_USD"].sum())
                kpi_row([
                    {"label": "Unmapped entities", "value": f"{len(unm.df)}", "delta_color": "inverse",
                     "help": "Facts re-stamp trailing 3 days nightly; older rows keep their "
                             "original company until a backfill re-run."},
                    {"label": "Unmapped spend, billed blind", "value": format_usd(_at_stake),
                     "delta_color": "inverse" if _at_stake > 0 else "normal",
                     "help": "Estimated $ (warehouse credits x contract rate) in this window "
                             "stamped UNKNOWN — real money not yet attributed to a company."},
                ])
                styled_table(_disp, height=240, column_config={
                    "EST_USD": st.column_config.NumberColumn("Est. $ (window)", format="$%.0f"),
                })
                result_caption(unm)
                _unmapped_mapper(_disp, is_operator)
    elif section == "Contract & Forecast":
        section_header("Contract pacing & renewal planner", "info", "contract")
        _contract_tab(settings)
    elif section == "Chargeback & AI":
        section_header("Department chargeback", "info", "chargeback", anchor="cost-chargeback")
        _chargeback_tab(f["company"], f["days"], rate, is_operator)
        st.divider()
        section_header("Query-tag governance", "info", "chargeback", anchor="cost-tags")
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
            # Capture the display frame BEFORE indexing so the positional
            # (iloc) selection stays stable; with_user_names adds a friendly
            # USER column but KEEPS the raw login USER_NAME the drill filters on.
            _disp = with_user_names(tdf_g, _PAGE)
            _usel = selectable_table(
                _disp, key=f"tagcov_sel_{f['company']}_{f['days']}_{f['database']}_{f['schema_contains']}",
                height=260,
                column_config={"TAGGED_PCT": st.column_config.NumberColumn("Tagged %", format="%.1f%%")})
            st.caption("Fix at the source: set QUERY_TAG in the tool/session that runs the "
                       "workload; the scoreboard moves within a day.")
            # Click a user row -> their top untagged statement types (live
            # QUERY_HISTORY read; interaction-gated). Index into the RAW
            # USER_NAME, not the USER display column. Bounds-guard the sticky
            # selection against a window narrow that shrinks the frame.
            _sel_user = (str(_disp.iloc[int(_usel)]["USER_NAME"])
                         if _usel is not None and 0 <= int(_usel) < len(_disp) else "")
            if _sel_user:
                _u_db, _u_sc = f["database"], f["schema_contains"]
                ut = run(cost_sql.untagged_executions_for_user(
                             _sel_user, f["days"], f["company"], _u_db, _u_sc),
                         page=_PAGE,
                         key=f"untagged_user_{f['company']}_{f['days']}_{_sel_user}_{_u_db}_{_u_sc}",
                         tier="historical", source="QUERY_HISTORY (untagged executions, per user)")
                st.markdown(f"**Untagged executions — "
                            f"{resolve_display(_sel_user, user_display_map(_PAGE))}**")
                if guard(ut, "No untagged executions for this user in the window/scope."):
                    styled_table(ut.df, height=240)
                    result_caption(ut)
                    # The drill is a LIVE scan capped at ~90d; the scoreboard above can be
                    # mart-served over a longer window, so its untagged seconds may exceed
                    # this decomposition. Say so rather than let the numbers silently differ.
                    if isinstance(f.get("days"), int) and f["days"] > 90:
                        st.caption("Live scan of the last ~90 days — with a longer page window the "
                                   "row above (mart-served) can span more, so these totals are a "
                                   "recent-90d subset, not a full-window decomposition.")
            else:
                st.caption("Click a user row to see their top untagged statement types.")
        st.divider()
        section_header("Cortex / AI spend", "info", "cost", anchor="cost-cortex")
        _cortex_spend_tab(f["days"], ai_rate)
        st.divider()
        section_header("AI users", "info", "operations", anchor="cost-ai-users")
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
