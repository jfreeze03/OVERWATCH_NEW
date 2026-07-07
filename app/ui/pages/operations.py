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
from app.data import insights_sql, mart_sql, ops_sql
from app.logic.ai_prompts import release_compare_prompt, task_failure_prompt
from app.logic.anomaly import flag_anomalies
from app.logic.formulas import credits_to_usd, safe_float
from app.logic.insights import build_failure_timeline, compare_release_periods, task_release_deltas
from app.ui import charts
from app.ui.ai_panel import ai_evaluation_panel
from app.ui.components import guard, kpi_row, load_settings, page_header, result_caption, styled_table

_PAGE = "Operations"


def _queries_tab(company: str, days: int, wh_filter: str, user_filter: str,
                 database: str = "", schema_contains: str = "") -> None:
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
            {"label": "p95 runtime", "value": f"{safe_float(row.get('P95_ELAPSED_SEC')):,.1f}s"},
            {"label": "Queued", "value": f"{safe_float(row.get('QUEUED_SEC')) / 60:,.0f} min"},
            {"label": "Remote spill", "value": f"{safe_float(row.get('SPILL_REMOTE_GB')):,.1f} GB"},
        ])
        result_caption(summary)

    st.markdown("**Heaviest queries**")
    top = run(ops_sql.top_queries_by_elapsed(days, company, 50, wh_filter, user_filter, database, schema_contains),
              page=_PAGE, key=f"q_top_{company}_{days}", tier="recent",
              source="ACCOUNT_USAGE.QUERY_HISTORY", max_rows=50)
    if guard(top, "No queries in this window/scope."):
        styled_table(
            top.df[["USER_NAME", "WAREHOUSE_NAME", "ELAPSED_SEC", "QUEUED_SEC",
                     "SPILL_REMOTE_GB", "EXECUTION_STATUS", "QUERY_PREVIEW"]],
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
    if candidate_ids:
        picked = st.selectbox("Query ID (from the table above, heaviest first)",
                              candidate_ids, key="ops_drill_pick")
        manual = st.text_input("...or paste any query ID", key="ops_drill_manual")
        target = (manual or picked or "").strip()
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
            (st.success if ok else st.error)(msg)
        elif not is_operator:
            st.caption("Copy and run as OVERWATCH_OPERATOR - in-app execution needs the operator role.")


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


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    page_header("Operations", "Queries, tasks, warehouses, contention, releases, and pipeline SLAs.",
                scope_note=f"{f['company']} · last {f['days']} days")
    tab_q, tab_t, tab_w, tab_c, tab_r, tab_s = st.tabs(
        ["Queries", "Tasks", "Warehouses", "Contention", "Release compare", "Pipeline SLA"]
    )
    with tab_q:
        _queries_tab(f["company"], f["days"], f["warehouse_contains"], f["user_contains"],
                     f["database"], f["schema_contains"])
    with tab_t:
        _tasks_tab(f["company"], f["days"], f["database"], f["schema_contains"])
    with tab_w:
        _warehouses_tab(f["company"], rate)
    with tab_c:
        _contention_tab(f["company"], f["days"])
    with tab_r:
        _release_compare_tab(f["company"])
    with tab_s:
        _pipeline_sla_tab(is_operator)
