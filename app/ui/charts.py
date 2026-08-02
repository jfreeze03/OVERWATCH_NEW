"""Altair charts with executive-grade formatting: dollar axes, tooltips,
budget rule, forecast band. Every chart renders real series or nothing —
callers use components.guard() first."""

from __future__ import annotations

import zlib

import altair as alt
import pandas as pd
import streamlit as st

from app.ui import palette
from app.ui.sizing import CHART_H_MD, CHART_H_SM

_HEIGHT = CHART_H_MD
HEATMAP_MAX_ROWS = 20  # 24px/row; beyond this the heatmap became a scroll trap

_ACCENT = palette.ACCENT
_ACCENT2 = palette.ACCENT2
_GRID = "rgba(148,163,184,0.14)"
_LABEL = palette.LOW
_TITLE = "#c3cddb"
_FONT = ("Inter var, Inter, 'SF Pro Display', -apple-system, BlinkMacSystemFont, "
         "'Segoe UI', Roboto, sans-serif")
# severity series hues from the single palette source (rec50), so a "bad"
# series looks bad the same way it does on every card and chip.
SEV_COLORS = dict(palette.SEVERITY_HUES)

# rec38: heat = orange (intuitive "hotness" for the hour x entity heatmap). ONE
# ramp, referenced by the theme range AND hour_heatmap, so it is not a one-off
# scheme string a shade off from everything else.
# Starts at a visible dark-orange (NOT the page background #0a0f1c) so a low-but-
# NONZERO cell separates from an EMPTY (undrawn) cell, which stays page-dark —
# the caller's "dark cells = no activity" reading depends on that separation.
_HEATMAP_RANGE = ["#431407", "#7c2d12", "#c2410c", "#ea580c", "#fdba74"]


def _overwatch_theme() -> dict:
    return {
        "config": {
            "background": "transparent",
            "view": {"stroke": "transparent"},
            "font": _FONT,
            "axis": {
                "gridColor": _GRID, "gridDash": [3, 4], "domainColor": _GRID,
                "tickColor": _GRID, "tickSize": 4,
                "labelColor": _LABEL, "titleColor": _TITLE,
                "labelFontSize": 11, "titleFontSize": 11, "titleFontWeight": 600,
                "labelFont": _FONT, "titleFont": _FONT, "titlePadding": 8,
            },
            "axisX": {"grid": False, "labelAngle": 0},
            "legend": {"labelColor": _LABEL, "titleColor": _TITLE, "labelFontSize": 11,
                       "labelFont": _FONT, "titleFont": _FONT, "symbolType": "circle",
                       "symbolSize": 90, "orient": "top", "titlePadding": 6},
            "range": {
                "category": [_ACCENT, palette.OK, "#c084fc", palette.WARN, palette.BAD,
                              palette.ACCENT2, "#a3e635", palette.HIGH],  # rec50 (#c084fc/#a3e635 chart-only)
                "heatmap": _HEATMAP_RANGE,  # rec38: one orange heat ramp
            },
            "bar": {"cornerRadiusEnd": 4, "color": _ACCENT},
            "line": {"color": _ACCENT, "strokeWidth": 2.4},
            "point": {"color": _ACCENT, "filled": True, "size": 42},
            "rule": {"color": _LABEL},
            "area": {"line": True, "opacity": 0.22},
        }
    }


try:  # altair >= 5.5: alt.theme is the surviving registry (alt.themes is
    # deprecated and removed in altair 6) — try it first so runs stop
    # emitting the AltairDeprecationWarning on every session.
    alt.theme.register("overwatch", enable=True)(_overwatch_theme)
except Exception:  # noqa: BLE001 - chart theming must never break a page
    try:  # altair 5.0–5.4: legacy registry
        alt.themes.register("overwatch", _overwatch_theme)
        alt.themes.enable("overwatch")
    except Exception:  # noqa: BLE001
        pass


def _base(df: pd.DataFrame, height: int | None = None) -> alt.Chart:
    return alt.Chart(df).properties(height=height or _HEIGHT)


def _empty_note(msg: str = "No plottable rows for this window.") -> None:
    """rec36: a chart helper must say 'checked, clean' — not render nothing —
    when coercion empties the frame (house rule 8). Callers guard the PRE-coercion
    frame, so a bad-typed column would otherwise leave blank space."""
    st.caption(msg)


def _day_axis(day_values, fmt_short: str = "%b %d") -> alt.Axis:
    """rec37: adaptive time ticks — day for a <=31d span, week to 120d, month
    beyond — so labels stay predictable at 180/365 (labelOverlap alone dropped
    them unevenly). Span is inferred from the data; no per-caller change."""
    try:
        s = pd.to_datetime(pd.Series(list(day_values)), errors="coerce").dropna()
        span = int((s.max() - s.min()).days) if len(s) else 0
    except Exception:  # noqa: BLE001 - axis tuning is cosmetic, never break a chart
        span = 0
    if span <= 31:
        unit, fmt = "day", fmt_short
    elif span <= 120:
        unit, fmt = "week", fmt_short
    else:
        unit, fmt = "month", "%b '%y"   # year-tag so a 365d span crossing Jan is unambiguous
    return alt.Axis(format=fmt, tickCount=unit, labelOverlap="greedy")


def _legend(wide: bool = False, **kw) -> alt.Legend:
    """rec39: the legend-placement RULE in one place — wide multi-category stacks
    read at the BOTTOM (many labels), few-series comparisons at the TOP (next to
    the title the eye just read). Not a blanket 'always top'."""
    return alt.Legend(orient="bottom" if wide else "top", title=kw.pop("title", None), **kw)


def _share_note(label: str, amount: float, total: float, *, dollars: bool = True) -> str:
    """rec35 helper: 'Top: X $Y (Z% of $total).' — the lead-with-the-conclusion
    line, computed from the data the chart already has. The share is omitted when
    it would be nonsensical (categories that net out negative make a positive top
    exceed 100% of the total)."""
    a = f"${amount:,.0f}" if dollars else f"{amount:,.0f}"
    share = (amount / total * 100) if total else 0.0
    if total > 0 and 0 <= share <= 100:
        t = f"${total:,.0f}" if dollars else f"{total:,.0f}"
        return f"Top: {label} {a} ({share:.0f}% of {t})."
    return f"Top: {label} {a}."


def spend_trend(
    df: pd.DataFrame,
    *,
    day_col: str = "DAY",
    usd_col: str = "USD",
    daily_budget_usd: float = 0.0,
) -> None:
    """Daily spend as bars with a 7-day average line (redesign, 2026-07-09).

    The old gradient area read as "abstract wash" — nobody could say what it
    meant (owner feedback, twice). Bars answer "how much did THAT day cost";
    the average line answers "which way is it heading"; the newest day
    renders dimmed because metering lags up to 24h — partial, not a crash
    (the question every viewer asked of the old chart). The forecast range
    lives in the Projected month-end KPI, not as a floating rectangle here.
    Dataset embeds ONCE on the layer (most-viewed chart, page-payload rule).
    """
    data = df[[day_col, usd_col]].copy()
    data.columns = ["Day", "USD"]
    data["Day"] = pd.to_datetime(data["Day"], errors="coerce")
    data["USD"] = pd.to_numeric(data["USD"], errors="coerce").fillna(0.0)
    data = data.dropna(subset=["Day"]).sort_values("Day")
    if data.empty:
        _empty_note()
        return
    data["AVG7"] = data["USD"].rolling(7, min_periods=3).mean().round(2)
    data["PROVISIONAL"] = data["Day"] == data["Day"].max()
    grad = alt.Gradient(gradient="linear", x1=0, x2=0, y1=1, y2=0,
                        stops=[alt.GradientStop(color=_ACCENT2, offset=0.0),
                               alt.GradientStop(color=_ACCENT, offset=1.0)])
    bar_size = max(4, min(20, int(660 / max(len(data), 1))))
    enc_x = alt.X("yearmonthdate(Day):T", title=None, axis=_day_axis(data["Day"]))
    tip = [alt.Tooltip("Day:T"),
           alt.Tooltip("USD:Q", format="$,.2f", title="Spend"),
           alt.Tooltip("AVG7:Q", format="$,.0f", title="7-day avg")]
    bars = (alt.Chart().mark_bar(color=grad, cornerRadiusEnd=2, size=bar_size)
            .encode(x=enc_x,
                    y=alt.Y("USD:Q", title="Spend (USD)", axis=alt.Axis(format="$,.0f")),
                    opacity=alt.condition("datum.PROVISIONAL",
                                          alt.value(0.45), alt.value(1.0)),
                    tooltip=tip))
    avg = (alt.Chart().mark_line(color="#c3cddb", strokeWidth=2, interpolate="monotone")
           .encode(x=enc_x, y=alt.Y("AVG7:Q"), tooltip=tip))
    layers = [alt.layer(bars, avg, data=data).properties(height=_HEIGHT)]
    if daily_budget_usd and daily_budget_usd > 0:
        rule_df = pd.DataFrame({"y": [daily_budget_usd]})
        layers.append(
            alt.Chart(rule_df)
            .mark_rule(strokeDash=[6, 4], color=SEV_COLORS["CRITICAL"])
            .encode(y="y:Q", tooltip=alt.value(f"Daily budget rate ${daily_budget_usd:,.0f}"))
        )
        # Visible without hover (screenshots, phones) — Codex r4 #17.
        layers.append(
            alt.Chart(rule_df)
            .mark_text(align="left", dx=6, dy=-7, fontSize=11, color=SEV_COLORS["CRITICAL"],  # A1 palette; C15 >=11px floor
                       text=f"budget ${daily_budget_usd:,.0f}/day")
            .encode(y="y:Q", x=alt.value(6))
        )
    st.altair_chart(alt.layer(*layers), use_container_width=True)
    total = float(data["USD"].sum())
    note = f"Bars = each day's spend (window total ${total:,.0f}); line = 7-day average"
    # r6-bug7: pace over COMPLETE days only. The newest day is PROVISIONAL (metering lags
    # up to 24h; the chart dims it and the caption disclaims it), so including it in the
    # trailing-7 mean understated the recent week and printed a phantom negative "pace"
    # on flat spend. Compare the last two COMPLETE 7-day windows instead.
    complete = data[~data["PROVISIONAL"]]
    if len(complete) >= 14:
        last7 = float(complete["USD"].tail(7).mean())
        prior7 = float(complete["USD"].iloc[-14:-7].mean())
        if prior7 > 0:
            note += f", pace {(last7 - prior7) / prior7 * 100:+.0f}% vs the prior week"
    st.caption(note + ". Newest day is dimmed: metering lags up to 24h, so it is partial, not a drop.")

def bar_usd(df: pd.DataFrame, label_col: str, usd_col: str, title: str = "", top_n: int = 10) -> None:
    data = df[[label_col, usd_col]].head(top_n).copy()
    data.columns = ["Label", "USD"]
    grad = alt.Gradient(gradient="linear", x1=0, x2=1, y1=0, y2=0,
                        stops=[alt.GradientStop(color=_ACCENT2, offset=0.0),
                               alt.GradientStop(color=_ACCENT, offset=1.0)])
    enc_y = alt.Y("Label:N", sort="-x", title=None,
                  axis=alt.Axis(labelLimit=260))  # full names (hover for longer)
    dmax = float(pd.to_numeric(data["USD"], errors="coerce").fillna(0).max())
    enc_x = alt.X("USD:Q", title=title or "USD", axis=alt.Axis(format="$,.0f"),
                  scale=alt.Scale(domain=[0, dmax * 1.16]) if dmax > 0 else alt.Scale())
    tip = [alt.Tooltip("Label:N"), alt.Tooltip("USD:Q", format="$,.2f")]
    base = _base(data, height=max(_HEIGHT, 30 * len(data)))
    bars = base.mark_bar(color=grad, cornerRadiusEnd=4).encode(y=enc_y, x=enc_x, tooltip=tip)
    labels = base.mark_text(align="left", dx=5, color=_LABEL, fontSize=11).encode(
        y=enc_y, x=enc_x, text=alt.Text("USD:Q", format="$,.0f"))
    st.altair_chart(bars + labels, use_container_width=True)


def clickable_bar_usd(df: pd.DataFrame, label_col: str, usd_col: str, *, key: str,
                      title: str = "", top_n: int = 10) -> str | None:
    """rec40: bar_usd whose bars are CLICKABLE. Returns the clicked Label on a NEW
    click (guarded against altair's sticky re-emit, same idiom as rec29), else
    None. DEGRADES to a plain non-clickable bar (returns None) on any runtime that
    lacks altair on_select or returns a selection shape we can't read — so the
    chart always renders and a missing click never breaks the page."""
    data = df[[label_col, usd_col]].head(top_n).copy()
    data.columns = ["Label", "USD"]
    grad = alt.Gradient(gradient="linear", x1=0, x2=1, y1=0, y2=0,
                        stops=[alt.GradientStop(color=_ACCENT2, offset=0.0),
                               alt.GradientStop(color=_ACCENT, offset=1.0)])
    enc_y = alt.Y("Label:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260))
    dmax = float(pd.to_numeric(data["USD"], errors="coerce").fillna(0).max())
    enc_x = alt.X("USD:Q", title=title or "USD", axis=alt.Axis(format="$,.0f"),
                  scale=alt.Scale(domain=[0, dmax * 1.16]) if dmax > 0 else alt.Scale())
    tip = [alt.Tooltip("Label:N"), alt.Tooltip("USD:Q", format="$,.2f")]
    base = _base(data, height=max(_HEIGHT, 30 * len(data)))
    bars = base.mark_bar(color=grad, cornerRadiusEnd=4).encode(y=enc_y, x=enc_x, tooltip=tip)
    labels = base.mark_text(align="left", dx=5, color=_LABEL, fontSize=11).encode(
        y=enc_y, x=enc_x, text=alt.Text("USD:Q", format="$,.0f"))
    plain = bars + labels
    try:
        sel = alt.selection_point(fields=["Label"], name="pt", on="click", clear="dblclick")
        event = st.altair_chart(plain.add_params(sel), use_container_width=True,
                                on_select="rerun", key=key)
    except Exception:  # noqa: BLE001 - runtime without on_select -> plain chart
        st.altair_chart(plain, use_container_width=True)
        return None
    picked = None
    try:  # selection store shape varies across versions; read it defensively
        store = getattr(event, "selection", None)
        if store is None and isinstance(event, dict):
            store = event.get("selection")
        rows = (store.get("pt") if isinstance(store, dict) else getattr(store, "pt", None)) or []
        if rows:
            first = rows[0]
            picked = first.get("Label") if isinstance(first, dict) else first
    except Exception:  # noqa: BLE001
        picked = None
    seen = f"_ow_barsel_{key}"
    if picked is None:
        # Fresh render with no active selection — e.g. the user navigated away on the
        # last click and returned, so Streamlit GC'd the chart's selection. Re-arm the
        # guard so the NEXT click of the same bar counts as new instead of a dead repeat
        # (without this the guard keeps the last label for the whole session).
        st.session_state.pop(seen, None)
        return None
    if picked != st.session_state.get(seen):
        st.session_state[seen] = picked
        return str(picked)
    return None


def daily_count_bars(df: pd.DataFrame, day_col: str, value_col: str, title: str = "") -> None:
    """Per-day count as vertical gradient bars over a TIME axis. Use this for
    'events/day' series — bar_count would render the date column as epoch
    millis on a nominal axis (the DDL-changes bug)."""
    data = df[[day_col, value_col]].copy()
    data.columns = ["Day", "Value"]
    data["Day"] = pd.to_datetime(data["Day"], errors="coerce")
    grad = alt.Gradient(gradient="linear", x1=0, x2=0, y1=1, y2=0,
                        stops=[alt.GradientStop(color=_ACCENT2, offset=0.0),
                               alt.GradientStop(color=_ACCENT, offset=1.0)])
    chart = (
        _base(data)
        .mark_bar(color=grad, cornerRadiusEnd=3, size=18)
        .encode(
            x=alt.X("yearmonthdate(Day):T", title=None, axis=_day_axis(data["Day"])),
            y=alt.Y("Value:Q", title=title or "Count", axis=alt.Axis(format=",.0f")),
            tooltip=[alt.Tooltip("Day:T", title="Day"),
                     alt.Tooltip("Value:Q", format=",.0f", title=title or "Count")],
        )
    )
    st.altair_chart(chart, use_container_width=True)


# C15 (a11y): a fixed categorical palette + a NAME-stable index, so a given
# entity ("ALFA", "WH_X") keeps the SAME color across renders regardless of which
# other entities share the frame. Default Altair coloring assigns by set order, so
# adding/removing one series recolors all of them — disorienting run-to-run. crc32
# (not hash()) so the mapping is deterministic across processes (hash() is salted).
_STABLE_PALETTE = ("#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2",
                   "#eeca3b", "#b279a2", "#ff9da6", "#9d755d", "#bab0ac")


def _stable_color_map(names) -> dict:
    """Deterministic {entity: hex} — pure, so the stability contract is testable."""
    uniq = sorted({str(n) for n in names})
    return {n: _STABLE_PALETTE[zlib.crc32(n.encode("utf-8")) % len(_STABLE_PALETTE)] for n in uniq}


def _stable_color(field: str, names, legend=None) -> alt.Color:
    cmap = _stable_color_map(names)
    uniq = list(cmap.keys())
    kwargs = {"scale": alt.Scale(domain=uniq, range=[cmap[n] for n in uniq])}
    if legend is not None:
        kwargs["legend"] = legend
    return alt.Color(f"{field}:N", **kwargs)


def daily_stacked_count(df: pd.DataFrame, day_col: str, category_col: str,
                        value_col: str, title: str = "Count", takeaway: bool = True) -> None:
    """Per-day stacked bars by category (counts) over a TIME axis — the
    'what kind of change, which day' view. Same day-grain axis contract as
    daily_stacked_usd; counts instead of dollars."""
    data = df[[day_col, category_col, value_col]].copy()
    data.columns = ["Day", "Category", "Value"]
    data["Day"] = pd.to_datetime(data["Day"], errors="coerce")
    if data.dropna(subset=["Day"]).empty:
        _empty_note()
        return
    chart = (
        _base(data)
        .mark_bar(cornerRadiusEnd=2)
        .encode(
            x=alt.X("yearmonthdate(Day):T", title=None,
                    axis=_day_axis(data["Day"])),
            y=alt.Y("sum(Value):Q", title=title, axis=alt.Axis(format=",.0f")),
            color=_stable_color("Category", data["Category"],
                                legend=_legend(wide=True)),
            tooltip=[alt.Tooltip("Day:T"), alt.Tooltip("Category:N"),
                     alt.Tooltip("sum(Value):Q", format=",.0f", title=title)],
        )
    )
    st.altair_chart(chart, use_container_width=True)
    if takeaway:  # rec35: lead with the conclusion
        _g = data.assign(_v=pd.to_numeric(data["Value"], errors="coerce")).groupby("Category")["_v"].sum()
        if float(_g.sum()) > 0:
            st.caption(_share_note(str(_g.idxmax()), float(_g.max()), float(_g.sum()), dollars=False))


def bar_count(df: pd.DataFrame, label_col: str, value_col: str, title: str = "", top_n: int = 10) -> None:
    data = df[[label_col, value_col]].head(top_n).copy()
    data.columns = ["Label", "Value"]
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            y=alt.Y("Label:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            x=alt.X("Value:Q", title=title or "Count", axis=alt.Axis(format=",.0f")),
            tooltip=[alt.Tooltip("Label:N"), alt.Tooltip("Value:Q", format=",.0f")],
        )
    )
    st.altair_chart(chart, use_container_width=True)


def daily_stacked_usd(df: pd.DataFrame, day_col: str, category_col: str, usd_col: str,
                      takeaway: bool = True) -> None:
    data = df[[day_col, category_col, usd_col]].copy()
    data.columns = ["Day", "Category", "USD"]
    if data.empty:
        _empty_note()
        return
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            x=alt.X("yearmonthdate(Day):T", title=None, axis=_day_axis(data["Day"])),
            y=alt.Y("sum(USD):Q", title="Spend (USD)", axis=alt.Axis(format="$,.0f")),
            color=_stable_color("Category", data["Category"],
                                legend=_legend(wide=True)),
            tooltip=[
                alt.Tooltip("Day:T"),
                alt.Tooltip("Category:N"),
                alt.Tooltip("sum(USD):Q", format="$,.2f", title="Spend"),
            ],
        )
    )
    st.altair_chart(chart, use_container_width=True)
    if takeaway:  # rec35: lead with the conclusion
        _g = data.assign(_v=pd.to_numeric(data["USD"], errors="coerce")).groupby("Category")["_v"].sum()
        if float(_g.sum()) > 0:
            st.caption(_share_note(str(_g.idxmax()), float(_g.max()), float(_g.sum())))


def sparkline_row(items: list[tuple[str, pd.DataFrame, str, str]]) -> None:
    """Row of tiny trend lines: [(label, df, day_col, value_col), ...].
    A KPI without direction is half a number — these add the direction."""
    cols = st.columns(len(items))
    for slot, (label, df, day_col, value_col) in zip(cols, items, strict=True):
        with slot:
            st.caption(label)
            if df is None or getattr(df, "empty", True):
                st.caption("—")  # rec27: one no-value glyph (em-dash)
                continue
            data = df[[day_col, value_col]].copy()
            data.columns = ["Day", "Value"]
            chart = (
                _base(data)
                .mark_area(line={"size": 2}, opacity=0.25)
                .encode(
                    x=alt.X("Day:T", axis=None),
                    y=alt.Y("Value:Q", axis=None),
                    tooltip=["Day:T", "Value:Q"],
                )
                .properties(height=56)
            )
            st.altair_chart(chart, use_container_width=True)


def hour_heatmap(df: pd.DataFrame, row_col: str, hour_col: str, value_col: str,
                 title: str = "", takeaway: bool = True) -> None:
    """Hour-of-day x entity heatmap (e.g. credits burned by warehouse-hour)."""
    data = df[[row_col, hour_col, value_col]].copy()
    data.columns = ["Row", "Hour", "Value"]
    if data.empty:
        _empty_note()
        return
    capped_note = ""
    n_rows = data["Row"].nunique()
    if n_rows > HEATMAP_MAX_ROWS:
        keep = (data.groupby("Row")["Value"].sum()
                .sort_values(ascending=False).head(HEATMAP_MAX_ROWS).index)
        data = data[data["Row"].isin(keep)]
        capped_note = f"Top {HEATMAP_MAX_ROWS} of {n_rows} by total — narrow the scope for the rest."
    chart = (
        _base(data)
        .mark_rect()
        .encode(
            x=alt.X("Hour:O", title="hour of day"),
            y=alt.Y("Row:N", title=None),
            color=alt.Color("Value:Q", title=title or value_col,
                            scale=alt.Scale(range=_HEATMAP_RANGE)),  # rec38: one orange heat ramp
            tooltip=["Row:N", "Hour:O", "Value:Q"],
        )
        .properties(height=max(120, 24 * data["Row"].nunique()))
    )
    st.altair_chart(chart, use_container_width=True)
    if capped_note:
        st.caption(capped_note)
    if takeaway:  # rec35: name the hottest cell (positional + coerced, so a
        # non-unique index or a non-integer Hour can never crash the render)
        _v = pd.to_numeric(data["Value"], errors="coerce").reset_index(drop=True)
        _h = pd.to_numeric(data["Hour"], errors="coerce").reset_index(drop=True)
        _r = data["Row"].reset_index(drop=True)
        if _v.notna().any() and float(_v.max()) > 0:
            _p = int(_v.idxmax())
            if pd.notna(_h.iloc[_p]):
                st.caption(f"Hottest: {_r.iloc[_p]} at hour "
                           f"{int(_h.iloc[_p]):02d} ({float(_v.iloc[_p]):,.0f}).")


def waterfall_usd(df: pd.DataFrame, label_col: str, usd_col: str, top_n: int = 10,
                  takeaway: bool = True) -> None:
    """Attribution waterfall: top-N contributors + Other, cumulative build-up."""
    data = df[[label_col, usd_col]].copy()
    data.columns = ["Label", "USD"]
    data["USD"] = pd.to_numeric(data["USD"], errors="coerce")
    data = data.groupby("Label", as_index=False)["USD"].sum().sort_values("USD", ascending=False)
    if data.empty:
        _empty_note()
        return
    top = data.head(top_n)
    rest = float(data["USD"][top_n:].sum())
    if rest > 0:
        top = pd.concat([top, pd.DataFrame([{"Label": "Other", "USD": rest}])], ignore_index=True)
    top["End"] = top["USD"].cumsum()
    top["Start"] = top["End"] - top["USD"]
    top["Order"] = range(len(top))
    chart = (
        _base(top)
        .mark_bar()
        .encode(
            x=alt.X("Label:N", sort=alt.SortField("Order"), title=None),
            y=alt.Y("Start:Q", title="Cumulative spend (USD)", axis=alt.Axis(format="$,.0f")),
            y2="End:Q",
            tooltip=["Label:N", alt.Tooltip("USD:Q", format="$,.0f"),
                     alt.Tooltip("End:Q", format="$,.0f", title="Cumulative")],
        )
        .properties(height=CHART_H_MD)
    )
    st.altair_chart(chart, use_container_width=True)
    if takeaway:  # rec35: name the top contributor (only when there is a real total)
        _total = float(data["USD"].sum())
        if _total > 0:
            _t = data.iloc[0]
            st.caption(_share_note(str(_t["Label"]), float(_t["USD"]), _total))


def event_timeline(df: pd.DataFrame) -> None:
    """Incident correlation strip: every event type on one time axis."""
    data = df.copy()
    dom = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    rng = [SEV_COLORS[s] for s in dom]
    color = alt.Color("SEVERITY:N", scale=alt.Scale(domain=dom, range=rng),
                      legend=alt.Legend(orient="top", title=None))
    # A2: a redundant SHAPE per severity so the dots stay distinguishable without
    # relying on the red->amber hue axis that red-green color-blindness compresses.
    # Same field + title as the color legend, so Altair merges them into ONE legend
    # showing each severity's shape AND color.
    shape = alt.Shape("SEVERITY:N",
                      scale=alt.Scale(domain=dom,
                                      range=["circle", "diamond", "triangle-up", "square", "cross"]),
                      legend=alt.Legend(orient="top", title=None))
    common = {"x": alt.X("AT:T", title=None), "y": alt.Y("EVENT_TYPE:N", title=None),
              "tooltip": ["AT:T", "EVENT_TYPE:N", "SEVERITY:N", "LABEL:N"]}
    glow = alt.Chart().mark_circle(size=240, opacity=0.16).encode(
        color=alt.Color("SEVERITY:N", scale=alt.Scale(domain=dom, range=rng), legend=None), **common)
    dots = alt.Chart().mark_point(size=110, opacity=0.95, filled=True,
                stroke="#0a0f1c", strokeWidth=0.6).encode(color=color, shape=shape, **common)
    st.altair_chart(alt.layer(glow, dots, data=data).properties(height=186),
                    use_container_width=True)


def daily_metric_line(df: pd.DataFrame, day_col: str, value_col: str,
                      title: str = "", rule_date: object = None) -> None:
    """Single daily metric as a line; optional vertical rule (e.g. change date)."""
    data = df[[day_col, value_col]].copy()
    data.columns = ["Day", "Value"]
    chart = (
        _base(data)
        .mark_line(point=True)
        .encode(
            x=alt.X("Day:T", title=None, axis=_day_axis(data["Day"])),
            y=alt.Y("Value:Q", title=title or value_col),
            tooltip=["Day:T", "Value:Q"],
        )
    )
    if rule_date is not None:
        rule = (
            alt.Chart(pd.DataFrame({"Day": [pd.Timestamp(rule_date)]}))
            .mark_rule(strokeDash=[6, 3])
            .encode(x="Day:T")
        )
        chart = chart + rule
    st.altair_chart(chart.properties(height=CHART_H_SM), use_container_width=True)


def events_by_day(df: pd.DataFrame, day_col: str = "DAY", severity_col: str = "SEVERITY",
                  count_col: str = "EVENTS", takeaway: bool = True) -> None:
    data = df[[day_col, severity_col, count_col]].copy()
    data.columns = ["Day", "Severity", "Events"]
    if data.empty:
        _empty_note()
        return
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            x=alt.X("Day:T", title=None),
            y=alt.Y("sum(Events):Q", title="Alert events"),
            color=alt.Color(
                "Severity:N",
                scale=alt.Scale(
                    # A1: read the ONE shared severity palette, not a divergent copy
                    # (was a hardcoded red+orange a shade off from every other surface)
                    domain=["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                    range=[SEV_COLORS["CRITICAL"], SEV_COLORS["HIGH"],
                           SEV_COLORS["MEDIUM"], SEV_COLORS["LOW"]],
                ),
                legend=_legend(wide=True),
            ),
            tooltip=["Day:T", "Severity:N", alt.Tooltip("sum(Events):Q", title="Events")],
        )
    )
    st.altair_chart(chart, use_container_width=True)
    if takeaway:  # rec35: name the worst day
        _by_day = data.assign(_v=pd.to_numeric(data["Events"], errors="coerce")).groupby("Day")["_v"].sum()
        if float(_by_day.sum()) > 0:
            _dl = pd.to_datetime(_by_day.idxmax(), errors="coerce")
            _ds = _dl.strftime("%b %d") if pd.notna(_dl) else str(_by_day.idxmax())
            st.caption(f"Most events: {_ds} ({float(_by_day.max()):,.0f}).")

def monthly_stacked_usd(df: pd.DataFrame, month_col: str, category_col: str,
                        usd_col: str, partial_month: str = "",
                        top_n: int = 5) -> None:
    """The boss chart: monthly spend stacked by warehouse. The in-flight
    month renders dimmed (partial, not a drop) — same honesty rule as the
    daily spend trend. Top-N categories + "Other" (owner report 2026-07-11:
    the legend clipped past ~8 warehouses; Codex r12 #20: unbounded
    warehouse-month payloads) — totals are unchanged, small movers group."""
    d = df.copy()
    totals = d.groupby(category_col)[usd_col].sum().sort_values(ascending=False)
    if len(totals) > top_n:
        keep = set(totals.head(top_n).index)
        d[category_col] = d[category_col].map(lambda c: c if c in keep else "Other")
        d = d.groupby([month_col, category_col], as_index=False)[usd_col].sum()
    d["_PARTIAL"] = d[month_col].astype(str) == str(partial_month)
    order = list(totals.head(top_n).index) + (["Other"] if len(totals) > top_n else [])
    # C15 contract: color by ENTITY identity (crc32-stable), so a warehouse keeps its
    # color as top-8 membership shifts month to month — the sibling stacked charts
    # already do this. Stack order stays by total (biggest at the base) via an explicit
    # _RANK order, which the color sort used to carry.
    d["_RANK"] = d[category_col].map({c: i for i, c in enumerate(order)}).fillna(len(order))
    bars = (_base(d, 280).mark_bar().encode(
        x=alt.X(f"{month_col}:O", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{usd_col}:Q", title="Spend (USD)", stack="zero"),  # A5: one dollar-axis spelling
        color=_stable_color(category_col, d[category_col],
                            legend=alt.Legend(orient="bottom", title=None,
                                              columns=4, symbolLimit=top_n + 1,
                                              labelLimit=160)),
        order=alt.Order("_RANK:Q"),
        opacity=alt.condition("datum._PARTIAL", alt.value(0.45), alt.value(1.0)),
        tooltip=[alt.Tooltip(f"{month_col}:O"), alt.Tooltip(f"{category_col}:N"),
                 alt.Tooltip(f"{usd_col}:Q", format="$,.0f")],
    ))
    st.altair_chart(bars, use_container_width=True)


def paired_bars(df: pd.DataFrame, label_col: str, a_col: str, b_col: str,
                a_label: str = "A", b_label: str = "B", title: str = "",
                top_n: int = 10, unit: str = "$") -> None:
    """Two-side grouped bars for compare mode: side A in accent, side B
    dimmed gray — the eye reads 'now vs then' without a legend hunt."""
    data = df[[label_col, a_col, b_col]].head(top_n).copy()
    data.columns = ["Label", a_label, b_label]
    folded = data.melt("Label", var_name="Side", value_name="Value")
    chart = (
        alt.Chart(folded)
        .mark_bar()
        .encode(
            x=alt.X("Label:N", sort=None, title=None,
                    axis=alt.Axis(labelAngle=-30, labelLimit=140)),
            xOffset=alt.XOffset("Side:N", sort=[a_label, b_label]),
            # rec41: dollar unit -> format axis + tooltip like every sibling
            # ($,.0f axis, $,.2f tooltip); a non-$ unit keeps the plain format.
            y=alt.Y("Value:Q", title=unit or None,
                    axis=alt.Axis(format="$,.0f") if unit == "$" else alt.Axis()),
            color=alt.Color("Side:N",
                            scale=alt.Scale(domain=[a_label, b_label],
                                            range=[_ACCENT, "#64748b"]),
                            legend=_legend()),  # rec39: 2-series compare reads at the top
            tooltip=["Label:N", "Side:N",
                     alt.Tooltip("Value:Q", format="$,.2f" if unit == "$" else ",.2f")],
        )
        .properties(height=CHART_H_MD, title=title or "")
    )
    st.altair_chart(chart, use_container_width=True)
