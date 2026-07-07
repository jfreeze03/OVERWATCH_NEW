"""Operations — queries, tasks, warehouses, contention."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.errors import safe_page
from app.core.query import execute_statement, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.core.state import filters
from app.data import change_impact_sql, insights_sql, mart_sql, ops_sql
from app.logic.ai_prompts import release_compare_prompt, task_failure_prompt
from app.logic.anomaly import flag_anomalies
from app.logic.formulas import credits_to_usd, safe_float
from app.logic.insights import build_failure_timeline, compare_release_periods, task_release_deltas
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    panel_help,
    result_caption,
    selectable_table,
    styled_table,
)

_PAGE = "Operations"


def _queries_tab(company: str, days: int, wh_filter: str, user_filter: str,
                 database: str = "", schema_contains: str = "") -> None:
    # Hot path: the hourly fact answers this without scanning QUERY_HISTORY.
    # The fact has warehouse/user/database dims but no schema — fall back to
    # live when a schema filter is on, or the mart has no rows yet.
    summary = None
    used_mart = False
    if not schema_contains:
        m = run(mart_sql.fact_query_window_summary(days, company, wh_filter, user_filter, database),
                page=_PAGE, key=f"q_fact_summary_{company}_{days}", tier="recent",
                source="FACT_QUERY_HOURLY (mart, loaded hourly)")
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
        kpi_row([
            {"label": f"Queries ({days}d)", "value": f"{qcount:,.0f}"},
            {"label": "Fail rate", "value": f"{(failed / qcount * 100) if qcount else 0:.2f}%",
             "delta": f"{failed:,.0f} failed", "delta_color": "off"},
            {"label": "p95 runtime" + (" (peak hourly)" if used_mart else ""),
             "value": f"{safe_float(row.get('P95_ELAPSED_SEC')):,.1f}s",
             "help": "Peak hourly-cohort p95 from the fact table; open with a schema filter for the exact raw p95."
                     if used_mart else None},
            {"label": "Queued", "value": f"{safe_float(row.get('QUEUED_SEC')) / 60:,.0f} min"},
            {"label": "Remote spill", "value": f"{safe_float(row.get('SPILL_REMOTE_GB')):,.1f} GB"},
        ])
        result_caption(summary)

    st.markdown("**Heaviest queries**")
    top = run(ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter, user_filter, database, schema_contains),
              page=_PAGE, key=f"q_top_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY", max_rows=50)
    if guard(top, "No queries in this window/scope."):
        sel_q = selectable_table(
            top.df[["USER_NAME", "WAREHOUSE_NAME", "ELAPSED_SEC", "QUEUED_SEC",
                     "SPILL_REMOTE_GB", "EXECUTION_STATUS", "QUERY_PREVIEW"]],
            key="ops_top_sel",
            column_config={
                "ELAPSED_SEC": st.column_config.NumberColumn("Elapsed s", format="%.1f"),
                "QUEUED_SEC": st.column_config.NumberColumn("Queued s", format="%.1f"),
                "SPILL_REMOTE_GB": st.column_config.NumberColumn("Spill GB", format="%.2f"),
            },
        )
        st.caption("Elapsed-time ranking. Per-query dollars are estimates; exact billing is per warehouse.")

    st.markdown("**Query drill-through**")
    candidate_ids: list[str] = []
    if top.usable():
        candidate_ids = [str(q) for q in top.df["QUERY_ID"].dropna().head(50)]
    clicked_qid = ""
    try:
        if top.usable() and sel_q is not None:
            clicked_qid = str(top.df.iloc[int(sel_q)]["QUERY_ID"])
            st.session_state["_ops_drill_target"] = clicked_qid
    except (KeyError, IndexError, ValueError, TypeError):
        clicked_qid = ""
    if candidate_ids:
        picked = st.selectbox("Query ID (from the table above, heaviest first — or click a row)",
                              candidate_ids, key="ops_drill_pick")
        manual = st.text_input("...or paste any query ID", key="ops_drill_manual")
        target = (clicked_qid or manual or picked or "").strip()
        if target and st.button("Load query detail", key="ops_drill_go"):
            st.session_state["_ops_drill_target"] = target
        target_id = st.session_state.get("_ops_drill_target", "")
        if target_id:
            try:
                detail_sql = insights_sql.query_detail(target_id)
            except ValueError as exc:
                st.error(str(exc))
                detail_sql = ""
            if detail_sql:
                detail = run(detail_sql, page=_PAGE, key=f"drill_{target_id[:16]}",
                             tier="recent", source="ACCOUNT_USAGE.QUERY_HISTORY (single query)")
                if guard(detail, "Query not found (IDs age out of QUERY_HISTORY after 365 days)."):
                    row = detail.df.iloc[0]
                    kpi_row([
                        {"label": "Elapsed", "value": f"{safe_float(row.get('ELAPSED_SEC')):,.1f}s",
                         "delta": f"queued {safe_float(row.get('QUEUED_SEC')):,.1f}s", "delta_color": "off"},
                        {"label": "Scanned", "value": f"{safe_float(row.get('GB_SCANNED')):,.2f} GB",
                         "delta": f"{safe_float(row.get('CACHE_PCT')):,.0f}% cache", "delta_color": "off"},
                        {"label": "Partitions", "value": (f"{int(safe_float(row.get('PARTITIONS_SCANNED'))):,}"
                                                          f"/{int(safe_float(row.get('PARTITIONS_TOTAL'))):,}"),
                         "help": "Scanned vs total - high ratios suggest missing pruning."},
                        {"label": "Spill", "value": f"{safe_float(row.get('REMOTE_SPILL_GB')):,.2f} GB remote"},
                        {"label": "Status", "value": str(row.get("EXECUTION_STATUS", "?"))},
                    ])
                    st.code(str(row.get("QUERY_TEXT") or ""), language="sql")
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

    st.markdown("**Failures by error**")
    fails = run(ops_sql.failures_by_error(days, company, database, schema_contains), page=_PAGE,
                key=f"q_fails_{company}_{days}", tier="recent",
                source="ACCOUNT_USAGE.QUERY_HISTORY")
    if guard(fails, "No failed queries in this window."):
        styled_table(fails.df)


def _failure_timeline_section(company: str, database: str = "", schema_contains: str = "") -> None:
    """Root-cause vs cascade view of recent task failures (ported)."""
    st.markdown("**Failure root-cause timeline (7d)**")
    res = run(insights_sql.task_failure_details(7, company, database, schema_contains), page=_PAGE,
              key=f"t_rca_{company}", tier="recent",
              source="ACCOUNT_USAGE.TASK_HISTORY (failures)")
    if not res.ok:
        st.error(f"Failure detail unavailable: {res.error}")
        return
    if res.empty:
        st.success("No task failures in the last 7 days for this scope.")
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
                     title="Failures by family")
    styled_table(
        timeline[["QUERY_START_TIME", "ROLE_IN_GRAPH", "ERROR_FAMILY", "DATABASE_NAME",
                   "SCHEMA_NAME", "TASK_NAME", "RUN_SEC", "ERROR_MESSAGE"]],
    )
    result_caption(res)
    ai_evaluation_panel(
        key=f"task_failures_{company}",
        prompt=task_failure_prompt(timeline, company),
        settings=load_settings(_PAGE),
        page=_PAGE,
        subject="diagnose these task failures",
    )


def _release_compare_tab(company: str) -> None:
    """Before/after a release date: query health + per-task regressions (ported)."""
    st.caption(
        "Pick the deploy date; each side compares the same number of days before and after. "
        "ACCOUNT_USAGE lag means very recent releases under-count the AFTER side."
    )
    col_date, col_window = st.columns([1.2, 1.0])
    with col_date:
        release_day = st.date_input("Release date", value=date.today() - timedelta(days=1),
                                    key="ops_release_date")
    with col_window:
        window = st.select_slider("Compare window (days each side)", options=[1, 2, 3, 5, 7, 14],
                                  value=3, key="ops_release_window")
    release_iso = release_day.isoformat()

    q_res = run(insights_sql.release_query_compare(release_iso, window, company), page=_PAGE,
                key=f"rel_q_{company}_{release_iso}_{window}", tier="historical",
                source="ACCOUNT_USAGE.QUERY_HISTORY")
    st.markdown("**Query health: before vs after**")
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
                st.success("No query-health regression beyond the 10% flat tolerance.")
        else:
            st.info("Need data on both sides of the release date to compare.")
        result_caption(q_res)

    st.markdown("**Task regressions**")
    t_res = run(insights_sql.release_task_compare(release_iso, window, company), page=_PAGE,
                key=f"rel_t_{company}_{release_iso}_{window}", tier="historical",
                source="ACCOUNT_USAGE.TASK_HISTORY")
    if guard(t_res, "No task runs in the compare windows."):
        deltas = task_release_deltas(t_res.df)
        worse = deltas[deltas["GOT_WORSE"]]
        if worse.empty:
            st.success("No task gained failures or slowed >25% after the release.")
        else:
            st.warning(f"{len(worse)} task(s) regressed after the release:")
        styled_table(
            deltas[["DATABASE_NAME", "TASK_NAME", "RUNS_BEFORE", "RUNS_AFTER",
                     "FAILED_BEFORE", "FAILED_AFTER", "NEW_FAILURES",
                     "AVG_SEC_BEFORE", "AVG_SEC_AFTER", "RUNTIME_DELTA_PCT", "GOT_WORSE"]],
        )
        result_caption(t_res)
        ai_evaluation_panel(
            key=f"release_{company}_{release_iso}_{window}",
            prompt=release_compare_prompt(verdicts, deltas, release_iso, window),
            settings=load_settings(_PAGE),
            page=_PAGE,
            subject="judge this release",
        )


def _pipeline_sla_tab(is_operator: bool) -> None:
    """Metadata-driven table freshness SLAs (ported; config lives in V006)."""
    res = run(insights_sql.pipeline_sla_status(), page=_PAGE, key="sla_status", tier="live",
              source="PIPELINE_SLA_STATUS")
    if not res.ok:
        st.info("Pipeline SLA objects not deployed yet — run migration V006.")
        return
    if res.empty:
        st.info("No tables registered. Add rows to PIPELINE_SLA_CONFIG below; the view scores them automatically.")
    else:
        df = res.df.copy()
        met = int(df["SLA_MET"].fillna(False).astype(bool).sum())
        total = len(df)
        kpi_row([
            {"label": "SLA compliance", "value": f"{met / total * 100:,.1f}%",
             "delta": f"{met}/{total} tables", "delta_color": "off"},
            {"label": "Breaching", "value": f"{total - met}",
             "delta_color": "inverse" if total - met else "off"},
        ])
        breaching = df[~df["SLA_MET"].fillna(False).astype(bool)]
        if not breaching.empty:
            st.warning("Tables past their freshness SLA:")
            styled_table(breaching)
        with st.expander("All registered tables"):
            styled_table(df)
        result_caption(res, note="Freshness from ACCOUNT_USAGE.TABLES.LAST_ALTERED (metadata lag up to ~2h).")

    with st.expander("Register a table"):
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
        if is_operator and db and schema and table and st.button("Execute insert", key="sla_exec"):
            ok, msg = execute_statement(insert_sql, page=_PAGE)
            notify(ok, msg)
        elif not is_operator:
            st.caption("Copy and run as OVERWATCH_OPERATOR - in-app execution needs the operator role.")

    st.markdown("**File-load failures (COPY / Snowpipe, 7d)**")
    cpf = run(ops_sql.copy_load_failures(7, "ALL"), page=_PAGE,
              key="copy_fails", tier="recent", source="ACCOUNT_USAGE.COPY_HISTORY")
    if cpf.ok and cpf.empty:
        st.success("No failed or partial file loads in the last 7 days.")
    elif guard(cpf, ""):
        styled_table(cpf.df, height=240)
        st.caption("The PIPE_COPY_FAILURES alert fires on these within the hour (V011); "
                   "this table is the 7-day picture with sample errors.")
        result_caption(cpf)

    st.markdown("**Dynamic table refresh health (7d)**")
    panel_help(
        "Source: ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY (up to ~3h lag). A FAILED "
        "row means every downstream consumer is reading stale data. The daily "
        "PIPE_DT_FAILURES alert fires on 24h failures; this is the weekly picture."
    )
    dth = run(ops_sql.dynamic_table_health(7), page=_PAGE, key="dt_health", tier="recent",
              source="ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY")
    if dth.ok and dth.empty:
        st.info("No dynamic-table refreshes recorded in 7 days (none defined, or the view is empty).")
    elif guard(dth, "", setup_hint="Needs the DYNAMIC_TABLE_REFRESH_HISTORY view (standard on current accounts)."):
        styled_table(dth.df, height=240)
        result_caption(dth)

    st.markdown("**Stream staleness**")
    panel_help(
        "SHOW STREAMS (live metadata — no ACCOUNT_USAGE view exists for staleness). "
        "A STALE stream has passed its retention without being consumed: downstream "
        "pipelines are silently missing changes. Fix = consume or recreate the stream."
    )
    if st.toggle("Check streams now (live SHOW command)", key="ops_streams_toggle"):
        strm = run(ops_sql.show_streams_sql(), page=_PAGE, key="streams_show", tier="live",
                   source="SHOW STREAMS IN ACCOUNT", max_rows=0)
        if strm.ok and strm.empty:
            st.success("No streams in the account.")
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
                    st.dataframe(stale[show_cols], hide_index=True, use_container_width=True)
            else:
                st.dataframe(sdf, hide_index=True, use_container_width=True)


def _tasks_tab(company: str, days: int, database: str = "", schema_contains: str = "") -> None:
    res = run(mart_sql.fact_task_daily(days, company, database), page=_PAGE, key=f"t_fact_{company}_{days}",
              tier="recent", source="FACT_TASK_DAILY")
    if not res.usable():
        res = run(ops_sql.task_runs(days, company, database, schema_contains), page=_PAGE, key=f"t_live_{company}_{days}",
                  tier="recent", source="ACCOUNT_USAGE.TASK_HISTORY (live fallback)")
    if guard(res, "No task runs recorded for this scope/window."):
        df = res.df.copy()
        failed_col = "FAILED" if "FAILED" in df.columns else None
        if failed_col:
            total_runs = safe_float(df.get("RUNS", 0).sum() if "RUNS" in df.columns else 0)
            total_failed = safe_float(df[failed_col].sum())
            kpi_row([
                {"label": f"Task runs ({days}d)", "value": f"{total_runs:,.0f}"},
                {"label": "Failed runs", "value": f"{total_failed:,.0f}",
                 "delta": f"{(total_failed / total_runs * 100) if total_runs else 0:.1f}%",
                 "delta_color": "inverse" if total_failed else "off"},
            ])
            df = df.sort_values(failed_col, ascending=False)
        styled_table(df)
        result_caption(res)
    st.divider()
    _failure_timeline_section(company, database, schema_contains)


def _warehouses_tab(company: str, rate: float) -> None:
    res = run(mart_sql.fact_warehouse_daily(30, company), page=_PAGE, key=f"w_fact_{company}",
              tier="recent", source="FACT_WAREHOUSE_DAILY")
    if not guard(res, "No warehouse dailies yet — V002 facts load them hourly.",
                 setup_hint="Live equivalent lives on Cost & Contract > Attribution."):
        return
    df = res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    flagged = flag_anomalies(df, "USD", group_col="WAREHOUSE_NAME")
    daily = df.groupby("DAY", as_index=False)["USD"].sum()
    charts.spend_trend(daily)
    anomalies = flagged[flagged["IS_ANOMALY"]]
    if anomalies.empty:
        st.success("No per-warehouse daily anomalies (30d, median/MAD z ≥ 3.5).")
    else:
        st.warning(f"{len(anomalies)} anomalous warehouse-day(s):")
        st.dataframe(
            anomalies[["DAY", "WAREHOUSE_NAME", "USD", "Z_SCORE"]].sort_values("Z_SCORE", ascending=False),
            hide_index=True, use_container_width=True,
            column_config={
                "USD": st.column_config.NumberColumn("Spend $", format="$%.0f"),
                "Z_SCORE": st.column_config.NumberColumn("Robust z", format="%.1f"),
            },
        )
    result_caption(res)

    st.markdown("**Concurrency peaks (right-size before queuing hurts)**")
    peaks = run(ops_sql.warehouse_concurrency_peaks(14, company), page=_PAGE,
                key=f"conc_peaks_{company}", tier="recent",
                source="ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY")
    if peaks.ok and peaks.empty:
        st.info("No warehouse load intervals recorded in the last 14 days.")
    elif guard(peaks, ""):
        st.caption("PEAK_QUEUED above ~1 on a sustained basis is the signal to add a cluster "
                   "or split workloads — before users feel it.")
        st.dataframe(peaks.df, hide_index=True, use_container_width=True)
        result_caption(peaks)


def _contention_tab(company: str, days: int) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**Warehouse queue & spill pressure**")
        res = run(ops_sql.warehouse_pressure(days, company), page=_PAGE,
                  key=f"c_pressure_{company}_{days}", tier="recent",
                  source="ACCOUNT_USAGE.QUERY_HISTORY")
        if guard(res, "No queueing or spill pressure in this window."):
            charts.bar_count(res.df.sort_values("QUEUED_SEC", ascending=False),
                             "WAREHOUSE_NAME", "QUEUED_SEC", title="Queued seconds")
            st.dataframe(res.df, hide_index=True, use_container_width=True)
    with right:
        st.markdown("**Lock waits (account-wide)**")
        res = run(ops_sql.lock_contention(min(days, 14)), page=_PAGE, key=f"c_locks_{days}",
                  tier="recent", source="ACCOUNT_USAGE.LOCK_WAIT_HISTORY")
        if guard(res, "No lock waits recorded (or the view is not accessible in this edition)."):
            st.dataframe(res.df, hide_index=True, use_container_width=True)


def _change_impact_tab(company: str, database: str, schema_contains: str,
                       is_operator: bool) -> None:
    st.caption(
        "When a stored procedure or task changes, the daily scan freezes a 14-day "
        "pre-change baseline and compares the 14 days after: runs, p95 runtime, failure "
        "rate, and measured credits/call (QUERY_ATTRIBUTION_HISTORY roll-up to the CALL). "
        "REGRESSED rows raise PERF_CHANGE_REGRESSION alerts automatically."
    )
    res = run(change_impact_sql.change_registry(90, company, database, schema_contains),
              page=_PAGE, key=f"chg_reg_{company}_{database}_{schema_contains}",
              tier="recent", source="OBJECT_CHANGE_REGISTRY")
    if res.ok and res.empty:
        st.info(
            "No procedure/task changes registered for this scope yet. The daily scan "
            "(TASK_CHANGE_IMPACT_SCAN) registers changes within a day of the ALTER / "
            "CREATE OR REPLACE, then tracks each one for 14 days."
        )
    elif guard(res, "", setup_hint="Run migration V010, then let the daily scan populate the registry."):
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
        show_cols = ["VERDICT", "OBJECT_TYPE", "DATABASE_NAME", "SCHEMA_NAME", "OBJECT_NAME",
                     "CHANGE_SEEN_AT", "CHANGED_BY", "BASELINE_CALLS", "AFTER_CALLS",
                     "BASELINE_P95_S", "AFTER_P95_S",
                     "BASELINE_CREDITS_PER_CALL", "AFTER_CREDITS_PER_CALL", "VERDICT_DETAIL"]
        sel_ci = selectable_table(df[[c for c in show_cols if c in df.columns]],
                                  key="chg_sel", height=320)
        result_caption(res)

        st.markdown("**Run history around one change**")
        picks = sorted({f"{t} {n}" for t, n in zip(df["OBJECT_TYPE"], df["OBJECT_NAME"], strict=True)})
        clicked_obj = None
        if sel_ci is not None:
            crow = df.iloc[int(sel_ci)]
            clicked_obj = f"{crow['OBJECT_TYPE']} {crow['OBJECT_NAME']}"
        pick = clicked_obj or st.selectbox("Object (or click a row above)", picks, key="chg_pick")
        if pick:
            otype, _, name = pick.partition(" ")
            hist = run(change_impact_sql.object_run_history(otype, name, 28),
                       page=_PAGE, key=f"chg_hist_{pick}", tier="recent",
                       source="ACCOUNT_USAGE.QUERY_HISTORY" if otype == "PROCEDURE"
                              else "ACCOUNT_USAGE.TASK_HISTORY")
            if guard(hist, "No runs recorded for this object in the last 28 days."):
                rule_at = None
                match = df[(df["OBJECT_TYPE"] == otype) & (df["OBJECT_NAME"] == name)]
                if not match.empty:
                    rule_at = match["CHANGE_SEEN_AT"].max()
                charts.daily_metric_line(hist.df, "DAY", "P95_S", "p95 runtime (s)", rule_date=rule_at)
                st.caption("Dashed line marks the registered change.")
                st.dataframe(hist.df, hide_index=True, use_container_width=True)
                result_caption(hist)

    if is_operator:
        if st.button("Run change-impact scan now", key="chg_scan_now",
                     help="Registers fresh changes and re-evaluates verdicts without waiting for the daily task."):
            ok, msg = execute_statement(change_impact_sql.run_scan_call(), page=_PAGE)
            notify(ok, msg)
    else:
        st.caption("The scan runs daily at 06:50; OVERWATCH_OPERATOR can also trigger it on demand.")


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    page_header("Operations", "Queries, tasks, warehouses, contention, releases, and pipeline SLAs.",
                scope_note=f"{f['company']} · last {f['days']} days")
    section = lazy_sections(
        ["Queries", "Tasks", "Warehouses", "Contention", "Release compare",
         "Change impact", "Pipeline SLA"], key="ops_section")
    if section == "Queries":
        _queries_tab(f["company"], f["days"], f["warehouse_contains"], f["user_contains"],
                     f["database"], f["schema_contains"])
    elif section == "Tasks":
        _tasks_tab(f["company"], f["days"], f["database"], f["schema_contains"])
    elif section == "Warehouses":
        _warehouses_tab(f["company"], rate)
    elif section == "Contention":
        _contention_tab(f["company"], f["days"])
    elif section == "Release compare":
        _release_compare_tab(f["company"])
    elif section == "Change impact":
        _change_impact_tab(f["company"], f["database"], f["schema_contains"], is_operator)
    else:
        _pipeline_sla_tab(is_operator)
