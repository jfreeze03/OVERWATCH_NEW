"""Operations — queries, tasks, warehouses, contention, change impact, releases, pipeline SLAs."""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from app import companies
from app.config import core_object
from app.core.errors import safe_page
from app.core.identity import identity_sql
from app.core.query import execute_cancel_query, execute_statement, run, run_batch
from app.core.session import is_operator as _is_operator
from app.core.sqlsafe import sql_literal
from app.core.state import filters, navigation_context, request_navigation
from app.data import (
    change_impact_sql,
    dq_sql,
    insights_sql,
    mart27_sql,
    mart_sql,
    ops_sql,
    security_sql,
    workbench_sql,
)
from app.logic import proc_regression, query_advisor, remediation, wh_change
from app.logic.ai_prompts import release_compare_prompt, task_failure_prompt
from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    anomaly_markers,
    complete_days_only,
    flag_anomalies,
    suppress_expected_spikes,
)
from app.logic.dq import row_volume_anomalies, summarize_row_volume
from app.logic.formulas import (
    account_today,
    credits_to_usd,
    format_usd,
    humanize_duration,
    humanize_gb,
    safe_float,
)
from app.logic.incident import route_incidents, summarize_incidents
from app.logic.insights import (
    build_failure_timeline,
    cluster_failures_by_family,
    compare_release_periods,
    duration_sla_forecast,
    pipeline_sla_forecast,
    rank_release_candidates,
    task_duration_anomalies,
    task_release_deltas,
    with_auto_suspend_settings,
)
from app.logic.sizing import size_recommendations, sizing_summary
from app.logic.task_graph import (
    analyze_task_run,
    canonical_task_name,
    compare_task_versions,
    inspect_task_graph,
)
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import (
    add_to_case_button,
    alarm_health,
    confirm_gate,
    empty_state,
    entity_nav_table,
    evidence_gate,
    exception_summary,
    export_button,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    nested_sections,
    notify,
    page_header,
    panel_help,
    result_caption,
    run_mart_first,
    section_filter_contract,
    section_header,
    selectable_nav_table,
    selectable_table,
    served_days,
    snowsight_profile_column,
    stamp_write,
    stash_section_count,
    stashed_counts,
    status_chips,
    styled_table,
    with_user_names,
    write_gate_open,
)

_PAGE = "Operations"


def _queries_tab(company: str, days: int, wh_filter: str, user_filter: str,
                 database: str = "", schema_contains: str = "") -> None:
    nav_query = str(navigation_context().get("query_id") or "").strip()
    nav_signature = f"query:{nav_query}"
    if nav_query and st.session_state.get("_ow_ops_context_applied") != nav_signature:
        st.session_state["_ops_drill_target"] = nav_query
        st.session_state["ops_drill_manual"] = nav_query
        st.session_state["_ow_ops_context_applied"] = nav_signature
    if str(company or "ALL").upper() != "ALL":
        st.caption(
            f"Scope: queries on {company} warehouses (by COMPANY_FOR_WAREHOUSE, matching "
            "Cost). A cross-company user's queries on those warehouses are included here and "
            "counted under the warehouse's company, not the user's (C10) — use ALL for the "
            "account-wide total."
        )
    # Hot path: batch the independent hourly mart reads that paint the normal
    # Operations landing (summary, sparklines, top queries, failure families).
    # Filtered top-N still needs the true live scan; every mart keeps its live
    # fallback below. Per-member cache reuse means a changed filter only executes
    # the members whose SQL changed.
    _use_diag = not (wh_filter or user_filter or database or schema_contains)
    _summary_sql = ""
    _summary_key = ""
    _summary_source = ""
    if not schema_contains:
        _summary_sql = mart_sql.fact_query_window_summary(
            days, company, wh_filter, user_filter, database)
        _summary_key = f"q_fact_summary_{company}_{days}"
        _summary_source = "FACT_QUERY_HOURLY (mart, loaded hourly)"
    elif not wh_filter and not user_filter:
        _summary_sql = mart27_sql.schema_window_summary(
            days, company, database, schema_contains)
        _summary_key = f"q_schema_fact_{company}_{days}"
        _summary_source = "FACT_QUERY_SCHEMA_HOURLY (mart — p95 is peak hourly)"
    _activity_sql = mart_sql.fact_daily_activity(14, company, database)
    _mart_jobs = [
        {"key": "activity", "sql": _activity_sql,
         "source": "FACT_QUERY_HOURLY (daily)"},
    ]
    if _summary_sql:
        _mart_jobs.append(
            {"key": "summary", "sql": _summary_sql, "source": _summary_source})
    if _use_diag:
        _mart_jobs.extend([
            {"key": "top", "sql": mart27_sql.ops_diag_top_queries(days, company, 50),
             "source": "MART_OPS_DIAG_HOURLY (mart — union of hourly top-50s)",
             "max_rows": 50},
            {"key": "fails", "sql": mart27_sql.ops_diag_failures(days, company),
             "source": "MART_OPS_DIAG_HOURLY (mart — users = HLL approx-distinct)"},
        ])
    _mart_pf = run_batch(_mart_jobs, page=_PAGE, tier="hourly") if len(_mart_jobs) > 1 else {}

    summary = None
    used_mart = False
    if _summary_sql:
        m = _mart_pf.get("summary") if isinstance(_mart_pf, dict) else None
        if m is None or not m.ok:
            m = run(_summary_sql, page=_PAGE, key=_summary_key, tier="hourly",
                    source=_summary_source)
        if m.ok and not m.empty and safe_float(m.df.iloc[0].get("QUERY_COUNT")) > 0:
            summary, used_mart = m, True
    if summary is None:
        summary = run(ops_sql.query_window_summary(days, company, wh_filter, user_filter, database, schema_contains),
                      page=_PAGE, key=f"q_summary_{company}_{days}", tier="recent",
                      source="ACCOUNT_USAGE.QUERY_HISTORY")
    if summary.usable():
        row = summary.df.iloc[0]
        qcount = safe_float(row.get("QUERY_COUNT"))
        failed = safe_float(row.get("FAILED_COUNT"))
        fail_pct = (failed / qcount * 100) if qcount else None
        activity = _mart_pf.get("activity") if isinstance(_mart_pf, dict) else None
        if activity is None or not activity.ok:
            activity = run(_activity_sql, page=_PAGE, key="ops_spark_activity",
                           tier="hourly", source="FACT_QUERY_HOURLY (daily)")
        q_spark = (activity.df["QUERIES"].tolist()
                   if activity.usable() and "QUERIES" in activity.df.columns else None)
        f_spark = (activity.df["FAILS"].tolist()
                   if activity.usable() and "FAILS" in activity.df.columns else None)
        kpi_row([
            {"label": f"Queries ({days}d)", "value": f"{qcount:,.0f}", "spark": q_spark},
            {"label": "Fail rate", "value": f"{fail_pct:.2f}%" if fail_pct is not None else "n/a",
             "delta": f"{failed:,.0f} failed" if fail_pct is not None else "No query denominator",
             "delta_color": "off", "spark": f_spark,
             # rec#49: warn only above the 2% materiality threshold Control Room
             # (> 0.02) and the platform score already use — not on any single
             # failed query, which made this tile alarm while the others stayed calm.
             "severity": ("warn" if (fail_pct is not None and fail_pct > 2.0)
                          else ("ok" if fail_pct is not None else ""))},
            {"label": "p95 runtime" + (" (peak hourly)" if used_mart else ""),
             "value": humanize_duration(row.get("P95_ELAPSED_SEC"), "s"),
             "help": "Highest hourly p95 from the fact table — a peak, not the "
                     "whole-window p95."
                     if used_mart else None},
            {"label": "Queued", "value": humanize_duration(row.get("QUEUED_SEC"), "s")},
            {"label": "Remote spill", "value": humanize_gb(row.get("SPILL_REMOTE_GB"))},
        ])
        result_caption(summary)

    if _use_diag:
        _qb = {
            "top": run_mart_first(
                mart27_sql.ops_diag_top_queries(days, company, 50),
                ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter,
                                               user_filter, database, schema_contains),
                page=_PAGE, key=f"q_top_{company}_{days}",
                mart_source="MART_OPS_DIAG_HOURLY (mart — union of hourly top-50s)",
                live_source="QUERY_HISTORY (live fallback)",
                mart_tier="hourly", live_tier="recent", max_rows=50,
                preloaded=_mart_pf.get("top") if isinstance(_mart_pf, dict) else None),
            "fails": run_mart_first(
                mart27_sql.ops_diag_failures(days, company),
                ops_sql.failures_by_error(days, company, wh_filter, user_filter, database, schema_contains),
                page=_PAGE, key=f"q_fails_{company}_{days}",
                mart_source="MART_OPS_DIAG_HOURLY (mart — users = HLL approx-distinct)",
                live_source="QUERY_HISTORY (live fallback)",
                mart_tier="hourly", live_tier="recent",
                preloaded=_mart_pf.get("fails") if isinstance(_mart_pf, dict) else None),
        }
    else:
        # Parallel path (same contract as Security): both queries submit async
        # in one shot; any failure falls back to the serial per-query calls.
        _qb = run_batch([
            {"key": "top", "sql": ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter,
                                                                 user_filter, database, schema_contains),
             "source": "ACCOUNT_USAGE.QUERY_HISTORY", "max_rows": 50},
            {"key": "fails", "sql": ops_sql.failures_by_error(days, company, wh_filter, user_filter, database, schema_contains),
             "source": "ACCOUNT_USAGE.QUERY_HISTORY"},
        ], page=_PAGE, tier="recent")

    section_header("Heaviest queries", "info", "search")
    top = _qb.get("top")
    if top is None or not top.ok:
        top = run(
            ops_sql.top_queries_by_elapsed(
                days, company, 50, wh_filter, user_filter, database, schema_contains),
            page=_PAGE, key=f"q_top_{company}_{days}", tier="recent",
            source="ACCOUNT_USAGE.QUERY_HISTORY", max_rows=50)
    if guard(top, "No queries in this window/scope."):
        # r24: every QUERY_ID row links straight to its Snowsight profile
        # (owner ask — the drill's single link earned its keep).
        _tp, _tp_cfg = snowsight_profile_column(top.df, _PAGE)
        _tp = with_user_names(_tp, _PAGE)
        # QUERY_ID leads so the drill target is visible (owner ask: "I don't know the query
        # id to select"); a row click also fills the drill field with it (below).
        _tp_cols = ["QUERY_ID", "START_TIME", "USER", "USER_NAME", "WAREHOUSE_NAME", "ELAPSED_SEC",
                    "QUEUED_SEC", "SPILL_REMOTE_GB", "EXECUTION_STATUS", "QUERY_PREVIEW"]
        if "PROFILE" in _tp.columns:
            _tp_cols.append("PROFILE")
        sel_q = selectable_table(
            _tp[_tp_cols],
            key="ops_top_sel",
            column_config={
                "START_TIME": st.column_config.DatetimeColumn("Started", format="MMM DD, HH:mm"),
                "ELAPSED_SEC": st.column_config.NumberColumn("Elapsed (s)", format="%.1f"),
                "QUEUED_SEC": st.column_config.NumberColumn("Queued (s)", format="%.1f"),
                "SPILL_REMOTE_GB": st.column_config.NumberColumn("Spill GB", format="%.2f"),
                **_tp_cfg,
            },
        )
        st.caption("Elapsed-time ranking.")

    # A second live QUERY_HISTORY scan (ranked by inefficiency, not elapsed). It is a
    # deliberate drill, so toggle-gate it off first paint — the Heaviest-queries scan
    # above already pays one live round-trip for this section.
    # C23: a toggle-gated TOOL, not a findings surface — neutral until it runs.
    section_header("Optimization triage", "", "optimize")
    _triage_on = st.toggle(
        "Run optimization triage (rank statements by spill / pruning / cold scan)",
        key="ops_triage_toggle",
        help="A second live scan that surfaces the specific statements an optimizer should "
             "fix — remote spill, poor partition pruning, or huge cold-cache scans.")
    if _triage_on:
        _triage = run(
            ops_sql.query_optimization_triage(
                days, company, wh_filter, user_filter, database, schema_contains),
            page=_PAGE, key=f"q_triage_{company}_{days}", tier="recent",
            source="ACCOUNT_USAGE.QUERY_HISTORY (spill / pruning / scan)", max_rows=50)
        if _triage.ok and _triage.empty:
            empty_state("clean", "No spill-heavy or poorly-pruned statements in this window — nothing to optimize first.")
        elif guard(_triage, "No queries in this window/scope."):
            _tr, _tr_cfg = snowsight_profile_column(_triage.df, _PAGE)
            _tr = with_user_names(_tr, _PAGE)
            _tr_cols = [c for c in ["REASON", "QUERY_ID", "USER", "WAREHOUSE_NAME", "SPILL_REMOTE_GB",
                                    "GB_SCANNED", "SCAN_PCT", "CACHE_PCT", "ELAPSED_SEC", "QUERY_PREVIEW",
                                    "PROFILE"] if c in _tr.columns]
            _tri_sel = selectable_table(_tr[_tr_cols], key="ops_triage_sel", column_config=_tr_cfg,
                                        sort_label="spill, then pruning, then scan")
            if _tri_sel is not None and _tri_sel != st.session_state.get("_ops_tri_sel_last"):
                st.session_state["_ops_tri_sel_last"] = _tri_sel
                _tqid = str(_tr.iloc[int(_tri_sel)]["QUERY_ID"])
                st.session_state["_ops_drill_target"] = _tqid
                st.session_state["ops_drill_manual"] = _tqid
            st.caption("Ranked worst-first: remote spill, then poor partition pruning (SCAN_PCT), then "
                       "large cold-cache scans. 'Real work' only — CALL wrappers and app queries excluded. "
                       "Click a row to load it in the drill-through below.")
            add_to_case_button(
                "Operations · Queries", _triage,
                title="Optimization triage — statements to fix first",
                summary="Heaviest statements ranked by spill / poor pruning / cold scan.",
                next_action="Open the worst rows in the query drill-through and profile in Snowsight.",
                key=f"ow_case_add_ops_triage_{company}_{days}")

    # A third live QUERY_HISTORY read: per-proc CALL rollup (which procs dominate
    # the workload, with fail rate) plus a p95-vs-prior-window degradation view.
    # Toggle-gated like the triage scan above — a live round-trip only when asked;
    # the two reads submit async via run_batch.
    section_header("Stored-procedure regression", "", "optimize")   # C23: tool, not alarm
    _proc_reg_on = st.toggle(
        "Roll up stored-proc runtimes and compare to the prior window",
        key="ops_proc_reg_toggle",
        help="A live scan that rolls up CALL statements per stored procedure "
             "(success-only p95/avg, ranked by SLA impact) and flags the ones whose "
             "p95 crept up versus the previous equal-length window.")
    if _proc_reg_on:
        _pb = run_batch([
            {"key": "roll", "sql": ops_sql.proc_sla_rollup(
                days, company, wh_filter, user_filter, database, schema_contains),
             "source": "ACCOUNT_USAGE.QUERY_HISTORY (proc SLA-impact rollup)", "max_rows": 200},
            {"key": "reg", "sql": ops_sql.proc_regression(
                days, company, wh_filter, user_filter, database, schema_contains,
                min_calls=proc_regression.MIN_CALLS),
             "source": "ACCOUNT_USAGE.QUERY_HISTORY (proc p95 vs prior window)", "max_rows": 200},
        ], page=_PAGE, tier="recent")

        _roll = _pb.get("roll")
        if _roll is not None and _roll.ok and _roll.empty:
            empty_state("no_data_yet", "No stored-procedure CALLs in this window/scope.")
        elif _roll is not None and guard(_roll, "No stored-procedure CALLs in this window/scope."):
            st.markdown("**Busiest procedures — by SLA impact (calls × p95)**")
            _roll_cols = [c for c in ["PROC_NAME", "DATABASE_NAME", "SCHEMA_NAME", "CALLS",
                                      "FAIL_PCT", "AVG_S", "P95_S", "MAX_S", "TOTAL_MIN"]
                          if c in _roll.df.columns]
            styled_table(_roll.df[_roll_cols], slug="proc_sla_rollup", days=days,
                         sort_label="calls × p95 desc")
            st.caption("Success-only latency; FAIL_PCT surfaces procs that are failing rather "
                       "than slow (including a proc broken enough to drop out of the regression "
                       "view below). Ranked by frequency × p95.")

        _reg = _pb.get("reg")
        st.markdown("**Runtime regression — this window vs the prior window**")
        if _reg is not None and _reg.ok and _reg.empty:
            empty_state(
                "clean",
                f"No stored procedure with at least {proc_regression.MIN_CALLS} successful "
                "calls in both windows moved materially — proc runtimes are stable.")
        elif _reg is not None and guard(_reg, "Not enough repeated proc calls to compare windows."):
            _pr = proc_regression.annotate(_reg.df)
            _regressed = int((_pr["VERDICT"] == "Regressed").sum())
            _failing = int((_pr["VERDICT"] == "Faster but failing").sum())
            kpi_row([
                {"label": "Procs compared", "value": f"{len(_pr):,}"},
                {"label": "Regressed", "value": f"{_regressed:,}",
                 "severity": ("bad" if _regressed else "ok")},
                {"label": "Faster but failing", "value": f"{_failing:,}",
                 "severity": ("warn" if _failing else "ok")},
            ])
            _pr_cols = [c for c in ["VERDICT", "PROC_NAME", "DATABASE_NAME", "SCHEMA_NAME",
                                    "CUR_CALLS", "PRIOR_CALLS", "CUR_P95_S", "PRIOR_P95_S",
                                    "P95_DELTA_PCT", "CUR_FAIL_PCT", "PRIOR_FAIL_PCT"]
                        if c in _pr.columns]
            styled_table(_pr[_pr_cols], slug="proc_regression", days=days,
                         sort_label="p95 growth desc")
            st.caption(
                "Success-only p95 this window vs the prior equal-length window (percent change "
                "from the unrounded values). A proc needs at least "
                f"{proc_regression.MIN_CALLS} successful calls in BOTH windows to be compared — a "
                "now-fully-failing proc has no latency signal, so watch the rollup's FAIL_PCT "
                "above. 'Faster but failing' flags a proc that only looks quicker because it now "
                "errors out. Proc name is parsed from the CALL text and grouped with DB+schema; a "
                "bare vs fully-qualified CALL can split one proc. ~6h ACCOUNT_USAGE latency, so "
                "the newest hours under-report.")

    section_header("Query drill-through", "info", "search")
    candidate_ids: list[str] = []
    if top.usable():
        candidate_ids = [str(q) for q in top.df["QUERY_ID"].dropna().head(50)]
    clicked_qid = ""
    try:
        # r6-bug10: act only on a NEW row selection. st.dataframe's selection is sticky and
        # re-emitted every rerun; overwriting _ops_drill_target unconditionally undid a
        # manual paste/pick, and after the table reloaded (e.g. a Window change) the stale
        # index silently repointed the detail to whatever query now sits at that row.
        if (top.usable() and sel_q is not None
                and sel_q != st.session_state.get("_ops_top_sel_last")):
            st.session_state["_ops_top_sel_last"] = sel_q
            clicked_qid = str(top.df.iloc[int(sel_q)]["QUERY_ID"])
            st.session_state["_ops_drill_target"] = clicked_qid
            # Surface the clicked id in the paste box so the click has a VISIBLE effect
            # (owner ask: "when I click to drilldown it does nothing"). Set before the
            # text_input renders below, mirroring the nav_query handoff at the top.
            st.session_state["ops_drill_manual"] = clicked_qid
    except (KeyError, IndexError, ValueError, TypeError):
        clicked_qid = ""
    # r10 #14: the drill no longer vanishes when the table is empty —
    # manual query-ID entry always works.
    if candidate_ids:
        picked = st.selectbox("Query ID (from the table above, heaviest first — or click a row)",
                              candidate_ids, key="ops_drill_pick")
    else:
        picked = ""
    manual = st.text_input("Paste any query ID (works even with no candidates above)",
                           key="ops_drill_manual")
    target = (clicked_qid or manual or picked or "").strip()
    if target and st.button("Load query detail", key="ops_drill_go"):
        st.session_state["_ops_drill_target"] = target
    target_id = st.session_state.get("_ops_drill_target", "")
    if target_id:
        # r22 #17: rows from the table carry START_TIME — bound the detail
        # scan to that day +/-1 instead of the whole 365-day retention.
        _hint = ""
        try:
            if top.usable() and "START_TIME" in top.df.columns:
                _match = top.df[top.df["QUERY_ID"].astype(str) == str(target_id)]
                if len(_match):
                    _hint = str(_match.iloc[0]["START_TIME"])[:10]
        except (KeyError, IndexError, ValueError, TypeError):
            _hint = ""
        try:
            detail_sql = insights_sql.query_detail(target_id, _hint)
        except ValueError as exc:
            st.error(str(exc))
            detail_sql = ""
        if detail_sql:
            detail = run(detail_sql, page=_PAGE, key=f"drill_{target_id[:16]}",
                         tier="recent", source="ACCOUNT_USAGE.QUERY_HISTORY (single query)")
            if guard(detail, "Query not found (IDs age out of QUERY_HISTORY after 365 days)."):
                row = detail.df.iloc[0]
                kpi_row([
                    {"label": "Elapsed", "value": humanize_duration(row.get("ELAPSED_SEC"), "s"),
                     "delta": f"queued {humanize_duration(row.get('QUEUED_SEC'), 's')}",
                     "delta_color": "off"},
                    {"label": "Scanned", "value": humanize_gb(row.get("GB_SCANNED")),
                     "delta": f"{safe_float(row.get('CACHE_PCT')):,.0f}% cache", "delta_color": "off"},
                    {"label": "Partitions", "value": (f"{int(safe_float(row.get('PARTITIONS_SCANNED'))):,}"
                                                      f"/{int(safe_float(row.get('PARTITIONS_TOTAL'))):,}"),
                     "help": "Scanned vs total — a high ratio means the query reads almost the whole table."},
                    {"label": "Spill", "value": f"{humanize_gb(row.get('REMOTE_SPILL_GB'))} remote"},
                    {"label": "Status", "value": str(row.get("EXECUTION_STATUS", "?"))},
                ])
                st.code(str(row.get("QUERY_TEXT") or ""), language="sql")
                # Per-query optimization advisor: deterministic findings + a
                # 0-100 "optimize me first" score from the row already loaded
                # (zero extra query, zero AI). Thresholds match the triage table.
                _adv_findings, _adv_score = query_advisor.advise(row)
                if _adv_findings:
                    section_header("Optimization advisor",
                                   "bad" if _adv_score >= 40 else "warn", "optimize")
                    st.markdown(
                        f"**Optimize-me-first score: {_adv_score}/100** — "
                        f"{len(_adv_findings)} finding"
                        f"{'s' if len(_adv_findings) != 1 else ''}, most impactful first.")
                    status_chips([(f.title, "bad" if f.severity == "bad" else "")
                                  for f in _adv_findings])
                    for _f in _adv_findings:
                        st.markdown(f"- {_f.detail}")
                    # OPT-IN Cortex rewrite: button-gated, credit-warned, grounded in
                    # exactly this query's stats + the findings above. Never auto-runs.
                    from app.logic.ai_prompts import query_optimization_prompt
                    from app.ui.ai_panel import ai_evaluation_panel
                    ai_evaluation_panel(
                        key=f"qadv_{target_id[:12]}",
                        prompt=query_optimization_prompt(row, _adv_findings),
                        settings=load_settings(_PAGE),
                        page=_PAGE,
                        subject="rewrite this query to remove the flagged inefficiencies",
                    )
                elif str(row.get("EXECUTION_STATUS", "")).upper() == "SUCCESS":
                    st.caption("No obvious inefficiencies detected for this query.")
                ctx = run(
                    "SELECT CURRENT_ORGANIZATION_NAME() AS ORG, CURRENT_ACCOUNT_NAME() AS ACCT",
                    page=_PAGE, key="drill_ctx", tier="metadata", source="session context")
                if ctx.usable():
                    org = str(ctx.df.iloc[0].get("ORG", "") or "")
                    acct = str(ctx.df.iloc[0].get("ACCT", "") or "")
                    if org and acct:
                        st.markdown(
                            f"[Open the query profile in Snowsight]"
                            f"(https://app.snowflake.com/{org.lower()}/{acct.lower()}"
                            f"/#/compute/history/queries/{target_id}/profile)")
                if str(row.get("ERROR_MESSAGE") or "").strip():
                    st.error(f"{row.get('ERROR_CODE')}: {row.get('ERROR_MESSAGE')}")
                result_caption(detail)

    fails = _qb.get("fails")
    if fails is None or not fails.ok:
        fails = run(
            ops_sql.failures_by_error(days, company, wh_filter, user_filter, database, schema_contains),
            page=_PAGE, key=f"q_fails_{company}_{days}", tier="recent",
            source="ACCOUNT_USAGE.QUERY_HISTORY")
    # C23: amber only when failures exist; a clean window reads verified-green.
    section_header("Failures by error", alarm_health(fails), "alerts")
    if guard(fails, "No failed queries in this window.", kind="clean"):
        styled_table(fails.df)

    # rec#17: failed / killed / aborted queries that consumed warehouse compute produced
    # zero value — allocate the hour-share credits to non-success queries and rank the
    # repeat offenders, so a retrier hammering a broken query surfaces as $ wasted.
    section_header("Wasted spend (failed / killed / aborted)", "", "cost")   # C23: toggle-gated tool
    st.caption(
        "Allocated, not billed: each non-success query's execution-time share of its "
        "warehouse-hour credits — money spent on runs that produced nothing. Grouped by "
        "query fingerprint; a repeat-failing fingerprint is usually a broken query on a "
        "retry loop (raise the timeout, fix the query, or stop the retrier)."
    )
    if st.toggle("Run wasted-spend scan (hour-share allocation)", key="ops_waste_toggle",
                 help="Allocates warehouse-hour credits to non-success queries by execution-time share."):
        rate = safe_float(load_settings(_PAGE).get("CREDIT_PRICE_USD"), 3.68)
        waste = run(
            insights_sql.wasted_query_spend_usd(days, company, wh_filter, user_filter,
                                                database, schema_contains),
            page=_PAGE, key=f"q_waste_{company}_{days}", tier="historical",
            source="QUERY_HISTORY x WAREHOUSE_METERING_HISTORY (hour-share, non-success)")
        if guard(waste, "No failed/killed queries consumed warehouse compute in this window.", kind="clean"):
            wdf = waste.df.copy()
            wdf["WASTED_USD"] = wdf["WASTED_CREDITS"].map(
                lambda c: round(credits_to_usd(safe_float(c), rate, round_cents=False), 2))
            monthly = float(wdf["WASTED_USD"].sum()) / max(days, 1) * 30.0
            kpi_row([
                {"label": f"Wasted spend ({days}d)", "value": format_usd(float(wdf["WASTED_USD"].sum())),
                 "help": "Allocated compute on non-success queries; monthly-ized at right."},
                {"label": "Monthly-ized", "value": format_usd(monthly)},
                {"label": "Repeat offenders",
                 "value": str(int((wdf["FAILED_RUNS"].map(safe_float) >= 5).sum())),
                 "help": "Fingerprints that failed 5+ times in the window."},
            ])
            wdf = with_user_names(wdf, _PAGE)
            _wcols = [c for c in ["WASTED_USD", "FAILED_RUNS", "USER", "USER_NAME", "WAREHOUSE_NAME",
                                  "EXECUTION_STATUS", "ERROR_CODE", "QUERY_TYPE", "QUERY_SNIPPET",
                                  "FINGERPRINT"] if c in wdf.columns]
            styled_table(
                wdf[_wcols], height=320, sort_label="wasted $ desc",
                column_config={"WASTED_USD": st.column_config.NumberColumn("Wasted $", format="$%.2f")})
            result_caption(waste, note="allocated by execution-second share; non-success queries only")

    _query_insights_panel()


def _query_insights_panel() -> None:
    """Snowflake-authored query improvement suggestions (repo review wave 2:
    QUERY_INSIGHTS) — free, vendor-written fixes beside our own heuristics.
    Optional view: probe-gated, honest not-available state."""
    section_header("Query insights (Snowflake-authored)", "info", "search")
    qi = run(insights_sql.query_insights_feed(7), page=_PAGE, key="query_insights_7",
             tier="historical", source="ACCOUNT_USAGE.QUERY_INSIGHTS", probe=True)
    if not qi.ok:
        st.caption("QUERY_INSIGHTS isn't available on this account/edition yet — "
                   "when Snowflake exposes it, its own per-query suggestions appear "
                   "here automatically.")
        return
    if qi.empty:
        empty_state("clean", "Snowflake recorded no query-improvement insights in the last 7 days.")
        return
    styled_table(qi.df, height=260)
    st.caption("Snowflake's OWN analysis of your queries (account-wide, 7d) — each row is a "
               "repeated improvement opportunity the engine itself identified; treat the "
               "sample message as the fix hint. Complements the family heuristics above.")
    result_caption(qi)


def _failure_timeline_section(company: str, database: str = "", schema_contains: str = "",
                              known_failures: float | None = None) -> None:
    """Root-cause vs cascade view of recent task failures (ported)."""
    # C23: the caller passes the already-known failure count when it has one.
    section_header("Failure root-cause timeline (7d)",
                   alarm_health(None if known_failures is None else int(known_failures)),
                   "alerts")
    if known_failures is not None and known_failures <= 0:
        empty_state("clean", "No task failures in the last 7 days for this scope.")
        return
    # P6: hourly, not recent. This is the page's most expensive read (~15s: a 7-day
    # TASK_HISTORY failure scan) and ACCOUNT_USAGE.TASK_HISTORY lags up to ~45 min
    # (ACCOUNT_USAGE_LAG_NOTE), so a 300s TTL bought no freshness whatsoever — it
    # just re-paid 15 seconds up to 12x an hour for bytes that could not have
    # changed. An hour still refreshes well inside the source's own latency.
    res = run(insights_sql.task_failure_details(7, company, database, schema_contains), page=_PAGE,
              key=f"t_rca_{company}", tier="hourly",
              source="ACCOUNT_USAGE.TASK_HISTORY (failures, ~45 min source lag)")
    if not res.ok:
        st.error(f"Failure detail unavailable: {res.error}")
        return
    if res.empty:
        empty_state("clean", "No task failures in the last 7 days for this scope.")
        return
    timeline = build_failure_timeline(res.df)
    roots = timeline[timeline["ROLE_IN_GRAPH"] == "Root cause"]
    kpi_row([
        {"label": "Failures (7d)", "value": f"{len(timeline)}"},
        {"label": "Root causes", "value": f"{len(roots)}",
         "help": "First failure per task-graph run; fix these, the cascade follows."},
        {"label": "Top error family",
         "value": str(timeline["ERROR_FAMILY"].mode().iloc[0]) if not timeline.empty else "n/a"},
    ])
    fam = timeline.groupby("ERROR_FAMILY", as_index=False).size().rename(columns={"size": "FAILURES"})
    charts.bar_count(fam.sort_values("FAILURES", ascending=False), "ERROR_FAMILY", "FAILURES",
                     title="Failures by family", takeaway=True)
    # #14: one error family hitting MANY distinct tasks is usually a single cause
    # (a revoked grant / dead source), not N separate bugs — surface it above the raw list.
    _clusters = cluster_failures_by_family(timeline)
    _systemic = _clusters[_clusters["SYSTEMIC"]] if not _clusters.empty else _clusters
    if not _systemic.empty:
        section_header("Systemic errors — one cause, many tasks", "warn", "alerts")
        kpi_row([
            {"label": "Systemic error families", "value": f"{len(_systemic)}"},
            {"label": "Distinct tasks hit", "value": f"{int(_systemic['DISTINCT_TASKS'].sum())}",
             "help": "One error family across 3+ tasks — investigate the shared cause first."},
        ])
        styled_table(_systemic[["ERROR_FAMILY", "DISTINCT_TASKS", "FAILURES",
                                "SAMPLE_TASK", "SAMPLE_ERROR"]],
                     slug="systemic-task-errors", sort_label="distinct tasks desc")
        st.caption("One error family hitting several tasks usually means a single revoked grant / "
                   "dead source, not N separate bugs.")
    timeline["TASK_FQN"] = (timeline["DATABASE_NAME"].astype(str) + "."
                            + timeline["SCHEMA_NAME"].astype(str) + "."
                            + timeline["TASK_NAME"].astype(str))
    entity_nav_table(
        timeline[["QUERY_START_TIME", "ROLE_IN_GRAPH", "ERROR_FAMILY", "DATABASE_NAME",
                   "SCHEMA_NAME", "TASK_NAME", "RUN_SEC", "ERROR_MESSAGE", "TASK_FQN"]],
        key="ops_fail_timeline", key_col="TASK_FQN", entity_type="TASK",
    )
    result_caption(res)
    _incident_routing_panel(timeline)
    ai_evaluation_panel(
        key=f"task_failures_{company}",
        prompt=task_failure_prompt(timeline, company),
        settings=load_settings(_PAGE),
        page=_PAGE,
        subject="diagnose these task failures",
    )


def _incident_routing_panel(timeline) -> None:
    """rec#27: stitch each root-cause failure to an owner + first response.

    Reuses the already-classified failure timeline (no new failure scan) and joins
    the entity catalog for owner/on-call, so an incident carries a name and a next
    move. Routing to ACTION_QUEUE / Teams and ack-timeout escalation from an on-call
    rotation are the deferred owner-migration half."""
    section_header("Incident routing — owner + first response (root causes)", "warn", "alerts")
    panel_help(
        "Each root-cause failure is matched to a first-response remediation by its error "
        "family (classify_task_error) and to an owner/on-call resolved from the catalog "
        "(the task's own TASK entry, else the database it lives in). Cascades are excluded "
        "so only the failure to fix pages someone. ROUTED_TO is unassigned when the task "
        "isn't in the catalog — register its owner in Decision Studio. Actually opening a "
        "routed ACTION_QUEUE item / Teams mention and escalating on an ack timeout from an "
        "on-call rotation are the deferred owner-migration half."
    )
    cat = run(workbench_sql.entity_catalog(limit=1000), page=_PAGE, key="incident_catalog",
              tier="hourly", source="ENTITY_CATALOG (owner / on-call)")
    if not cat.ok:
        st.warning(
            "Owner lookup unavailable — the catalog read failed, so the incidents below are "
            f"shown UNROUTED for that reason (owners may well be registered). {cat.error or ''}"
        )
    catalog_df = cat.df if cat.ok else None
    incidents = route_incidents(timeline, catalog_df)
    if incidents.empty:
        empty_state("no_data_yet", "No root-cause failures to route.")
        return
    stats = summarize_incidents(incidents)
    kpi_row([
        {"label": "Incidents", "value": f"{stats['incidents']}",
         "help": "Distinct failing tasks (root cause), collapsed to one incident each."},
        {"label": "High severity", "value": f"{stats['critical']}",
         "delta_color": "inverse" if stats["critical"] else "off",
         "help": "CRITICAL or HIGH by the owning entity's catalog criticality."},
        {"label": "Unrouted", "value": f"{stats['unrouted']}",
         "delta_color": "inverse" if stats["unrouted"] else "off",
         "help": "No owner/on-call in the catalog — register the task to route it."},
    ])
    if cat.ok and cat.empty:
        st.caption("The entity catalog is empty, so every incident is unassigned — register owners in Decision Studio to route them.")
    styled_table(incidents, height=280, slug="incident-routing",
                 sort_label="most severe, then most failures")
    st.caption("ROUTED_TO is the on-call (else owner) to page; REMEDIATION is the first move for that error family.")


def _release_compare_tab(company: str) -> None:
    """Before/after a release date: query health + per-task regressions (ported)."""
    st.caption(
        "Pick the deploy date; each side compares the same number of days before and after. "
        "ACCOUNT_USAGE lag means very recent releases under-count the AFTER side."
    )
    # O16: auto-detect candidate deploy days (schema-change DDL) so the owner
    # picks a real release instead of guessing. Manual entry stays as the escape
    # hatch (and the only path when nothing deploy-like is detected).
    det = run(insights_sql.detect_release_days(90, company), page=_PAGE,
              key=f"rel_detect_{company}", tier="historical",
              source="ACCOUNT_USAGE.QUERY_HISTORY (CREATE/ALTER/DROP)")
    candidates = rank_release_candidates(det.df) if det.ok else None

    _MANUAL = "Custom date…"
    col_pick, col_window = st.columns([1.6, 1.0])
    with col_window:
        window = st.select_slider("Compare window (days each side)", options=[1, 2, 3, 5, 7, 14],
                                  value=3, key="ops_release_window")
    with col_pick:
        if candidates is not None and not candidates.empty:
            labels: list[str] = []
            label_to_iso: dict[str, str] = {}
            for _, cand in candidates.iterrows():
                day = str(cand["DAY"])
                actor = cand.get("TOP_ACTOR")
                actor_note = f" · {actor}" if isinstance(actor, str) and actor.strip() else ""
                label = f"{day} — {int(safe_float(cand['DDL_COUNT']))} object change(s){actor_note}"
                labels.append(label)
                label_to_iso[label] = day
            choice = st.selectbox(
                "Detected deploys (last 90d)", [*labels, _MANUAL], key="ops_release_pick",
                help="Days with the most CREATE/ALTER/DROP activity — the likely deploys. "
                     "Excludes CTAS/session ops and OVERWATCH's own maintenance; a heuristic, "
                     "not a definitive release log. Pick 'Custom date…' to enter your own.")
            release_iso = (
                st.date_input("Release date", value=account_today() - timedelta(days=1),
                              key="ops_release_date").isoformat()
                if choice == _MANUAL else label_to_iso[choice]
            )
        else:
            st.caption(
                "No deploy-like DDL detected in the last 90 days — enter the date manually."
                if candidates is not None
                else "Deploy auto-detect unavailable — enter the date manually.")
            release_iso = st.date_input(
                "Release date", value=account_today() - timedelta(days=1),
                key="ops_release_date").isoformat()

    q_res = run(insights_sql.release_query_compare(release_iso, window, company), page=_PAGE,
                key=f"rel_q_{company}_{release_iso}_{window}", tier="historical",
                source="ACCOUNT_USAGE.QUERY_HISTORY")
    section_header("Query health: before vs after", "info", "search")
    verdicts: list[dict] = []
    if guard(q_res, "No query history in the compare windows."):
        verdicts = compare_release_periods(q_res.df)
        if verdicts:
            import pandas as _pd

            styled_table(_pd.DataFrame(verdicts))
            worse = [v["Metric"] for v in verdicts if v["Verdict"] == "Worse"]
            if worse:
                st.warning("Regressed after release: " + ", ".join(worse))
            else:
                empty_state("clean", "No query-health regression beyond the 10% flat tolerance.")
        else:
            empty_state("no_data_yet", "Need data on both sides of the release date to compare.")
        result_caption(q_res)

    section_header("Task regressions", "info", "operations")
    t_res = run(insights_sql.release_task_compare(release_iso, window, company), page=_PAGE,
                key=f"rel_t_{company}_{release_iso}_{window}", tier="historical",
                source="ACCOUNT_USAGE.TASK_HISTORY")
    if guard(t_res, "No task runs in the compare windows."):
        deltas = task_release_deltas(t_res.df)
        worse = deltas[deltas["GOT_WORSE"]]
        if worse.empty:
            empty_state("clean", "No task gained failures or slowed >25% after the release.")
        else:
            st.warning(f"{len(worse)} task(s) regressed after the release:")
        _delta_cols = [c for c in ["DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME",
                                   "RUNS_BEFORE", "RUNS_AFTER", "FAILED_BEFORE",
                                   "FAILED_AFTER", "NEW_FAILURES", "AVG_SEC_BEFORE",
                                   "AVG_SEC_AFTER", "RUNTIME_DELTA_PCT", "GOT_WORSE"]
                       if c in deltas.columns]
        deltas["TASK_FQN"] = (deltas["DATABASE_NAME"].astype(str) + "."
                              + deltas["SCHEMA_NAME"].astype(str) + "."
                              + deltas["TASK_NAME"].astype(str))
        entity_nav_table(deltas[[*_delta_cols, "TASK_FQN"]], key="ops_release_regr",
                         key_col="TASK_FQN", entity_type="TASK")
        result_caption(t_res)
        ai_evaluation_panel(
            key=f"release_{company}_{release_iso}_{window}",
            prompt=release_compare_prompt(verdicts, deltas, release_iso, window),
            settings=load_settings(_PAGE),
            page=_PAGE,
            subject="judge this release",
        )


def _dq_row_volume_panel(preloaded=None) -> None:
    """rec#26: robust-z row-volume data-quality monitor for registered products.

    Complements the 50%-cliff Volume drops alert with an outlier-resistant
    (median/MAD) score of yesterday's rows-added vs each table's own trailing
    baseline, so it catches both spikes and drops on steady movers and routes each
    finding to the catalog owner. Null-rate / schema-drift monitors (which need a
    stored baseline) and the DQ_BREACH alert are the deferred owner-migration
    halves."""
    section_header("Row-volume anomalies — registered products (robust-z, 28d)", "info", "pipeline")
    panel_help(
        "Robust z-score (median / MAD, floored at 15% of the median so a rock-steady table "
        "doesn't fire on jitter; threshold 3.5) of each table's MOST RECENT load of "
        "rows-added versus its own prior loads, scoped to catalog-registered data products "
        "so every finding has an owner. Unlike the 50%-cliff Volume drops alert it flags "
        "anomalous loads in BOTH directions (partial or duplicate loads, upstream volume "
        "shifts) and resists a single prior outlier. Only days that actually added rows "
        "count as loads, so a business-day table is never falsely flagged because the "
        "newest data lands on a weekend; a table is scored only with >=10 prior loads and "
        "a baseline median >=100 rows/day. **Not covered here** (by design): a load that "
        "ran but inserted 0 rows (indistinguishable from an update-only day — freshness is "
        "the backstop), a table whose weekend loads are legitimately smaller-but-nonzero "
        "(its own baseline widens), and objects not registered by their exact "
        "DB.SCHEMA.TABLE name (only the database registration is then used). DAYS_STALE "
        "marks a row scored on an old load. The DQ_BREACH alert, plus null-rate and "
        "schema-drift monitors, are the deferred owner-migration half."
    )
    rv = preloaded or run(dq_sql.product_row_volume(28), page=_PAGE, key="dq_row_volume", tier="recent",
             source="ACCOUNT_USAGE.TABLE_DML_HISTORY x ENTITY_CATALOG")
    if rv.ok and rv.empty:
        empty_state("needs_setup", "No registered-product tables added rows in the window. Register data products in the catalog (Decision Studio) to monitor their volume here.")
        return
    if not guard(rv, "", setup_hint="Needs TABLE_DML_HISTORY and a populated ENTITY_CATALOG with DATA_PRODUCT."):
        return
    anomalies = row_volume_anomalies(rv.df)
    if anomalies.empty:
        empty_state("no_data_yet", "No registered-product table has enough load history yet (needs >=10 prior loads and a >=100 rows/day baseline median).")
        result_caption(rv)
        return
    stats = summarize_row_volume(anomalies)
    kpi_row([
        {"label": "Tables monitored", "value": f"{stats['monitored']}",
         "help": "Registered-product tables with >=10 prior loads and a material baseline."},
        {"label": "Low-volume loads", "value": f"{stats['drops']}",
         "delta_color": "inverse" if stats["drops"] else "off",
         "help": "Most recent load added >=3.5 robust-z fewer rows than the table's baseline (partial load)."},
        {"label": "High-volume loads", "value": f"{stats['spikes']}",
         "delta_color": "inverse" if stats["spikes"] else "off",
         "help": "Most recent load added >=3.5 robust-z more rows than baseline (duplicate / backfill)."},
        {"label": "Products affected", "value": f"{stats['products']}",
         "help": "Distinct data products with at least one flagged table."},
    ])
    if stats["stale"]:
        st.caption(f"{stats['stale']} monitored table(s) last loaded >=3 days behind the freshest registered load (see DAYS_STALE) — scored on that older load, so the freshness panel above is the live view.")
    flagged = anomalies[anomalies["FLAGGED"]]
    if flagged.empty:
        empty_state("clean", "Every monitored product table's most recent load is within its normal volume (no robust-z breach).")
    else:
        styled_table(flagged, height=280, slug="dq-row-volume",
                     sort_label="largest robust-z deviation first")
        st.caption("Each row is a table whose most recent load broke its own volume baseline; LATEST_DAY is when it loaded, OWNER_NAME is who to route the DQ finding to.")
    result_caption(rv)


def _pipeline_sla_tab(is_operator: bool, company: str = "ALL") -> None:
    """Metadata-driven table freshness SLAs (config in PIPELINE_SLA_CONFIG).

    Owner ask 2026-08-17: the DB-grain diagnostics honor the company filter; the
    SLA-horizon config/forecast is account-wide (thresholds are account policy)."""
    res = run(insights_sql.pipeline_sla_forecast(14), page=_PAGE, key="sla_status", tier="recent",
              source="ACCOUNT_USAGE.TABLE_DML_HISTORY x PIPELINE_SLA_STATUS")
    if not res.ok:
        empty_state("needs_setup", "Pipeline SLAs are not installed yet — an admin can verify on Admin → Migrations & freshness.")
        return
    if res.empty:
        empty_state("needs_setup", "No tables registered. Add rows to PIPELINE_SLA_CONFIG below; the view scores them automatically.")
    else:
        # O10: fold each table's refresh cadence into a forward-looking tier so the
        # tab warns BEFORE a miss, not only after. On-track/at-risk/overdue/breached.
        df = pipeline_sla_forecast(res.df.copy())
        met = int(df["SLA_MET"].fillna(False).astype(bool).sum())
        total = len(df)
        overdue = int((df["FORECAST"] == "Overdue").sum())
        at_risk = int((df["FORECAST"] == "At risk").sum())
        kpi_row([
            {"label": "SLA compliance", "value": f"{met / total * 100:,.1f}%",
             "delta": f"{met}/{total} tables", "delta_color": "off"},
            {"label": "Breaching now", "value": f"{total - met}",
             "delta_color": "inverse" if total - met else "off"},
            {"label": "Trending to miss", "value": f"{overdue + at_risk}",
             "delta": f"{overdue} overdue · {at_risk} at risk", "delta_color": "off",
             "help": "Meets SLA now but overdue vs its own refresh cadence, or within "
                     "one refresh cycle of the deadline — a leading indicator, not a miss yet."},
        ])
        _fcols = ["DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME", "OWNER", "FORECAST",
                  "DETAIL", "HOURS_SINCE", "MAX_AGE_HOURS"]
        forecast_rows = df[df["FORECAST"].isin(["Overdue", "At risk"])]
        if not forecast_rows.empty:
            st.warning("Forecast — registered tables trending toward a miss (still within SLA now):")
            styled_table(forecast_rows[_fcols], sort_label="soonest to miss")
        breaching = df[~df["SLA_MET"].fillna(False).astype(bool)]
        if not breaching.empty:
            st.warning("Tables past their freshness SLA:")
            styled_table(breaching[_fcols])
        with st.expander("All registered tables"):
            styled_table(df)
        result_caption(res, note="Freshness from ACCOUNT_USAGE.TABLES.LAST_ALTERED (metadata lag "
                                  "up to ~2h); refresh cadence from TABLE_DML_HISTORY over 14 days.")

    with st.expander("Register a table"):
        # rec43 NOT applied here: st.form would freeze the live SQL preview until
        # submit, so the operator couldn't review the EXACT INSERT before running
        # it (the submit both shows and runs). The "see the SQL first" contract
        # wins over batched submit — keep the live preview + a separate button.
        c1, c2, c3 = st.columns(3)
        with c1:
            db = st.text_input("Database", key="sla_db")
        with c2:
            schema = st.text_input("Schema", key="sla_schema")
        with c3:
            table = st.text_input("Table", key="sla_table")
        max_age = st.number_input("Max age (hours)", min_value=1.0, max_value=168.0, value=24.0, key="sla_age")
        owner = st.text_input("Owner", value="Data Engineering", key="sla_owner")
        insert_sql = (
            f"INSERT INTO {core_object('PIPELINE_SLA_CONFIG')} "
            "(DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, MAX_AGE_HOURS, OWNER)\n"
            f"VALUES ({sql_literal(db.upper())}, {sql_literal(schema.upper())}, "
            f"{sql_literal(table.upper())}, {max_age}, {sql_literal(owner)});"
        )
        st.code(insert_sql, language="sql")
        if (is_operator and st.button("Execute insert", key="sla_exec",
                                      disabled=not (db and schema and table))
                and write_gate_open("sla_exec")):
            ok, msg = execute_statement(insert_sql, page=_PAGE)
            stamp_write("sla_exec", ok)  # C48
            notify(ok, msg)
        if not is_operator:
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin profile.")

    # Perf: the tab's FOUR independent 'recent' reads (COPY failures, volume deltas,
    # registered-product row-volume, dynamic-table health) prefetch in ONE parallel batch instead
    # of four serial account-usage scans; each keeps its render point and run() fallback, so which
    # scans fire and the render order are unchanged. Cold latency ~MAX(scan) instead of SUM(scans).
    _psb = run_batch([
        {"key": "cpf", "sql": ops_sql.copy_load_failures(7, company), "source": "ACCOUNT_USAGE.COPY_HISTORY"},
        {"key": "vd", "sql": ops_sql.volume_deltas(), "source": "ACCOUNT_USAGE.TABLE_DML_HISTORY"},
        {"key": "rv", "sql": dq_sql.product_row_volume(28),
         "source": "ACCOUNT_USAGE.TABLE_DML_HISTORY x ENTITY_CATALOG"},
        {"key": "dth", "sql": ops_sql.dynamic_table_health(7),
         "source": "ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY"},
    ], page=_PAGE, tier="recent")

    cpf = _psb.get("cpf") or run(ops_sql.copy_load_failures(7, company), page=_PAGE,
              key=f"copy_fails_{company}", tier="recent", source="ACCOUNT_USAGE.COPY_HISTORY")
    section_header("File-load failures (COPY / Snowpipe, 7d)", alarm_health(cpf), "pipeline")
    if cpf.ok and cpf.empty:
        empty_state("clean", "No failed or partial file loads in the last 7 days.")
    elif guard(cpf, ""):
        styled_table(cpf.df, height=240)
        st.caption("The PIPE_COPY_FAILURES alert fires on these within the hour; "
                   "this table is the 7-day picture with sample errors.")
        result_caption(cpf)

    section_header("Volume drops (yesterday vs prior-7d average)", "info", "pipeline")
    panel_help(
        "Rows added per table yesterday vs its prior-7-day average (steady movers: "
        "≥1,000 rows/day AND written on ≥3 of the prior 7 days). **Timing:** 'yesterday' "
        "is the prior full calendar day in account time; an overnight batch that finishes "
        "after midnight books its rows on the day it ran, so a table loaded at 00:30 for "
        "the prior day reads as 0 for 'yesterday'. To cut the truncate-reload false "
        "positives, dated/temp tables, *STAG* schemas, the SNOWFLAKE database, and "
        "one-shot tables are now excluded — so this shows persistent movers only. The "
        "PIPE_VOLUME_DROP alert fires past a 50% drop on PROD databases (SP_ANOMALY_SWEEP / "
        "TASK_ANOMALY_SWEEP, HIGH, gated on ALERT_CONFIG.ENABLED). DAYS_ACTIVE_7D shows how "
        "many of the prior 7 days the table actually moved."
    )
    vd = _psb.get("vd") or run(ops_sql.volume_deltas(), page=_PAGE, key="volume_deltas", tier="recent",
             source="ACCOUNT_USAGE.TABLE_DML_HISTORY")
    if vd.ok and vd.empty:
        empty_state("clean", "Every moving table is within its normal daily volume.")
    elif guard(vd, "", setup_hint="Needs TABLE_DML_HISTORY (standard on current accounts)."):
        styled_table(vd.df, height=240)
        result_caption(vd)

    _dq_row_volume_panel(_psb.get("rv"))

    section_header("Dynamic table refresh health (7d)", "info", "pipeline")
    panel_help(
        "Source: ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY (up to ~3h lag). A FAILED "
        "row means every downstream consumer is reading stale data. The daily "
        "PIPE_DT_FAILURES alert fires on 24h failures; this is the weekly picture."
    )
    dth = _psb.get("dth") or run(ops_sql.dynamic_table_health(7), page=_PAGE, key="dt_health", tier="recent",
              source="ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY")
    if dth.ok and dth.empty:
        empty_state("no_data_yet", "No dynamic-table refreshes recorded in 7 days (none defined, or the view is empty).")
    elif guard(dth, "", setup_hint="Needs the DYNAMIC_TABLE_REFRESH_HISTORY view (standard on current accounts)."):
        styled_table(dth.df, height=240)
        result_caption(dth)

    section_header("Stream staleness", "info", "pipeline")
    panel_help(
        "SHOW STREAMS (live metadata — no ACCOUNT_USAGE view exists for staleness). "
        "A STALE stream has passed its retention without being consumed: downstream "
        "pipelines are silently missing changes. Fix = consume or recreate the stream."
    )
    if st.toggle("Check streams now (live SHOW command)", key="ops_streams_toggle"):
        strm = run(ops_sql.show_streams_sql(), page=_PAGE, key="streams_show", tier="live",
                   source="SHOW STREAMS IN ACCOUNT", max_rows=0)
        if strm.ok and strm.empty:
            empty_state("no_data_yet", "No streams in the account.")
        elif guard(strm, ""):
            sdf = strm.df.copy()
            sdf.columns = [str(c).upper() for c in sdf.columns]
            if "STALE" in sdf.columns:
                stale = sdf[sdf["STALE"].astype(str).str.lower() == "true"]
                kpi_row([
                    {"label": "Streams", "value": f"{len(sdf)}"},
                    {"label": "Stale", "value": f"{len(stale)}",
                     "delta_color": "inverse" if len(stale) else "off"},
                ])
                if not stale.empty:
                    show_cols = [c for c in ("NAME", "DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME",
                                             "STALE_AFTER", "MODE") if c in stale.columns]
                    styled_table(stale[show_cols])
            else:
                styled_table(sdf)


def _task_health_view(company: str, days: int, database: str = "",
                      schema_contains: str = "") -> None:
    res = run(mart_sql.fact_task_daily(days, company, database), page=_PAGE, key=f"t_fact_{company}_{days}",
              tier="hourly", source="FACT_TASK_DAILY")
    if not res.usable():
        res = run(ops_sql.task_runs(days, company, database, schema_contains), page=_PAGE, key=f"t_live_{company}_{days}",
                  tier="recent", source="ACCOUNT_USAGE.TASK_HISTORY (live fallback)")
    known_failed = None
    if guard(res, "No task runs recorded for this scope/window."):
        df = res.df.copy()
        failed_col = "FAILED" if "FAILED" in df.columns else None
        task_sort_label = ""
        if failed_col:
            total_runs = safe_float(df.get("RUNS", 0).sum() if "RUNS" in df.columns else 0)
            total_failed = safe_float(df[failed_col].sum())
            task_fail_pct = (total_failed / total_runs * 100) if total_runs else None
            known_failed = total_failed
            kpi_row([
                {"label": f"Task runs ({days}d)", "value": f"{total_runs:,.0f}"},
                {"label": "Failed runs", "value": f"{total_failed:,.0f}",
                 "delta": (f"{task_fail_pct:.1f}%" if task_fail_pct is not None
                           else "No run denominator"),
                 "delta_color": "inverse" if total_failed else "off"},
            ])
            df = df.sort_values(failed_col, ascending=False)
            task_sort_label = "failed runs desc"
        if {"DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME"}.issubset(df.columns):
            df["TASK_FQN"] = (df["DATABASE_NAME"].astype(str) + "."
                              + df["SCHEMA_NAME"].astype(str) + "."
                              + df["TASK_NAME"].astype(str))
            entity_nav_table(df, key=f"ops_task_health_{company}_{days}",
                             key_col="TASK_FQN", entity_type="TASK",
                             sort_label=task_sort_label)
        else:
            styled_table(df, sort_label=task_sort_label)
        result_caption(res)
        # #15: a task 100% successful but quietly slower than its own baseline is
        # otherwise invisible. Reuses the fact_task_daily frame already fetched (mart
        # path only — the live task_runs fallback carries no daily AVG_SEC series).
        if {"DAY", "AVG_SEC", "TASK_NAME"}.issubset(res.df.columns):
            _drift = task_duration_anomalies(res.df)
            section_header("Duration drift — tasks slower than their own baseline",
                           alarm_health(len(_drift)), "clock")
            if _drift.empty:
                st.caption("No task is running materially slower than its own recent baseline.")
            else:
                kpi_row([
                    {"label": "Tasks drifting slower", "value": f"{len(_drift)}",
                     "help": "Daily mean runtime is a robust-z outlier above the task's own median "
                             "(>= 30s, 7+ active days).",
                     "delta_color": "inverse"},
                ])
                styled_table(_drift, slug="task-duration-drift", sort_label="x slower desc")
            # #5 (Upgrade Board): the LEADING half of the duration signal — tasks
            # whose daily runtime is climbing toward a miss, before they cross. Same
            # already-loaded daily frame as the drift above; no extra scan.
            _fc = duration_sla_forecast(res.df)
            section_header("Predicted SLA miss — tasks trending slower",
                           alarm_health(len(_fc)), "schedule")
            if _fc.empty:
                st.caption("No task's recent runtime is climbing toward a miss.")
            else:
                _miss = int((_fc["FORECAST"] == "Predicted miss").sum())
                kpi_row([
                    {"label": "Tasks trending late", "value": f"{len(_fc)}",
                     "delta_color": "inverse"},
                    {"label": "Predicted miss", "value": f"{_miss}",
                     "severity": ("bad" if _miss else "ok")},
                ])
                _fc_cols = [c for c in ["FORECAST", "SEVERITY", "DATABASE_NAME", "SCHEMA_NAME",
                                        "TASK_NAME", "BASELINE_SEC", "LATEST_SEC", "SLOWER_X", "DAY"]
                            if c in _fc.columns]
                styled_table(_fc[_fc_cols], slug="task-duration-forecast",
                             sort_label="x over baseline desc")
                st.caption(
                    "Latest daily runtime vs the task's own median baseline, shown only when the "
                    "last few days are climbing — a leading indicator that complements the drift "
                    "above (which flags a task already slow on some day). Trailing completed-run "
                    "averages; ACCOUNT_USAGE.TASK_HISTORY lags ~45min, so this forecasts a trend, "
                    "not a live in-flight run.")
    st.divider()
    _failure_timeline_section(company, database, schema_contains,
                              known_failures=known_failed if days >= 7 else None)


def _task_sla_view(company: str, days: int, database: str = "",
                   schema_contains: str = "") -> None:
    """Two 'task health beyond run counts' lenses (repo wave-2): tasks failing
    repeatedly right now (leading FAILED streak), and tasks that quietly stopped
    being scheduled (freshness vs their own cadence). Both read live TASK_HISTORY —
    neither task mart preserves ordered per-run STATE — so this is its own sub-tab,
    off the default Health paint."""
    from app.config import ACCOUNT_USAGE_LAG_NOTE
    from app.logic.insights import task_failure_streaks, task_freshness_status

    _b = run_batch([
        {"key": "streak",
         "sql": ops_sql.task_recent_states(days, company, database, schema_contains),
         "source": "ACCOUNT_USAGE.TASK_HISTORY (recent per-task states)", "max_rows": 4000},
        {"key": "fresh",
         "sql": ops_sql.task_freshness_sla(max(days, 14), company, database, schema_contains),
         "source": "ACCOUNT_USAGE.TASK_HISTORY (cadence + silence)"},
    ], page=_PAGE, tier="recent")

    _sres = _b.get("streak")
    _streaks_known = _sres is not None and _sres.usable()
    streaks = task_failure_streaks(_sres.df) if _streaks_known else None
    section_header("Actively broken tasks (failure streaks)",
                   alarm_health(len(streaks)) if _streaks_known else "", "alerts")
    if not _streaks_known:
        st.caption("No SUCCEEDED/FAILED task runs in this window/scope.")
    else:
        if streaks.empty:
            empty_state("clean", "No task is in a failure streak — every task's newest run succeeded.")
        else:
            broken = streaks[streaks["ACTIVELY_BROKEN"]]
            kpi_row([
                {"label": "Tasks failing now", "value": f"{len(streaks)}",
                 "help": "Newest run failed (leading FAILED streak >= 1)."},
                {"label": "Actively broken", "value": f"{len(broken)}",
                 "help": "Two or more consecutive failures since the last success — a retry loop, not a blip.",
                 "delta_color": "inverse" if len(broken) else "off"},
            ])
            styled_table(
                streaks[["SEVERITY", "DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME",
                         "FAIL_STREAK", "LAST_RUN", "LAST_ERROR"]],
                sort_label="failure streak desc")
        result_caption(_sres)

    st.divider()
    _fres = _b.get("fresh")
    _fresh_known = _fres is not None and _fres.usable()
    fresh = task_freshness_status(_fres.df) if _fresh_known else None
    late = (fresh[fresh["STATUS"].isin(["Late", "Stale"])]
            if _fresh_known and not fresh.empty else fresh)
    section_header("Task freshness — silent-stop detection",
                   alarm_health(len(late)) if _fresh_known else "", "clock")
    if not _fresh_known:
        st.caption("Not enough scheduled history to derive task cadence in this window/scope.")
    else:
        if fresh.empty or late.empty:
            empty_state("clean", "Every task with a derivable cadence is on-time against its own schedule.")
        else:
            stale = late[late["STATUS"] == "Stale"]
            kpi_row([
                {"label": "Late or stale", "value": f"{len(late)}"},
                {"label": "Stale (silently stopped)", "value": f"{len(stale)}",
                 "help": "Silent for 2x+ its typical cadence beyond the ~45-min telemetry lag — likely stopped being scheduled.",
                 "delta_color": "inverse" if len(stale) else "off"},
            ])
            styled_table(
                late[["STATUS", "DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME",
                      "MEDIAN_GAP_MIN", "MINS_SINCE_SUCCESS", "OVERDUE_MIN", "LAST_SUCCESS"]],
                sort_label="overdue desc")
        result_caption(_fres)
    st.caption(f"Cadence is each task's own median scheduled-gap. {ACCOUNT_USAGE_LAG_NOTE}")
    # C16: park the Tasks pill badge — tasks needing attention across BOTH panels
    # above (failure streaks + late-vs-cadence), deduplicated on the task's
    # fully-qualified name where both frames carry it. Values already computed
    # above; zero new queries. Skipped when neither feed was usable, so the badge
    # never asserts "0" on missing data.
    if _streaks_known and _fresh_known:   # review fix: half the evidence must not badge
        _frames = [_fr for _fr in (streaks if _streaks_known else None,
                                   late if _fresh_known else None)
                   if _fr is not None and not _fr.empty]
        _key_cols = ["DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME"]
        if all(set(_key_cols) <= set(_fr.columns) for _fr in _frames):
            _attn: set[tuple] = set()
            for _fr in _frames:
                _attn.update(_fr[_key_cols].itertuples(index=False, name=None))
            _n = len(_attn)
        else:  # a frame lost the shared task columns — sum rather than guess a join
            _n = sum(len(_fr) for _fr in _frames)
        # review fix: the SLA feeds honor database/schema filters too — the
        # badge declares all four dims so a filter flip never serves it stale.
        stash_section_count(_PAGE, "Tasks", _n,
                            dims=("company", "days", "database", "schema_contains"))


def _task_runs_view(company: str, days: int, database: str = "",
                    schema_contains: str = "") -> None:
    section_header("Node run timing", "info", "clock")
    nres = run(
        mart27_sql.task_nodes(days, company, database, schema_contains),
        page=_PAGE,
        key=f"t_node_{company}_{days}",
        tier="hourly",
        source="MART_TASK_NODE_DAILY",
    )
    if guard(
        nres,
        "No per-node timing yet — MART_TASK_NODE_DAILY is empty for this scope "
        "(it loads hourly once V058 is applied).",
    ):
        ndf = nres.df.copy()
        if {"FAILED", "RUNS"}.issubset(ndf.columns):
            ndf["FAIL_PCT"] = [
                round(safe_float(failed) / safe_float(runs) * 100, 1)
                if safe_float(runs)
                else 0.0
                for failed, runs in zip(ndf["FAILED"], ndf["RUNS"], strict=False)
            ]
        ndf["TASK_FQN"] = (ndf["DATABASE_NAME"].astype(str) + "."
                           + ndf["SCHEMA_NAME"].astype(str) + "."
                           + ndf["TASK_NAME"].astype(str))
        entity_nav_table(ndf, key=f"ops_node_timing_{company}", key_col="TASK_FQN",
                         entity_type="TASK", sort_label="p95 queue desc, failed desc")
        st.caption(
            "Dispatch queue = scheduled-to-start delay; execution = start-to-complete. "
            "The mart is task-grain and ordered by p95 dispatch queue."
        )
        result_caption(nres)


def _task_run_analyzer(root_id: str, topology, shape) -> None:
    runs = run(
        ops_sql.task_graph_recent_runs(root_id, 7, 40), page=_PAGE,
        key=f"task_graph_runs_{root_id}", tier="recent",
        source="TASK_HISTORY (selected root, bounded 7d)", max_rows=40,
    )
    if not guard(runs, "No completed graph executions were found for this root in 7 days."):
        return
    run_frame = runs.df.reset_index(drop=True)
    selected = selectable_table(
        run_frame, key=f"task_graph_run_pick_{root_id}", height=230,
        sort_label="newest scheduled run",
    )
    run_index = int(selected) if selected is not None else 0
    run_row = run_frame.iloc[run_index]
    run_key = str(run_row.get("RUN_KEY") or "")
    if not evidence_gate(
        "task_run",
        key=f"task_graph_run_evidence_{root_id}_{run_key}",
        label="Load selected run analysis",
    ):
        return
    nodes = run(
        ops_sql.task_graph_run_nodes(root_id, run_key, 7), page=_PAGE,
        key=f"task_graph_run_nodes_{root_id}_{run_key}", tier="recent",
        source="TASK_HISTORY (one graph run)", max_rows=2_000,
    )
    if not guard(nodes, "The selected graph execution has no task rows."):
        return
    diagnostics = analyze_task_run(nodes.df, shape)
    failed = int(diagnostics["STATE"].astype(str).eq("FAILED").sum())
    critical = diagnostics[diagnostics["CRITICAL_PATH"]]
    critical_sec = safe_float(critical["PATH_SEC"].max()) if not critical.empty else 0.0
    queue_sec = safe_float(run_row.get("QUEUE_SEC"))
    wall_sec = safe_float(run_row.get("WALL_SEC"))
    exceptions = []
    if failed:
        exceptions.append({
            "label": "Failed tasks",
            "value": f"{failed:,}",
            "detail": "Open the ranked nodes below for the task error and query profile.",
            "severity": "bad",
        })
    if queue_sec >= max(60.0, wall_sec * 0.2):
        exceptions.append({
            "label": "Dispatch delay",
            "value": humanize_duration(queue_sec, "s"),
            "detail": "Queue time is at least 60 seconds and 20% of run wall time.",
            "severity": "warn",
        })
    exception_summary(exceptions, "No failures or material dispatch delay in this run.")
    kpi_row([
        {"label": "Run wall time", "value": humanize_duration(run_row.get("WALL_SEC"), "s")},
        {"label": "Dispatch queue", "value": humanize_duration(run_row.get("QUEUE_SEC"), "s")},
        {"label": "Failed tasks", "value": f"{failed:,}",
         "severity": "bad" if failed else "ok"},
        {"label": "Critical path", "value": humanize_duration(critical_sec, "s")},
    ])

    lookup = {
        canonical_task_name(row.get("TASK_FQN")): row
        for _, row in diagnostics.iterrows()
    }
    overlay = topology.copy()
    for column in ("RUN_SEC", "QUEUE_SEC", "EXEC_SEC", "RUN_STATE", "CRITICAL_PATH"):
        source_column = "STATE" if column == "RUN_STATE" else column
        overlay[column] = overlay["TASK_FQN"].map(
            lambda value, col=source_column: lookup.get(
                canonical_task_name(value), {}
            ).get(col)
        )
    if not charts.interactive_task_dag(overlay, shape, height=620):
        try:
            st.graphviz_chart(charts.task_dag_dot(overlay, shape), width="stretch", height=620)
        except TypeError:
            st.graphviz_chart(charts.task_dag_dot(overlay, shape), width="stretch")
    st.caption(
        "The highlighted path is the longest observed dependency path for this run. "
        "Node duration includes dispatch queue plus execution; current topology supplies the edges."
    )

    ranked = diagnostics.sort_values(
        ["CRITICAL_PATH", "BOTTLENECK_SCORE", "RUN_SEC"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    profiled, profile_config = snowsight_profile_column(ranked, _PAGE, id_col="QUERY_ID")
    columns = [
        "TASK_FQN", "STATE", "CRITICAL_PATH", "DOWNSTREAM_TASKS", "QUEUE_SEC",
        "EXEC_SEC", "RUN_SEC", "BOTTLENECK_SCORE", "ERROR_MESSAGE",
    ]
    if "PROFILE" in profiled.columns:
        columns.append("PROFILE")

    def _open_task(index: int) -> None:
        task = str(ranked.iloc[int(index)]["TASK_FQN"])
        request_navigation(
            "Control Room", "Entity 360",
            context={"entity_type": "TASK", "entity_key": task},
        )

    selectable_nav_table(
        profiled[columns], key=f"task_run_nodes_table_{root_id}_{run_key}",
        on_select=_open_task, height=340, column_config=profile_config,
        sort_label="critical path, bottleneck score, then duration",
    )
    result_caption(nodes, note=f"graph run {run_key}")


def _task_version_compare(root_id: str) -> None:
    versions = run(
        ops_sql.task_graph_versions(root_id, 30), page=_PAGE,
        key=f"task_graph_versions_{root_id}", tier="historical",
        source="TASK_VERSIONS (selected root)", max_rows=30,
    )
    if not guard(versions, "No historical graph versions were found for this root."):
        return
    options = [int(safe_float(value)) for value in versions.df["GRAPH_VERSION"]]
    options = list(dict.fromkeys(options))
    if len(options) < 2:
        empty_state("no_data_yet", "Only one graph version exists, so there is nothing to compare yet.")
        return
    c1, c2 = st.columns(2)
    with c1:
        before_version = st.selectbox(
            "Before", options, index=1, key=f"task_graph_before_{root_id}",
            format_func=lambda value: f"Graph v{value}",
        )
    with c2:
        after_version = st.selectbox(
            "After", options, index=0, key=f"task_graph_after_{root_id}",
            format_func=lambda value: f"Graph v{value}",
        )
    if before_version == after_version:
        st.info("Choose two different graph versions.")
        return
    before = run(
        ops_sql.task_graph_version_nodes(root_id, int(before_version)), page=_PAGE,
        key=f"task_graph_version_{root_id}_{before_version}", tier="historical",
        source="TASK_VERSIONS (historical topology)", max_rows=2_000,
    )
    after = run(
        ops_sql.task_graph_version_nodes(root_id, int(after_version)), page=_PAGE,
        key=f"task_graph_version_{root_id}_{after_version}", tier="historical",
        source="TASK_VERSIONS (historical topology)", max_rows=2_000,
    )
    if not guard(before, "The before-version snapshot is unavailable."):
        return
    if not guard(after, "The after-version snapshot is unavailable."):
        return
    changes = compare_task_versions(before.df, after.df)
    added = int(changes["CHANGE"].eq("ADDED").sum()) if not changes.empty else 0
    removed = int(changes["CHANGE"].eq("REMOVED").sum()) if not changes.empty else 0
    rewired = int(changes["CHANGE"].eq("DEPENDENCIES_CHANGED").sum()) if not changes.empty else 0
    kpi_row([
        {"label": "Before tasks", "value": f"{len(before.df):,}"},
        {"label": "After tasks", "value": f"{len(after.df):,}"},
        {"label": "Added", "value": f"{added:,}", "severity": "warn" if added else "ok"},
        {"label": "Removed", "value": f"{removed:,}", "severity": "bad" if removed else "ok"},
        {"label": "Rewired", "value": f"{rewired:,}", "severity": "warn" if rewired else "ok"},
    ])
    if changes.empty:
        empty_state("clean", "These versions have the same tasks and dependency edges.")
        return

    def _open_changed_task(index: int) -> None:
        task = str(changes.iloc[int(index)]["TASK_FQN"])
        request_navigation(
            "Control Room", "Entity 360",
            context={"entity_type": "TASK", "entity_key": task},
        )

    selectable_nav_table(
        changes, key=f"task_graph_diff_{root_id}_{before_version}_{after_version}",
        on_select=_open_changed_task, height=360,
        sort_label="task name within changed nodes",
    )


def _task_graph_view() -> None:
    section_header("Pipeline topology", "info", "pipeline")
    section_filter_contract(
        filters(),
        applies=(),
        note="Account-wide topology; select one current root and coherent graph version.",
    )
    roots = run(
        ops_sql.task_graph_roots(),
        page=_PAGE,
        key="task_graph_roots",
        # Day-grain failure counts don't need 5-minute freshness; the hourly
        # tier spares repeat visits the snapshot-CTE cost.
        tier="hourly",
        source="TASK_VERSIONS coherent snapshots + current TASKS + MART_TASK_NODE_DAILY",
        max_rows=500,
    )
    if not guard(roots, "No active task graphs found in TASK_VERSIONS."):
        return
    if filters().get("database"):
        st.info("The **Database** filter does not narrow this graph — topology is account-wide "
                "(it scopes the Health and Runs views instead). Use the filter below to find a "
                "root graph by name.")
    if roots.truncated:
        st.caption("This account has more than 500 root task graphs; showing the 500 with the "
                   "most recent failures, then the most tasks. Use the filter below to find a "
                   "specific root.")

    root_rows = {
        str(row.get("ROOT_TASK_ID") or ""): row
        for _, row in roots.df.iterrows()
        if str(row.get("ROOT_TASK_ID") or "").strip()
    }
    root_ids = list(root_rows)
    if not root_ids:
        empty_state("no_data_yet", "No current root tasks were found.")
        return

    def _root_label(root_id: str) -> str:
        row = root_rows[root_id]
        fqn = str(row.get("ROOT_TASK_FQN") or root_id)
        nodes = int(safe_float(row.get("NODE_COUNT")))
        version = int(safe_float(row.get("GRAPH_VERSION")))
        # rec34: surface why a graph sorts to the top — recent failures lead the list.
        # "failed runs" (mart run count, today + yesterday), not distinct tasks — a flaky
        # task that runs every few minutes can log many failed runs in a small graph.
        fails = int(safe_float(row.get("RECENT_FAILURES")))
        fail_note = f" · ⚠ {fails} recent failed runs" if fails else ""
        return f"{fqn} · {nodes} tasks · graph v{version}{fail_note}"

    root_filter = st.text_input(
        "Filter roots (task name contains)",
        key="ops_task_graph_filter",
        placeholder="e.g. ALFA_EDW_PRD or WF_BASE_BILLING",
    ).strip().upper()
    if root_filter:
        matches = [rid for rid in root_ids if root_filter in _root_label(rid).upper()]
        if not matches:
            empty_state("no_data_yet", f"No root task graph matches '{root_filter}'.")
            return
        root_ids = matches

    root_id = st.selectbox(
        "Root task",
        root_ids,
        format_func=_root_label,
        key="ops_task_graph_root",
        help="Ordered by recent failed runs first (today + yesterday, from the daily task "
             "mart), then task count — the graph most likely to need attention is on top.",
    )
    root_row = root_rows[str(root_id)]
    graph_version = int(safe_float(root_row.get("GRAPH_VERSION")))
    graph = run(
        ops_sql.task_graph_nodes(str(root_id), graph_version),
        page=_PAGE,
        key=f"task_graph_{root_id}_{graph_version}",
        tier="recent",
        source="TASK_VERSIONS selected root/version + current TASKS + MART_TASK_NODE_DAILY",
        max_rows=2_000,
    )
    if not guard(graph, "The selected task graph has no nodes."):
        return

    frame = graph.df.copy()
    expected = int(safe_float(frame.iloc[0].get("SNAPSHOT_NODE_COUNT")))
    if graph.truncated or expected != len(frame):
        st.error(
            f"Graph snapshot incomplete: received {len(frame):,} of {expected:,} nodes. "
            "The diagram was not rendered."
        )
        return
    shape = inspect_task_graph(frame["TASK_FQN"], frame["PREDECESSORS"])
    integrity_errors = []
    if shape.duplicate_nodes:
        integrity_errors.append(f"duplicate nodes: {', '.join(shape.duplicate_nodes[:8])}")
    if shape.missing_predecessors:
        integrity_errors.append(
            f"missing predecessors: {', '.join(shape.missing_predecessors[:8])}"
        )
    if shape.cyclic_nodes:
        integrity_errors.append(f"cycle detected around: {', '.join(shape.cyclic_nodes[:8])}")
    if integrity_errors:
        st.error("Graph integrity check failed; nothing was drawn. " + " · ".join(integrity_errors))
        return

    failed = int(frame["RECENT_FAILURES"].map(safe_float).gt(0).sum())
    suspended = int(frame["STATE"].astype(str).str.contains("suspend", case=False).sum())
    kpi_row([
        {"label": "Tasks", "value": f"{len(frame):,}"},
        {"label": "Dependencies", "value": f"{len(shape.edges):,}"},
        {"label": "Failed tasks (recent)", "value": f"{failed:,}",
         "severity": "bad" if failed else "ok"},
        {"label": "Suspended", "value": f"{suspended:,}",
         "severity": "warn" if suspended else "ok"},
    ])

    graph_view = nested_sections(
        ["Topology", "Run analyzer", "Version compare"],
        key="ops_task_graph_detail_view",
    )
    if graph_view == "Run analyzer":
        _task_run_analyzer(str(root_id), frame, shape)
        return
    if graph_view == "Version compare":
        _task_version_compare(str(root_id))
        return

    dot = charts.task_dag_dot(frame, shape)
    renderer_options = ["Interactive", "Graphviz"]
    renderer_key = "ops_task_graph_renderer"
    if st.session_state.get(renderer_key) not in renderer_options:
        st.session_state[renderer_key] = renderer_options[0]
    if hasattr(st, "segmented_control"):
        st.session_state[f"_{renderer_key}_last"] = st.session_state[renderer_key]

        def _keep_renderer() -> None:
            if st.session_state.get(renderer_key) is None:
                st.session_state[renderer_key] = (
                    st.session_state.get(f"_{renderer_key}_last") or renderer_options[0]
                )

        renderer = st.segmented_control(
            "Renderer",
            renderer_options,
            key=renderer_key,
            on_change=_keep_renderer,
        )
    else:
        renderer = st.selectbox(
            "Renderer", renderer_options, key=renderer_key
        )
    renderer = renderer or st.session_state.get(f"_{renderer_key}_last") or "Interactive"
    rendered = renderer != "Graphviz" and charts.interactive_task_dag(frame, shape, height=680)
    if not rendered:
        try:
            st.graphviz_chart(dot, width="stretch", height=680)
        except TypeError:
            st.graphviz_chart(dot, width="stretch")
    export_button(
        "Task graph (DOT)",
        dot,
        file_name="overwatch-task-graph.dot",
        mime="text/vnd.graphviz",
        key="ops_task_graph_dot",
    )
    st.caption(
        "Green = healthy · red = failed recently (today + yesterday, daily task mart) · "
        "gray = suspended. "
        "The selected root and graph version are rendered as one coherent snapshot."
    )
    result_caption(graph, note=f"root {root_id} · graph version {graph_version}")


def _tasks_tab(company: str, days: int, database: str = "", schema_contains: str = "") -> None:
    view = nested_sections(
        ["Health", "SLA", "Graph", "Runs"],
        key="ops_tasks_view",
    )
    if view == "Health":
        _task_health_view(company, days, database, schema_contains)
    elif view == "SLA":
        _task_sla_view(company, days, database, schema_contains)
    elif view == "Graph":
        _task_graph_view()
    else:
        _task_runs_view(company, days, database, schema_contains)


def _warehouses_tab(company: str, rate: float, days: int) -> None:
    section_header("Warehouse spend & anomalies", "info", "warehouse", anchor="ops-wh-spend")
    res = run(mart_sql.fact_warehouse_daily(30, company), page=_PAGE, key=f"w_fact_{company}",
              tier="hourly", source="FACT_WAREHOUSE_DAILY")
    if not guard(res, "No warehouse dailies yet — the hourly loader fills them.",
                 setup_hint="Live equivalent lives on Cost & Contract > Spend & Attribution."):
        return
    df = res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    # B4: score only complete days (today's partial row false-flags a steady wh);
    # the trend chart below keeps the full frame including today.
    flagged = flag_anomalies(complete_days_only(df), "USD", group_col="WAREHOUSE_NAME",
                             min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
    # Known-spike calendar: same suppression as the Cost sweep, so the same day
    # never reads "expected" there but anomalous here.
    flagged = suppress_expected_spikes(
        flagged, str(load_settings(_PAGE).get("EXPECTED_SPIKE_CALENDAR") or ""))
    daily = df.groupby("DAY", as_index=False)["USD"].sum()
    st.caption("Click a day in the trend to break its spend down by warehouse. "
               "Dashed rules flag anomalous warehouse-days.")
    # UI15: mark the anomalous warehouse-days already scored above on the trend.
    picked_day = charts.spend_trend(daily, key=f"ops_wh_spend_day_{company}",
                                    markers=anomaly_markers(flagged, "DAY", "WAREHOUSE_NAME"))
    if picked_day:
        # UI23: the clicked day's per-warehouse breakdown, from the frame already
        # loaded above — no extra scan. Each warehouse row drills to its Entity 360.
        # Match DAY via the SAME strftime the chart used for its click key, so a
        # date/Timestamp representation gap can't blank a valid click.
        import pandas as pd
        _day = pd.to_datetime(df["DAY"], errors="coerce").dt.strftime("%Y-%m-%d")
        day_rows = df[_day == picked_day]
        if not day_rows.empty:
            breakdown = (day_rows.groupby("WAREHOUSE_NAME", as_index=False)["USD"].sum()
                         .sort_values("USD", ascending=False))
            st.markdown(f"**{picked_day} — spend by warehouse**")
            entity_nav_table(
                breakdown, key=f"ops_wh_day_break_{company}", key_col="WAREHOUSE_NAME",
                entity_type="WAREHOUSE", size_note=False,
                column_config={"USD": st.column_config.NumberColumn("Spend $", format="$%.0f")})
    anomalies = flagged[flagged["IS_ANOMALY"]]
    if anomalies.empty:
        empty_state("clean", "No per-warehouse daily anomalies (30d, median/MAD z ≥ 3.5).")
    else:
        st.warning(f"{len(anomalies)} anomalous warehouse-day(s):")
        # N10: rank by |z| so spend COLLAPSES (large negative z — a stalled-loader
        # signal) surface next to spikes instead of sinking to the bottom.
        _anom = anomalies[["DAY", "WAREHOUSE_NAME", "USD", "Z_SCORE"]]
        _anom = _anom.reindex(_anom["Z_SCORE"].abs().sort_values(ascending=False).index)
        styled_table(  # rec21 + rec31: the warning above already states the count
            _anom, size_note=False,
            column_config={
                "USD": st.column_config.NumberColumn("Spend $", format="$%.0f"),
                "Z_SCORE": st.column_config.NumberColumn("Robust z", format="%.1f"),
            },
        )
    result_caption(res)

    section_header(
        "Concurrency peaks (right-size before queuing hurts)",
        "info",
        "warehouse",
        anchor="ops-wh-concurrency",
    )
    peaks = run(ops_sql.warehouse_concurrency_peaks(14, company), page=_PAGE,
                key=f"conc_peaks_{company}", tier="recent",
                source="ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY")
    if peaks.ok and peaks.empty:
        empty_state("no_data_yet", "No warehouse load intervals recorded in the last 14 days.")
    elif guard(peaks, ""):
        st.caption("PEAK_QUEUED above ~1 on a sustained basis is the signal to add a cluster "
                   "or split workloads — before users feel it. Select a warehouse to open its "
                   "Entity 360.")
        entity_nav_table(peaks.df, key=f"ops_wh_peaks_{company}", key_col="WAREHOUSE_NAME",
                         entity_type="WAREHOUSE", column_config={
            "PEAK_RUNNING": st.column_config.NumberColumn("Peak Running", format="%.1f"),
            "PEAK_QUEUED": st.column_config.NumberColumn("Peak Queued", format="%.1f"),
        })
        result_caption(peaks)

    # O12: warehouse utilization & right-sizing — reuse the Cost-side profile
    # (idle share, queue/spill, size verdict) here as a DIAGNOSTIC; execution
    # (the ALTER + savings booking) stays on Cost & Contract -> Optimize.
    section_header("Utilization & right-sizing", "info", "warehouse", anchor="ops-wh-utilization")
    if not st.toggle("Load utilization profile (heavy scan)", key="ops_wh_sizing_load",
                     help="Per-warehouse idle %, queue/spill, p95 and a size verdict over the window."):
        st.caption("Toggle to profile every warehouse's idle share and right-size verdict on demand.")
    elif guard((_prof := run_mart_first(
                    mart27_sql.eff_sizing_profile(days, company),
                    insights_sql.warehouse_sizing_profile(days, company),
                    page=_PAGE, key=f"ops_sizing_{company}_{days}", days=days,
                    mart_source="MART_WAREHOUSE_EFFICIENCY_DAILY (mart — p95 is peak daily)",
                    live_source="WAREHOUSE_METERING_HISTORY x QUERY_HISTORY (live fallback)")),
               "No warehouse activity to profile in this window."):
        import pandas as pd
        _whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="jump_wh",
                   tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
        from app.logic.cost_peers import cost_per_query_peers
        from app.logic.wh_health import warehouse_health
        _sized = size_recommendations(
            with_auto_suspend_settings(
                _prof.df, _whs.df if _whs.ok and not _whs.empty else pd.DataFrame()),
            rate, served_days(_prof, days))
        _sum = sizing_summary(_sized)
        # Wave 3: per-warehouse health chip — a transparent 0-100 grade (base 100 minus
        # capped queue/spill/runtime/low-util penalties) joined onto the sizing table.
        _health = warehouse_health(_sized)
        if not _health.empty:
            _sized = _sized.merge(_health[["WAREHOUSE_NAME", "SCORE", "GRADE", "WHY"]],
                                  on="WAREHOUSE_NAME", how="left")
        kpi_row([
            {"label": "Size up / add cluster", "value": f"{_sum['up']}",
             "delta_color": "inverse" if _sum["up"] else "off"},
            {"label": "Tune auto-suspend first", "value": f"{_sum['suspend']}"},
            {"label": "Size-down candidates", "value": f"{_sum['down']}"},
            {"label": "Idle $ on suspend-first WHs", "value": format_usd(_sum["idle_saving_usd"])},
        ])
        _cols = [c for c in ["WAREHOUSE_NAME", "SCORE", "GRADE", "RECOMMENDATION", "WHY",
                             "IDLE_PCT", "QUEUED_MIN_PER_DAY", "SPILL_GB_PER_DAY",
                             "ACTIVE_DAYS_PER_30D", "P95_ELAPSED_SEC", "MONTHLY_USD_NOW"]
                 if c in _sized.columns]
        entity_nav_table(_sized[_cols], key=f"ops_wh_sizing_tbl_{company}", key_col="WAREHOUSE_NAME",
                         entity_type="WAREHOUSE", column_config={
                             "SCORE": st.column_config.NumberColumn("Health", format="%d"),
                             "WHY": st.column_config.TextColumn("Health penalties"),
                             "IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.0f%%"),
                             "MONTHLY_USD_NOW": st.column_config.NumberColumn("Now $/mo", format="$%.0f"),
                         })
        st.caption("Health = 100 − capped penalties for queueing, remote spill, long p95 runtime, "
                   "and low utilization (evidence-gated). On the mart path p95 is the PEAK-DAY "
                   "value (one bad day penalizes), not the window p95. Diagnostic only — generate "
                   "the ALTER and book the saving on Cost & Contract → Optimize → Idle & sizing.")

        # Wave 3: cost-per-query outliers — cross-sectional (peer) z-score of $/query
        # against the fleet, from the same cached profile (no new scan).
        _peers = cost_per_query_peers(_prof.df, rate)
        if not _peers.empty:
            _out = _peers[_peers["IS_OUTLIER"]]
            section_header("Cost-per-query outliers (vs the fleet)", "info", "warehouse",
                           anchor="ops-wh-peers")
            kpi_row([
                {"label": "Warehouses compared", "value": f"{len(_peers):,}"},
                {"label": "Cost-per-query outliers", "value": f"{len(_out):,}",
                 "severity": "warn" if len(_out) else "",
                 "help": "Warehouses whose $/query is a robust-z outlier (≥3.5, median/MAD) vs their "
                         "company's fleet — 'costs far more per query than its peers'. Materiality "
                         "floor: ≥100 queries and ≥1 credit. Needs ≥5 material warehouses to z-score; "
                         "on a smaller fleet nothing flags — read the × fleet-median column instead."},
            ])
            entity_nav_table(_peers.head(50), key=f"ops_cpq_out_{company}",
                             key_col="WAREHOUSE_NAME", entity_type="WAREHOUSE",
                             height=260, column_config={
                "USD_PER_QUERY": st.column_config.NumberColumn("$/query", format="$%.4f"),
                "MULT_OF_MEDIAN": st.column_config.NumberColumn("× fleet median", format="%.1fx"),
                "Z_SCORE": st.column_config.NumberColumn("Peer z", format="%.1f"),
                "QUERY_COUNT": st.column_config.NumberColumn("Queries", format="%d"),
            })
            st.caption("Cross-sectional (not temporal): each warehouse's $/query vs its company "
                       "fleet's median. A high multiple points at an oversized or under-utilized "
                       "warehouse for its workload — cross-check the right-size verdict above.")

    # O13: quiet-hours (off-hours schedule opportunity) — the hour-of-day view +
    # a per-warehouse quiet-window verdict, reusing the Cost-side reader.
    section_header("Quiet-hours (off-hours schedule opportunity)", "info", "warehouse",
                   anchor="ops-wh-quiet")
    if not st.toggle("Run quiet-hours scan (hour-of-day activity)", key="ops_wh_quiet_load",
                     help="Reads hour-of-day credits vs query activity per warehouse."):
        st.caption("Toggle to find warehouses burning credits in hours with ~no queries.")
    elif guard((_hh := run(
                    insights_sql.warehouse_hourly_activity(days, company), page=_PAGE,
                    key=f"ops_wh_hourly_{company}_{days}", tier="historical",
                    source="WAREHOUSE_METERING_HISTORY x FACT_QUERY_HOURLY (hour-of-day activity)")),
               "No hour-of-day warehouse activity in this window."):
        import pandas as pd
        charts.hour_heatmap(_hh.df, "WAREHOUSE_NAME", "HOUR_OF_DAY", "AVG_CREDITS",
                            title="avg credits/hour")
        st.caption("Dark cells with credits but no matching query activity are the schedule "
                   "opportunity. Generate the SUSPEND/RESUME schedule on Cost & Contract → Optimize.")
        _windows = []
        for _wh in sorted(_hh.df["WAREHOUSE_NAME"].astype(str).unique()):
            _p = remediation.propose_quiet_window(
                _hh.df[_hh.df["WAREHOUSE_NAME"].astype(str) == _wh].to_dict("records"))
            if _p:
                _windows.append({
                    "WAREHOUSE_NAME": _wh,
                    "QUIET_WINDOW": f"{int(_p['start']):02d}:00–{int(_p['end']):02d}:00",
                    "HOURS": int(_p["hours"]),
                    "AVG_CREDITS_PER_DAY": round(safe_float(_p.get("avg_credits_per_day")), 2)})
        if _windows:
            st.markdown("**Warehouses with a defensible quiet window (4h+)**")
            entity_nav_table(pd.DataFrame(_windows), key=f"ops_wh_quiet_tbl_{company}",
                             key_col="WAREHOUSE_NAME", entity_type="WAREHOUSE", size_note=False)
        else:
            st.caption("No warehouse has a recurring 4h+ window burning credits with ~no queries "
                       "(sparse or all-day-idle profiles route to auto-suspend instead).")
        result_caption(_hh)

    _monitor_coverage_panel()
    _adaptive_candidacy_panel(company, days)


def _adaptive_candidacy_panel(company: str, days: int) -> None:
    """Adaptive-compute candidacy (repo review wave 3): which warehouses would
    benefit from auto-scaling compute — bursty, material-volume load — vs those a
    fixed size or auto-suspend serves better. Scored from the hour-of-day profile
    already read for the off-hours advisor (cached), no new scan."""
    from app.logic.adaptive import adaptive_compute_candidacy, candidacy_summary

    section_header("Adaptive-compute candidacy", "info", "operations")
    # Both feeds share ONE window (review #4): warehouse_hourly_activity caps at 30d,
    # so cap idle to match — else the idle discount would cover a longer span than the
    # burst profile it modifies.
    _win = min(int(days), 30)
    # Perf: the two independent 'hourly' reads (idle_warehouse_analysis is a heavy
    # QUERY_HISTORY+METERING join) prefetch in one parallel batch; each keeps its run() fallback.
    _acb = run_batch([
        {"key": "hourly", "sql": insights_sql.warehouse_hourly_activity(_win, company),
         "source": "WAREHOUSE_METERING_HISTORY x FACT_QUERY_HOURLY (hour-of-day)"},
        {"key": "idle", "sql": insights_sql.idle_warehouse_analysis(_win, company),
         "source": "WAREHOUSE_METERING_HISTORY (idle credits)"},
    ], page=_PAGE, tier="hourly")
    hourly = _acb.get("hourly") or run(insights_sql.warehouse_hourly_activity(_win, company), page=_PAGE,
                 key=f"ops_wh_hourly_{company}_{_win}", tier="hourly",
                 source="WAREHOUSE_METERING_HISTORY x FACT_QUERY_HOURLY (hour-of-day)")
    idle = _acb.get("idle") or run(insights_sql.idle_warehouse_analysis(_win, company), page=_PAGE,
               key=f"ops_wh_idle_{company}_{_win}", tier="hourly",
               source="WAREHOUSE_METERING_HISTORY (idle credits)")
    if not guard(hourly, "Needs hour-of-day warehouse metering to score candidacy."):
        return
    scored = adaptive_compute_candidacy(hourly.df, idle.df if idle.ok else None)
    if scored.empty:
        st.caption("No warehouse has a material, varying load profile to score in this window.")
        return
    k = candidacy_summary(scored)
    kpi_row([
        {"label": "Warehouses scored", "value": f"{k['warehouses']:,}"},
        {"label": "Strong candidates", "value": f"{k['strong']:,}",
         "severity": "ok" if k["strong"] else "",
         "help": "Bursty (peak >> average), material-volume warehouses where adding clusters "
                 "at the peak and dropping them off-peak pays off."},
        {"label": "Worth considering", "value": f"{k['consider']:,}"},
    ])
    styled_table(scored, height=300, column_config={
        "SCORE": st.column_config.NumberColumn("Score", format="%d"),
        "PEAK_TO_MEAN": st.column_config.NumberColumn("Peak÷mean", format="%.1fx"),
        "DAILY_CREDITS": st.column_config.NumberColumn("Credits/day", format="%.1f"),
        "IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.0f%%"),
    })
    st.caption("Score 0-100 = burstiness (peak-to-mean of the hour-of-day credit profile) × "
               "material volume × an idle discount. High burst + volume ⇒ adaptive scaling "
               "helps; a heavy-idle warehouse is flagged **auto-suspend first** (the steadier "
               "lever); flat load ⇒ a fixed size is fine. An ordering heuristic — the rationale "
               "shows the inputs.")
    result_caption(hourly)


def _monitor_coverage_panel() -> None:
    """Resource-monitor coverage map (repo review wave 2): the app can't CREATE
    monitors (read-only), but it CAN show which warehouses run with NO spend cap
    at all — the governance blind spot Snowsight never rolls up."""
    from app.logic.wave2 import monitor_coverage

    _whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="rmcov_wh",
               tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
    _mons = run(ops_sql.show_resource_monitors_sql(), page=_PAGE, key="rmcov_mons",
                tier="metadata", source="SHOW RESOURCE MONITORS", max_rows=0, probe=True)
    if not _whs.ok:
        section_header("Resource-monitor coverage", "", "cost")
        st.caption("SHOW WAREHOUSES failed — coverage can't be assessed this pass.")
        return
    cov = monitor_coverage(_whs.df, _mons.df if _mons.ok else None)
    # C23: amber only when warehouses are genuinely uncapped — an ACCOUNT-level
    # monitor caps everything, so per-warehouse non-assignment stays calm.
    section_header("Resource-monitor coverage",
                   alarm_health(0 if bool(cov.get("account_monitor"))
                                else int(cov["uncovered"])), "cost")
    # Review #4: a LEVEL=ACCOUNT monitor caps every warehouse — per-warehouse
    # non-assignment is then a detail, not an "uncapped" alarm.
    _acct = bool(cov.get("account_monitor"))
    _unc_label = ("No dedicated monitor (account cap applies)" if _acct
                  else "No monitor — uncapped")
    kpi_row([
        {"label": "Warehouses with a monitor", "value": f"{cov['covered']:,}"},
        {"label": _unc_label, "value": f"{cov['uncovered']:,}",
         "delta_color": "off" if _acct else ("inverse" if cov["uncovered"] else "off"),
         "help": ("An ACCOUNT-level monitor caps all warehouses; these just lack a "
                  "dedicated per-warehouse quota." if _acct else
                  "No resource monitor = no credit quota, no suspend threshold — a runaway "
                  "workload on these has no automatic ceiling. Monitors are created by an "
                  "admin in Snowsight; this map is the coverage picture for monitors this "
                  "role can see.")},
    ])
    if cov["uncovered_names"] and not _acct:
        st.warning("Uncapped warehouses: " + ", ".join(cov["uncovered_names"][:20])
                   + (" …" if len(cov["uncovered_names"]) > 20 else ""))
    if not cov["monitors_df"].empty:
        st.markdown("**Existing monitors — quota consumed**")
        styled_table(cov["monitors_df"], height=200)
    elif _mons.ok:
        st.caption("No resource monitors are visible to this role (SHOW returns only "
                   "monitors it can MONITOR) — none may exist, or the grant is missing.")
    result_caption(_whs)


def _contention_tab(company: str, days: int) -> None:
    left, right = st.columns(2)
    with left:
        section_header("Warehouse queue & spill pressure", "info", "warehouse")
        # r23 #1: the hourly fact answers this without a QUERY_HISTORY scan
        # (the live read sat at 17.8s on the fleet board). r19 #18 still
        # holds — no one-member batch; mart-first with the labeled fallback.
        res = run_mart_first(
            mart_sql.fact_warehouse_pressure(days, company),
            ops_sql.warehouse_pressure(days, company),
            page=_PAGE, key=f"c_pressure_{company}_{days}",
            mart_source="FACT_QUERY_HOURLY (mart — p95 is peak hourly)",
            live_source="QUERY_HISTORY (live fallback)",
            mart_tier="hourly", live_tier="recent")
        if guard(res, "No queueing or spill pressure in this window.", kind="clean"):
            import pandas as pd
            pdf = res.df.copy()
            if {"QUEUED_SEC", "QUERY_COUNT"}.issubset(pdf.columns):
                _qc = pd.to_numeric(pdf["QUERY_COUNT"], errors="coerce").replace(0, pd.NA)
                pdf["AVG_QUEUE_SEC"] = pd.to_numeric(pdf["QUEUED_SEC"], errors="coerce") / _qc
            _chart_metric = "AVG_QUEUE_SEC" if "AVG_QUEUE_SEC" in pdf.columns else "QUEUED_SEC"
            _chart_title = ("Average queue per query (seconds)"
                            if _chart_metric == "AVG_QUEUE_SEC" else "Queued seconds (total)")
            charts.bar_count(pdf.sort_values(_chart_metric, ascending=False),
                             "WAREHOUSE_NAME", _chart_metric, title=_chart_title,
                             takeaway=True)
            st.caption("Ranked by **Avg queue per query**, the user-felt stall signal. Query count "
                       "and total queued time remain in the evidence table so sustained materiality "
                       "is visible beside the rate. Select a warehouse to open its Entity 360.")
            entity_nav_table(pdf.sort_values("AVG_QUEUE_SEC", ascending=False)
                             if "AVG_QUEUE_SEC" in pdf.columns else pdf,
                             key=f"ops_wh_pressure_{company}_{days}", key_col="WAREHOUSE_NAME",
                             entity_type="WAREHOUSE")
    with right:
        section_header("Lock waits", "info", "warehouse")
        _lock_db = str(st.session_state.get("flt_database", "") or "").strip()
        # V035: the live scan read 46-56 GB / 74-259s per view (Joe's own
        # Heaviest-queries panel, 2026-07-10) — mart-first, always.
        res = run_mart_first(
            mart27_sql.lock_wait_daily(min(days, 14), company),
            ops_sql.lock_contention(min(days, 14)),
            page=_PAGE, key=f"c_locks_{company}_{days}_{_lock_db}",  # #34: scope in the cache key
            mart_source=f"MART_LOCK_WAIT_DAILY ({company} + account-level)",
            live_source="ACCOUNT_USAGE.LOCK_WAIT_HISTORY (account-wide, pre-V035)",
            empty_is_answer=True)
        if guard(res, "No lock waits recorded (or the view is not accessible in this edition)."):
            # #34: bring both paths to one company + database contract. Neither
            # builder (both outside this cluster) takes a database predicate, and
            # the live LOCK_WAIT_HISTORY fallback carries no company scope at all
            # (COMPANY lives only on the mart) — yet both return DATABASE_NAME. Scope
            # at the seam on that object grain: the live fallback to the company by
            # database classification, and the active Database filter to both.
            _ldf = res.df
            _served_live = bool(getattr(_ldf, "attrs", {}).get("_ow_served_live"))
            if _served_live and company not in ("ALL", "") and "DATABASE_NAME" in _ldf.columns:
                _ldf = _ldf[_ldf["DATABASE_NAME"].map(companies.classify_database) == company]
            if _lock_db and "DATABASE_NAME" in _ldf.columns:
                _ldf = _ldf[_ldf["DATABASE_NAME"].astype(str).str.upper() == _lock_db.upper()]
            if _ldf.empty:
                st.caption("No lock waits in this company/database scope.")
            else:
                styled_table(_ldf)
                result_caption(res)



def _wh_change_block(company: str, is_operator: bool) -> None:
    st.divider()
    section_header("Warehouse setting changes", "info", "warehouse", anchor="ops-change-wh")
    st.caption(
        "Daily warehouse setting diffs, with before/after cost and performance verdicts."
    )
    panel_help(
        "Detection is snapshot-diff (daily SHOW WAREHOUSES — this account has no "
        "ACCOUNT_USAGE.WAREHOUSES view), so a change is seen within a day. Each change "
        "freezes a 14-day baseline and tracks 14 days after: dollars/day, p95, queueing, "
        "spill, and failures. Confirmed regressions raise WH_CHANGE_REGRESSION alerts. "
        "CHANGE_SOURCE: MANAGED = made by a DEPLOY_ACTORS service user; MANUAL = a human; "
        "UNKNOWN = no matching ALTER found near the snapshot."
    )
    wh_contains = str(st.session_state.get("flt_warehouse_contains", "") or "")
    res = run(change_impact_sql.warehouse_change_registry(90, company, wh_contains),
              page=_PAGE, key=f"whchg_{company}_{wh_contains}", tier="recent",
              source="WAREHOUSE_CHANGE_REGISTRY")
    if res.ok and res.empty:
        empty_state(
            "no_data_yet",
            "No warehouse setting changes detected yet. The daily scan "
            "(TASK_WAREHOUSE_CHANGE_SCAN) seeds its first snapshot on the first run "
            "and detects changes from the second snapshot onward."
        )
    elif guard(res, "", setup_hint="Not installed yet — apply V024, then the daily scan populates this."):
        df = res.df.copy()
        k = wh_change.registry_kpis(df)
        kpi_row([
            {"label": "Changes tracked (90d)", "value": f"{k['changes']}"},
            {"label": "Regressed", "value": f"{k['regressed']}",
             "delta_color": "inverse" if k["regressed"] else "off",
             "help": "Worse $/day, p95, queueing, or failure rate vs the frozen pre-change baseline."},
            {"label": "Improved", "value": f"{k['improved']}"},
            {"label": "Still accumulating", "value": f"{k['pending']}",
             "help": "Fewer than 3 after-days or 20 after-queries so far — no verdict yet."},
        ])
        sel = selectable_table(df[[c for c in (
            "VERDICT", "WAREHOUSE_NAME", "SETTING", "OLD_VALUE", "NEW_VALUE",
            "CHANGE_SEEN_AT") if c in df.columns]],
            key="whchg_sel", height=260)
        result_caption(res)
        row = df.iloc[int(sel)] if sel is not None and 0 <= int(sel) < len(df) else None
        if row is None:
            st.caption("Select a warehouse change to inspect its before/after evidence.")
        else:
            deltas = wh_change.change_deltas(row.to_dict())
            st.markdown(f"**{row['WAREHOUSE_NAME']}** — {row['SETTING']} "
                        f"{row.get('OLD_VALUE', '?')} → {row.get('NEW_VALUE', '?')}")
            _verdict_detail = str(row.get("VERDICT_DETAIL") or "").strip()
            if _verdict_detail:
                st.caption(f"**{row.get('VERDICT')}** — {_verdict_detail}")
            if deltas:
                def _wc_val(d: dict) -> str:
                    # P95_S is an elapsed time in seconds — humanize it (30m, 1h 30m),
                    # not a bare "1800". The other four metrics are rates/counts/%.
                    if d.get("col") == "P95_S":
                        return f"{humanize_duration(d['base'], 's')} → {humanize_duration(d['after'], 's')}"
                    return f"{d['base']:g} → {d['after']:g}"

                kpi_row([{
                    "label": "p95" if d.get("col") == "P95_S" else d["metric"],
                    "value": _wc_val(d),
                    "delta": (f"{d['delta_pct']:+.1f}%"
                              if d["delta_pct"] is not None else "new load"),
                    "delta_color": ("inverse" if d["direction"] == "worse"
                                    else "normal" if d["direction"] == "better" else "off"),
                } for d in deltas[:5]])
            else:
                st.caption("Before/after stats still accumulating for this change.")
        # T1.4: the 28d WMH+QH history join is heavy and used to run every render on
        # the defaulted first row. Load it only on an explicit row selection, and off
        # the live cadence (historical tier — the sources lag 45min+).
        if sel is not None and row is not None:
            hist = run(change_impact_sql.warehouse_daily_series(str(row["WAREHOUSE_NAME"]), 28),
                       page=_PAGE, key=f"whchg_hist_{row['WAREHOUSE_NAME']}", tier="historical",
                       source="WAREHOUSE_METERING_HISTORY + QUERY_HISTORY")
            if guard(hist, "No activity recorded for this warehouse in the last 28 days."):
                charts.daily_metric_line(hist.df, "DAY", "CREDITS", title="credits/day",
                                         rule_date=row.get("CHANGE_SEEN_AT"))
                st.caption("Dashed line marks the detected change (seen within a day of the ALTER).")
        else:
            st.caption("Select a change above to load its 28-day credits/day history.")
    if is_operator:
        if (st.button("Run warehouse scan now", key="whchg_scan_now",
                      help="Snapshots settings, registers diffs, and re-evaluates verdicts immediately.")
                and write_gate_open("whchg_scan_now")):
            ok, msg = execute_statement(change_impact_sql.run_wh_scan_call(), page=_PAGE)
            stamp_write("whchg_scan_now", ok)  # C48
            notify(ok, msg)
    else:
        st.caption("The warehouse scan runs daily at 06:40; admins can trigger it on demand.")


def _change_impact_tab(company: str, database: str, schema_contains: str,
                       is_operator: bool) -> None:
    section_header("Procedure and task changes", "info", "operations", anchor="ops-change-objects")
    st.caption(
        "Daily procedure/task diffs, ranked by before/after runtime, failures, and credits/call."
    )
    panel_help(
        "When a stored procedure or task changes, the daily scan freezes a 14-day "
        "pre-change baseline and compares the 14 days after: runs, p95 runtime, failure "
        "rate, and measured credits/call (QUERY_ATTRIBUTION_HISTORY roll-up to the CALL). "
        "REGRESSED rows raise PERF_CHANGE_REGRESSION alerts automatically."
    )
    res = run(change_impact_sql.change_registry(90, company, database, schema_contains),
              page=_PAGE, key=f"chg_reg_{company}_{database}_{schema_contains}",
              tier="recent", source="OBJECT_CHANGE_REGISTRY")
    if res.ok and res.empty:
        empty_state(
            "no_data_yet",
            "No procedure/task changes registered for this scope yet. The daily scan "
            "(TASK_CHANGE_IMPACT_SCAN) registers changes within a day of the ALTER / "
            "CREATE OR REPLACE, then tracks each one for 14 days."
        )
    elif guard(res, "", setup_hint="Not installed yet — an admin can verify on Admin → Migrations & freshness. The daily scan then populates this within a day."):
        df = res.df.copy()
        verdicts = df["VERDICT"].astype(str).str.upper()
        kpi_row([
            {"label": "Changes tracked (90d)", "value": f"{len(df)}"},
            {"label": "Regressed", "value": f"{int((verdicts == 'REGRESSED').sum())}",
             "delta_color": "inverse" if (verdicts == "REGRESSED").any() else "off",
             "help": "Worse credits/call, p95, or failure rate vs the frozen pre-change baseline."},
            {"label": "Improved", "value": f"{int((verdicts == 'IMPROVED').sum())}"},
            {"label": "Still accumulating", "value": f"{int((verdicts == 'PENDING').sum())}",
             "help": "Fewer than 5 post-change runs so far — no verdict yet."},
        ])
        _ci = with_user_names(df, _PAGE, user_col="CHANGED_BY", display_col="Changed by")
        # r4: a readable, laptop-fittable change table. with_user_names already added the
        # resolved "Changed by", so the raw CHANGED_BY was a duplicate identity column;
        # drop it, collapse each baseline/after pair into ONE signed delta (what you scan),
        # move the long VERDICT_DETAIL into the row drill below, and label/format columns.
        # Absolutes stay one click away in the run-history drill.
        import pandas as pd

        def _chg_delta(after: str, base: str):
            return (pd.to_numeric(_ci.get(after), errors="coerce")
                    - pd.to_numeric(_ci.get(base), errors="coerce"))

        _ci = _ci.assign(
            D_CALLS=_chg_delta("AFTER_CALLS", "BASELINE_CALLS"),
            D_P95_S=_chg_delta("AFTER_P95_S", "BASELINE_P95_S"),
            D_CPC=_chg_delta("AFTER_CREDITS_PER_CALL", "BASELINE_CREDITS_PER_CALL"),
        )
        show_cols = ["VERDICT", "OBJECT_TYPE", "DATABASE_NAME", "SCHEMA_NAME", "OBJECT_NAME",
                     "CHANGE_SEEN_AT", "Changed by", "AFTER_CALLS", "D_CALLS", "D_P95_S", "D_CPC"]
        sel_ci = selectable_table(
            _ci[[c for c in show_cols if c in _ci.columns]], key="chg_sel", height=320,
            column_config={
                "OBJECT_TYPE": st.column_config.TextColumn("Type"),
                "DATABASE_NAME": st.column_config.TextColumn("Database"),
                "SCHEMA_NAME": st.column_config.TextColumn("Schema"),
                "OBJECT_NAME": st.column_config.TextColumn("Object"),
                "CHANGE_SEEN_AT": st.column_config.TextColumn("Changed"),
                "AFTER_CALLS": st.column_config.NumberColumn("Calls (after)", format="%d"),
                "D_CALLS": st.column_config.NumberColumn("Δ calls", format="%+d"),
                "D_P95_S": st.column_config.Column("Δ p95"),
                "D_CPC": st.column_config.NumberColumn("Δ cr/call", format="%+.4f"),
            })
        result_caption(res)

        section_header("Run history around one change", "info", "operations")
        picks = sorted({f"{t} {n}" for t, n in zip(df["OBJECT_TYPE"], df["OBJECT_NAME"], strict=True)})
        clicked_obj = None
        if sel_ci is not None:
            crow = df.iloc[int(sel_ci)]
            clicked_obj = f"{crow['OBJECT_TYPE']} {crow['OBJECT_NAME']}"
            # r4: the full verdict rationale lives here now (was a wide table column)
            _vd = str(crow.get("VERDICT_DETAIL") or "").strip()
            if _vd:
                st.caption(f"**{crow.get('VERDICT')}** — {_vd}")
        pick = clicked_obj or st.selectbox("Object (or click a row above)", picks, key="chg_pick")
        # T1.4: the 28d QUERY/TASK_HISTORY scan used to run every render on the
        # auto-selected first object. A row click loads it immediately; otherwise it
        # waits behind a load toggle (the DAG/streams pattern on this page). Historical.
        _load_hist = pick and (sel_ci is not None
                               or st.toggle("Load 28-day run history", key="chg_hist_toggle"))
        if _load_hist:
            otype, _, name = pick.partition(" ")
            hist = run(change_impact_sql.object_run_history(otype, name, 28),
                       page=_PAGE, key=f"chg_hist_{pick}", tier="historical",
                       source="ACCOUNT_USAGE.QUERY_HISTORY" if otype == "PROCEDURE"
                              else "ACCOUNT_USAGE.TASK_HISTORY")
            if guard(hist, "No runs recorded for this object in the last 28 days."):
                rule_at = None
                match = df[(df["OBJECT_TYPE"] == otype) & (df["OBJECT_NAME"] == name)]
                if not match.empty:
                    rule_at = match["CHANGE_SEEN_AT"].max()
                charts.daily_metric_line(hist.df, "DAY", "P95_S", "p95 runtime (s)", rule_date=rule_at)
                st.caption("Dashed line marks the registered change.")
                styled_table(hist.df)
                result_caption(hist)

    if is_operator:
        if (st.button("Run change-impact scan now", key="chg_scan_now",
                      help="Registers fresh changes and re-evaluates verdicts without waiting for the daily task.")
                and write_gate_open("chg_scan_now")):
            ok, msg = execute_statement(change_impact_sql.run_scan_call(), page=_PAGE)
            stamp_write("chg_scan_now", ok)  # C48
            notify(ok, msg)
    else:
        st.caption("The scan runs daily at 06:50; admins can also trigger it on demand.")

    _wh_change_block(company, is_operator)


# Moved from Admin (v4.50): a live incident-response console belongs where
# the incidents are worked — its subjects (warehouses, queries, pipes,
# tasks) are this page's sections. Executions audit to REMEDIATION_LOG
# under this page name.
_EMERGENCY_CATALOG = """
| Lever | Statement | When |
|---|---|---|
| Suspend warehouse | `ALTER WAREHOUSE <wh> SUSPEND` | Runaway spend — the kill-switch. Billing stops when running queries end. |
| Resume warehouse | `ALTER WAREHOUSE <wh> RESUME` | After the fix. |
| Statement timeout (WH) | `SET STATEMENT_TIMEOUT_IN_SECONDS = n` | Queries running for hours; caps every new statement on that warehouse. |
| Cluster range | `SET MIN/MAX_CLUSTER_COUNT` | Multi-cluster fan-out burning credits, or raise it during a queue emergency. |
| Scaling policy | `SET SCALING_POLICY = ECONOMY` | Slows cluster spawn during bursty-but-tolerant loads. |
| Warehouse size | `SET WAREHOUSE_SIZE = <size>` | Down = cost triage; up = performance firefight (use the remediation panel's resize). |
| Auto-suspend | `SET AUTO_SUSPEND = 60` | Idle-burn discovered mid-incident (remediation panel). |
| Pause pipe | `ALTER PIPE ... SET PIPE_EXECUTION_PAUSED = TRUE` | Ingestion flood / bad file loop. |
| Suspend task | `ALTER TASK <root> SUSPEND` | Runaway or failing task graph (suspend the ROOT). |
| Disable user | `ALTER USER <u> SET DISABLED = TRUE` | Compromised credentials — kills new sessions immediately. |
| Cortex model allowlist | `ALTER ACCOUNT SET CORTEX_MODELS_ALLOWLIST = 'None'` | AI spend kill-switch (Cortex Code / LLM functions). **Account-level: run as SNOW_ACCOUNTADMINS.** |
| Account stmt timeout | `ALTER ACCOUNT SET STATEMENT_TIMEOUT_IN_SECONDS = n` | Global default cap. **Account-level.** |
| Network policy | `ALTER ACCOUNT SET NETWORK_POLICY = <p>` | Access lockdown. **Account-level; not generated here — coordinate before locking yourself out.** |
"""


def _emergency_tab(is_operator: bool) -> None:
    """On-the-fly incident levers: generate exact SQL, confirm, execute, audit."""
    st.caption(
        "Every execution writes a REMEDIATION_LOG audit row (append-only). Warehouse/"
        "pipe/task/user levers run under your role; ACCOUNT-level levers (Cortex "
        "allowlist, account timeout) need SNOW_ACCOUNTADMINS — the SQL is still "
        "generated here for copy-paste."
    )
    panel_help(
        "The catalogue below is the education; the generator builds exact statements "
        "with validated identifiers. Suspending a warehouse does not kill in-flight "
        "queries — pair with a statement timeout when something is stuck. Cortex "
        "allowlist changes apply account-wide within minutes."
    )
    with st.expander("Known emergency levers (reference)", expanded=False):
        st.markdown(_EMERGENCY_CATALOG)

    whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="emg_show_wh",
              tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
    wh_names = []
    if whs.ok and not whs.empty:
        wdf = whs.df.copy()
        wdf.columns = [str(c).lower() for c in wdf.columns]
        if "name" in wdf.columns:
            wh_names = sorted(wdf["name"].astype(str))

    action = st.selectbox("Lever", [
        "Suspend warehouse", "Resume warehouse", "Warehouse statement timeout",
        "Cluster range", "Scaling policy", "Pause pipe", "Resume pipe", "Suspend task",
        "Resume task", "Disable user", "Re-enable user",
        "Cortex allowlist (ACCOUNT)", "Account statement timeout (ACCOUNT)",
    ], key="emg_action")

    stmt = ""
    try:
        if action in ("Suspend warehouse", "Resume warehouse", "Warehouse statement timeout",
                      "Cluster range", "Scaling policy"):
            wh = (st.selectbox("Warehouse", wh_names, key="emg_wh") if wh_names
                  else st.text_input("Warehouse", key="emg_wh_txt"))
            if action == "Suspend warehouse" and wh:
                stmt = remediation.suspend_warehouse(wh)
            elif action == "Resume warehouse" and wh:
                stmt = remediation.resume_warehouse(wh)
            elif action == "Warehouse statement timeout" and wh:
                secs = st.number_input("Timeout seconds (0 = no cap)", 0, 604800, 3600,
                                       step=300, key="emg_secs")
                stmt = remediation.statement_timeout_fix(wh, int(secs))
            elif action == "Cluster range" and wh:
                c1, c2 = st.columns(2)
                lo = c1.number_input("Min clusters", 1, 10, 1, key="emg_min")
                hi = c2.number_input("Max clusters", 1, 10, 1, key="emg_max")
                stmt = remediation.cluster_range_fix(wh, int(lo), int(hi))
            elif action == "Scaling policy" and wh:
                pol = st.radio("Policy", ["ECONOMY", "STANDARD"], horizontal=True, key="emg_pol")
                stmt = remediation.scaling_policy_fix(wh, pol)
        elif action in ("Pause pipe", "Resume pipe"):
            fqn = st.text_input("Pipe (DB.SCHEMA.PIPE)", key="emg_pipe")
            parts = [p for p in fqn.split(".") if p.strip()]
            if len(parts) == 3:
                stmt = remediation.pause_pipe(*parts, paused=(action == "Pause pipe"))
        elif action in ("Suspend task", "Resume task"):
            fqn = st.text_input("Task (DB.SCHEMA.TASK — suspend the ROOT of a graph)",
                                key="emg_task")
            parts = [p for p in fqn.split(".") if p.strip()]
            if len(parts) == 3:
                stmt = remediation.suspend_task_fqn(*parts, resume=(action == "Resume task"))
        elif action in ("Disable user", "Re-enable user"):
            usr = st.text_input("User name", key="emg_user")
            if usr:
                stmt = remediation.disable_user(usr, disabled=(action == "Disable user"))
        elif action == "Cortex allowlist (ACCOUNT)":
            choice = st.radio("Allowlist", ["None (block all AI)", "All (restore)",
                                            "Pinned models"], key="emg_cx")
            if choice.startswith("None"):
                stmt = remediation.cortex_allowlist("None")
            elif choice.startswith("All"):
                stmt = remediation.cortex_allowlist("All")
            else:
                models = st.text_input("Model list (comma-separated)", "llama3.1-8b",
                                       key="emg_cx_models")
                if models:
                    stmt = remediation.cortex_allowlist(models)
        elif action == "Account statement timeout (ACCOUNT)":
            secs = st.number_input("Timeout seconds", 0, 604800, 7200, step=600, key="emg_asecs")
            stmt = remediation.account_statement_timeout(int(secs))
    except ValueError as exc:
        st.error(str(exc))

    if stmt:
        is_account = "ALTER ACCOUNT" in stmt

        def _emg_preview() -> None:
            # rec44: blast-radius warning + SQL preview shown together (inline or in the modal).
            st.code(stmt, language="sql")
            if is_account:
                st.warning("ACCOUNT-level: execute as SNOW_ACCOUNTADMINS. Copy the SQL if this "
                           "session's role lacks the privilege.")

        def _emg_confirm() -> None:
            _emg_preview()
            # C48 re-verify fix: the latch key MUST scope by lever+target — this
            # runs inside a fragment/dialog where the run seq never advances, so
            # a bare "emg" key would lock out EVERY subsequent emergency action
            # for the whole backstop after one success. Short backstop: levers
            # are idempotent state-sets and this surface exists for back-to-back
            # actions under pressure.
            _emg_key = f"emg:{action}:{stmt[:64]}"
            # rec42: one type-to-confirm gate (input + button); EMERGENCY matches EXACT case.
            if (confirm_gate("EMERGENCY", "Execute + audit", key="emg",
                             prompt="Type EMERGENCY to confirm execution", enabled=is_operator)
                    and write_gate_open(_emg_key, backstop=15.0)):
                ok, msg = execute_statement(stmt, page=_PAGE)
                log_sql = (
                    f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                    "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE, EXECUTED_BY) "
                    f"SELECT 'EMERGENCY', {sql_literal(action)}, {sql_literal(stmt[:4000])}, "
                    f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}"
                )
                execute_statement(log_sql, page=_PAGE)
                stamp_write(_emg_key, ok)  # C48
                notify(ok, msg)

        if not is_operator:
            _emg_preview()
            st.caption("Copy the SQL; executing from the app requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")
        elif hasattr(st, "dialog"):
            # rec44: this is the highest-blast-radius single-shot lever, so gate it
            # behind a modal (same hasattr degrade pattern as the Wave 2/3 widgets).
            # Dialogs rerun like fragments, so confirm+execute+audit runs correctly
            # inside _emergency_fragment; older Streamlit falls back to the inline flow.
            @st.dialog("Emergency lever — confirm")
            def _emg_modal() -> None:
                _emg_confirm()

            if st.button("Review + confirm execution", key="emg_open"):
                _emg_modal()
        else:
            _emg_confirm()


def _emergency_extras(is_operator: bool) -> None:
    st.divider()
    # C23: deliberately amber — a destructive control surface, not a findings count.
    section_header("Running queries (kill-switch)", "warn", "bolt")
    panel_help(
        "Live in-flight statements via INFORMATION_SCHEMA (real time). Cancel needs "
        "ownership of the query or OPERATE on its warehouse; the attempt is audited "
        "either way. Suspending a warehouse does NOT kill these — this does."
    )
    if st.toggle("Show running queries now", key="emg_rq_toggle"):
        _rq_whs = run(security_sql.show_warehouses_sql(), page=_PAGE, key="emg_show_wh",
                      tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
        _rq_names: list = []
        if _rq_whs.ok and not _rq_whs.empty:
            _rqdf = _rq_whs.df.copy()
            _rqdf.columns = [str(c).lower() for c in _rqdf.columns]
            if "name" in _rqdf.columns:
                _rq_names = sorted(_rqdf["name"].astype(str))
        _rq_pick = (st.selectbox("Warehouse to inspect", _rq_names, key="emg_rq_wh")
                    if _rq_names else st.text_input("Warehouse to inspect", key="emg_rq_wh_txt"))
        if not _rq_pick:
            st.caption("Pick a warehouse — the in-flight view is per warehouse "
                       "(current-user scoping is unavailable inside SiS).")
            return
        rq = run(ops_sql.running_queries(_rq_pick), page=_PAGE,
                 key=f"emg_running_{_rq_pick}", tier="live",
                 source=f"INFORMATION_SCHEMA.QUERY_HISTORY_BY_WAREHOUSE ({_rq_pick}, live)",
                 max_rows=0)
        if rq.ok and rq.empty:
            empty_state("clean", "Nothing running or queued right now.")
        elif guard(rq, ""):
            _rqdf, _rq_cfg = snowsight_profile_column(rq.df, _PAGE)
            _rqdf = with_user_names(_rqdf, _PAGE)   # who you'd cancel
            sel_rq = selectable_table(_rqdf, key="emg_rq_sel", height=240,
                                      column_config=_rq_cfg or None)
            if sel_rq is not None and is_operator:
                qrow = rq.df.iloc[int(sel_rq)]
                qid = str(qrow["QUERY_ID"])
                st.code(f"SELECT SYSTEM$CANCEL_QUERY('{qid}');", language="sql")
                # C48: short backstop — SYSTEM$CANCEL_QUERY is async and idempotent;
                # re-cancelling a query that survived the first request must not
                # sit behind the default 120s bound (fragment seq never advances).
                if confirm_gate("CANCEL", "Cancel query + audit", key="emg_rq",
                                prompt="Type CANCEL to confirm") and write_gate_open(
                                    f"emg_rq_{qid[:8]}", backstop=15.0):
                    ok, msg = execute_cancel_query(qid, page=_PAGE)   # B2: SELECT is outside the write allow-list
                    execute_statement(
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE, EXECUTED_BY) "
                        f"SELECT 'CANCEL_QUERY', {sql_literal(qid)}, "
                        f"{sql_literal('SYSTEM$CANCEL_QUERY ' + qid)}, "
                        f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}",
                        page=_PAGE)
                    stamp_write(f"emg_rq_{qid[:8]}", ok)  # C48
                    notify(ok, msg)


@st.fragment
def _emergency_fragment(is_operator: bool) -> None:
    """Fragment: lever interactions rerun this section only."""
    _emergency_tab(is_operator)
    _emergency_extras(is_operator)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    # #3: operator gating from the VIEWER identity + allowlist, not CURRENT_ROLE().
    is_operator = _is_operator()
    page_header("Operations", "Queries, tasks, warehouses, contention, change impact, releases, pipeline SLAs, and emergency levers.", icon_name="operations",
                scope_note=f"{f['company']} · {f['window_label']}")
    # Contention folded under Warehouses (CoCo): warehouse health and the
    # contention it causes read together.
    section = lazy_sections(
        ["Queries", "Tasks", "Warehouses", "Change impact",
         "Pipeline SLA", "Release compare", "Emergency"], key="ops_section",
        counts=stashed_counts(_PAGE) or None)
    _contracts = {
        "Queries": {
            "applies": ("company", "days", "database", "warehouse_contains",
                        "user_contains", "schema_contains"),
            "note": "Schema scope uses the live path where the hourly fact lacks that grain.",
        },
        "Tasks": {
            "applies": (),
            "partial": ("company", "days", "database", "schema_contains"),
            "note": "Health and Runs honor this scope; Graph is account-wide and declares that locally.",
        },
        "Warehouses": {
            "applies": ("company",),
            "partial": ("days",),
            "note": "Contention uses Window; warehouse anomaly history is a fixed 30-day view.",
        },
        "Change impact": {
            # v4.157.0: the warehouse-settings registry honors warehouse contains —
            # declare it so the banner stops warning "ignored" where it filters.
            "applies": ("company", "database", "schema_contains", "warehouse_contains"),
            "note": "Panel-local before/after windows replace the global Window.",
        },
        "Pipeline SLA": {
            "applies": (),
            "partial": ("company",),
            "note": "SLA horizons are account-wide policy; the file-load-failure panel "
                    "narrows to the selected Company. (Volume/DT/row-volume panels remain "
                    "account-wide — a follow-up will scope them.)",
        },
        "Release compare": {
            "applies": ("company",),
            "note": "Release date and comparison span are selected inside the panel.",
        },
        "Emergency": {
            "applies": (),
            "note": "Account-level controls; global analytical filters do not constrain actions.",
        },
    }
    section_filter_contract(f, **_contracts[section])
    if section == "Queries":
        _queries_tab(f["company"], f["days"], f["warehouse_contains"], f["user_contains"],
                     f["database"], f["schema_contains"])
    elif section == "Tasks":
        _tasks_tab(f["company"], f["days"], f["database"], f["schema_contains"])
    elif section == "Warehouses":
        _warehouses_tab(f["company"], rate, f["days"])
        st.divider()
        section_header(
            "Contention (queue, spill & lock waits)",
            "info",
            "warehouse",
            anchor="ops-wh-contention",
        )
        _contention_tab(f["company"], f["days"])
    elif section == "Change impact":
        _change_impact_tab(f["company"], f["database"], f["schema_contains"], is_operator)
    elif section == "Pipeline SLA":
        _pipeline_sla_tab(is_operator, f["company"])
    elif section == "Emergency":
        # C23: deliberately amber — dangerous controls warrant standing caution.
        section_header("Emergency levers", "warn", "warehouse")
        _emergency_fragment(is_operator)
    else:
        _release_compare_tab(f["company"])
