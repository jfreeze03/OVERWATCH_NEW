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


def page_header(title: str, subtitle: str, scope_note: str = "") -> None:
    st.markdown('<div class="ow-kicker">OVERWATCH</div>', unsafe_allow_html=True)
    st.title(title)
    caption = subtitle if not scope_note else f"{subtitle} · {scope_note}"
    st.caption(caption)


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
               source="CORE.SETTINGS").df


def load_settings(page: str) -> dict:
    """Settings from CORE.SETTINGS with code defaults as offline fallback."""
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
            merged["_source"] = "CORE.SETTINGS"
            return merged
    except Exception:  # noqa: BLE001 — settings fallback must never break a page
        pass
    merged["_source"] = "code defaults (CORE.SETTINGS not reachable)"
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
        "help": "Budget from CORE.SETTINGS; spend from warehouse metering.",
    }


def download_text_button(label: str, text: str, filename: str) -> None:
    """A real download (the old app's 'copy' button that didn't copy is dead)."""
    st.download_button(label, data=text, file_name=filename, mime="text/plain")
