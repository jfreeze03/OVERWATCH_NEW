"""Altair charts with executive-grade formatting: dollar axes, tooltips,
budget rule, forecast band. Every chart renders real series or nothing —
callers use components.guard() first."""

from __future__ import annotations

import html
import zlib
from collections import defaultdict

import altair as alt
import pandas as pd
import streamlit as st

from app.logic.task_graph import TaskGraphShape, canonical_task_name
from app.ui import palette
from app.ui.sizing import CHART_H_MD, CHART_H_SM

_HEIGHT = CHART_H_MD
HEATMAP_MAX_ROWS = 20  # 24px/row; beyond this the heatmap became a scroll trap

_ACCENT = palette.ACCENT
_ACCENT2 = palette.ACCENT2
_GRID = "rgba(148,163,184,0.14)"
_LABEL = palette.LOW
_TITLE = palette.INK_SOFT
_FONT = ("Inter var, Inter, 'SF Pro Display', -apple-system, BlinkMacSystemFont, "
         "'Segoe UI', Roboto, sans-serif")
# severity series hues from the single palette source (rec50), so a "bad"
# series looks bad the same way it does on every card and chip.
SEV_COLORS = dict(palette.SEVERITY_HUES)

# rec38: heat = orange (intuitive "hotness" for the hour x entity heatmap). ONE
# ramp, referenced by the theme range AND hour_heatmap, so it is not a one-off
# scheme string a shade off from everything else.
# Starts at a visible dark-orange (NOT the page background) so a low-but-
# NONZERO cell separates from an EMPTY (undrawn) cell, which stays page-dark —
# the caller's "dark cells = no activity" reading depends on that separation.
_HEATMAP_RANGE = ["#451a03", "#78350f", "#b45309", "#d97706", "#fcd34d"]


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


def _magnitude_usd(value: float) -> str:
    """Whole dollars normally, cents only when the value is sub-dollar (would
    otherwise round to '$0'). v4.157.0: the Cortex/AI spend takeaway read
    'Top: X $0 (58% of $0)' because whole-dollar rounding flattened a pennies
    total; keep the cents where the number actually lives in cents."""
    return f"${value:,.2f}" if 0 < abs(value) < 1 else f"${value:,.0f}"


def _share_note(label: str, amount: float, total: float, *, dollars: bool = True) -> str:
    """rec35 helper: 'Top: X $Y (Z% of $total).' — the lead-with-the-conclusion
    line, computed from the data the chart already has. The share is omitted when
    it would be nonsensical (categories that net out negative make a positive top
    exceed 100% of the total)."""
    a = _magnitude_usd(amount) if dollars else f"{amount:,.0f}"
    share = (amount / total * 100) if total else 0.0
    if total > 0 and 0 <= share <= 100:
        t = _magnitude_usd(total) if dollars else f"{total:,.0f}"
        return f"Top: {label} {a} ({share:.0f}% of {t})."
    return f"Top: {label} {a}."


def _task_node_style(row: pd.Series) -> tuple[str, str]:
    try:
        failures = int(float(row.get("RECENT_FAILURES") or 0))
    except (TypeError, ValueError, OverflowError):
        failures = 0
    state = str(row.get("RUN_STATE") or row.get("STATE") or "").lower()
    if state == "failed":
        return palette.BAD, "failed in selected run"
    if failures:
        return palette.BAD, f"{failures} failed recently"
    critical_path = str(row.get("CRITICAL_PATH") or "").lower() == "true"
    if critical_path:
        duration = pd.to_numeric(row.get("RUN_SEC"), errors="coerce")
        suffix = f" · {float(duration):,.1f}s" if pd.notna(duration) else ""
        return palette.ACCENT, f"critical path{suffix}"
    if "suspend" in state:
        return palette.MUTED, "suspended"
    return palette.OK, "healthy"


def _dot_escape(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def task_dag_dot(df: pd.DataFrame, shape: TaskGraphShape) -> str:
    """Graphviz fallback for a previously validated coherent task snapshot."""
    lookup = {
        canonical_task_name(row.get("TASK_FQN")): row
        for _, row in df.iterrows()
        if canonical_task_name(row.get("TASK_FQN"))
    }
    lines = [
        "digraph tasks {",
        'bgcolor="transparent"; rankdir=LR; nodesep=0.32; ranksep=0.65;',
        f'graph [fontcolor="{palette.INK}", pad=0.25];',
        (f'node [shape=box, style="rounded,filled", fontsize=10, '
         f'fontcolor="{palette.INK}", fillcolor="{palette.RAISED}"];'),
        f'edge [color="{palette.INK_MUTE}", arrowsize=0.7];',
    ]
    for canonical, row in lookup.items():
        fqn = str(row.get("TASK_FQN") or canonical)
        parts = fqn.split(".")
        label = ".".join(parts[-2:]) if len(parts) >= 2 else fqn
        color, status = _task_node_style(row)
        tooltip = " | ".join(
            value
            for value in (
                fqn,
                status,
                str(row.get("WAREHOUSE_NAME") or "serverless"),
                str(row.get("SCHEDULE") or "triggered"),
            )
            if value
        )
        lines.append(
            f'"{_dot_escape(canonical)}" [label="{_dot_escape(label)}", '
            f'color="{color}", penwidth=2, tooltip="{_dot_escape(tooltip)}"];'
        )
    for predecessor, child in shape.edges:
        lines.append(f'"{_dot_escape(predecessor)}" -> "{_dot_escape(child)}";')
    lines.append("}")
    return "\n".join(lines)


def _task_dag_markup(df: pd.DataFrame, shape: TaskGraphShape, *, height: int) -> str:
    """Build a self-contained SVG task graph with pan/zoom controls."""
    lookup = {
        canonical_task_name(row.get("TASK_FQN")): row
        for _, row in df.iterrows()
        if canonical_task_name(row.get("TASK_FQN"))
    }
    levels = dict(shape.levels)
    by_level: dict[int, list[str]] = defaultdict(list)
    for name in lookup:
        by_level[levels.get(name, 0)].append(name)
    for names in by_level.values():
        names.sort(key=lambda name: str(lookup[name].get("TASK_FQN") or name))

    node_w, node_h = 238, 76
    x_gap, y_gap = 112, 26
    max_rows = max((len(names) for names in by_level.values()), default=1)
    max_level = max(by_level, default=0)
    graph_width = max(760, 56 + (max_level + 1) * (node_w + x_gap))
    graph_height = max(500, 56 + max_rows * (node_h + y_gap))
    positions: dict[str, tuple[float, float]] = {}
    for level, names in sorted(by_level.items()):
        offset = (max_rows - len(names)) * (node_h + y_gap) / 2
        for index, name in enumerate(names):
            positions[name] = (
                32 + level * (node_w + x_gap),
                28 + offset + index * (node_h + y_gap),
            )

    edge_markup: list[str] = []
    for predecessor, child in shape.edges:
        if predecessor not in positions or child not in positions:
            continue
        x1, y1 = positions[predecessor]
        x2, y2 = positions[child]
        start_x, start_y = x1 + node_w, y1 + node_h / 2
        end_x, end_y = x2, y2 + node_h / 2
        bend = max(42.0, (end_x - start_x) * 0.48)
        edge_markup.append(
            f'<path class="edge" d="M {start_x:.1f} {start_y:.1f} '
            f'C {start_x + bend:.1f} {start_y:.1f}, '
            f'{end_x - bend:.1f} {end_y:.1f}, {end_x:.1f} {end_y:.1f}"/>'
        )

    node_markup: list[str] = []
    for name, (x, y) in positions.items():
        row = lookup[name]
        fqn = str(row.get("TASK_FQN") or name)
        parts = fqn.split(".")
        task_label = parts[-1]
        parent_label = ".".join(parts[-3:-1]) if len(parts) >= 3 else ".".join(parts[:-1])
        task_display = task_label if len(task_label) <= 31 else task_label[:28] + "..."
        parent_display = parent_label if len(parent_label) <= 38 else "..." + parent_label[-35:]
        color, status = _task_node_style(row)
        details = " | ".join(
            value
            for value in (
                fqn,
                status,
                f"warehouse: {row.get('WAREHOUSE_NAME') or 'serverless'}",
                f"schedule: {row.get('SCHEDULE') or 'triggered'}",
                (f"selected run: {float(row.get('RUN_SEC')):,.1f}s"
                 if pd.notna(pd.to_numeric(row.get("RUN_SEC"), errors="coerce")) else ""),
            )
            if value
        )
        aria = html.escape(details, quote=True)
        critical_path = str(row.get("CRITICAL_PATH") or "").lower() == "true"
        node_class = "node critical-path" if critical_path else "node"
        search_text = html.escape(fqn.lower(), quote=True)
        node_markup.append(
            f'<g class="{node_class}" transform="translate({x:.1f} {y:.1f})" tabindex="0" '
            f'data-name="{search_text}" data-x="{x:.1f}" data-y="{y:.1f}" '
            f'role="group" aria-label="{aria}">'
            f'<title>{html.escape(details)}</title>'
            f'<rect width="{node_w}" height="{node_h}" rx="6" '
            f'style="--node-color:{color}"/>'
            f'<text class="node-title" x="14" y="24">{html.escape(task_display)}</text>'
            f'<text class="node-meta" x="14" y="44">{html.escape(parent_display)}</text>'
            f'<text class="node-state" x="{node_w - 12}" y="64" text-anchor="end" '
            f'style="fill:{color}">{html.escape(status)}</text>'
            "</g>"
        )

    return (
        _TASK_DAG_TEMPLATE.replace("__HEIGHT__", str(int(height)))
        .replace("__GRAPH_WIDTH__", str(int(graph_width)))
        .replace("__GRAPH_HEIGHT__", str(int(graph_height)))
        .replace("__EDGES__", "".join(edge_markup))
        .replace("__NODES__", "".join(node_markup))
    )


def interactive_task_dag(
    df: pd.DataFrame, shape: TaskGraphShape, *, height: int = 680
) -> bool:
    """Render the dependency-free pan/zoom SVG; return False for fallback."""
    if df is None or df.empty:
        return False
    try:
        from streamlit.components.v1 import html as component_html

        component_html(
            _task_dag_markup(df, shape, height=height),
            height=height + 2,
            scrolling=False,
        )
        return True
    except Exception:  # noqa: BLE001 - native Graphviz remains the core-value fallback
        return False


_TASK_DAG_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
html, body {
  margin: 0; width: 100%; height: 100%; overflow: hidden; background: transparent;
}
body { font-family: Inter, "Segoe UI", Arial, sans-serif; }
.dag-host {
  position: relative; width: 100%; height: __HEIGHT__px; overflow: hidden;
  background: #111827; border: 1px solid rgba(148,163,184,.28); border-radius: 6px;
}
.dag-host:fullscreen { width: 100vw; height: 100vh; border: 0; border-radius: 0; }
.dag-host:fullscreen svg { height: 100vh; }
.dag-toolbar {
  position: absolute; z-index: 4; top: 10px; right: 10px; display: flex; gap: 5px;
  padding: 5px; background: rgba(15,23,41,.94); border: 1px solid rgba(148,163,184,.28);
  border-radius: 6px; box-shadow: 0 6px 20px rgba(0,0,0,.35);
}
.dag-toolbar input {
  width: 190px; height: 32px; padding: 0 9px; border: 1px solid rgba(148,163,184,.32);
  border-radius: 5px; background: #111827; color: #f8fafc; font: 500 12px Inter, sans-serif;
}
.dag-toolbar input:focus-visible { outline: 2px solid #60a5fa; outline-offset: 1px; }
.dag-toolbar button {
  min-width: 34px; height: 32px; padding: 0 9px; border: 1px solid rgba(148,163,184,.32);
  border-radius: 5px; background: #1f2937; color: #f8fafc;
  font: 650 13px/1 Inter, "Segoe UI", sans-serif; cursor: pointer;
}
.dag-toolbar button:hover { border-color: #60a5fa; background: #243244; }
.dag-toolbar button:focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
svg {
  width: 100%; height: __HEIGHT__px; display: block; cursor: grab;
  touch-action: none; user-select: none;
}
svg.dragging { cursor: grabbing; }
.edge {
  fill: none; stroke: #94a3b8; stroke-width: 1.5; opacity: .72;
  marker-end: url(#arrow);
}
.node rect {
  fill: #1f2937; stroke: var(--node-color); stroke-width: 2;
  filter: url(#shadow);
}
.node:focus { outline: none; }
.node:focus rect { stroke-width: 4; }
.node.search-hit rect { stroke: #f8fafc; stroke-width: 5; }
.node.critical-path rect { stroke-dasharray: 7 3; }
.node-title { fill: #f8fafc; font-size: 13px; font-weight: 700; }
.node-meta { fill: #cbd5e1; font-size: 10px; }
.node-state { font-size: 9px; font-weight: 700; text-transform: uppercase; }
.dag-host.compact .node-meta, .dag-host.compact .node-state { display: none; }
.dag-host.tiny .node-title { display: none; }
</style>
</head>
<body>
<div class="dag-host" id="host">
  <div class="dag-toolbar" role="toolbar" aria-label="Task graph controls">
    <input id="taskSearch" type="search" placeholder="Find task" aria-label="Find task">
    <button id="find" title="Find next task" aria-label="Find next task">Find</button>
    <button id="zoomIn" title="Zoom in" aria-label="Zoom in">+</button>
    <button id="zoomOut" title="Zoom out" aria-label="Zoom out">-</button>
    <button id="fit" title="Fit graph" aria-label="Fit graph">Fit</button>
    <button id="full" title="Full screen" aria-label="Full screen">Full</button>
    <button id="download" title="Download SVG" aria-label="Download SVG">SVG</button>
  </div>
  <svg id="dag" tabindex="0" role="img"
       aria-label="Task dependency graph. Use mouse wheel or plus and minus to zoom; drag to pan.">
    <defs>
      <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3"
              orient="auto" markerUnits="strokeWidth">
        <path d="M0,0 L0,6 L8,3 z" fill="#94a3b8"/>
      </marker>
      <filter id="shadow" x="-20%" y="-30%" width="140%" height="160%">
        <feDropShadow dx="0" dy="3" stdDeviation="3" flood-color="#000"
                      flood-opacity=".32"/>
      </filter>
    </defs>
    <g id="scene">__EDGES____NODES__</g>
  </svg>
</div>
<script>
(() => {
  const host = document.getElementById("host");
  const svg = document.getElementById("dag");
  const scene = document.getElementById("scene");
  const graphWidth = __GRAPH_WIDTH__;
  const graphHeight = __GRAPH_HEIGHT__;
  let scale = 1;
  let tx = 0;
  let ty = 0;
  let dragging = false;
  let px = 0;
  let py = 0;
  let searchIndex = -1;
  // The 2,000-node safety cap can still produce a very tall graph. Keep the
  // minimum low enough that Fit always means fit, then let users zoom back in.
  const clamp = value => Math.max(0.002, Math.min(4.5, value));
  const apply = () => {
    scene.setAttribute("transform", `translate(${tx} ${ty}) scale(${scale})`);
    host.classList.toggle("compact", scale < 0.48);
    host.classList.toggle("tiny", scale < 0.16);
  };
  const fit = () => {
    const width = Math.max(svg.clientWidth, 1);
    const height = Math.max(svg.clientHeight, 1);
    scale = clamp(Math.min((width - 44) / graphWidth, (height - 44) / graphHeight));
    tx = (width - graphWidth * scale) / 2;
    ty = (height - graphHeight * scale) / 2;
    apply();
  };
  const zoomAt = (factor, x = svg.clientWidth / 2, y = svg.clientHeight / 2) => {
    const next = clamp(scale * factor);
    const ratio = next / scale;
    tx = x - (x - tx) * ratio;
    ty = y - (y - ty) * ratio;
    scale = next;
    apply();
  };
  const graphNodes = Array.from(scene.querySelectorAll(".node"));
  const focusNode = node => {
    const x = Number(node.dataset.x) + 119;
    const y = Number(node.dataset.y) + 38;
    scale = Math.max(scale, 1.05);
    tx = svg.clientWidth / 2 - x * scale;
    ty = svg.clientHeight / 2 - y * scale;
    graphNodes.forEach(item => item.classList.remove("search-hit"));
    node.classList.add("search-hit");
    node.focus({preventScroll: true});
    apply();
  };
  const findTask = () => {
    const query = document.getElementById("taskSearch").value.trim().toLowerCase();
    if (!query) return;
    const matches = graphNodes.filter(node => (node.dataset.name || "").includes(query));
    if (!matches.length) {
      document.getElementById("taskSearch").setCustomValidity("No matching task");
      document.getElementById("taskSearch").reportValidity();
      return;
    }
    document.getElementById("taskSearch").setCustomValidity("");
    searchIndex = (searchIndex + 1) % matches.length;
    focusNode(matches[searchIndex]);
  };
  document.getElementById("find").addEventListener("click", findTask);
  document.getElementById("taskSearch").addEventListener("input", () => { searchIndex = -1; });
  document.getElementById("taskSearch").addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); findTask(); }
  });
  graphNodes.forEach(node => node.addEventListener("click", event => {
    event.stopPropagation();
    focusNode(node);
  }));
  document.getElementById("zoomIn").addEventListener("click", () => zoomAt(1.25));
  document.getElementById("zoomOut").addEventListener("click", () => zoomAt(0.8));
  document.getElementById("fit").addEventListener("click", fit);
  document.getElementById("full").addEventListener("click", async () => {
    if (!document.fullscreenElement && host.requestFullscreen) {
      await host.requestFullscreen();
    } else if (document.exitFullscreen) {
      await document.exitFullscreen();
    }
  });
  document.addEventListener("fullscreenchange", () => setTimeout(fit, 60));
  document.getElementById("download").addEventListener("click", () => {
    const clone = svg.cloneNode(true);
    const cloneScene = clone.querySelector("#scene");
    cloneScene.removeAttribute("transform");
    clone.setAttribute("viewBox", `0 0 ${graphWidth} ${graphHeight}`);
    clone.setAttribute("width", graphWidth);
    clone.setAttribute("height", graphHeight);
    const source = new XMLSerializer().serializeToString(clone);
    const blob = new Blob([source], {type: "image/svg+xml"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "overwatch-task-graph.svg";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  svg.addEventListener("wheel", event => {
    event.preventDefault();
    const rect = svg.getBoundingClientRect();
    zoomAt(
      event.deltaY < 0 ? 1.12 : 0.89,
      event.clientX - rect.left,
      event.clientY - rect.top
    );
  }, {passive: false});
  svg.addEventListener("pointerdown", event => {
    dragging = true;
    px = event.clientX;
    py = event.clientY;
    svg.classList.add("dragging");
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", event => {
    if (!dragging) return;
    tx += event.clientX - px;
    ty += event.clientY - py;
    px = event.clientX;
    py = event.clientY;
    apply();
  });
  const stop = event => {
    dragging = false;
    svg.classList.remove("dragging");
    if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
  };
  svg.addEventListener("pointerup", stop);
  svg.addEventListener("pointercancel", stop);
  svg.addEventListener("dblclick", fit);
  svg.addEventListener("keydown", event => {
    if (event.key === "+" || event.key === "=") zoomAt(1.2);
    else if (event.key === "-") zoomAt(0.83);
    else if (event.key === "0") fit();
    else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      const step = 42;
      if (event.key === "ArrowLeft") tx += step;
      if (event.key === "ArrowRight") tx -= step;
      if (event.key === "ArrowUp") ty += step;
      if (event.key === "ArrowDown") ty -= step;
      apply();
    }
  });
  requestAnimationFrame(fit);
})();
</script>
</body>
</html>"""


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
    avg = (alt.Chart().mark_line(color=palette.INK_SOFT, strokeWidth=2, interpolate="monotone")
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

def bar_usd(df: pd.DataFrame, label_col: str, usd_col: str, title: str = "", top_n: int = 10,
            *, takeaway: bool = False) -> None:
    data = df[[label_col, usd_col]].head(top_n).copy()
    data.columns = ["Label", "USD"]
    data["USD"] = pd.to_numeric(data["USD"], errors="coerce").fillna(0.0)
    if data.empty:
        _empty_note()
        return
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
    if takeaway and float(data["USD"].sum()) > 0:
        top = data.loc[data["USD"].idxmax()]
        st.caption(_share_note(str(top["Label"]), float(top["USD"]),
                               float(data["USD"].sum())))


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


def bar_count(df: pd.DataFrame, label_col: str, value_col: str, title: str = "", top_n: int = 10,
              *, takeaway: bool = False) -> None:
    data = df[[label_col, value_col]].head(top_n).copy()
    data.columns = ["Label", "Value"]
    data["Value"] = pd.to_numeric(data["Value"], errors="coerce").fillna(0.0)
    if data.empty:
        _empty_note()
        return
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
    if takeaway and float(data["Value"].sum()) > 0:
        top = data.loc[data["Value"].idxmax()]
        st.caption(_share_note(str(top["Label"]), float(top["Value"]),
                               float(data["Value"].sum()), dollars=False))


def daily_stacked_usd(df: pd.DataFrame, day_col: str, category_col: str, usd_col: str,
                      takeaway: bool = True) -> None:
    data = df[[day_col, category_col, usd_col]].copy()
    data.columns = ["Day", "Category", "USD"]
    if data.empty:
        _empty_note()
        return
    # v4.157.0: magnitude-aware axis — a genuinely sub-dollar stacked total would
    # otherwise draw tall auto-scaled bars against an all-"$0" whole-dollar axis
    # (the Cortex/AI spend "signal but $0" report). Show cents only when the
    # biggest day's stack is sub-dollar (else whole-dollar ticks stay legible).
    _max_stack = float(
        pd.to_numeric(data["USD"], errors="coerce").groupby(data["Day"]).sum().max() or 0.0
    )
    _yfmt = "$,.2f" if 0 < _max_stack < 1 else "$,.0f"
    chart = (
        _base(data)
        .mark_bar()
        .encode(
            x=alt.X("yearmonthdate(Day):T", title=None, axis=_day_axis(data["Day"])),
            y=alt.Y("sum(USD):Q", title="Spend (USD)", axis=alt.Axis(format=_yfmt)),
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
                stroke=palette.BG, strokeWidth=0.6).encode(color=color, shape=shape, **common)
    st.altair_chart(alt.layer(glow, dots, data=data).properties(height=186),
                    use_container_width=True)


def operational_replay(df: pd.DataFrame, credits: pd.DataFrame | None = None) -> None:
    """Multi-lane event replay with a persistent overview brush.

    CR9: when ``credits`` (columns HOUR_TS + USD) is supplied, an hourly-spend
    bar panel is layered above the events on ONE explicit shared time domain, so
    a cost spike and the events around it line up on first paint (the brush-zoom
    is reserved for the no-overlay view). HOUR_TS must already be in the same
    display timezone as the events' AT column (the caller localizes both).
    """
    data = df.copy()
    if data is None or data.empty:
        _empty_note("No operational events to replay in this window.")
        return
    data["AT"] = pd.to_datetime(data["AT"], errors="coerce")
    data = data.dropna(subset=["AT"])
    if data.empty:
        _empty_note("No timestamped operational events to replay in this window.")
        return
    domain = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    colors = [SEV_COLORS[level] for level in domain]
    shapes = ["circle", "diamond", "triangle-up", "square", "cross"]
    tooltip = [
        alt.Tooltip("AT:T", title="At"),
        alt.Tooltip("EVENT_TYPE:N", title="Lane"),
        alt.Tooltip("SEVERITY:N", title="Severity"),
        alt.Tooltip("LABEL:N", title="Event"),
        alt.Tooltip("REF_ID:N", title="Reference"),
    ]

    def _focus(xscale):
        return (
            alt.Chart(data)
            .mark_point(size=125, filled=True, stroke=palette.BG, strokeWidth=0.7)
            .encode(
                x=alt.X("AT:T", title=None, scale=xscale),
                y=alt.Y("EVENT_TYPE:N", title=None, sort="-x"),
                color=alt.Color(
                    "SEVERITY:N", scale=alt.Scale(domain=domain, range=colors),
                    legend=alt.Legend(orient="top", title=None),
                ),
                shape=alt.Shape(
                    "SEVERITY:N", scale=alt.Scale(domain=domain, range=shapes),
                    legend=alt.Legend(orient="top", title=None),
                ),
                tooltip=tooltip,
            )
            .properties(height=225)
        )

    # CR9: an optional hourly-spend series overlays as a panel above the events.
    credit_data = None
    if credits is not None and not getattr(credits, "empty", True):
        cd = credits.copy()
        cd["HOUR_TS"] = pd.to_datetime(cd.get("HOUR_TS"), errors="coerce")
        cd = cd.dropna(subset=["HOUR_TS"])
        if not cd.empty and "USD" in cd.columns:
            credit_data = cd

    if credit_data is not None:
        # Alignment: alt.vconcat resolves x scales INDEPENDENTLY per panel, and an
        # empty brush would fall back to each panel's OWN data extent — dense
        # credits span the whole window, sparse events a narrower range, so the
        # spend would not sit above the event at the same instant until the user
        # brushed. Pin both panels to ONE explicit shared domain so they align on
        # first paint. A bar mark (not area) leaves idle hours as gaps instead of
        # interpolating filled spend across hours that metered nothing.
        span = pd.concat([data["AT"], credit_data["HOUR_TS"]])
        xdom = alt.Scale(domain=[span.min().isoformat(), span.max().isoformat()])
        spend = (
            alt.Chart(credit_data)
            .mark_bar(color=palette.ACCENT, opacity=0.55)
            .encode(
                x=alt.X("HOUR_TS:T", title=None, scale=xdom),
                y=alt.Y("USD:Q", title="Spend $/hr", axis=alt.Axis(format="$,.0f")),
                tooltip=[alt.Tooltip("HOUR_TS:T", title="Hour"),
                         alt.Tooltip("USD:Q", title="Spend", format="$,.2f")],
            )
            .properties(height=70)
        )
        focus = _focus(xdom)
        st.altair_chart(alt.vconcat(spend, focus, spacing=8), use_container_width=True)
        return

    # No overlay: the persistent overview brush zooms the event focus.
    brush = alt.selection_interval(encodings=["x"], name="replay_window")
    focus = _focus(alt.Scale(domain=brush))
    overview = (
        alt.Chart(data)
        .mark_tick(thickness=2, size=24)
        .encode(
            x=alt.X("AT:T", title=None),
            color=alt.Color(
                "SEVERITY:N", scale=alt.Scale(domain=domain, range=colors), legend=None,
            ),
            tooltip=tooltip,
        )
        .add_params(brush)
        .properties(height=48)
    )
    st.altair_chart(alt.vconcat(focus, overview, spacing=8), use_container_width=True)


def workload_portfolio(df: pd.DataFrame) -> None:
    """Impact x confidence portfolio; size is the measured blast-radius proxy."""
    if df is None or df.empty:
        _empty_note("No workloads qualify for the portfolio.")
        return
    data = df.copy()
    for column in ("IMPACT_USD_30D", "CONFIDENCE", "BLAST_RADIUS", "PRIORITY_SCORE"):
        data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0.0)
    lane_domain = ["ACT NOW", "PLAN", "VALIDATE"]
    lane_colors = [palette.BAD, palette.ACCENT, palette.WARN]
    chart = (
        alt.Chart(data)
        .mark_circle(opacity=0.82, stroke=palette.BG, strokeWidth=0.8)
        .encode(
            x=alt.X(
                "IMPACT_USD_30D:Q", title="Measured impact normalized to 30d (USD)",
                axis=alt.Axis(format="$,.0f"), scale=alt.Scale(zero=True),
            ),
            y=alt.Y("CONFIDENCE:Q", title="Evidence confidence", scale=alt.Scale(domain=[0, 1])),
            size=alt.Size(
                "BLAST_RADIUS:Q", title="Users + databases", scale=alt.Scale(range=[70, 780]),
            ),
            color=alt.Color(
                "LANE:N", scale=alt.Scale(domain=lane_domain, range=lane_colors),
                legend=alt.Legend(orient="top", title=None),
            ),
            tooltip=[
                alt.Tooltip("FINGERPRINT:N", title="Fingerprint"),
                alt.Tooltip("LANE:N", title="Lane"),
                alt.Tooltip("IMPACT_USD_30D:Q", title="Impact / 30d", format="$,.2f"),
                alt.Tooltip("CONFIDENCE:Q", title="Confidence", format=".0%"),
                alt.Tooltip("PRIORITY_SCORE:Q", title="Priority", format=",.2f"),
                alt.Tooltip("NEXT_MOVE:N", title="Next move"),
            ],
        )
        .properties(height=330)
    )
    st.altair_chart(chart, use_container_width=True)


def daily_metric_line(df: pd.DataFrame, day_col: str, value_col: str,
                      title: str = "", rule_date: object = None,
                      *, takeaway: bool = True) -> None:
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
    # rec35 / CoCo UI#14: name the peak day so the line leads with a conclusion.
    if takeaway and not data.empty:
        _v = pd.to_numeric(data["Value"], errors="coerce").dropna()
        if not _v.empty and float(_v.max()) > 0:
            _peak = float(_v.max())
            _pday = pd.to_datetime(data.loc[_v.idxmax(), "Day"], errors="coerce")
            _ds = _pday.strftime("%b %d") if pd.notna(_pday) else str(data.loc[_v.idxmax(), "Day"])
            _pf = f"{_peak:,.0f}" if abs(_peak) >= 100 else f"{_peak:,.1f}"
            st.caption(f"Peak {_pf} on {_ds}.")


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
                        top_n: int = 5, *, takeaway: bool = True) -> None:
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
    # rec35 / CoCo UI#14: lead the boss chart with its conclusion — the top spender.
    if takeaway and float(totals.sum()) > 0:
        st.caption(_share_note(str(totals.index[0]), float(totals.iloc[0]), float(totals.sum())))


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
