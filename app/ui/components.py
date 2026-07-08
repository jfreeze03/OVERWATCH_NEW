"""Shared UI components. Honest states only: real data, labeled errors,
labeled empties, visible truncation. No synthetic fallbacks — ever."""

from __future__ import annotations

import html

import streamlit as st

from app.config import ACCOUNT_USAGE_LAG_NOTE, DEFAULT_SETTINGS
from app.core.result import QueryResult
from app.data import mart_sql
from app.logic.formulas import ACCOUNT_TIMEZONE, format_usd, safe_float
from app.theme import chip
from app.ui.icons import icon
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


# One spelling of account time across the app (formulas.account_today uses it too).
_ACCOUNT_TZ = ACCOUNT_TIMEZONE


def display_timezone() -> str:
    """The user's chosen display timezone ('' = account time as stored)."""
    return str(st.session_state.get("_ow_display_tz") or "")


def localize_timestamps(df, columns: list[str]):
    """Convert naive account-time timestamp columns to the display timezone.

    Returns (df, note): note is '' when no conversion happened. Conversion is
    display-only — SQL, dedupe keys, and exports stay in account time.
    """
    import pandas as pd

    tz = display_timezone()
    if not tz or tz.startswith("Account") or df is None or getattr(df, "empty", True):
        return df, ""
    out = df.copy()
    converted = False
    for col in columns:
        if col not in out.columns:
            continue
        try:
            series = pd.to_datetime(out[col], errors="coerce")
            out[col] = (series.dt.tz_localize(_ACCOUNT_TZ, ambiguous="NaT", nonexistent="NaT")
                        .dt.tz_convert(tz).dt.tz_localize(None))
            converted = True
        except (TypeError, ValueError):
            continue
    if converted:
        try:
            out.attrs["_ow_tz_converted"] = True  # central pass must not convert again
        except Exception:  # noqa: BLE001 - attrs are best-effort metadata
            pass
    return out, (f"Times shown in {tz} (stored in account time)." if converted else "")


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
    elif st.session_state.get(key) not in labels:
        # A router/saved-view pointed at a section that no longer exists
        # (labels get consolidated); land on the first section instead of
        # crashing the radio. The navigation-consistency test keeps the
        # router honest; this keeps stale SAVED links harmless forever.
        st.session_state[key] = labels[0]
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


def page_header(title: str, subtitle: str, scope_note: str = "", icon_name: str = "") -> None:
    st.session_state["_ow_dl_seq"] = 0
    st.markdown('<div class="ow-kicker">OVERWATCH</div>', unsafe_allow_html=True)
    if icon_name:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:11px;margin:-2px 0 2px 0">'
            f'<span style="color:var(--ow-accent);display:inline-flex">{icon(icon_name, 26)}</span>'
            f'<span style="font-size:1.72rem;font-weight:750;letter-spacing:-0.015em;'
            f'color:var(--ow-ink)">{title}</span></div>', unsafe_allow_html=True)
    else:
        st.title(title)
    caption = subtitle if not scope_note else f"{subtitle} · {scope_note}"
    st.caption(caption)
    chips = _scope_chip_html()
    if chips:
        st.markdown(f'<div class="ow-scope-row">{chips}</div>', unsafe_allow_html=True)


_SEV_HEX = {"ok": "#34d399", "warn": "#fbbf24", "bad": "#fb7185",
            "info": "#38bdf8", "": "#38bdf8"}


def spark_svg(values, width: int = 84, height: int = 24, color: str = "#38bdf8",
              fill: bool = True) -> str:
    """Inline SVG sparkline (polyline + soft area). Pure — embeds anywhere.

    A number without direction is half a number; this puts the direction on
    the card. Returns '' for fewer than 2 finite points.
    """
    nums = []
    for v in (values or []):
        f = safe_float(v, None) if v is not None else None
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = None
        if f is not None and f == f:  # not NaN
            nums.append(f)
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    rng = (hi - lo) or 1.0
    n = len(nums)
    pts = [(round(i / (n - 1) * (width - 2) + 1, 1),
            round(height - 2 - (v - lo) / rng * (height - 4), 1)) for i, v in enumerate(nums)]
    line = " ".join(f"{x},{y}" for x, y in pts)
    uid = abs(hash(line)) % 100000
    area = ""
    if fill:
        area = (f'<defs><linearGradient id="g{uid}" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0" stop-color="{color}" stop-opacity="0.35"/>'
                f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
                f'<polygon points="1,{height-1} {line} {width-1},{height-1}" fill="url(#g{uid})"/>')
    last = pts[-1]
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'style="display:block">{area}'
            f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{last[0]}" cy="{last[1]}" r="1.9" fill="{color}"/></svg>')


def _delta_html(delta, delta_color: str) -> str:
    if not delta:
        return ""
    text = html.escape(str(delta))
    down = str(delta).strip().startswith("-")
    if delta_color == "off":
        col, arrow = "#6b7a90", "flat"
    else:
        good = down if delta_color == "inverse" else (not down)
        col = "#34d399" if good else "#fb7185"
        arrow = "down" if down else "up"
    return (f'<div style="font-size:0.78rem;font-weight:640;color:{col};'
            f'display:flex;align-items:center;gap:4px;margin-top:3px">'
            f'{icon(arrow, 13)}{text}</div>')


def metric_card_html(item: dict) -> str:
    """Rich KPI card: severity stripe, uppercase micro-label, big tabular
    value, colored trend delta, optional inline sparkline. Full design
    control where st.metric could not go."""
    sev = str(item.get("severity", "") or "")
    label = html.escape(str(item.get("label", "")))
    value = html.escape(str(item.get("value", "—")))
    help_t = html.escape(str(item.get("help", "") or ""))
    cls = f"ow-card ow-card--{sev}" if sev in ("ok", "warn", "bad", "info") else "ow-card"
    spark = ""
    if item.get("spark"):
        spark = ('<div class="ow-card__meta" style="margin-top:6px">'
                 + spark_svg(item["spark"], color=_SEV_HEX.get(sev, "#38bdf8")) + "</div>")
    delta = _delta_html(item.get("delta"), str(item.get("delta_color", "normal")))
    title_attr = f' title="{help_t}"' if help_t else ""
    return (f'<div class="{cls}" style="min-height:96px"{title_attr}>'
            f'<div class="ow-card__title">{label}</div>'
            f'<div class="ow-card__value">{value}</div>{delta}{spark}</div>')


def kpi_row(items: list[dict], columns: int | None = None) -> None:
    """Row of rich KPI cards. Item keys: label, value, delta, delta_color,
    help, plus optional severity ('ok'|'warn'|'bad'|'info') and spark (list
    of numbers). One centralized upgrade lifts every KPI row in the app."""
    items = [i for i in items if i]
    if not items:
        return
    cols = st.columns(columns or len(items))
    for idx, item in enumerate(items):
        with cols[idx % len(cols)]:
            st.markdown(metric_card_html(item), unsafe_allow_html=True)


def section_header(title: str, health: str = "", icon_name: str = "",
                   badge: str = "") -> None:
    """Section header with a left severity stripe + optional icon and status
    badge — gives visual weight to what matters (CoCo's #2 high item)."""
    hcls = f" ow-section--{health}" if health in ("ok", "warn", "bad", "info") else ""
    ico = f'<span class="ow-section__icon">{icon(icon_name)}</span>' if icon_name else ""
    bdg = f'<span class="ow-section__badge">{html.escape(badge)}</span>' if badge else ""
    st.markdown(f'<div class="ow-section{hcls}">{ico}'
                f'<span class="ow-section__title">{html.escape(title)}</span>{bdg}</div>',
                unsafe_allow_html=True)


def status_bar(stats: list[dict]) -> None:
    """Persistent status strip: [{k, v, sev?, spark?, icon?}]. Rendered once
    per page so the 3-4 numbers that matter follow the user everywhere."""
    stats = [s for s in stats if s]
    if not stats:
        return
    cells = []
    for s in stats:
        sev = str(s.get("sev", "") or "")
        scls = f"ow-stat ow-stat--{sev}" if sev in ("ok", "warn", "bad", "info") else "ow-stat"
        ico = f'{icon(s["icon"], 13)} ' if s.get("icon") else ""
        spark = ""
        if s.get("spark"):
            spark = ('<div class="ow-stat__spark">'
                     + spark_svg(s["spark"], width=104, height=18,
                                 color=_SEV_HEX.get(sev, "#38bdf8")) + "</div>")
        cells.append(f'<div class="{scls}"><div class="ow-stat__k">{html.escape(str(s.get("k","")))}</div>'
                     f'<div class="ow-stat__v">{ico}{html.escape(str(s.get("v","—")))}</div>{spark}</div>')
    st.markdown(f'<div class="ow-statusbar">{"".join(cells)}</div>', unsafe_allow_html=True)


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
        # Absence-of-setup is a state, not a failure: fresh deployments show
        # one calm line instead of a wall of red while marts install.
        if "run the migrations and roles.sql" in str(result.error):
            st.info("This panel needs OVERWATCH's objects installed — an admin can see "
                    "what's pending on Admin → Migrations & freshness.")
        else:
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


SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


def severity_sort(df, sev_col: str = "SEVERITY", time_col: str = "RAISED_AT"):
    """Triage order: worst severity first, newest within a severity.

    Default table order was pure recency, which let an hour-old CRITICAL sink
    below a minute-old INFO. Returns a re-indexed copy; no-op when the
    severity column is absent.
    """
    if df is None or getattr(df, "empty", True) or sev_col not in df.columns:
        return df
    out = df.copy()
    out["_SEV_RANK"] = out[sev_col].astype(str).str.upper().map(SEVERITY_RANK).fillna(-1)
    by, ascending = ["_SEV_RANK"], [False]
    if time_col in out.columns:
        by.append(time_col)
        ascending.append(False)
    return out.sort_values(by, ascending=ascending).drop(columns=["_SEV_RANK"]).reset_index(drop=True)


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
            "help": "Set MONTHLY_BUDGET_USD on the Admin page. No default is assumed. "
                    "MTD spend here is account-wide (metering-daily has no company split).",
        }
    pct = spend_usd / budget * 100 if budget else 0.0
    return {
        "label": "MTD spend vs budget",
        "value": f"{format_usd(spend_usd)} / {format_usd(budget)}",
        "delta": f"{pct:,.0f}% of budget",
        "delta_color": "inverse" if pct >= 100 else "off",
        "help": "Budget from SETTINGS; spend is account-wide billed credits (metering-daily has no company split).",
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
        if c.endswith(("_USD", "_PRICE")) or c == "USD":
            fmts[col] = "${:,.2f}"
        elif "CREDITS" in c:
            fmts[col] = "{:,.2f}"
        elif c.endswith(("_PCT", "_SHARE", "_GB", "_TB", "_MB", "_HOURS", "_S", "_SEC", "_MS")) or c == "HIT_PCT":
            fmts[col] = "{:,.1f}"
        elif c.endswith(_COUNT_SUFFIXES):
            fmts[col] = "{:,.0f}"
    return fmts


# Above this row count, pandas Styler (per-cell styles for the WHOLE frame)
# dominates paint time — fall back to Arrow-native printf formats instead.
STYLER_MAX_ROWS = 1500

# Styler format -> printf equivalent for the large-frame path. Commas are
# lost above the cap (printf has no grouping); that trade is deliberate.
_PRINTF_EQUIV = {"${:,.2f}": "$%.2f", "{:,.2f}": "%.2f", "{:,.1f}": "%.1f", "{:,.0f}": "%.0f"}

# Columns that hold account-time timestamps, by naming convention — the
# display-timezone conversion (Views popover) applies to every table via
# _render_table, not just the pages that remembered to call it.
_TS_SUFFIXES = ("_AT", "_TIME", "_TS", "_DML", "_READ", "_SEND")
_TS_EXACT = ("AT", "TIMESTAMP", "NEWEST", "OLDEST", "LAST_DML", "LAST_READ", "LAST_SEND")


def timestampish_columns(columns) -> list[str]:
    """Column names that look like account-time timestamps (convention)."""
    out = []
    for col in columns:
        c = str(col).upper()
        if c.endswith(_TS_SUFFIXES) or c in _TS_EXACT:
            out.append(col)
    return out


def _auto_pin(df, column_config: dict | None) -> dict | None:
    """Pin the first column on wide tables so horizontal scroll keeps the
    row's identity. No-op when the caller configured that column, when the
    table is narrow, or on runtimes without pinning support."""
    if len(df.columns) < 8:
        return column_config
    first = df.columns[0]
    cfg = dict(column_config or {})
    if first in cfg:
        return column_config
    try:
        cfg[first] = st.column_config.Column(pinned=True)
    except TypeError:  # runtime predates pinned=
        return column_config
    return cfg


def _render_table(df, *, height: int | None, column_config: dict | None,
                  key: str | None = None, selectable: bool = False) -> int | None:
    if df is None or getattr(df, "empty", True):
        st.dataframe(df, hide_index=True, use_container_width=True)
        return None
    display_df = df
    try:  # display-timezone conversion is display-only; the CSV keeps account time
        ts_cols = [] if getattr(df, "attrs", {}).get("_ow_tz_converted") else timestampish_columns(df.columns)
        if ts_cols:
            display_df, tz_note = localize_timestamps(df, ts_cols)
            if tz_note:
                st.caption(tz_note)
    except Exception:  # noqa: BLE001 - conversion is cosmetic
        display_df = df
    data = display_df
    fmts = _auto_formats(df, set(column_config or {}))
    if len(df) <= STYLER_MAX_ROWS:
        try:
            styler = display_df.style
            for col in status_columns_in(list(df.columns)):
                styler = styler.map(lambda v, _c=col: status_css(_c, v), subset=[col])
            if fmts:
                styler = styler.format(fmts, na_rep="–")
            data = styler
        except Exception:  # noqa: BLE001 - styling is cosmetic, table must render
            data = display_df
    else:
        # Large frame: skip Styler entirely; carry the number formats through
        # column_config so display stays consistent (minus thousands commas).
        cfg = dict(column_config or {})
        for col, fmt in fmts.items():
            if col not in cfg and fmt in _PRINTF_EQUIV:
                try:
                    cfg[col] = st.column_config.NumberColumn(format=_PRINTF_EQUIV[fmt])
                except Exception:  # noqa: BLE001
                    break
        column_config = cfg
    column_config = _auto_pin(df, column_config)
    if height is None and len(df) > 10:
        height = 380
    kwargs = {"hide_index": True, "use_container_width": True, "column_config": column_config}
    if isinstance(height, int) and height > 0:  # newer Streamlit rejects height=None
        kwargs["height"] = height
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
    try:  # every real table is exportable; tiny KPI-ish frames skip the button
        if len(df) >= 4:
            seq = int(st.session_state.get("_ow_dl_seq", 0))
            st.session_state["_ow_dl_seq"] = seq + 1
            st.download_button("⬇", df.to_csv(index=False).encode("utf-8"),
                               file_name=f"overwatch_table_{seq}.csv", mime="text/csv",
                               key=f"ow_dl_{key or ''}_{seq}", type="tertiary",
                               help="Download this table as CSV (account time).")
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


def mark_refreshed() -> None:
    """Stamp the session's last data-refresh time (called on load/refresh)."""
    from datetime import datetime
    st.session_state["_ow_refreshed_at"] = datetime.now()


def last_refreshed_note() -> str:
    """Human 'updated Nm ago' for the always-visible sidebar indicator."""
    from datetime import datetime
    ts = st.session_state.get("_ow_refreshed_at")
    if not ts:
        return "Live · cached per tier"
    secs = max(0, int((datetime.now() - ts).total_seconds()))
    if secs < 60:
        return f"Updated {secs}s ago"
    mins = secs // 60
    return f"Updated {mins}m ago" if mins < 60 else f"Updated {mins // 60}h ago"


def download_text_button(label: str, text: str, filename: str) -> None:
    """A real download (the old app's 'copy' button that didn't copy is dead)."""
    st.download_button(label, data=text, file_name=filename, mime="text/plain")
