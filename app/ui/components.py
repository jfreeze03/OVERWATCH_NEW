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
    if key not in st.session_state:
        try:  # deep link: ?section=<slug> selects the section on first render
            want = str(st.query_params.get("section") or "")
            for label in labels:
                if _section_slug(label) == want:
                    st.session_state[key] = label
                    break
        except Exception:  # noqa: BLE001 - deep links are progressive enhancement
            pass
    choice = st.radio("Section", labels, key=key, horizontal=True,
                      label_visibility="collapsed")
    try:
        st.query_params["section"] = _section_slug(choice)
    except Exception:  # noqa: BLE001
        pass
    st.markdown("<hr style='margin: 0.2rem 0 0.9rem 0; opacity: 0.25;'>",
                unsafe_allow_html=True)
    return str(choice)


def _section_slug(label: str) -> str:
    return str(label).lower().replace("&", "and").replace(" ", "-")


def page_header(title: str, subtitle: str, scope_note: str = "") -> None:
    st.session_state["_ow_dl_seq"] = 0
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


_COUNT_SUFFIXES = ("_COUNT", "RUNS", "CALLS", "FAILS", "FAILED", "QUERIES", "EVENTS",
                   "ATTEMPTS", "FILES", "USERS", "STMTS", "INTERVALS", "REFRESHES",
                   "FAILURES", "STATEMENTS", "ROWS")


def _auto_formats(df, skip: set) -> dict:
    """Consistent number display by column-name convention (item: every table
    shows $ and thousands separators without per-site column_config)."""
    from pandas.api import types as ptypes

    fmts: dict = {}
    for col in df.columns:
        if col in skip or not ptypes.is_numeric_dtype(df[col]):
            continue
        c = str(col).upper()
        if c.endswith("_USD") or c == "USD" or c.endswith("_PRICE"):
            fmts[col] = "${:,.2f}"
        elif "CREDITS" in c:
            fmts[col] = "{:,.2f}"
        elif c.endswith("_PCT") or c.endswith("_SHARE") or c == "HIT_PCT":
            fmts[col] = "{:,.1f}"
        elif c.endswith(("_GB", "_TB", "_MB", "_HOURS", "_S", "_SEC", "_MS")):
            fmts[col] = "{:,.1f}"
        elif c.endswith(_COUNT_SUFFIXES):
            fmts[col] = "{:,.0f}"
    return fmts


def _render_table(df, *, height: int | None, column_config: dict | None,
                  key: str | None = None, selectable: bool = False) -> int | None:
    if df is None or getattr(df, "empty", True):
        st.dataframe(df, hide_index=True, use_container_width=True)
        return None
    data = df
    try:
        styler = df.style
        for col in status_columns_in(list(df.columns)):
            styler = styler.map(lambda v, _c=col: status_css(_c, v), subset=[col])
        fmts = _auto_formats(df, set(column_config or {}))
        if fmts:
            styler = styler.format(fmts, na_rep="–")
        data = styler
    except Exception:  # noqa: BLE001 - styling is cosmetic, table must render
        data = df
    if height is None and len(df) > 10:
        height = 380
    kwargs = dict(hide_index=True, use_container_width=True, height=height,
                  column_config=column_config)
    selected: int | None = None
    if selectable and key:
        try:
            event = st.dataframe(data, key=key, on_select="rerun",
                                 selection_mode="single-row", **kwargs)
            rows = list(getattr(getattr(event, "selection", None), "rows", None) or [])
            selected = int(rows[0]) if rows else None
        except TypeError:  # runtime without selection support: render, no selection
            st.dataframe(data, **kwargs)
    else:
        st.dataframe(data, **kwargs)
    try:  # every table is exportable; auditors and managers ask constantly
        seq = int(st.session_state.get("_ow_dl_seq", 0))
        st.session_state["_ow_dl_seq"] = seq + 1
        st.download_button("⬇ CSV", df.to_csv(index=False).encode("utf-8"),
                           file_name=f"overwatch_table_{seq}.csv", mime="text/csv",
                           key=f"ow_dl_{key or ''}_{seq}", type="tertiary")
    except Exception:  # noqa: BLE001 - export is a convenience, never break the table
        pass
    return selected


def styled_table(df, *, height: int | None = None, column_config: dict | None = None) -> None:
    """st.dataframe with semantic status colors, convention-based number
    formats, a height cap, and a CSV download."""
    _render_table(df, height=height, column_config=column_config)


def selectable_table(df, key: str, *, height: int | None = None,
                     column_config: dict | None = None) -> int | None:
    """styled_table + single-row click selection; returns the positional row
    index or None. Degrades to a plain table on runtimes without selections."""
    return _render_table(df, height=height, column_config=column_config,
                         key=key, selectable=True)


def notify(ok: bool, msg: str) -> None:
    """Operator-action feedback: toast (survives layout shifts) + inline state."""
    try:
        st.toast(msg[:120], icon="✅" if ok else "⚠️")
    except Exception:  # noqa: BLE001 - toast is a nicety
        pass
    (st.success if ok else st.error)(msg)


def download_text_button(label: str, text: str, filename: str) -> None:
    """A real download (the old app's 'copy' button that didn't copy is dead)."""
    st.download_button(label, data=text, file_name=filename, mime="text/plain")
