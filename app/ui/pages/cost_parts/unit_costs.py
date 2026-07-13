"""Cost & Contract — Unit costs: the price tag on one query, one CALL, one
AI request.

Measurement honesty (the panel's whole point):
- Query and procedure dollars are MEASURED — QUERY_ATTRIBUTION_HISTORY
  credits (child statements roll up to the CALL), ~6h lag. Attribution
  excludes warehouse idle time, so these numbers answer "what did running
  THIS cost" — the Optimization section's expensive-queries view keeps the
  ALLOCATED lens (incl. idle share) for "who owns the bill".
- AI dollars are billed Cortex credits at the AI rate; the function/model
  table adds $/1M tokens so model choices carry a price.
"""

from __future__ import annotations

import streamlit as st

from app.core.query import run, run_batch
from app.data import cortex_sql, insights_sql, mart27_sql
from app.logic.formulas import credits_to_usd, format_usd, safe_float
from app.ui import charts
from app.ui.components import (
    guard,
    kpi_row,
    result_caption,
    snowsight_profile_column,
    styled_table,
)

_PAGE = "Cost & Contract"


def _unit_costs_tab(f: dict, rate: float, ai_rate: float) -> None:
    company, days = f["company"], f["days"]
    database, schema_contains = f["database"], f["schema_contains"]
    st.caption(
        "Measured price tags: QUERY_ATTRIBUTION_HISTORY credits (~6h lag, idle time "
        "excluded) at your contract rate. For 'who owns the bill' including idle, "
        "use Optimization's allocated view; for pipelines, Operations → Task graphs ($)."
    )

    # AI fact-first BEFORE the batch (r18 #3): read FACT_AI_USAGE_DAILY and
    # pay the live Cortex scan only when the fact can't answer — the old
    # order paid the live scan in every batch, then usually threw it away.
    _ai_m = run(mart27_sql.ai_costs_by_model(days), page=_PAGE, key=f"unit_ai_mart_{days}",
                tier="recent", source="FACT_AI_USAGE_DAILY (mart, loaded daily — Code + Functions)")

    # ---- independent historical reads -> one parallel batch (Codex #15).
    # All share the same filter inputs so the batch cache stays coherent;
    # any failure falls back to the serial cached path.
    _jobs = [
        {"key": "q", "sql": insights_sql.measured_query_costs(
            days, company, database, schema_contains,
            f["warehouse_contains"], f["user_contains"], 50),
         "source": "QUERY_ATTRIBUTION_HISTORY + QUERY_HISTORY", "max_rows": 50},
        {"key": "p", "sql": insights_sql.procedure_costs_usd(days, company, database, schema_contains, 50),
         "source": "QUERY_ATTRIBUTION_HISTORY (rolled up to CALL)", "max_rows": 50},
    ]
    if not _ai_m.usable():
        _jobs.append({"key": "ai", "sql": cortex_sql.cortex_model_costs(days),
                      "source": "CORTEX_FUNCTIONS_USAGE_HISTORY", "max_rows": 200})
    _ub = run_batch(_jobs, page=_PAGE, tier="historical")
    if _ub is not None:
        q_res, p_res = _ub["q"], _ub["p"]
        ai_res = _ai_m if _ai_m.usable() else _ub["ai"]
    else:
        q_res = run(insights_sql.measured_query_costs(
                        days, company, database, schema_contains,
                        f["warehouse_contains"], f["user_contains"], 50),
                    page=_PAGE, key=f"unit_q_{company}_{days}_{database}", tier="historical",
                    source="QUERY_ATTRIBUTION_HISTORY + QUERY_HISTORY")
        p_res = run(insights_sql.procedure_costs_usd(days, company, database, schema_contains, 50),
                    page=_PAGE, key=f"unit_p_{company}_{days}_{database}", tier="historical",
                    source="QUERY_ATTRIBUTION_HISTORY (rolled up to CALL)")
        ai_res = _ai_m if _ai_m.usable() else run(
            cortex_sql.cortex_model_costs(days), page=_PAGE,
            key=f"unit_ai_{days}", tier="historical",
            source="CORTEX_FUNCTIONS_USAGE_HISTORY")

    kpis = []
    if q_res.usable():
        top_q = q_res.df.iloc[0]
        kpis.append({"label": "Priciest single query",
                     "value": format_usd(credits_to_usd(safe_float(top_q.get("CREDITS")), rate)),
                     "delta": f"{top_q.get('USER_NAME')} · {top_q.get('WAREHOUSE_NAME')}",
                     "delta_color": "off"})
    if p_res.usable():
        top_p = p_res.df.iloc[0]
        kpis.append({"label": "Priciest procedure (per call)",
                     "value": format_usd(credits_to_usd(safe_float(top_p.get("CREDITS_PER_CALL")), rate)),
                     "delta": str(top_p.get("PROC_NAME")), "delta_color": "off"})
    if ai_res.usable():
        ai_credits = float(ai_res.df["CREDITS"].map(safe_float).sum())
        kpis.append({"label": "AI spend (window)",
                     "value": format_usd(credits_to_usd(ai_credits, ai_rate)),
                     "delta": f"{len(ai_res.df)} function/model pair(s)",
                     "delta_color": "off"})
    if kpis:
        kpi_row(kpis)

    st.markdown("**Most expensive queries — measured $ each**")
    if guard(q_res, "No attributed query credits in this scope/window (attribution lags ~6h)."):
        qdf = q_res.df.copy()
        qdf["USD"] = qdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
        styled_table(qdf, height=280, column_config={
            "USD": st.column_config.NumberColumn("$", format="$%.4f"),
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
        pdf["USD_PER_CALL"] = pdf["CREDITS_PER_CALL"].map(lambda c: credits_to_usd(c, rate))
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
                                   "lag (~6h) or children ran without a warehouse. "
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

    with st.expander("Trend one procedure — total $ and $/call over time"):
        st.caption(
            "Type a procedure name (bare or db.schema-qualified — paste PROC_NAME "
            "from the leaderboard above). Same measured rollup as the leaderboard "
            "(children via ROOT_QUERY_ID), sliced by day; honors the page filters. "
            "Attribution lags ~6h; idle time excluded."
        )
        _pname = st.text_input("Procedure name", key="uc_proc_trend_name")
        if _pname.strip():
            tres = run(insights_sql.proc_cost_trend(
                           _pname.strip(), days, company, database, schema_contains),
                       page=_PAGE, key=f"proc_trend_{_pname.strip()[:30]}_{company}_{days}",
                       tier="historical",
                       source="QUERY_ATTRIBUTION_HISTORY rolled to CALLs, day grain")
            if guard(tres, "No CALLs matched that name in this window/scope — "
                           "check spelling (bare names match any db.schema)."):
                tdf = tres.df.copy()
                tdf["USD"] = tdf["CREDITS"].map(lambda c: credits_to_usd(c, rate))
                _tot = float(tdf["USD"].sum())
                _calls = int(tdf["CALLS"].sum())
                kpi_row([
                    {"label": f"Total, {days}d", "value": format_usd(_tot)},
                    {"label": "Calls", "value": f"{_calls:,}"},
                    {"label": "Avg $/call",
                     "value": format_usd(_tot / _calls) if _calls else "n/a"},
                ])
                charts.spend_trend(tdf, day_col="DAY", usd_col="USD")
                styled_table(tdf, height=200, column_config={
                    "USD": st.column_config.NumberColumn("$", format="$%.4f"),
                    "CREDITS_PER_CALL": st.column_config.NumberColumn("cr/call", format="%.6f"),
                })
                st.caption("$0 days with calls = attribution not caught up (~6h) or "
                           "children ran without a warehouse — same caveats as the leaderboard.")

    with st.expander("Price a specific CALL or session (measured)"):
        st.caption(
            "Paste a CALL's QUERY_ID for that one proc run, or a SESSION_ID to price "
            "every proc the session ran (e.g. your three ad-hoc CALLs). Children roll "
            "up via QUERY_ATTRIBUTION_HISTORY.ROOT_QUERY_ID — no task graph id needed. "
            "Attribution lags ~6h; idle time excluded."
        )
        _ident = st.text_input("QUERY_ID or SESSION_ID", key="uc_call_ident")
        if _ident.strip():
            cres = run(insights_sql.call_cost_lookup(_ident.strip()), page=_PAGE,
                       key=f"call_cost_{_ident.strip()[:24]}", tier="historical",
                       source="QUERY_ATTRIBUTION_HISTORY (ROOT_QUERY_ID rollup, 7d)")
            if guard(cres, "No CALLs matched in the last 7 days — check the id; "
                           "attribution lags ~6h, so very recent runs may not price yet."):
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
                        st.markdown("**Where the money went inside this CALL**")
                        styled_table(kdf, height=240, column_config={
                            "USD": st.column_config.NumberColumn("$", format="$%.4f"),
                        })

    st.divider()
    st.markdown("**AI — $ by function and model**")
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
