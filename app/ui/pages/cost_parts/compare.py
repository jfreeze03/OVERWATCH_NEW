"""Cost & Contract — Compare: period vs period (Phase 1).

The spreadsheet-killer: "spend is up 12% — WHICH warehouses/patterns did
it?" answered from existing facts/marts only — no live Account Usage scans
(live-scan budget pinned at 0).

Grain honesty (Codex r11 #12): warehouse spend = FACT_WAREHOUSE_DAILY
(exact metering, company-scopable); queries/fails/queued =
FACT_QUERY_HOURLY (company-scoped); account billed = FACT_METERING_DAILY
(account-wide by construction, labeled so). The current partial month is
never a compare side by default; the escape hatch pairs equal-length
windows and says so.

Scope honesty (#49): Compare receives only COMPANY + DATES. The retired
Environment picker never scoped these reads; environment-vs-environment
comparison is not built (Phase 2, docs/design/COMPARE_MODE.md).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.query import run, run_batch
from app.data import mart27_sql
from app.logic import compare as compare_logic
from app.logic.formulas import (
    account_today,
    blended_billed_usd,
    format_usd,
    humanize_duration,
    pct_delta,
    safe_float,
)
from app.ui import charts
from app.ui.components import (
    empty_state,
    guard,
    kpi_row,
    panel_help,
    result_caption,
    selectable_table,
    styled_table,
)

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
    """pct_delta returns None when B is zero (its documented contract —
    live crash 2026-07-11: an empty B side met an f-string format spec)."""
    d = pct_delta(a, b)
    if d is None:
        return "no B-side data"
    return f"{d:+.{decimals}f}% vs B"


def _coverage_warning(df: pd.DataFrame, pair: dict) -> str:
    """#34: per-side coverage contract. A side is 'incomplete' only when the loader
    has NOT yet reached that side's window end — judged against LOADED_THROUGH (the
    company scope's GLOBAL last loaded day), not against a per-side active-day count.
    FACT_WAREHOUSE_DAILY is sparse (built from metering, no date spine): a
    weekend-suspended workload has fewer active DISTINCT days than the calendar span
    even when fully loaded, so the old COUNT(DISTINCT DAY) < span test raised a false
    'Incomplete coverage' banner on correct month-over-month data. Idle days never
    lower LOADED_THROUGH, so it cleanly separates an unfinished backfill (real) from a
    quiet day (not a gap). Returns '' when the loader has reached both window ends."""
    from datetime import date, timedelta

    if df.empty or "LOADED_THROUGH" not in df.columns:
        return ""
    loaded = pd.to_datetime(df["LOADED_THROUGH"].iloc[0], errors="coerce")
    if pd.isna(loaded):
        return ""
    loaded = loaded.date()

    def _last_expected(hi: object) -> date | None:
        # windows are end-EXCLUSIVE, so the last day that could carry data is hi - 1
        try:
            return date.fromisoformat(str(hi)) - timedelta(days=1)
        except ValueError:
            return None

    msgs = []
    for _side, window, label in (("A", pair["a"], pair["label_a"]),
                                 ("B", pair["b"], pair["label_b"])):
        last_expected = _last_expected(window[1])
        if last_expected is not None and loaded < last_expected:
            msgs.append(f"{label}: loaded through {loaded}, window ends {last_expected}")
    if not msgs:
        return ""
    return ("Incomplete coverage — " + "; ".join(msgs)
            + ". Movers and Δ% below are provisional until the backfill reaches both "
              "window ends; a side missing its tail days can show false 100% moves.")


def _compare_tab(company: str, rate: float, ai_rate: float) -> None:
    # #49: Compare is scoped by company + dates only; no environment argument is
    # accepted or applied to the reads below.
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
    panel_help(
        "Period-vs-period from facts only (no live Account Usage scans): A vs B on warehouse "
        "spend, queries, fail rate, queued time and account billed, then the warehouses and "
        "query patterns that moved the bill. If a delta looks extreme, check the 'Incomplete "
        "coverage' warning first — a half-loaded window manufactures false 100% moves."
    )

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
        # Use the account/company-wide TOTALS (constant across every row via the cov CROSS JOIN),
        # NOT the sum of the returned frame: that frame is the top-100 movers by |A-B|, which drops
        # the largest STEADY warehouse first and understated this LEVEL KPI (bug-hunt 2026-08-30).
        if "TOTAL_A_CREDITS" in wh.df.columns and len(wh.df):
            a_usd = safe_float(wh.df["TOTAL_A_CREDITS"].iloc[0]) * rate
            b_usd = safe_float(wh.df["TOTAL_B_CREDITS"].iloc[0]) * rate
        else:  # defensive fallback for an old-shape frame
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
        kpis.append({"label": "Queued", "value": humanize_duration(aqu, "s"),
                     "delta": _delta_chip(aqu, bqu),
                     "delta_color": "inverse" if aqu > bqu else "normal",
                     "help": f"B = {humanize_duration(bqu, 's')}."})
    if bill.usable():
        # C1: price AI/Cortex credits at the AI rate. compare_billed carries the
        # AI/OTHER split; fall back to the flat rate if it's absent (old cache).
        if {"CREDITS_BILLED_OTHER", "CREDITS_BILLED_AI"} <= set(bill.df.columns):
            ab = blended_billed_usd(_side_value(bill.df, "A", "CREDITS_BILLED_OTHER"),
                                    _side_value(bill.df, "A", "CREDITS_BILLED_AI"), rate, ai_rate)
            bb = blended_billed_usd(_side_value(bill.df, "B", "CREDITS_BILLED_OTHER"),
                                    _side_value(bill.df, "B", "CREDITS_BILLED_AI"), rate, ai_rate)
        else:
            ab = _side_value(bill.df, "A", "CREDITS_BILLED") * rate
            bb = _side_value(bill.df, "B", "CREDITS_BILLED") * rate
        kpis.append({
            "label": "Account billed",
            "value": format_usd(ab),
            "delta": _delta_chip(ab, bb),
            "delta_color": "inverse" if ab > bb else "normal",
            "help": "Every service, account-wide — metering-daily has no "
                    "company grain, so this ignores the company filter. AI/Cortex "
                    f"credits price at the AI rate. B = {format_usd(bb)}.",
        })
    if kpis:
        kpi_row(kpis)
    elif all(r.ok for r in (wh, act, bill)):
        empty_state("no_data_yet", "No fact rows in either window yet — the hourly loaders fill these.")

    # ---- warehouse movers ---------------------------------------------------
    st.markdown("**Warehouse movers — who moved the bill**")
    _sel_wh = ""  # sticky selection drives the pattern-movers scope below
    if guard(wh, "No warehouse credits in either window."):
        view = wh.df.copy()
        # #34: a partial backfill on either side manufactures false movers — gate
        # on the per-side coverage the reader now returns before drawing the deltas.
        _cov_warn = _coverage_warning(view, pair)
        if _cov_warn:
            st.warning(_cov_warn)
        view["A_USD"] = view["A_CREDITS"].map(safe_float) * rate
        view["B_USD"] = view["B_CREDITS"].map(safe_float) * rate
        view["DELTA_USD"] = view["A_USD"] - view["B_USD"]
        view["DELTA_PCT"] = view.apply(lambda r: pct_delta(r["A_USD"], r["B_USD"]), axis=1)
        view = view.reindex(view["DELTA_USD"].abs().sort_values(ascending=False).index)
        charts.paired_bars(view, "WAREHOUSE_NAME", "A_USD", "B_USD",
                           a_label=pair["label_a"], b_label=pair["label_b"])
        # Selectable (owner ask #6): click a warehouse to scope the pattern movers
        # below to it. Index off the EXACT displayed sub-frame (reindex-sorted then
        # head(15)); a positional selection maps only to the frame passed in.
        disp = view[["WAREHOUSE_NAME", "A_USD", "B_USD", "DELTA_USD", "DELTA_PCT"]].head(15)
        _wh_sel = selectable_table(
            disp,
            key=f"cmp_wh_sel_{company}_{a0}_{b0}",
            height=260,
            column_config={
                "A_USD": st.column_config.NumberColumn(f"A $ ({pair['label_a']})", format="$%.0f"),
                "B_USD": st.column_config.NumberColumn(f"B $ ({pair['label_b']})", format="$%.0f"),
                "DELTA_USD": st.column_config.NumberColumn("Δ $", format="$%.0f"),
                "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%"),
            })
        _sel_wh = (str(disp.iloc[int(_wh_sel)]["WAREHOUSE_NAME"])
                   if _wh_sel is not None and 0 <= int(_wh_sel) < len(disp) else "")
        result_caption(wh)

    # ---- pattern movers -----------------------------------------------------
    # #6: a warehouse selection above scopes these to that warehouse via a LIVE
    # per-warehouse read (MART_PATTERN_COST_DAILY has no warehouse grain); the
    # scan is interaction-gated (fires only on the row click, never first paint),
    # the one exception to Compare's zero-live-scan invariant. Account-wide (mart)
    # until a warehouse is clicked.
    if _sel_wh:
        st.markdown(f"**Pattern movers on {_sel_wh} — the silent-spend delta (measured $)**")
        st.caption("Live per-warehouse scan (QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY), "
                   "measured compute credits at ~8h view lag — same $ attribution basis as the "
                   "account-wide movers. RUNS here counts distinct executions (the account-wide "
                   "table counts attribution rows, which run higher for multi-hour queries). "
                   "Click another warehouse to switch.")
        _pat = run(
            mart27_sql.compare_pattern_costs_by_warehouse(a0, a1, b0, b1, _sel_wh),
            page=_PAGE, key=f"cmp_pat_wh_{company}_{a0}_{b0}_{_sel_wh}", tier="recent",
            source="QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY (per-warehouse pattern movers, "
                   "interaction-gated)")
        _pat_empty = f"No repeated pattern on {_sel_wh} crossed the 0.01-credit floor in either window."
        _pat_note = ("Measured attribution compute credits per parameterized hash on "
                     f"{_sel_wh} — new-in-A patterns show B = $0.")
    else:
        st.markdown("**Pattern movers — the silent-spend delta (measured $)**")
        st.caption("Click a warehouse above to scope these to it; account-wide until then.")
        _pat = pat
        _pat_empty = "No repeated pattern crossed the 0.01-credit floor in either window."
        _pat_note = ("Measured QUERY_ATTRIBUTION_HISTORY credits per parameterized hash — "
                     "new-in-A patterns show B = $0.")
    if not _sel_wh and not pat.ok:
        empty_state("needs_setup",
                    "Pattern movers need migration V037 (MART_PATTERN_COST_DAILY v2) — "
                    "an admin can apply the pending schema update on Admin → Migrations & freshness.")
    # review fix: verified-clean green only for the LIVE per-warehouse scan;
    # the account-wide mart leg's empty also covers installed-but-not-yet-
    # loaded windows, which must not read as an all-clear.
    elif guard(_pat, _pat_empty, kind="clean" if _sel_wh else "no_data_yet"):
        pv = _pat.df.copy()
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
        result_caption(_pat, note=_pat_note)

    # ---- volume shape ---------------------------------------------------------
    if act.usable():
        st.markdown("**Volume shape**")
        rows = []
        for metric, col, scale in (("Queries", "QUERIES", 1.0), ("Fails", "FAILS", 1.0),
                                   ("Queued min", "QUEUED_SEC", 1 / 60), ("Remote spill GB", "SPILL_REMOTE_GB", 1.0)):
            a_v = _side_value(act.df, "A", col) * scale
            b_v = _side_value(act.df, "B", col) * scale
            d = pct_delta(a_v, b_v)          # None when B is zero — never format it
            rows.append({"METRIC": metric, "A": round(a_v, 1), "B": round(b_v, 1),
                         "DELTA_PCT": d})
        styled_table(pd.DataFrame(rows), height=180, column_config={
            "DELTA_PCT": st.column_config.NumberColumn("Δ %", format="%.1f%%")})
        result_caption(act)
