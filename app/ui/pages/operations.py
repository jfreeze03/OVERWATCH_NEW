"""Operations — queries, tasks, warehouses, contention, change impact, releases, pipeline SLAs."""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from app.config import OPERATOR_PROFILES, core_object, resolve_role_profile
from app.core.errors import safe_page
from app.core.identity import identity_sql
from app.core.query import execute_cancel_query, execute_statement, run, run_batch
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.core.state import filters
from app.data import change_impact_sql, insights_sql, mart27_sql, mart_sql, ops_sql, security_sql
from app.logic import remediation, wh_change
from app.logic.ai_prompts import release_compare_prompt, task_failure_prompt
from app.logic.anomaly import complete_days_only, flag_anomalies
from app.logic.formulas import account_today, credits_to_usd, safe_float
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
    run_mart_first,
    section_header,
    selectable_table,
    snowsight_profile_column,
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
    elif not wh_filter and not user_filter:
        # schema filter active: the schema-hour fact answers it (V027) —
        # the read that used to force a live QUERY_HISTORY scan.
        m = run(mart27_sql.schema_window_summary(days, company, database, schema_contains),
                page=_PAGE, key=f"q_schema_fact_{company}_{days}", tier="recent",
                source="FACT_QUERY_SCHEMA_HOURLY (mart — p95 is peak hourly)")
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
        activity = run(mart_sql.fact_daily_activity(14, company, database), page=_PAGE,
                       key="ops_spark_activity", tier="recent",
                       source="FACT_QUERY_HOURLY (daily)")
        q_spark = (activity.df["QUERIES"].tolist()
                   if activity.usable() and "QUERIES" in activity.df.columns else None)
        f_spark = (activity.df["FAILS"].tolist()
                   if activity.usable() and "FAILS" in activity.df.columns else None)
        kpi_row([
            {"label": f"Queries ({days}d)", "value": f"{qcount:,.0f}", "spark": q_spark},
            {"label": "Fail rate", "value": f"{(failed / qcount * 100) if qcount else 0:.2f}%",
             "delta": f"{failed:,.0f} failed", "delta_color": "off", "spark": f_spark,
             "severity": "warn" if failed else ""},
            {"label": "p95 runtime" + (" (peak hourly)" if used_mart else ""),
             "value": f"{safe_float(row.get('P95_ELAPSED_SEC')):,.1f}s",
             "help": "Highest hourly p95 from the fact table — a peak, not the "
                     "whole-window p95."
                     if used_mart else None},
            {"label": "Queued", "value": f"{safe_float(row.get('QUEUED_SEC')) / 60:,.0f} min"},
            {"label": "Remote spill", "value": f"{safe_float(row.get('SPILL_REMOTE_GB')):,.1f} GB"},
        ])
        result_caption(summary)

    # V041 R7: the UNFILTERED first paint reads MART_OPS_DIAG_HOURLY
    # (top-50/hour by elapsed + failure families; Operations led the fleet
    # pain board and this batch ran 30-37s live). An entity or schema filter
    # needs the true filtered top-N, which only the live scan has — that
    # path keeps the parallel batch below, unchanged.
    _use_diag = not (wh_filter or user_filter or database or schema_contains)
    if _use_diag:
        _qb = {
            "top": run_mart_first(
                mart27_sql.ops_diag_top_queries(days, company, 50),
                ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter,
                                               user_filter, database, schema_contains),
                page=_PAGE, key=f"q_top_{company}_{days}",
                mart_source="MART_OPS_DIAG_HOURLY (mart — union of hourly top-50s)",
                live_source="QUERY_HISTORY (live fallback)",
                mart_tier="recent", live_tier="recent", max_rows=50),
            "fails": run_mart_first(
                mart27_sql.ops_diag_failures(days, company),
                ops_sql.failures_by_error(days, company, database, schema_contains),
                page=_PAGE, key=f"q_fails_{company}_{days}",
                mart_source="MART_OPS_DIAG_HOURLY (mart — users = HLL approx-distinct)",
                live_source="QUERY_HISTORY (live fallback)",
                mart_tier="recent", live_tier="recent"),
        }
    else:
        # Parallel path (same contract as Security): both queries submit async
        # in one shot; any failure falls back to the serial per-query calls.
        _qb = run_batch([
            {"key": "top", "sql": ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter,
                                                                 user_filter, database, schema_contains),
             "source": "ACCOUNT_USAGE.QUERY_HISTORY", "max_rows": 50},
            {"key": "fails", "sql": ops_sql.failures_by_error(days, company, database, schema_contains),
             "source": "ACCOUNT_USAGE.QUERY_HISTORY"},
        ], page=_PAGE, tier="recent")

    st.markdown("**Heaviest queries**")
    top = _qb.get("top") or run(
        ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter, user_filter, database, schema_contains),
        page=_PAGE, key=f"q_top_{company}_{days}", tier="recent",
        source="ACCOUNT_USAGE.QUERY_HISTORY", max_rows=50)
    if guard(top, "No queries in this window/scope."):
        # r24: every QUERY_ID row links straight to its Snowsight profile
        # (owner ask — the drill's single link earned its keep).
        _tp, _tp_cfg = snowsight_profile_column(top.df, _PAGE)
        _tp_cols = ["START_TIME", "USER_NAME", "WAREHOUSE_NAME", "ELAPSED_SEC", "QUEUED_SEC",
                    "SPILL_REMOTE_GB", "EXECUTION_STATUS", "QUERY_PREVIEW"]
        if "PROFILE" in _tp.columns:
            _tp_cols.append("PROFILE")
        sel_q = selectable_table(
            _tp[_tp_cols],
            key="ops_top_sel",
            column_config={
                "START_TIME": st.column_config.DatetimeColumn("Started", format="MMM DD, HH:mm"),
                "ELAPSED_SEC": st.column_config.NumberColumn("Elapsed s", format="%.1f"),
                "QUEUED_SEC": st.column_config.NumberColumn("Queued s", format="%.1f"),
                "SPILL_REMOTE_GB": st.column_config.NumberColumn("Spill GB", format="%.2f"),
                **_tp_cfg,
            },
        )
        st.caption("Elapsed-time ranking.")

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
                    {"label": "Elapsed", "value": f"{safe_float(row.get('ELAPSED_SEC')):,.1f}s",
                     "delta": f"queued {safe_float(row.get('QUEUED_SEC')):,.1f}s", "delta_color": "off"},
                    {"label": "Scanned", "value": f"{safe_float(row.get('GB_SCANNED')):,.2f} GB",
                     "delta": f"{safe_float(row.get('CACHE_PCT')):,.0f}% cache", "delta_color": "off"},
                    {"label": "Partitions", "value": (f"{int(safe_float(row.get('PARTITIONS_SCANNED'))):,}"
                                                      f"/{int(safe_float(row.get('PARTITIONS_TOTAL'))):,}"),
                     "help": "Scanned vs total — a high ratio means the query reads almost the whole table."},
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
    fails = _qb.get("fails") or run(
        ops_sql.failures_by_error(days, company, database, schema_contains), page=_PAGE,
        key=f"q_fails_{company}_{days}", tier="recent",
        source="ACCOUNT_USAGE.QUERY_HISTORY")
    if guard(fails, "No failed queries in this window."):
        styled_table(fails.df)


def _failure_timeline_section(company: str, database: str = "", schema_contains: str = "",
                              known_failures: float | None = None) -> None:
    """Root-cause vs cascade view of recent task failures (ported)."""
    st.markdown("**Failure root-cause timeline (7d)**")
    if known_failures is not None and known_failures <= 0:
        st.success("No task failures in the last 7 days for this scope.")
        return
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
        release_day = st.date_input("Release date", value=account_today() - timedelta(days=1),
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
    """Metadata-driven table freshness SLAs (config in PIPELINE_SLA_CONFIG)."""
    res = run(insights_sql.pipeline_sla_status(), page=_PAGE, key="sla_status", tier="live",
              source="PIPELINE_SLA_STATUS")
    if not res.ok:
        st.info("Pipeline SLAs are not installed yet — an admin can verify on Admin → Migrations & freshness.")
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
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin profile.")

    st.markdown("**File-load failures (COPY / Snowpipe, 7d)**")
    cpf = run(ops_sql.copy_load_failures(7, "ALL"), page=_PAGE,
              key="copy_fails", tier="recent", source="ACCOUNT_USAGE.COPY_HISTORY")
    if cpf.ok and cpf.empty:
        st.success("No failed or partial file loads in the last 7 days.")
    elif guard(cpf, ""):
        styled_table(cpf.df, height=240)
        st.caption("The PIPE_COPY_FAILURES alert fires on these within the hour; "
                   "this table is the 7-day picture with sample errors.")
        result_caption(cpf)

    st.markdown("**Volume drops (yesterday vs prior-7d average)**")
    panel_help(
        "Rows added per table yesterday vs its prior-7-day average (tables moving "
        "≥1,000 rows/day). The PIPE_VOLUME_DROP alert fires past 50% (PROD databases only); this table also "
        "shows the 30-50% WATCH band so you see decay before it alerts."
    )
    vd = run(ops_sql.volume_deltas(), page=_PAGE, key="volume_deltas", tier="recent",
             source="ACCOUNT_USAGE.TABLE_DML_HISTORY")
    if vd.ok and vd.empty:
        st.success("Every moving table is within its normal daily volume.")
    elif guard(vd, "", setup_hint="Needs TABLE_DML_HISTORY (standard on current accounts)."):
        styled_table(vd.df, height=240)
        result_caption(vd)

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
                    styled_table(stale[show_cols])
            else:
                styled_table(sdf)


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
            _known_failed = total_failed
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
    # r19 #6: the summary window (>=7d, same scope) already counted failures
    # — when it says zero, skip the 7d TASK_HISTORY detail scan entirely.
    _failure_timeline_section(company, database, schema_contains,
                              known_failures=locals().get("_known_failed") if days >= 7 else None)

    st.markdown("**Task graph (DAG)**")
    if st.toggle("Render account task topology", key="ops_dag_toggle",
                 help="Latest task versions + predecessors; red = failed in 24h, gray = suspended."):
        gres = run(ops_sql.task_graph_nodes(), page=_PAGE, key="task_dag", tier="recent",
                   source="TASK_VERSIONS + TASK_HISTORY")
        if guard(gres, "No tasks recorded in TASK_VERSIONS."):
            import json as _json

            lines = ["digraph tasks {", 'rankdir=LR; node [shape=box, style="rounded,filled", fontsize=10];']
            for _, r in gres.df.iterrows():
                fqn = str(r["TASK_FQN"])
                short = fqn.split(".")[-1]
                fails = int(r.get("FAILURES_24H") or 0)
                state = str(r.get("STATE") or "").lower()
                color = "#fecaca" if fails else ("#e2e8f0" if "suspend" in state else "#bbf7d0")
                lines.append(f'"{fqn}" [label="{short}", fillcolor="{color}"];')
                preds_raw = r.get("PREDECESSORS")
                preds = []
                try:
                    parsed = _json.loads(preds_raw) if isinstance(preds_raw, str) else preds_raw
                    if isinstance(parsed, list):
                        preds = [str(p) for p in parsed]
                except (TypeError, ValueError):
                    if preds_raw:
                        preds = [p.strip().strip('"') for p in str(preds_raw).strip("[]").split(",") if p.strip()]
                lines.extend(f'"{p.upper()}" -> "{fqn}";' for p in preds)
            lines.append("}")
            st.graphviz_chart("\n".join(lines))
            st.caption("Green = healthy, red = failed in the last 24h, gray = suspended. "
                       "Edges point downstream.")
            result_caption(gres)


def _warehouses_tab(company: str, rate: float) -> None:
    res = run(mart_sql.fact_warehouse_daily(30, company), page=_PAGE, key=f"w_fact_{company}",
              tier="recent", source="FACT_WAREHOUSE_DAILY")
    if not guard(res, "No warehouse dailies yet — the hourly loader fills them.",
                 setup_hint="Live equivalent lives on Cost & Contract > Spend & Attribution."):
        return
    df = res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    # B4: score only complete days (today's partial row false-flags a steady wh);
    # the trend chart below keeps the full frame including today.
    flagged = flag_anomalies(complete_days_only(df), "USD", group_col="WAREHOUSE_NAME")
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
        styled_table(peaks.df)
        result_caption(peaks)


def _contention_tab(company: str, days: int) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**Warehouse queue & spill pressure**")
        # r23 #1: the hourly fact answers this without a QUERY_HISTORY scan
        # (the live read sat at 17.8s on the fleet board). r19 #18 still
        # holds — no one-member batch; mart-first with the labeled fallback.
        res = run_mart_first(
            mart_sql.fact_warehouse_pressure(days, company),
            ops_sql.warehouse_pressure(days, company),
            page=_PAGE, key=f"c_pressure_{company}_{days}",
            mart_source="FACT_QUERY_HOURLY (mart — p95 is peak hourly)",
            live_source="QUERY_HISTORY (live fallback)",
            mart_tier="recent", live_tier="recent")
        if guard(res, "No queueing or spill pressure in this window."):
            charts.bar_count(res.df.sort_values("QUEUED_SEC", ascending=False),
                             "WAREHOUSE_NAME", "QUEUED_SEC", title="Queued seconds")
            styled_table(res.df)
    with right:
        st.markdown("**Lock waits**")
        # V035: the live scan read 46-56 GB / 74-259s per view (Joe's own
        # Heaviest-queries panel, 2026-07-10) — mart-first, always.
        res = run_mart_first(
            mart27_sql.lock_wait_daily(min(days, 14), company),
            ops_sql.lock_contention(min(days, 14)),
            page=_PAGE, key=f"c_locks_{company}_{days}",
            mart_source=f"MART_LOCK_WAIT_DAILY ({company} + account-level)",
            live_source="ACCOUNT_USAGE.LOCK_WAIT_HISTORY (account-wide, pre-V035)",
            empty_is_answer=True)
        if guard(res, "No lock waits recorded (or the view is not accessible in this edition)."):
            styled_table(res.df)
            result_caption(res)



def _wh_change_block(company: str, is_operator: bool) -> None:
    st.divider()
    section_header("Warehouse setting changes", "info", "warehouse")
    st.caption(
        "Detection is snapshot-diff (daily SHOW WAREHOUSES — this account has no "
        "ACCOUNT_USAGE.WAREHOUSES view), so a change is seen within a day. Each change "
        "freezes a 14-day baseline and tracks 14 days after: $/day, p95, queueing, "
        "spill, failures. Confirmed regressions raise WH_CHANGE_REGRESSION alerts."
    )
    wh_contains = str(st.session_state.get("flt_warehouse_contains", "") or "")
    st.caption("CHANGE_SOURCE: MANAGED = made by a DEPLOY_ACTORS service user (Settings; empty until Flyway/Terraform land), MANUAL = a human, UNKNOWN = no matching ALTER found near the snapshot.")
    res = run(change_impact_sql.warehouse_change_registry(90, company, wh_contains),
              page=_PAGE, key=f"whchg_{company}_{wh_contains}", tier="recent",
              source="WAREHOUSE_CHANGE_REGISTRY")
    if res.ok and res.empty:
        st.info(
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
            "CHANGE_SEEN_AT", "BASELINE_CREDITS_PER_DAY", "AFTER_CREDITS_PER_DAY",
            "BASELINE_P95_S", "AFTER_P95_S", "VERDICT_DETAIL") if c in df.columns]],
            key="whchg_sel", height=260)
        result_caption(res)
        row = df.iloc[int(sel)] if sel is not None else df.iloc[0]
        deltas = wh_change.change_deltas(row.to_dict())
        st.markdown(f"**{row['WAREHOUSE_NAME']}** — {row['SETTING']} "
                    f"{row.get('OLD_VALUE', '?')} → {row.get('NEW_VALUE', '?')}")
        if deltas:
            kpi_row([{
                "label": d["metric"],
                "value": f"{d['base']:g} → {d['after']:g}",
                "delta": (f"{d['delta_pct']:+.1f}%" if d["delta_pct"] is not None else "new load"),
                "delta_color": ("inverse" if d["direction"] == "worse"
                                else "normal" if d["direction"] == "better" else "off"),
            } for d in deltas[:5]])
        else:
            st.caption("Before/after stats still accumulating for this change.")
        hist = run(change_impact_sql.warehouse_daily_series(str(row["WAREHOUSE_NAME"]), 28),
                   page=_PAGE, key=f"whchg_hist_{row['WAREHOUSE_NAME']}", tier="recent",
                   source="WAREHOUSE_METERING_HISTORY + QUERY_HISTORY")
        if guard(hist, "No activity recorded for this warehouse in the last 28 days."):
            charts.daily_metric_line(hist.df, "DAY", "CREDITS", title="credits/day",
                                     rule_date=row.get("CHANGE_SEEN_AT"))
            st.caption("Dashed line marks the detected change (seen within a day of the ALTER).")
    if is_operator:
        if st.button("Run warehouse scan now", key="whchg_scan_now",
                     help="Snapshots settings, registers diffs, and re-evaluates verdicts immediately."):
            ok, msg = execute_statement(change_impact_sql.run_wh_scan_call(), page=_PAGE)
            notify(ok, msg)
    else:
        st.caption("The warehouse scan runs daily at 06:40; admins can trigger it on demand.")


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
                styled_table(hist.df)
                result_caption(hist)

    if is_operator:
        if st.button("Run change-impact scan now", key="chg_scan_now",
                     help="Registers fresh changes and re-evaluates verdicts without waiting for the daily task."):
            ok, msg = execute_statement(change_impact_sql.run_scan_call(), page=_PAGE)
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
        st.code(stmt, language="sql")
        if "ALTER ACCOUNT" in stmt:
            st.warning("ACCOUNT-level: execute as SNOW_ACCOUNTADMINS. Copy the SQL if this "
                       "session's role lacks the privilege.")
        if is_operator:
            confirm = st.text_input("Type EMERGENCY to confirm execution", key="emg_confirm")
            if st.button("Execute + audit", key="emg_exec", disabled=(confirm != "EMERGENCY")):
                ok, msg = execute_statement(stmt, page=_PAGE)
                log_sql = (
                    f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                    "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE, EXECUTED_BY) "
                    f"SELECT 'EMERGENCY', {sql_literal(action)}, {sql_literal(stmt[:4000])}, "
                    f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}"
                )
                execute_statement(log_sql, page=_PAGE)
                notify(ok, msg)
        else:
            st.caption("Copy the SQL; executing from the app requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS.")


def _emergency_extras(is_operator: bool) -> None:
    st.divider()
    st.markdown("**Running queries (kill-switch)**")
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
            st.success("Nothing running or queued right now.")
        elif guard(rq, ""):
            _rqdf, _rq_cfg = snowsight_profile_column(rq.df, _PAGE)
            sel_rq = selectable_table(_rqdf, key="emg_rq_sel", height=240,
                                      column_config=_rq_cfg or None)
            if sel_rq is not None and is_operator:
                qrow = rq.df.iloc[int(sel_rq)]
                qid = str(qrow["QUERY_ID"])
                st.code(f"SELECT SYSTEM$CANCEL_QUERY('{qid}');", language="sql")
                confirm_q = st.text_input("Type CANCEL to confirm", key="emg_rq_confirm")
                if st.button("Cancel query + audit", key="emg_rq_exec",
                             disabled=(confirm_q != "CANCEL")):
                    ok, msg = execute_cancel_query(qid, page=_PAGE)   # B2: SELECT is outside the write allow-list
                    execute_statement(
                        f"INSERT INTO {core_object('REMEDIATION_LOG')} "
                        "(FINDING_TYPE, TARGET_OBJECT, STATEMENT_SQL, STATUS, RESULT_NOTE, EXECUTED_BY) "
                        f"SELECT 'CANCEL_QUERY', {sql_literal(qid)}, "
                        f"{sql_literal('SYSTEM$CANCEL_QUERY ' + qid)}, "
                        f"{sql_literal('EXECUTED' if ok else 'FAILED')}, {sql_literal(msg[:2000])}, {identity_sql()}",
                        page=_PAGE)
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
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    page_header("Operations", "Queries, tasks, warehouses, contention, change impact, releases, pipeline SLAs, and emergency levers.", icon_name="operations",
                scope_note=f"{f['company']} · last {f['days']} days")
    # Contention folded under Warehouses (CoCo): warehouse health and the
    # contention it causes read together.
    section = lazy_sections(
        ["Queries", "Tasks", "Warehouses", "Change impact",
         "Pipeline SLA", "Release compare", "Emergency"], key="ops_section")
    if section == "Queries":
        _queries_tab(f["company"], f["days"], f["warehouse_contains"], f["user_contains"],
                     f["database"], f["schema_contains"])
    elif section == "Tasks":
        _tasks_tab(f["company"], f["days"], f["database"], f["schema_contains"])
    elif section == "Warehouses":
        _warehouses_tab(f["company"], rate)
        st.divider()
        section_header("Contention (queue, spill & lock waits)", "info", "warehouse")
        _contention_tab(f["company"], f["days"])
    elif section == "Change impact":
        _change_impact_tab(f["company"], f["database"], f["schema_contains"], is_operator)
    elif section == "Pipeline SLA":
        _pipeline_sla_tab(is_operator)
    elif section == "Emergency":
        section_header("Emergency levers", "warn", "warehouse")
        _emergency_fragment(is_operator)
    else:
        _release_compare_tab(f["company"])
