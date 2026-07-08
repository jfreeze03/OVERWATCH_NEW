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

from app.core.query import run
from app.data import cortex_sql, insights_sql
from app.logic.formulas import credits_to_usd, format_usd, safe_float
from app.ui.components import guard, kpi_row, result_caption, styled_table

_PAGE = "Cost & Contract"


def _unit_costs_tab(f: dict, rate: float, ai_rate: float) -> None:
    company, days = f["company"], f["days"]
    database, schema_contains = f["database"], f["schema_contains"]
    st.caption(
        "Measured price tags: QUERY_ATTRIBUTION_HISTORY credits (~6h lag, idle time "
        "excluded) at your contract rate. For 'who owns the bill' including idle, "
        "use Optimization's allocated view; for pipelines, Operations → Task graphs ($)."
    )

    # ---- most expensive individual queries (measured) ----------------------
    q_res = run(insights_sql.measured_query_costs(
                    days, company, database, schema_contains,
                    f["warehouse_contains"], f["user_contains"], 50),
                page=_PAGE, key=f"unit_q_{company}_{days}_{database}", tier="historical",
                source="QUERY_ATTRIBUTION_HISTORY + QUERY_HISTORY")
    p_res = run(insights_sql.procedure_costs_usd(days, company, database, schema_contains, 50),
                page=_PAGE, key=f"unit_p_{company}_{days}_{database}", tier="historical",
                source="QUERY_ATTRIBUTION_HISTORY (rolled up to CALL)")
    ai_res = run(cortex_sql.cortex_model_costs(days), page=_PAGE,
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
    st.markdown("**Stored procedures — $/call leaderboard (every proc, not just changed ones)**")
    if guard(p_res, "No CALLs with attributed credits in this scope/window."):
        pdf = p_res.df.copy()
        pdf["USD_TOTAL"] = pdf["TOTAL_CREDITS"].map(lambda c: credits_to_usd(c, rate))
        pdf["USD_PER_CALL"] = pdf["CREDITS_PER_CALL"].map(lambda c: credits_to_usd(c, rate))
        styled_table(pdf, height=280, column_config={
            "USD_TOTAL": st.column_config.NumberColumn("$ (window)", format="$%.2f"),
            "USD_PER_CALL": st.column_config.NumberColumn("$/call", format="$%.4f"),
            "FAIL_PCT": st.column_config.NumberColumn("Fail %", format="%.1f%%"),
        })
        result_caption(p_res, note="Database/schema = the CALL's session context; a proc "
                                   "may read other databases. Change-impact (Operations) "
                                   "watches these same numbers around each ALTER.")

    st.divider()
    st.markdown("**AI — $ by function and model**")
    if not ai_res.ok:
        st.caption("CORTEX_FUNCTIONS_USAGE_HISTORY is not accessible on this account/role — "
                   "per-user AI spend remains available under Chargeback & AI.")
    elif ai_res.empty:
        st.caption("No Cortex function usage in this window.")
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
