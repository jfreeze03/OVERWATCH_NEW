"""Cost & Contract — Unit costs: the price tag on one query, one CALL, one
AI request.

Measurement honesty (the panel's whole point):
- Query and procedure dollars are MEASURED — QUERY_ATTRIBUTION_HISTORY
  credits (child statements roll up to the CALL), ~8h lag. Attribution
  excludes warehouse idle time, so these numbers answer "what did running
  THIS cost" — the Optimization section's expensive-queries view keeps the
  ALLOCATED lens (incl. idle share) for "who owns the bill".
- AI dollars are billed Cortex credits at the AI rate; the function/model
  table adds $/1M tokens so model choices carry a price.
"""

from __future__ import annotations

import streamlit as st

from app.core.query import run, run_batch
from app.data import cortex_sql, etl_sql, graph_sql, insights_sql, mart27_sql
from app.logic import graphs
from app.logic.call_tree import build_call_tree
from app.logic.directory import resolve_display
from app.logic.formulas import credits_to_usd, format_usd, md_dollars, safe_float
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    panel_help,
    result_caption,
    run_mart_first,
    snowsight_profile_column,
    styled_table,
    user_display_map,
    with_user_names,
)

_PAGE = "Cost & Contract"

# P5 (audit 2026-07-31): a unit cost is a PRICE, not a trend. Scanning
# QUERY_ATTRIBUTION_HISTORY over the full page window (up to 365d) took ~48s to
# answer "what does one query cost" — and the answer barely moves past a month,
# because the metric is already per-query / per-call. So this panel clamps its
# own measured reads independently of the page filter. This TIGHTENS below the
# account-wide MAX_LIVE_WINDOW_DAYS guardrail; it never loosens it. Anyone who
# genuinely wants the long window opts back in with the toggle below.
_UNIT_COST_MAX_DAYS = 30


def _unit_costs_tab(f: dict, rate: float, ai_rate: float) -> None:
    company, days = f["company"], f["days"]
    database, schema_contains = f["database"], f["schema_contains"]
    st.caption(
        "Measured price tags: QUERY_ATTRIBUTION_HISTORY credits (~8h lag, idle time "
        "excluded) at your contract rate. For 'who owns the bill' including idle, "
        "use Optimization's allocated view; for pipelines, the task-graph panel below."
    )
    panel_help(
        "The price tag on one query, one procedure CALL, one AI request — MEASURED from "
        "QUERY_ATTRIBUTION_HISTORY credits (~8h lag, warehouse idle excluded) at your contract "
        "rate. A high $/call or $/run is your tuning target; for 'who owns the bill' including "
        "idle, use Optimization's allocated view instead."
    )
    _window_label = str(f.get("window_label") or f"Last {days} days")
    _uc_full = st.toggle(
        f"Price over the full {_window_label.lower()}",
        key=f"uc_full_window_{company}_{days}",
        help="Off by default. The measured query/procedure reads are capped at "
             f"{_UNIT_COST_MAX_DAYS} days because a long QUERY_ATTRIBUTION_HISTORY scan "
             "costs tens of seconds and barely changes a per-query price. Turn it on to "
             "scan the whole page window anyway.")
    uc_days = days if (_uc_full or int(days) <= _UNIT_COST_MAX_DAYS) else _UNIT_COST_MAX_DAYS
    if int(uc_days) != int(days):
        st.caption(f"Page window is {_window_label.lower()}, but unit prices use "
                   f"<={_UNIT_COST_MAX_DAYS}d "
                   f"(scanning {uc_days}d) — a per-query price is stable, and the long scan "
                   "is the slowest read on this page. The AI, pattern, ETL and task-graph "
                   "panels below still follow the page window.")

    # AI fact-first BEFORE the batch (r18 #3): read FACT_AI_USAGE_DAILY and
    # pay the live Cortex scan only when the fact can't answer — the old
    # order paid the live scan in every batch, then usually threw it away.
    _ai_m = run(mart27_sql.ai_costs_by_model(days), page=_PAGE, key=f"unit_ai_mart_{days}",
                tier="recent", source="FACT_AI_USAGE_DAILY (mart, loaded daily — Code + Functions)")

    # ---- independent historical reads -> one parallel batch (Codex #15).
    # All share the same filter inputs so the batch cache stays coherent;
    # any failure falls back to the serial cached path.
    # P5: the two measured-attribution reads run on uc_days (<=30d unless the
    # viewer opted in), NOT the page window — they are the ~48s stall. The AI leg
    # keeps the page window: it is fact-first and cheap, and "AI spend (window)"
    # is a total, not a unit price, so shortening it would change its meaning.
    _jobs = [
        {"key": "q", "sql": insights_sql.measured_query_costs(
            uc_days, company, database, schema_contains,
            f["warehouse_contains"], f["user_contains"], 50),
         "source": f"QUERY_ATTRIBUTION_HISTORY + QUERY_HISTORY ({uc_days}d)", "max_rows": 50},
        {"key": "p", "sql": insights_sql.procedure_costs_usd(uc_days, company, database, schema_contains, 50),
         "source": f"QUERY_ATTRIBUTION_HISTORY (rolled up to CALL, {uc_days}d)", "max_rows": 50},
    ]
    if not _ai_m.usable():
        _jobs.append({"key": "ai", "sql": cortex_sql.cortex_model_costs(days),
                      "source": "CORTEX_FUNCTIONS_USAGE_HISTORY", "max_rows": 200})
    _ub = run_batch(_jobs, page=_PAGE, tier="historical")
    if _ub is not None:
        q_res, p_res = _ub["q"], _ub["p"]
        ai_res = _ai_m if _ai_m.usable() else _ub["ai"]
    else:
        # uc_days is in both cache keys: flipping the toggle must re-read, not
        # serve the other window's cached frame under the same key.
        q_res = run(insights_sql.measured_query_costs(
                        uc_days, company, database, schema_contains,
                        f["warehouse_contains"], f["user_contains"], 50),
                    page=_PAGE, key=f"unit_q_{company}_{uc_days}_{database}", tier="historical",
                    source=f"QUERY_ATTRIBUTION_HISTORY + QUERY_HISTORY ({uc_days}d)")
        p_res = run(insights_sql.procedure_costs_usd(uc_days, company, database, schema_contains, 50),
                    page=_PAGE, key=f"unit_p_{company}_{uc_days}_{database}", tier="historical",
                    source=f"QUERY_ATTRIBUTION_HISTORY (rolled up to CALL, {uc_days}d)")
        ai_res = _ai_m if _ai_m.usable() else run(
            cortex_sql.cortex_model_costs(days), page=_PAGE,
            key=f"unit_ai_{days}", tier="historical",
            source="CORTEX_FUNCTIONS_USAGE_HISTORY")

    kpis = []
    if q_res.usable():
        top_q = q_res.df.iloc[0]
        kpis.append({"label": "Priciest single query",
                     "value": format_usd(credits_to_usd(safe_float(top_q.get("CREDITS")), rate)),
                     "delta": f"{resolve_display(top_q.get('USER_NAME'), user_display_map(_PAGE))} "
                              f"· {top_q.get('WAREHOUSE_NAME')}",
                     "delta_color": "off"})
    if p_res.usable():
        top_p = p_res.df.iloc[0]
        kpis.append({"label": "Priciest procedure (per call)",
                     "value": format_usd(credits_to_usd(safe_float(top_p.get("CREDITS_PER_CALL")), rate, round_cents=False)),
                     "delta": str(top_p.get("PROC_NAME")), "delta_color": "off"})
    if ai_res.usable():
        ai_credits = float(ai_res.df["CREDITS"].map(safe_float).sum())
        kpis.append({"label": "AI spend (window)",
                     "value": format_usd(credits_to_usd(ai_credits, ai_rate)),
                     "delta": f"{len(ai_res.df)} source/model pair(s)",
                     "delta_color": "off"})
    if kpis:
        kpi_row(kpis)

    st.markdown("**Most expensive queries — measured $ each**")
    if guard(q_res, "No attributed query credits in this scope/window (attribution lags ~8h)."):
        qdf = q_res.df.copy()
        qdf["USD"] = qdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
        qdf = with_user_names(qdf, _PAGE)
        qdf, _q_cfg = snowsight_profile_column(qdf, _PAGE)
        styled_table(qdf, height=280, column_config={
            "USD": st.column_config.NumberColumn("$", format="$%.4f"),
            **_q_cfg,
        })
        result_caption(q_res, note="Idle-time excluded by design — that burn lives with "
                                   "the idle advisor, not the query that happened to run.")

    st.divider()
    st.markdown("**Stored procedures — $/call leaderboard**")
    if guard(p_res, "No CALLs with attributed credits in this scope/window."):
        pdf = p_res.df.copy()
        # Click a row -> the trend panel below prefills with that proc
        # (Codex r7 #5: the trend was findable only by typing).
        pdf["USD_TOTAL"] = pdf["TOTAL_CREDITS"].map(lambda c: credits_to_usd(c, rate))
        pdf["USD_PER_CALL"] = pdf["CREDITS_PER_CALL"].map(lambda c: credits_to_usd(c, rate, round_cents=False))
        from app.ui.components import selectable_table
        _psel = selectable_table(pdf, key="uc_proc_sel", height=280, column_config={
            "USD_TOTAL": st.column_config.NumberColumn("$ (window)", format="$%.2f"),
            "USD_PER_CALL": st.column_config.NumberColumn("$/call", format="$%.4f"),
            "FAIL_PCT": st.column_config.NumberColumn("Fail %", format="%.1f%%"),
        })
        if _psel is not None:
            st.session_state["uc_proc_trend_name"] = str(pdf.iloc[int(_psel)]["PROC_NAME"])
            st.caption(f"Selected **{st.session_state['uc_proc_trend_name']}** — "
                       "the trend panel below is prefilled.")
        result_caption(p_res, note="Database/schema = the CALL's session context; procs may "
                                   "read other databases. ATTRIBUTED_CALLS = calls the "
                                   "attribution view matched; $0 with calls = attribution "
                                   "lag (~8h) or children ran without a warehouse. "
                                   "Change-impact (Operations) watches these numbers around "
                                   "each ALTER.")

    st.markdown("**Repeated patterns — the silent spend (measured $)**")
    # Owner ask (2026-07-11): "a visual of bad code and how it could cost us
    # silently." Unlike the POC's estimates these are MEASURED attribution
    # credits per parameterized hash — one cheap query run thousands of
    # times shows its real bill.
    _pc = run(mart27_sql.pattern_cost(days, company, 25), page=_PAGE,
              key=f"patterns_{company}_{days}", tier="recent",
              source=f"MART_PATTERN_COST_DAILY ({company} + account-level)", probe=True)
    if _pc.ok and not _pc.empty:
        _pd_df = _pc.df.copy()
        _pd_df["USD"] = _pd_df["CREDITS"].map(safe_float) * rate
        _pd_df["USD_PER_RUN"] = _pd_df["CREDITS_PER_RUN"].map(safe_float) * rate
        styled_table(_pd_df[["SAMPLE_TEXT", "RUNS", "USD", "USD_PER_RUN", "USERS"]],
                     height=260)
        st.caption("Measured QUERY_ATTRIBUTION_HISTORY compute, grouped by "
                   "parameterized hash — cheap-but-constant often out-bills "
                   "expensive-but-rare.")
    elif _pc.ok:
        st.success("No repeated pattern crossed the $0.01 floor in this window.")
    else:
        st.info("Pattern costs arrive with migration V037 (MART_PATTERN_COST_DAILY v2) — "
                "an admin can apply the pending schema update on Admin → Migrations & freshness.")

    # $-escape: expander LABELS render markdown+LaTeX too — "total $ and $/call" paired
    with st.expander(md_dollars("Trend one procedure — total $ and $/call over time")):
        st.caption(
            "Type a procedure name (bare or db.schema-qualified — paste PROC_NAME "
            "from the leaderboard above). Same measured rollup as the leaderboard "
            "(children via ROOT_QUERY_ID), sliced by day; honors the page filters "
            f"and the same {uc_days}-day scan window. Attribution lags ~8h; idle "
            "time excluded."
        )
        _pname = st.text_input("Procedure name", key="uc_proc_trend_name")
        if _pname.strip():
            # P5: same measured lens, same window cap as the leaderboard above —
            # a 365d scan here is the same stall, and the KPI below would
            # otherwise label a different window than the table it drills into.
            tres = run(insights_sql.proc_cost_trend(
                           _pname.strip(), uc_days, company, database, schema_contains),
                       page=_PAGE, key=f"proc_trend_{_pname.strip()[:30]}_{company}_{uc_days}",
                       tier="historical",
                       source=f"QUERY_ATTRIBUTION_HISTORY rolled to CALLs, day grain ({uc_days}d)")
            if guard(tres, "No CALLs matched that name in this window/scope — "
                           "check spelling (bare names match any db.schema)."):
                tdf = tres.df.copy()
                tdf["USD"] = tdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
                _tot = float(tdf["USD"].sum())
                _calls = int(tdf["CALLS"].sum())
                kpi_row([
                    {"label": f"Total, {uc_days}d", "value": format_usd(_tot)},
                    {"label": "Calls", "value": f"{_calls:,}"},
                    {"label": "Avg $/call",
                     "value": format_usd(_tot / _calls) if _calls else "n/a"},
                ])
                charts.spend_trend(tdf, day_col="DAY", usd_col="USD")
                styled_table(tdf, height=200, column_config={
                    "USD": st.column_config.NumberColumn("$", format="$%.4f"),
                    "CREDITS_PER_CALL": st.column_config.NumberColumn("cr/call", format="%.6f"),
                })
                st.caption("$0 days with calls = attribution not caught up (~8h) or "
                           "children ran without a warehouse — same caveats as the leaderboard.")

    with st.expander("Price a specific CALL or session (measured)"):
        st.caption(
            "Paste a CALL's QUERY_ID for that one proc run, or a SESSION_ID to price "
            "every proc the session ran (e.g. your three ad-hoc CALLs). Children roll "
            "up via QUERY_ATTRIBUTION_HISTORY.ROOT_QUERY_ID — no task graph id needed. "
            "Attribution lags ~8h; idle time excluded."
        )
        _ident = st.text_input("QUERY_ID or SESSION_ID", key="uc_call_ident")
        if _ident.strip():
            cres = run(insights_sql.call_cost_lookup(_ident.strip()), page=_PAGE,
                       key=f"call_cost_{_ident.strip()[:24]}", tier="historical",
                       source="QUERY_ATTRIBUTION_HISTORY (ROOT_QUERY_ID rollup, 7d)")
            if guard(cres, "No CALLs matched in the last 7 days — check the id; "
                           "attribution lags ~8h, so very recent runs may not price yet."):
                cdf = cres.df.copy()
                cdf["USD"] = cdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
                cdf, _c_cfg = snowsight_profile_column(cdf, _PAGE)
                styled_table(cdf, height=170, column_config={
                    "USD": st.column_config.NumberColumn("$", format="$%.4f"),
                    **_c_cfg,
                })
                st.caption(f"{len(cdf)} CALL(s) — one row per proc run.")
                if len(cdf) == 1:
                    _cid = str(cdf.iloc[0]["QUERY_ID"])
                    kids = run(insights_sql.call_children_costs(_cid), page=_PAGE,
                               key=f"call_kids_{_cid[:24]}", tier="historical",
                               source="attribution children of this CALL")
                    if kids.usable():
                        kdf = kids.df.copy()
                        kdf["USD"] = kdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
                        kdf = build_call_tree(kdf, _cid)
                        kdf, _tree_cfg = snowsight_profile_column(kdf, _PAGE)
                        st.markdown("**Where the money went inside this CALL**")
                        _tree_columns = [
                            "STEP", "QUERY_ID", "PROFILE", "STEP_PREVIEW", "STEP_START",
                            "ELAPSED_SEC", "CREDITS", "USD",
                        ]
                        styled_table(kdf[[c for c in _tree_columns if c in kdf.columns]],
                                     height=300, column_config={
                            "USD": st.column_config.NumberColumn("$", format="$%.4f"),
                            **_tree_cfg,
                        })
                        st.caption(
                            "Parent-before-child execution order. Indentation follows "
                            "PARENT_QUERY_ID; orphaned history stays visible at the top level."
                        )

    st.divider()
    st.markdown("**AI — $ by function/model (or Cortex Code source)**")
    if not ai_res.usable():
        # This account bills AI through Cortex CODE (Snowsight/CLI token
        # credits), not SQL Cortex functions — fall back to those views
        # (live finding 2026-07-08: model view empty, code credits real).
        ai_res = run(cortex_sql.cortex_source_costs(days), page=_PAGE,
                     key=f"unit_ai_src_{days}", tier="historical",
                     source="CORTEX_CODE_*_USAGE_HISTORY (source grain)")
    if not ai_res.ok:
        st.caption("Neither CORTEX_FUNCTIONS_USAGE_HISTORY nor the Cortex Code usage views "
                   "are accessible on this account/role — per-user AI spend remains "
                   "available under Chargeback & AI.")
    elif ai_res.empty:
        st.caption("No Cortex usage recorded in this window (functions or code).")
    else:
        adf = ai_res.df.copy()
        adf["USD"] = adf["CREDITS"].map(lambda c: credits_to_usd(c, ai_rate))
        adf["USD_PER_1M_TOKENS"] = adf["CREDITS_PER_1M_TOKENS"].map(
            lambda c: credits_to_usd(c, ai_rate))
        styled_table(adf, height=240, column_config={
            "USD": st.column_config.NumberColumn("$", format="$%.2f"),
            "USD_PER_1M_TOKENS": st.column_config.NumberColumn("$/1M tokens", format="$%.2f"),
        })
        result_caption(ai_res, note=f"Billed Cortex credits at ${ai_rate}/credit. Account-wide "
                                    "(the usage view carries no database dimension); per-user "
                                    "attribution lives under Chargeback & AI.")

    st.divider()
    st.markdown("**ETL unit costs (tagged pipelines)**")
    # $-escape: four $/unit tokens in one caption pair into LaTeX math spans
    st.caption(md_dollars(
        "Cost governance for pipelines that set a JSON QUERY_TAG "
        "(pipeline / run_id / target_object / environment / cost_center — see "
        "docs/design/ETL_COST_TAGS.md). MEASURED attribution credits per pipeline: "
        "$/run, $/M rows written, $/TiB scanned, and failed-run waste. Untagged queries "
        "fall out — coverage says how much; $/run divides only run_id-tagged spend."
    ))
    if st.toggle("Run ETL unit-cost scan", key="etl_unit_toggle",
                 help="Scans the window's QUERY_HISTORY for JSON pipeline tags joined to "
                      "measured attribution credits. Off by default (keeps first paint fast)."):
        cov = run(etl_sql.etl_tag_coverage(days, company, f["database"], f["schema_contains"]), page=_PAGE,
                  key=f"etl_cov_{company}_{days}", tier="historical",
                  source="QUERY_HISTORY + QUERY_ATTRIBUTION_HISTORY (tag coverage)")
        if cov.ok and not cov.empty:
            c0 = cov.df.iloc[0]
            kpi_row([
                {"label": "Tagged credit coverage",
                 "value": f"{safe_float(c0.get('TAGGED_CREDIT_PCT')):.0f}%",
                 "help": "Share of MEASURED compute credits carrying a pipeline tag. "
                         "Low coverage means most spend can't be tied to a pipeline yet."},
                {"label": "Untagged credits ($)",
                 "value": format_usd(credits_to_usd(safe_float(c0.get("UNTAGGED_CREDITS")), rate)),
                 "delta_color": "off",
                 "help": "Measured compute with no pipeline tag, at the configured rate."},
            ])
        etl = run(etl_sql.etl_cost_by_pipeline(days, company, f["database"], f["schema_contains"]), page=_PAGE,
                  key=f"etl_pipe_{company}_{days}", tier="historical",
                  source="QUERY_HISTORY + QUERY_ATTRIBUTION_HISTORY (per pipeline)")
        if guard(etl, "No tagged pipeline runs with attributed credits in this window — "
                      "adopt the JSON QUERY_TAG (docs/design/ETL_COST_TAGS.md)."):
            edf = etl.df.copy()
            edf["USD"] = edf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
            edf["USD_PER_RUN"] = edf["CREDITS_PER_RUN"].map(lambda c: credits_to_usd(c, rate, round_cents=False))
            edf["USD_PER_M_ROWS"] = edf["CREDITS_PER_M_ROWS"].map(lambda c: credits_to_usd(c, rate, round_cents=False))
            edf["USD_PER_TIB"] = edf["CREDITS_PER_TIB"].map(lambda c: credits_to_usd(c, rate, round_cents=False))
            edf["WASTE_USD"] = edf["RETRY_WASTE_CREDITS"].map(lambda c: credits_to_usd(c, rate))
            styled_table(
                edf[["PIPELINE", "RUNS", "USD", "USD_PER_RUN", "RUN_ID_CREDIT_PCT",
                     "USD_PER_M_ROWS", "USD_PER_TIB", "WASTE_USD"]],
                height=300, column_config={
                    "USD": st.column_config.NumberColumn("$", format="$%.2f"),
                    "USD_PER_RUN": st.column_config.NumberColumn("$/run", format="$%.4f"),
                    "RUN_ID_CREDIT_PCT": st.column_config.NumberColumn("run_id %", format="%.0f%%",
                                                                        help="Share of this pipeline's credits carrying a run_id — $/run divides only that share."),
                    "USD_PER_M_ROWS": st.column_config.NumberColumn("$/M rows written", format="$%.4f"),
                    "USD_PER_TIB": st.column_config.NumberColumn("$/TiB", format="$%.2f"),
                    "WASTE_USD": st.column_config.NumberColumn("Failed-run $", format="$%.2f"),
                })
            result_caption(etl, note="Failed-run $ = attributed credits on non-SUCCESS statements, "
                                     "run grain (retry/abort waste). Rows = write statements only. "
                                     "Method = MEASURED (Admin → Metrics).")

    st.divider()
    st.markdown("**Task-graph pipeline costs**")
    # Moved from Operations > Task graphs ($) (v4.50): $/run per pipeline is
    # unit-cost material on the same QUERY_ATTRIBUTION_HISTORY basis as the
    # ETL panel above. Operational task health (failures, RCA, runtimes)
    # stays on Operations > Tasks.
    _graphs_tab(f["company"], f["days"], rate, f["database"], f["schema_contains"])


def _graphs_tab(company: str, days: int, rate: float, database: str = "",
                schema_contains: str = "") -> None:
    st.caption(
        "Cost per pipeline = measured warehouse credits for every task run in the "
        "graph (QUERY_ATTRIBUTION_HISTORY roll-up, ~8h lag) at the configured rate. "
        "$/run is a day's cost over that day's runs — allocated, not per-run metered. "
        "Serverless tasks bill separately and are listed below at task-day grain."
    )
    res = run_mart_first(
        mart27_sql.task_graphs(days, company, database, schema_contains),
        graph_sql.graph_daily_costs(days, company, database, schema_contains),
        page=_PAGE, key=f"graph_costs_{company}_{days}_{database}_{schema_contains}",
        mart_source="MART_TASK_GRAPH_DAILY (mart, loaded hourly)",
        live_source="TASK_HISTORY + QUERY_ATTRIBUTION_HISTORY (live fallback)")
    if not guard(res, "No task-graph runs in this scope/window."):
        return
    daily = graphs.enrich_graph_daily(res.df, rate)
    summary = graphs.pipeline_summary(daily)
    top = summary.iloc[0] if not summary.empty else None
    worst = summary.sort_values("SUCCESS_PCT").iloc[0] if not summary.empty else None
    kpi_row([
        {"label": "Pipeline spend (window)",
         "value": format_usd(float(summary["USD"].sum()) if not summary.empty else 0.0),
         "delta": f"{len(summary)} pipeline(s)", "delta_color": "off"},
        {"label": "Most expensive",
         "value": str(top["PIPELINE"]) if top is not None else "n/a",
         "delta": format_usd(float(top["USD"])) if top is not None else None,
         "delta_color": "off"},
        {"label": "Worst success",
         "value": f"{float(worst['SUCCESS_PCT']):.1f}%" if worst is not None else "n/a",
         "delta": str(worst["PIPELINE"]) if worst is not None else None,
         "delta_color": "inverse" if worst is not None and float(worst["SUCCESS_PCT"]) < 100 else "off"},
    ])
    top5 = summary.head(5)["PIPELINE"].tolist() if not summary.empty else []
    chart_df = daily[daily["PIPELINE"].isin(top5)][["DAY", "PIPELINE", "USD"]]
    if not chart_df.empty:
        charts.daily_stacked_usd(chart_df, "DAY", "PIPELINE", "USD")
    styled_table(summary, height=300, column_config={
        "USD": st.column_config.NumberColumn("$ (window)", format="$%.2f"),
        "USD_PER_RUN": st.column_config.NumberColumn("$/run (alloc)", format="$%.4f"),
        "SUCCESS_PCT": st.column_config.NumberColumn("Success %", format="%.1f%%"),
    })
    with st.expander("Daily detail (per pipeline per day)"):
        styled_table(daily.sort_values(["DAY", "USD"], ascending=[False, False]), height=280)
    result_caption(res, note="TREND compares $/run between window halves (±10% = FLAT). "
                             "Pipeline label = the graph's root task.")

    sls = run(graph_sql.serverless_task_daily(days, company, database, schema_contains),
              page=_PAGE, key=f"sls_costs_{company}_{days}_{database}_{schema_contains}",
              tier="historical", source="SERVERLESS_TASK_HISTORY")
    st.markdown("**Serverless tasks (billed separately, task-day grain)**")
    if not sls.ok:
        st.caption("SERVERLESS_TASK_HISTORY is not accessible on this account/role.")
    elif sls.empty:
        st.caption("No serverless task credits in this scope/window.")
    else:
        sdf = sls.df.copy()
        sdf["USD"] = sdf["SERVERLESS_CREDITS"].map(lambda c: credits_to_usd(c, rate))
        styled_table(sdf, height=220, column_config={
            "USD": st.column_config.NumberColumn("$", format="$%.2f")})
