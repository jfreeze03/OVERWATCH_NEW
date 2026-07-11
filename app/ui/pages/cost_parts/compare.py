"""Cost & Contract — Compare: period vs period (Phase 1).

The spreadsheet-killer: "spend is up 12% — WHICH warehouses/patterns did
it?" answered from existing facts/marts only — no live Account Usage scans
(live-scan budget pinned at 0).

Grain honesty (Codex r11 #12): warehouse spend = FACT_WAREHOUSE_DAILY
(exact metering, company-scopable); queries/fails/queued =
FACT_QUERY_HOURLY (company-scoped); account billed = FACT_METERING_DAILY
(account-wide by construction, labeled so). The current partial month is
never a compare side by default; the escape hatch pairs equal-length
windows and says so. Env-vs-env is Phase 2 (docs/design/COMPARE_MODE.md).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.query import run, run_batch
from app.data import mart27_sql
from app.logic import compare as compare_logic
from app.logic.formulas import account_today, format_usd, pct_delta, safe_float
from app.ui import charts
from app.ui.components import guard, kpi_row, result_caption, styled_table

_PAGE = "Cost & Contract"

_PAIRINGS = {
    "Last full month vs prior": "month",
    "Trailing 7d vs prior": "7d",
    "Trailing 30d vs prior": "30d",
}


def _side_value(df: pd.DataFrame, side: str, col: str) -> float:
    rows = df[df["SIDE"].astype(str) == side]
    return safe_float(rows.iloc[0].get(col)) if not rows.empty else 0.0


def _delta_chip(a: float, b: float, decimals: int = 1) -> str:
    return f"{pct_delta(a, b):+.{decimals}f}% vs B"


def _compare_tab(company: str, rate: float) -> None:
    pick = st.radio("Pairing", list(_PAIRINGS), horizontal=True, key="cmp_kind")
    kind = _PAIRINGS[pick]
    include_partial = False
    if kind == "month":
        include_partial = st.toggle(
            "Include current month (partial)", key="cmp_partial",
            help="Pairs MTD against the SAME number of days of the prior "
                 "month — equal-length windows or nothing. Labeled partial.")
    pair = compare_logic.period_pair(kind, account_today(), include_partial)
    a0, a1 = pair["a"]
    b0, b1 = pair["b"]
    st.caption(f"A = {pair['label_a']} ({a0} to {a1}, end-exclusive) · "
               f"B = {pair['label_b']} ({b0} to {b1}) · account time"
               + (" · A is partial" if pair["partial"] else ""))

    _b = run_batch([
        {"key": "wh", "sql": mart27_sql.compare_warehouse_credits(a0, a1, b0, b1, company),
         "source": "FACT_WAREHOUSE_DAILY (exact metering, both sides)"},
        {"key": "act", "sql": mart27_sql.compare_activity(a0, a1, b0, b1, company),
         "source": "FACT_QUERY_HOURLY (both sides)"},
        {"key": "bill", "sql": mart27_sql.compare_billed(a0, a1, b0, b1),
         "source": "FACT_METERING_DAILY (account-wide)"},
        {"key": "pat", "sql": mart27_sql.compare_pattern_costs(a0, a1, b0, b1, company),
         "source": f"MART_PATTERN_COST_DAILY v2 ({company} + account-level)"},
    ], page=_PAGE, tier="recent")

    def _get(k: str, sql: str, source: str):
        return (_b or {}).get(k) or run(sql, page=_PAGE, key=f"cmp_{k}_{company}_{a0}_{b0}",
                                        tier="recent", source=source)

    wh = _get("wh", mart27_sql.compare_warehouse_credits(a0, a1, b0, b1, company),
              "FACT_WAREHOUSE_DAILY (exact metering, both sides)")
    act = _get("act", mart27_sql.compare_activity(a0, a1, b0, b1, company),
               "FACT_QUERY_HOURLY (both sides)")
    bill = _get("bill", mart27_sql.compare_billed(a0, a1, b0, b1),
                "FACT_METERING_DAILY (account-wide)")
    pat = _get("pat", mart27_sql.compare_pattern_costs(a0, a1, b0, b1, company),
               f"MART_PATTERN_COST_DAILY v2 ({company} + account-level)")

    # ---- paired KPI strip ---------------------------------------------------
    kpis: list[dict] = []
    if wh.usable():
        a_usd = float(wh.df["A_CREDITS"].map(safe_float).sum()) * rate
        b_usd = float(wh.df["B_CREDITS"].map(safe_float).sum()) * rate
        kpis.append({
            "label": f"Warehouse spend — {pair['label_a']}",
            "value": format_usd(a_usd),
            "delta": _delta_chip(a_usd, b_usd),
            "delta_color": "inverse" if a_usd > b_usd else "normal",
            "help": "Exact warehouse metering x rate, company-scopable. "
                    f"B = {format_usd(b_usd)}.",
        })
    if act.usable():
        aq, bq = _side_value(act.df, "A", "QUERIES"), _side_value(act.df, "B", "QUERIES")
        af, bf = _side_value(act.df, "A", "FAILS"), _side_value(act.df, "B", "FAILS")
        aqu, bqu = _side_value(act.df, "A", "QUEUED_SEC"), _side_value(act.df, "B", "QUEUED_SEC")
        kpis.append({"label": "Queries", "value": f"{aq:,.0f}",
                     "delta": _delta_chip(aq, bq), "delta_color": "off",
                     "help": f"B = {bq:,.0f}. FACT_QUERY_HOURLY, company-scoped."})
        a_rate = (af / aq * 100) if aq else 0.0
        b_rate = (bf / bq * 100) if bq else 0.0
        kpis.append({"label": "Fail rate", "value": f"{a_rate:.2f}%",
                     "delta": f"{a_rate - b_rate:+.2f} pts vs B",
                     "delta_color": "inverse" if a_rate > b_rate else "normal",
                     "help": f"B = {b_rate:.2f}% ({bf:,.0f} of {bq:,.0f})."})
        kpis.append({"label": "Queued", "value": f"{aqu / 60:,.0f} min",
                     "delta": _delta_chip(aqu, bqu),
                     "delta_color": "inverse" if aqu > bqu else "normal",
                     "help": f"B = {bqu / 60:,.0f} min."})
    if bill.usable():
        ab = _side_value(bill.df, "A", "CREDITS_BILLED") * rate
        bb = _side_value(bill.df, "B", "CREDITS_BILLED") * rate
        kpis.append({
            "label": "Account billed",
            "value": format_usd(ab),
            "delta": _delta_chip(ab, bb),
            "delta_color": "inverse" if ab > bb else "normal",
            "help": "Every service, account-wide — metering-daily has no "
                    f"company grain, so this ignores the company filter. B = {format_usd(bb)}.",
        })
    if kpis:
        kpi_row(kpis)
    elif all(r.ok for r in (wh, act, bill)):
        st.info("No fact rows in either window yet — the hourly loaders fill these.")

    # ---- warehouse movers ---------------------------------------------------
    st.markdown("**Warehouse movers — who moved the bill**")
    if guard(wh, "No warehouse credits in either window."):
        view = wh.df.copy()
        view["A_USD"] = view["A_CREDITS"].map(safe_float) * rate
        view["B_USD"] = view["B_CREDITS"].map(safe_float) * rate
        view["DELTA_USD"] = view["A_USD"] - view["B_USD"]
        view["DELTA_PCT"] = view.apply(lambda r: pct_delta(r["A_USD"], r["B_USD"]), axis=1)
        view = view.reindex(view["DELTA_USD"].abs().sort_values(ascending=False).index)
        charts.paired_bars(view, "WAREHOUSE_NAME", "A_USD", "B_USD",
                           a_label=pair["label_a"], b_label=pair["label_b"])
        styled_table(
            view[["WAREHOUSE_NAME", "A_USD", "B_USD", "DELTA_USD", "DELTA_PCT"]].head(15),
            height=260,
            column_config={
                "A_USD": st.column_config.NumberColumn(f"A $ ({pair['label_a']})", format="$%.0f"),
                "B_USD": st.column_config.NumberColumn(f"B $ ({pair['label_b']})", format="$%.0f"),
                "DELTA_USD": st.column_config.NumberColumn("Δ $", format="$%.0f"),
                "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
            })
        result_caption(wh)

    # ---- pattern movers -----------------------------------------------------
    st.markdown("**Pattern movers — the silent-spend delta (measured $)**")
    if not pat.ok:
        st.info("Pattern movers need migration V037 (MART_PATTERN_COST_DAILY v2) — "
                "an admin can apply the pending schema update on Admin → Migrations & freshness.")
    elif guard(pat, "No repeated pattern crossed the $0.01 floor in either window."):
        pv = pat.df.copy()
        pv["A_USD"] = pv["A_CREDITS"].map(safe_float) * rate
        pv["B_USD"] = pv["B_CREDITS"].map(safe_float) * rate
        pv["DELTA_USD"] = pv["A_USD"] - pv["B_USD"]
        styled_table(
            pv[["SAMPLE_TEXT", "A_RUNS", "B_RUNS", "A_USD", "B_USD", "DELTA_USD"]],
            height=280,
            column_config={
                "A_USD": st.column_config.NumberColumn("A $", format="$%.2f"),
                "B_USD": st.column_config.NumberColumn("B $", format="$%.2f"),
                "DELTA_USD": st.column_config.NumberColumn("Δ $", format="$%.2f"),
            })
        result_caption(pat, note="Measured QUERY_ATTRIBUTION_HISTORY credits per "
                                 "parameterized hash — new-in-A patterns show B = $0.")

    # ---- volume shape ---------------------------------------------------------
    if act.usable():
        st.markdown("**Volume shape**")
        rows = []
        for metric, col, scale in (("Queries", "QUERIES", 1.0), ("Fails", "FAILS", 1.0),
                                   ("Queued min", "QUEUED_SEC", 1 / 60), ("Remote spill GB", "SPILL_REMOTE_GB", 1.0)):
            a_v = _side_value(act.df, "A", col) * scale
            b_v = _side_value(act.df, "B", col) * scale
            rows.append({"METRIC": metric, "A": round(a_v, 1), "B": round(b_v, 1),
                         "DELTA_PCT": round(pct_delta(a_v, b_v), 1)})
        styled_table(pd.DataFrame(rows), height=180, column_config={
            "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%")})
        result_caption(act)
