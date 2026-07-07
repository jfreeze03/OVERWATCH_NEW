"""Shared UI components. Honest states only: real data, labeled errors,
labeled empties, visible truncation. No synthetic fallbacks — ever."""

from __future__ import annotations

import html

import streamlit as st

from app.config import ACCOUNT_USAGE_LAG_NOTE, DEFAULT_SETTINGS
from app.core.result import QueryResult
from app.data import mart_sql
from app.logic.formulas import format_usd, safe_float
from app.theme import chip
from app.ui.status_colors import status_columns_in, status_css


def _scope_chip_html() -> str:
    """Active scope as chips so context survives scrolling past the top bar."""
    try:
        from app.core.state import filters  # local import: avoid module cycle

        f = filters()
    except Exception:  # noqa: BLE001 - header chrome must never break a page
        return ""
    chips = [chip(html.escape(f["company"]), "ok"), chip(f"{f['days']}d")]
    if f["environment"] != "ALL":
        chips.append(chip(html.escape(f["environment"])))
    for key, label in (("database", ""), ("schema_contains", "schema~"),
                        ("warehouse_contains", "wh~"), ("user_contains", "user~")):
        value = str(f.get(key) or "").strip()
        if value:
            chips.append(chip(html.escape(f"{label}{value}")))
    return "".join(chips)


def panel_help(text: str) -> None:
    """Per-panel 'what is this / when red do X' popover (help mode)."""
    with st.popover("ⓘ about this panel", use_container_width=False):
        st.markdown(text)


def lazy_sections(labels: list[str], key: str) -> str:
    """Tab-style navigation that renders ONLY the selected section.

    st.tabs executes every tab body on every rerun and merely hides the
    output — an 8-tab page fires every tab's queries to paint one. This
    pill radio keeps the navigation but lets the page dispatch a single
    section, so first paint costs one section, not all of them.
    """
    choice = st.radio("Section", labels, key=key, horizontal=True,
                      label_visibility="collapsed")
    st.markdown("<hr style='margin: 0.2rem 0 0.9rem 0; opacity: 0.25;'>",
                unsafe_allow_html=True)
    return str(choice)


def page_header(title: str, subtitle: str, scope_note: str = "") -> None:
    st.markdown('<div class="ow-kicker">OVERWATCH</div>', unsafe_allow_html=True)
    st.title(title)
    caption = subtitle if not scope_note else f"{subtitle} · {scope_note}"
    st.caption(caption)
    chips = _scope_chip_html()
    if chips:
        st.markdown(f'<div class="ow-scope-row">{chips}</div>', unsafe_allow_html=True)


def kpi_row(items: list[dict], columns: int | None = None) -> None:
    """st.metric row. Item keys: label, value, delta (opt), help (opt)."""
    items = [i for i in items if i]
    if not items:
        return
    cols = st.columns(columns or len(items))
    for idx, item in enumerate(items):
        with cols[idx % len(cols)]:
            st.metric(
                label=str(item.get("label", "")),
                value=str(item.get("value", "—")),
                delta=item.get("delta"),
                delta_color=item.get("delta_color", "normal"),
                help=item.get("help"),
            )


def result_caption(result: QueryResult, note: str = "") -> None:
    """Source + freshness line under any data panel."""
    bits = []
    if result.source:
        bits.append(f"Source: {result.source}")
    if result.fetched_at:
        bits.append(f"fetched {result.fetched_at.strftime('%H:%M:%S')}")
    bits.append(ACCOUNT_USAGE_LAG_NOTE)
    if note:
        bits.append(note)
    st.caption(" · ".join(bits))


def guard(result: QueryResult, empty_message: str, setup_hint: str = "") -> bool:
    """Standard render gate: labeled error / labeled empty / truncation banner.

    Returns True when the caller should render the data.
    """
    if not result.ok:
        st.error(f"Query failed: {result.error}")
        if setup_hint:
            st.caption(setup_hint)
        return False
    if result.empty:
        st.info(empty_message)
        if setup_hint:
            st.caption(setup_hint)
        return False
    if result.truncated:
        st.warning(
            f"Showing the first {len(result.df):,} rows — the result was larger. "
            "Narrow the window or filters to see everything."
        )
    return True


def status_chips(pairs: list[tuple[str, str]]) -> None:
    """Row of small chips: [(text, 'ok'|'bad'|''), ...]."""
    html_out = "".join(chip(html.escape(text), state) for text, state in pairs)
    st.markdown(html_out, unsafe_allow_html=True)


@st.cache_data(ttl=300, show_spinner=False)
def _settings_frame_cached(scope: str):
    from app.core.query import run  # local import: avoid module cycle

    return run(mart_sql.settings(), page="Settings", key="settings", tier="recent",
               source="SETTINGS").df


def load_settings(page: str) -> dict:
    """Settings from SETTINGS with code defaults as offline fallback."""
    merged = dict(DEFAULT_SETTINGS)
    try:
        df = _settings_frame_cached("global")
        if df is not None and not df.empty and {"KEY", "VALUE"}.issubset(df.columns):
            for _, row in df.iterrows():
                key = str(row["KEY"]).upper()
                if key in merged:
                    raw = row["VALUE"]
                    if isinstance(merged[key], float):
                        merged[key] = safe_float(raw, merged[key])
                    else:
                        merged[key] = str(raw if raw is not None else merged[key])
            merged["_source"] = "SETTINGS"
            return merged
    except Exception:  # noqa: BLE001 — settings fallback must never break a page
        pass
    merged["_source"] = "code defaults (SETTINGS not reachable)"
    return merged


def budget_kpi(settings: dict, spend_usd: float) -> dict:
    """Budget KPI that refuses to invent a denominator (old-app finding)."""
    budget = safe_float(settings.get("MONTHLY_BUDGET_USD"))
    if budget <= 0:
        return {
            "label": "Monthly budget",
            "value": "Not configured",
            "help": "Set MONTHLY_BUDGET_USD on the Admin page. No default is assumed.",
        }
    pct = spend_usd / budget * 100 if budget else 0.0
    return {
        "label": "MTD spend vs budget",
        "value": f"{format_usd(spend_usd)} / {format_usd(budget)}",
        "delta": f"{pct:,.0f}% of budget",
        "delta_color": "inverse" if pct >= 100 else "off",
        "help": "Budget from SETTINGS; spend from warehouse metering.",
    }


def styled_table(df, *, height: int | None = None, column_config: dict | None = None) -> None:
    """st.dataframe with the app's semantic status colors and a height cap.

    Status-bearing columns (SEVERITY, STATUS, STATE, SLA_MET, ...) get
    background tints via pandas Styler; long tables cap at 380px so pages
    keep their reading rhythm.
    """
    if df is None or getattr(df, "empty", True):
        st.dataframe(df, hide_index=True, use_container_width=True)
        return
    status_cols = status_columns_in(list(df.columns))
    data = df
    if status_cols:
        try:
            data = df.style
            for col in status_cols:
                data = data.map(lambda v, _c=col: status_css(_c, v), subset=[col])
        except Exception:  # noqa: BLE001 - styling is cosmetic, table must render
            data = df
    if height is None and len(df) > 10:
        height = 380
    st.dataframe(data, hide_index=True, use_container_width=True,
                 height=height, column_config=column_config)


def download_text_button(label: str, text: str, filename: str) -> None:
    """A real download (the old app's 'copy' button that didn't copy is dead)."""
    st.download_button(label, data=text, file_name=filename, mime="text/plain")
