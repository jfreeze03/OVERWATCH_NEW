"""Altair charts with executive-grade formatting: dollar axes, tooltips,
budget rule, forecast band. Every chart renders real series or nothing —
callers use components.guard() first."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

_HEIGHT = 264

_ACCENT = "#38bdf8"
_ACCENT2 = "#22d3ee"
_GRID = "rgba(148,163,184,0.14)"
_LABEL = "#8b98ad"
_TITLE = "#c3cddb"
_FONT = ("Inter var, Inter, 'SF Pro Display', -apple-system, BlinkMacSystemFont, "
         "'Segoe UI', Roboto, sans-serif")
# severity palette shared with status_colors, so a "bad" series looks bad
SEV_COLORS = {"CRITICAL": "#fb7185", "HIGH": "#fb923c", "MEDIUM": "#fbbf24",
              "LOW": "#8b98ad", "INFO": "#38bdf8", "OK": "#34d399"}


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
                "category": [_ACCENT, "#34d399", "#c084fc", "#fbbf24", "#fb7185",
                              "#22d3ee", "#a3e635", "#fb923c"],
                "heatmap": ["#0f1729", "#164e63", "#0891b2", "#22d3ee", "#a5f3fc"],
            },
            "bar": {"cornerRadiusEnd": 4, "color": _ACCENT},
            "line": {"color": _ACCENT, "strokeWidth": 2.4},
            "point": {"color": _ACCENT, "filled": True, "size": 42},
            "rule": {"color": _LABEL},
            "area": {"line": True, "opacity": 0.22},
        }
    }


try:  # altair >=5 keeps both registries during the theme API transition
    alt.themes.register("overwatch", _overwatch_theme)
    alt.themes.enable("overwatch")
except Exception:  # noqa: BLE001 - chart theming must never break a page
    pass


def _base(df: pd.DataFrame, height: int | None = None) -> alt.Chart:
    return alt.Chart(df).properties(height=height or _HEIGHT)


def _grad(color: str = _ACCENT):
    """Vertical fade for area fills — the polish that reads as 'product'."""
    return alt.Gradient(
        gradient="linear", x1=1, x2=1, y1=1, y2=0,
        stops=[alt.GradientStop(color=color, offset=0.0),
               alt.GradientStop(color=color, offset=1.0)],
    )


def spend_trend(
    df: pd.DataFrame,
    *,
    day_col: str = "DAY",
    usd_col: str = "USD",
    daily_budget_usd: float = 0.0,
    band: tuple[float, float] | None = None,
) -> None:
    """Daily spend line with optional daily-budget rule and forecast band."""
    data = df[[day_col, usd_col]].copy()
    data.columns = ["Day", "USD"]
    enc_x = alt.X("Day:T", title=None)
    enc_y = alt.Y("USD:Q", title="Spend (USD)", axis=alt.Axis(format="$,.0f"))
    tip = [alt.Tooltip("Day:T"), alt.Tooltip("USD:Q", format="$,.2f", title="Spend")]
    area = (_base(data).mark_area(
                line={"color": _ACCENT, "strokeWidth": 2.4},
                color=alt.Gradient(gradient="linear", x1=1, x2=1, y1=1, y2=0,
                    stops=[alt.GradientStop(color=_ACCENT, offset=0.0),
                           alt.GradientStop(color=_ACCENT, offset=0.0)]),
                opacity=0.16)
            .encode(x=enc_x, y=enc_y, tooltip=tip))
    line = (_base(data).mark_line(point={"filled": True, "size": 34}, strokeWidth=2.4)
            .encode(x=enc_x, y=enc_y, tooltip=tip))
    layers = [area, line]
    if daily_budget_usd and daily_budget_usd > 0:
        rule_df = pd.DataFrame({"y": [daily_budget_usd]})
        layers.append(
            alt.Chart(rule_df)
            .mark_rule(strokeDash=[6, 4], color="#f87171")
            .encode(y="y:Q", tooltip=alt.value(f"Daily budget rate ${daily_budget_usd:,.0f}"))
        )
    if band:
        band_df = pd.DataFrame({"low": [band[0]], "high": [band[1]]})
        layers.append(
            alt.Chart(band_df).mark_rect(opacity=0.08, color="#38bdf8").encode(y="low:Q", y2="high:Q")
        )
    st.altair_chart(alt.layer(*layers), use_container_width=True)


def bar_usd(df: pd.DataFrame, label_col: str, usd_col: str, title: str = "", top_n: int = 10) -> None:
    data = df[[label_col, usd_col]].head(top_n).copy()
    data.columns = ["Label", "USD"]
    grad = alt.Gradient(gradient="linear", x1=0, x2=1, y1=0, y2=0,
                        stops=[alt.GradientStop(color=_ACCENT2, offset=0.0),
                               alt.GradientStop(color=_ACCENT, offset=1.0)])
    enc_y = alt.Y("Label:N", sort="-x", title=None)
    enc_x = alt.X("USD:Q", title=title or "USD", axis=alt.Axis(format="$,.0f"))
    tip = [alt.Tooltip("Label:N"), alt.Tooltip("USD:Q", format="$,.2f")]
    base = _base(data, height=max(_HEIGHT, 30 * len(data)))
    bars = base.mark_bar(color=grad, cornerRadiusEnd=4).encode(y=enc_y, x=enc_x, tooltip=tip)
    labels = base.mark_text(align="left", dx=5, color=_LABEL, fontSize=11).encode(
        y=enc_y, x=enc_x, text=alt.Text("USD:Q", format="$,.0f"))
    st.altair_chart(bars + labels, use_container_width=True)


def bar_count(df: pd.DataFrame, label_col: str, value_col: str, title: str = "", top_n: int = 10) -> None:
    data = df[[label_col, value_col]].head(top_n).copy()
    data.columns = ["Label", "Value"]
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            y=alt.Y("Label:N", sort="-x", title=None),
            x=alt.X("Value:Q", title=title or "Count", axis=alt.Axis(format=",.0f")),
            tooltip=[alt.Tooltip("Label:N"), alt.Tooltip("Value:Q", format=",.0f")],
        )
    )
    st.altair_chart(chart, use_container_width=True)


def daily_stacked_usd(df: pd.DataFrame, day_col: str, category_col: str, usd_col: str) -> None:
    data = df[[day_col, category_col, usd_col]].copy()
    data.columns = ["Day", "Category", "USD"]
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            x=alt.X("Day:T", title=None),
            y=alt.Y("sum(USD):Q", title="Spend (USD)", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("Category:N", legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[
                alt.Tooltip("Day:T"),
                alt.Tooltip("Category:N"),
                alt.Tooltip("sum(USD):Q", format="$,.2f", title="Spend"),
            ],
        )
    )
    st.altair_chart(chart, use_container_width=True)


def sparkline_row(items: list[tuple[str, pd.DataFrame, str, str]]) -> None:
    """Row of tiny trend lines: [(label, df, day_col, value_col), ...].
    A KPI without direction is half a number — these add the direction."""
    cols = st.columns(len(items))
    for slot, (label, df, day_col, value_col) in zip(cols, items, strict=True):
        with slot:
            st.caption(label)
            if df is None or getattr(df, "empty", True):
                st.caption("–")
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
                 title: str = "") -> None:
    """Hour-of-day x entity heatmap (e.g. credits burned by warehouse-hour)."""
    data = df[[row_col, hour_col, value_col]].copy()
    data.columns = ["Row", "Hour", "Value"]
    chart = (
        _base(data)
        .mark_rect()
        .encode(
            x=alt.X("Hour:O", title="hour of day"),
            y=alt.Y("Row:N", title=None),
            color=alt.Color("Value:Q", title=title or value_col,
                            scale=alt.Scale(scheme="orangered")),
            tooltip=["Row:N", "Hour:O", "Value:Q"],
        )
        .properties(height=max(120, 24 * data["Row"].nunique()))
    )
    st.altair_chart(chart, use_container_width=True)


def waterfall_usd(df: pd.DataFrame, label_col: str, usd_col: str, top_n: int = 10) -> None:
    """Attribution waterfall: top-N contributors + Other, cumulative build-up."""
    data = df[[label_col, usd_col]].copy()
    data.columns = ["Label", "USD"]
    data = data.groupby("Label", as_index=False)["USD"].sum().sort_values("USD", ascending=False)
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
        .properties(height=260)
    )
    st.altair_chart(chart, use_container_width=True)


def event_timeline(df: pd.DataFrame) -> None:
    """Incident correlation strip: every event type on one time axis."""
    data = df.copy()
    dom = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    rng = [SEV_COLORS[s] for s in dom]
    color = alt.Color("SEVERITY:N", scale=alt.Scale(domain=dom, range=rng),
                      legend=alt.Legend(orient="top", title=None))
    common = dict(x=alt.X("AT:T", title=None), y=alt.Y("EVENT_TYPE:N", title=None),
                  tooltip=["AT:T", "EVENT_TYPE:N", "SEVERITY:N", "LABEL:N"])
    glow = _base(data, height=186).mark_circle(size=240, opacity=0.16).encode(color=color, **common)
    dots = _base(data, height=186).mark_circle(size=90, opacity=0.95,
                stroke="#0a0f1c", strokeWidth=0.6).encode(color=color, **common)
    st.altair_chart(glow + dots, use_container_width=True)


def daily_metric_line(df: pd.DataFrame, day_col: str, value_col: str,
                      title: str = "", rule_date: object = None) -> None:
    """Single daily metric as a line; optional vertical rule (e.g. change date)."""
    data = df[[day_col, value_col]].copy()
    data.columns = ["Day", "Value"]
    chart = (
        _base(data)
        .mark_line(point=True)
        .encode(
            x=alt.X("Day:T", title=None),
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
    st.altair_chart(chart.properties(height=220), use_container_width=True)


def events_by_day(df: pd.DataFrame, day_col: str = "DAY", severity_col: str = "SEVERITY", count_col: str = "EVENTS") -> None:
    data = df[[day_col, severity_col, count_col]].copy()
    data.columns = ["Day", "Severity", "Events"]
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            x=alt.X("Day:T", title=None),
            y=alt.Y("sum(Events):Q", title="Alert events"),
            color=alt.Color(
                "Severity:N",
                scale=alt.Scale(
                    domain=["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                    range=["#ef4444", "#f97316", "#eab308", "#64748b"],
                ),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=["Day:T", "Severity:N", alt.Tooltip("sum(Events):Q", title="Events")],
        )
    )
    st.altair_chart(chart, use_container_width=True)
