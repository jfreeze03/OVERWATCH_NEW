"""Shared UI components. Honest states only: real data, labeled errors,
labeled empties, visible truncation. No synthetic fallbacks — ever."""

from __future__ import annotations

import html
import re

import streamlit as st

from app.config import ACCOUNT_USAGE_LAG_NOTE, DEFAULT_SETTINGS
from app.core.result import QueryResult
from app.data import mart_sql
from app.logic.formulas import ACCOUNT_TIMEZONE, account_today, format_usd, safe_float
from app.logic.metric_registry import COLUMN_HELP
from app.theme import chip
from app.ui import palette
from app.ui.icons import icon
from app.ui.sizing import TABLE_H_SM, TABLE_H_XL
from app.ui.status_colors import delta_css, is_delta_column, status_columns_in, status_css


def _scope_chip_html() -> str:
    """Active scope as chips so context survives scrolling past the top bar."""
    try:
        from app.core.state import filters  # local import: avoid module cycle

        f = filters()
    except Exception:  # noqa: BLE001 - header chrome must never break a page
        return ""
    chips = [chip(html.escape(f["company"]), "ok"), chip(f"{f['days']}d")]
    # Environment is a hidden compatibility field fixed to ALL, so it is not scope.
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
            # codex#35: an already-tz-aware column must be CONVERTED, not localized again —
            # tz_localize raises on aware input, and the old except-continue then silently
            # skipped conversion, leaving aware timestamps shown in the wrong zone.
            if getattr(series.dt, "tz", None) is not None:
                out[col] = series.dt.tz_convert(tz).dt.tz_localize(None)
            else:
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
    from app.logic.formulas import md_dollars
    with st.popover("ⓘ about this panel", width="content"):
        st.markdown(md_dollars(text))  # two $'s in help prose must not go LaTeX-math


def lazy_sections(labels: list[str], key: str, deep_link: bool = True,
                  counts: dict[str, int] | None = None) -> str:
    """Tab-style navigation that renders ONLY the selected section.

    st.tabs executes every tab body on every rerun and merely hides the
    output — an 8-tab page fires every tab's queries to paint one. This
    pill radio keeps the navigation but lets the page dispatch a single
    section, so first paint costs one section, not all of them.

    counts (rec7): {label: n} badges a pill as "Label (n)" where the number is
    already in hand (zero extra queries). The DISPLAYED label carries the count
    via format_func; the option VALUE stays the base label, so a caller's
    ``section == "Label"`` dispatch is unchanged.

    deep_link=False (rec2): for a NESTED pill row (a SECOND lazy_sections on the
    same page), so it neither seeds from nor writes the shared ?section= query
    param that the page-level pills own — otherwise the inner row clobbers the
    outer section's deep link. Nested rows keep their own session key only.
    """
    if deep_link and key not in st.session_state:
        try:  # deep link: ?section=<slug> selects the section on first render
            want = str(st.query_params.get("section") or "")
            for label in labels:
                if _section_slug(label) == want:
                    st.session_state[key] = label
                    break
        except Exception:  # noqa: BLE001 - deep links are progressive enhancement
            pass
    # Always land on a VALID section BEFORE the widget renders: seeds the first
    # section on a fresh page (a deep-link miss leaves the key unset) and resets a
    # stale saved-view section (labels get consolidated). rec24: segmented_control
    # shows NO pill when its key is unset, so this must run unconditionally — the
    # old `elif` skipped it after a deep-link miss and left the bar blank on first
    # paint (radio silently defaulted to index 0).
    if st.session_state.get(key) not in labels:
        st.session_state[key] = labels[0]
    def _fmt(_l: str) -> str:  # rec7: badge a pill with its count; value stays the base label
        return f"{_l} ({counts[_l]})" if counts and _l in counts else _l
    if hasattr(st, "segmented_control"):
        # rec24: the native single-select widget replaces the pill LOOK that was
        # CSS-on-radio keyed off aria-labels the theme's own docstring calls
        # version-fragile. Radio stays the degrade path. segmented_control can
        # DESELECT to None on a second click of the active pill; an on_change guard
        # keeps the current section so the page is never left section-less.
        if st.session_state.get(key) in labels:
            st.session_state[f"_{key}_last"] = st.session_state[key]

        def _keep_section(_k=key, _first=labels[0]) -> None:
            if st.session_state.get(_k) is None:
                st.session_state[_k] = st.session_state.get(f"_{_k}_last") or _first

        choice = st.segmented_control("Section", labels, key=key, format_func=_fmt,
                                      label_visibility="collapsed", on_change=_keep_section)
        if choice is None:  # belt-and-suspenders for the first-render edge
            choice = st.session_state.get(f"_{key}_last") or labels[0]
    else:
        choice = st.radio("Section", labels, key=key, horizontal=True, format_func=_fmt,
                          label_visibility="collapsed")
    if deep_link:
        try:
            _slug = _section_slug(choice)
            if st.query_params.get("section") != _slug:  # r21 #19: no no-op writes
                st.query_params["section"] = _slug
        except Exception:  # noqa: BLE001
            pass
    st.markdown("<hr style='margin: 0.2rem 0 0.9rem 0; opacity: 0.25;'>",
                unsafe_allow_html=True)
    return str(choice)


def nested_sections(
    labels: list[str],
    key: str,
    counts: dict[str, int] | None = None,
) -> str:
    """Render a local section switcher without taking ownership of the page URL."""
    return lazy_sections(labels, key, deep_link=False, counts=counts)


def _section_slug(label: str) -> str:
    return str(label).lower().replace("&", "and").replace(" ", "-")


def page_header(title: str, subtitle: str, scope_note: str = "", icon_name: str = "") -> None:
    st.session_state["_ow_dl_seq"] = 0
    st.session_state["_ow_tz_note_shown"] = False   # rec34: tz note prints once per page
    st.session_state["_ow_dl_page"] = str(title)   # T1.6: page identity for the CSV-prep slot
    # rec5: no per-page OVERWATCH kicker — the sidebar brand + browser tab are the one
    # brand anchor; repeating it above every header was orientation noise, not signal.
    if icon_name:
        st.markdown(
            f'<div class="ow-page-heading">'
            f'<span class="ow-page-heading__icon">{icon(icon_name, 26)}</span>'
            f'<h1>{html.escape(title)}</h1></div>',
            unsafe_allow_html=True,
        )
    else:
        st.title(title)
    chips = _scope_chip_html()
    # rec1: when the scope CHIPS render they ARE the in-header scope statement, so the
    # caption is the subtitle only — scope used to print twice (caption + chips).
    caption = subtitle if (chips or not scope_note) else f"{subtitle} · {scope_note}"
    st.caption(caption)
    if chips:
        st.markdown(f'<div class="ow-scope-row">{chips}</div>', unsafe_allow_html=True)
    # rec11: the ACCOUNT_USAGE lag note lives ONCE per page here, not appended verbatim
    # to every panel's result_caption (it was on all ~76 of them).
    st.caption(ACCOUNT_USAGE_LAG_NOTE)
    # rec24: if this page was reached by a jump that reshaped the GLOBAL filters,
    # announce it ONCE on arrival — otherwise the scoped view reads as if the user
    # set it. Drop only the note; any drill identity (event_id, query_id) survives.
    _nav_ctx = st.session_state.get("_ow_nav_context")
    if isinstance(_nav_ctx, dict) and _nav_ctx.get("filter_note"):
        # Point at the always-present sidebar scope controls, not the Reset button
        # (which only renders when a filter is active — a cross-company database filter
        # can be validated away, leaving no button to name).
        st.info(f"{_nav_ctx['filter_note']} — adjust or clear scope in the sidebar.")
        st.session_state["_ow_nav_context"] = {
            k: v for k, v in _nav_ctx.items() if k != "filter_note"
        }


_SEV_HEX = {"ok": palette.OK, "warn": palette.WARN, "bad": palette.BAD,
            "info": palette.INFO, "": palette.INFO}


def spark_svg(values, width: int = 84, height: int = 24, color: str = palette.INFO,
              fill: bool = True) -> str:
    """Inline SVG sparkline (polyline + soft area). Pure — embeds anywhere.

    A number without direction is half a number; this puts the direction on
    the card. Returns '' for fewer than 2 finite points.
    """
    import math
    nums = []
    for v in (values or []):
        # codex#36: one finite normalizer. The old code reassigned f = float(v) (dropping the
        # safe_float result) and then filtered only NaN via `f == f`, so +/-inf slipped through
        # and broke the min/max scaling below. isfinite() rejects NaN AND infinities.
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
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
        # route the neutral delta through the WCAG-AA muted token (rec15), not the
        # retired pre-a11y #6b7a90 (~3.9:1) — this is inline style=, so the var resolves
        col, arrow = "var(--ow-ink-mute)", "flat"
    else:
        good = down if delta_color == "inverse" else (not down)
        col = palette.OK if good else palette.BAD   # rec50: single hue source
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
                 + spark_svg(item["spark"], color=_SEV_HEX.get(sev, palette.INFO)) + "</div>")
    delta = _delta_html(item.get("delta"), str(item.get("delta_color", "normal")))
    # rec 15 (a11y): a keyboard-focusable + touch-tappable '?' affordance carrying the
    # help text, replacing the hover-only card title= (invisible on touch, unreachable
    # by Tab). aria-label announces it to screen readers; the CSS tooltip fires on hover
    # AND focus; native title= stays as a pointer fallback. Empty help -> no affordance.
    help_html = ""
    if help_t:
        help_html = (f'<span class="ow-help" tabindex="0" aria-label="{help_t}" '
                     f'data-help="{help_t}" title="{help_t}">?</span>')
    # Codex r7 #12 + rec 13: tiny trust chips INSIDE the card. THREE distinct tokens
    # so scope and freshness no longer compete for one slot (C11 crammed 'account-wide'
    # into the freshness badge, so a card could show scope OR freshness, never both):
    #   badge  = data FRESHNESS (mart|live|stale)
    #   method = how the number was derived (allocated|billed|metering|...)
    #   scope  = account-wide|company
    # Each renders as its own small chip. Free text; unknown badge values -> 'other'.
    badge = ""
    _b = str(item.get("badge", "") or "")
    if _b:
        _bk = _b.lower() if _b.lower() in ("mart", "live", "stale") else "other"
        badge = f'<span class="ow-src-badge ow-src-badge--{_bk}">{html.escape(_b)}</span>'
    for _kind in ("method", "scope"):
        _v = str(item.get(_kind, "") or "")
        if _v:
            badge += f'<span class="ow-src-badge ow-src-badge--{_kind}">{html.escape(_v)}</span>'
    # the freshness/method/scope chips ride in a right-aligned group that wraps to
    # its own line on a long two-chip $ card instead of shrinking the label.
    chips = f'<span class="ow-card__chips">{badge}</span>' if badge else ""
    # rec28: an optional secondary line under the value — e.g. credits beneath a dollar
    # headline ("$45,200 MTD" / "12,300 cr") so a $-primary card still reconciles against
    # Snowsight's credit numbers without mental math. Muted, reuses .ow-card__meta.
    sub = ""
    _sub = str(item.get("sub", "") or "")
    if _sub:
        sub = f'<div class="ow-card__meta">{html.escape(_sub)}</div>'
    # Ov15: an optional 'as of <date>' freshness watermark under the value — the
    # data-through stamp for a $ figure (metering lags up to 24h). Muted, reuses
    # .ow-card__meta; empty -> nothing, so cards without it are byte-unchanged.
    as_of = ""
    _asof = str(item.get("as_of", "") or "")
    if _asof:
        as_of = f'<div class="ow-card__meta">as of {html.escape(_asof)}</div>'
    return (f'<div class="{cls}" style="min-height:96px">'
            f'<div class="ow-card__title">{label}{help_html}{chips}</div>'
            f'<div class="ow-card__value">{value}</div>{sub}{as_of}{delta}{spark}</div>')


# Global dimension chips a MIXED-scope panel may or may not honor. Kept in one
# place (rec 11/13 single source) so the per-card scope tokens above and the
# section-level "what this panel ignores" line below never drift apart.
_SCOPE_DIM_LABELS = {
    "company": "Company",
    "days": "Window",
    "database": "Database",
    "warehouse_contains": "Warehouse",
    "user_contains": "User",
    "schema_contains": "Schema",
}


def _active_scope_dimensions(filters: dict) -> tuple[str, ...]:
    active = []
    for key in _SCOPE_DIM_LABELS:
        value = filters.get(key)
        if key == "days":
            active.append(key)
        elif key == "company":
            if str(value or "ALL").upper() != "ALL":
                active.append(key)
        elif str(value or "").strip():
            active.append(key)
    return tuple(active)


def filter_contract_text(
    filters: dict,
    *,
    applies: tuple[str, ...],
    partial: tuple[str, ...] = (),
    note: str = "",
) -> str:
    """Return one compact, metadata-driven statement of a section's filter behavior."""
    apply_labels = [_SCOPE_DIM_LABELS[key] for key in applies if key in _SCOPE_DIM_LABELS]
    applied = " · ".join(apply_labels) if apply_labels else "Account-wide / fixed horizon"
    ignored = [
        _SCOPE_DIM_LABELS[key]
        for key in _active_scope_dimensions(filters)
        if key not in applies and key not in partial
    ]
    parts = [f"Applies: {applied}"]
    partial_labels = [_SCOPE_DIM_LABELS[key] for key in partial if key in _SCOPE_DIM_LABELS]
    if partial_labels:
        parts.append("Panel-dependent: " + " · ".join(partial_labels))
    if ignored:
        parts.append("Active but ignored: " + " · ".join(ignored))
    if note:
        parts.append(str(note).strip())
    return " | ".join(parts)


def section_filter_contract(
    filters: dict,
    *,
    applies: tuple[str, ...],
    partial: tuple[str, ...] = (),
    note: str = "",
) -> str:
    """Render a section-level scope contract next to the metrics it governs."""
    text = filter_contract_text(filters, applies=applies, partial=partial, note=note)
    st.markdown(
        f'<div class="ow-filter-contract" role="note" aria-label="Filter contract">'
        f'{html.escape(text)}</div>',
        unsafe_allow_html=True,
    )
    return text


def section_scope_note(filters: dict, *, honored: tuple[str, ...] = ()) -> str:
    """rec 11: one honest line naming which ACTIVE global filters a section does
    NOT apply. The Overview headline KPIs are account-/company-scoped and
    deliberately ignore the warehouse/schema/user/database dimension chips (those
    bite on the Operations/Cost detail pages). A user who set one of those chips
    should be told it is inert here, not left to assume the number narrowed.
    Returns '' when nothing active is ignored, so the caller renders NOTHING on
    the common no-filter path — the note only appears when it prevents a
    misread. `honored` names the filter keys this particular section DOES apply."""
    ignored = [
        lbl.lower()
        for key, lbl in _SCOPE_DIM_LABELS.items()
        if key not in ("company", "days")
        if key not in honored and str(filters.get(key, "") or "").strip()
    ]
    if not ignored:
        return ""
    joined = ", ".join(ignored)
    plural = "filters" if len(ignored) > 1 else "filter"
    return (f"Scope: account-/company-wide. These figures ignore the active "
            f"{joined} {plural} — those apply on the Operations and Cost detail pages.")


def kpi_row(items: list[dict], columns: int | None = None) -> None:
    """Row of rich KPI cards. Item keys: label, value, delta, delta_color,
    help, plus optional severity ('ok'|'warn'|'bad'|'info') and spark (list
    of numbers). One centralized upgrade lifts every KPI row in the app."""
    items = [i for i in items if i]
    if not items:
        return
    # Cap rows at four cards (Codex r4 #3): five-plus equal columns cramp on
    # laptops. Overflow wraps to the next row — but REBALANCE evenly across rows
    # so a 5th card is not marooned in one quarter-width column beside three empty
    # ones (Overview's flagship 'Platform score' was the orphan). 5 -> 3+2, 7 -> 4+3.
    width = max(1, min(columns or 4, 4, len(items)))
    rows = (len(items) + width - 1) // width          # number of rows (ceil)
    base, extra = divmod(len(items), rows)            # spread items ~evenly
    start = 0
    for r in range(rows):
        size = base + (1 if r < extra else 0)
        chunk = items[start:start + size]
        start += size
        cols = st.columns(len(chunk))                 # fill the row, never orphan
        for idx, item in enumerate(chunk):
            with cols[idx]:
                st.markdown(metric_card_html(item), unsafe_allow_html=True)


def section_header(title: str, health: str = "", icon_name: str = "",
                   badge: str = "", anchor: str = "") -> None:
    """Section header with a left severity stripe + optional icon and status
    badge — gives visual weight to what matters (CoCo's #2 high item).

    `anchor` emits an id on the header. It is inert today (the in-page jump
    chips were removed in v4.157.0 — they could not scroll across the SiS
    component iframe), but kept as a harmless, stable hook for deep links."""
    hcls = f" ow-section--{health}" if health in ("ok", "warn", "bad", "info") else ""
    ico = f'<span class="ow-section__icon">{icon(icon_name)}</span>' if icon_name else ""
    bdg = f'<span class="ow-section__badge">{html.escape(badge)}</span>' if badge else ""
    aid = f' id="{html.escape(anchor)}"' if anchor else ""
    st.markdown(
        f'<h2 class="ow-section{hcls}"{aid}>{ico}'
        f'<span class="ow-section__title">{html.escape(title)}</span>{bdg}</h2>',
        unsafe_allow_html=True,
    )


def page_verdict_line(verdict: dict) -> None:
    """One severity-striped 'should I worry?' line above a page's content (CoCo
    do-first #1). `verdict` is the dict from app.logic.verdict.page_verdict —
    keys severity ('ok'|'warn'|'bad'), label, body. No new query: pages feed it
    signals they already computed."""
    sev = str(verdict.get("severity", "") or "")
    cls = f" ow-verdict--{sev}" if sev in ("ok", "warn", "bad") else ""
    label = html.escape(str(verdict.get("label", "")))
    body = html.escape(str(verdict.get("body", "")))
    st.markdown(
        f'<div class="ow-verdict{cls}" role="status">'
        f'<span class="ow-verdict__label">{label}</span>'
        f'<span class="ow-verdict__body">{body}</span></div>',
        unsafe_allow_html=True,
    )


def contract_runway_bar(runway: dict | None) -> None:
    """Persistent contract-runway bar (CoCo Overview #20 / Cost #4): a thin,
    colour-banded %-consumed bar with days-left, exhaust, and a 'decide-by' date.
    `runway` is the dict from logic.formulas.contract_runway, or None (renders
    nothing). Runway is the single most important committed-spend number, so it
    rides above the fold on every visit rather than hiding in a conditional KPI."""
    if not runway:
        return
    sev = str(runway.get("severity", "") or "")
    cls = f" ow-runway--{sev}" if sev in ("ok", "warn", "bad") else ""
    pct = max(0.0, min(float(runway.get("pct_consumed") or 0.0), 100.0))
    days_left = runway.get("days_left")
    left = (f"{float(days_left):,.0f} days left"
            if isinstance(days_left, (int, float)) and days_left >= 0 else "runway n/a")
    exhaust = runway.get("exhaust_date") or "—"
    decide_by = runway.get("decide_by")
    decide = f" · decide by {decide_by}" if decide_by else ""
    label = html.escape(
        f"{pct:.0f}% of contract consumed · {left} (exhausts {exhaust}{decide})")
    st.markdown(
        f'<div class="ow-runway{cls}">'
        f'<div class="ow-runway__track">'
        f'<div class="ow-runway__fill" style="width:{pct:.1f}%"></div></div>'
        f'<div class="ow-runway__label">{label}</div></div>',
        unsafe_allow_html=True,
    )


def export_button(label: str, data: str | bytes, *, file_name: str, mime: str,
                  key: str | None = None, help: str = "", disabled: bool = False,
                  width: str = "content") -> bool:
    """rec20: one page-level export affordance for the whole app. Thin wrapper over
    st.download_button that prefixes the same download glyph the per-table export
    uses ("⬇ ") and suppresses the rerun a download would otherwise trigger.
    Callers pass the noun phrase only — e.g. "Driver inventory (CSV)" — the glyph
    conveys "download". Returns the clicked-this-run bool from download_button."""
    return st.download_button(
        f"⬇ {label}", data=data, file_name=file_name, mime=mime, key=key,
        help=help or None, disabled=disabled,
        width=width, on_click="ignore")


def add_to_case_button(section: str, result: QueryResult, *, summary: str,
                       next_action: str = "", as_of: str = "", title: str = "",
                       preview_rows: int = 8, key: str) -> bool:
    """One-click 'Add to Case': snapshot this evidence into the session-only
    Operator Case File (reviewed and exported on Brief). Scope comes from
    ``filters()``, source/freshness/tier from the ``result``, and up to
    ``preview_rows`` of ``result.df``; only summary/next_action/as_of/title are
    authored per call site. Dedup-guarded (re-adding the same evidence is a no-op);
    the button is disabled when the result is not usable. Uses a callback-free
    button + toast, mutating session_state before the natural rerun."""
    from app.core.state import filters  # local import: avoid module cycle
    from app.logic import case_file

    usable = bool(getattr(result, "usable", lambda: False)())
    clicked = st.button("＋ Add to Case", key=key, disabled=not usable, width="content",
                        help="Snapshot this evidence into the session Case File (see Brief ▸ Operator Case File).")
    if clicked and usable:
        f = filters()
        head = result.df.head(max(1, int(preview_rows)))
        fetched = (result.fetched_at.strftime("%H:%M:%S")
                   if getattr(result, "fetched_at", None) else "")
        item = case_file.new_case_item(
            section=section,
            company=str(f.get("company", "ALL")),
            window=str(f.get("window_label", "")),
            days=served_days(result, int(f.get("days", 0) or 0)),
            source=str(getattr(result, "source", "") or ""),
            fetched_at=fetched,
            tier=str(getattr(result, "tier", "") or ""),
            summary=summary, next_action=next_action, as_of=as_of, title=title,
            added_at=account_today().isoformat(),
            preview_columns=list(head.columns),
            preview_rows=head.astype(str).to_numpy().tolist(),
            # Truncated when the query was row-capped OR the source has more
            # rows/columns than the preview shows (the head()/col cap above).
            truncated=(bool(getattr(result, "truncated", False))
                       or len(result.df.index) > len(head.index)
                       or len(result.df.columns) > case_file.MAX_PREVIEW_COLS),
        )
        current = list(st.session_state.get(case_file.CASE_STATE_KEY, []))
        updated = case_file.add_item(current, item)
        st.session_state[case_file.CASE_STATE_KEY] = updated
        if hasattr(st, "toast"):
            st.toast("Added to Case File" if len(updated) > len(current) else "Already in the Case File")
    return bool(clicked)


def status_bar(stats: list[dict]) -> None:
    """Persistent status strip: [{k, v, sev?, spark?, icon?, target?}].

    ``target`` is a ``(page, section)`` tuple routed through request_navigation;
    raw query-string anchors are unreliable inside Streamlit-in-Snowflake.
    """
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
                                 color=_SEV_HEX.get(sev, palette.INFO)) + "</div>")
        label = str(s.get("k", ""))
        value = str(s.get("v", "—"))
        cells.append(
            f'<div class="{scls}"><div class="ow-stat__k">{html.escape(label)}</div>'
            f'<div class="ow-stat__v">{ico}{html.escape(value)}</div>{spark}</div>'
        )
    st.markdown(f'<div class="ow-statusbar">{"".join(cells)}</div>', unsafe_allow_html=True)
    if any(s.get("target") for s in stats):
        from app.core.state import request_navigation

        with st.container(key="ow_status_actions"):
            action_cols = st.columns(len(stats), gap="small")
            for index, (s, col) in enumerate(zip(stats, action_cols, strict=True)):
                target = s.get("target")
                if not isinstance(target, (tuple, list)) or len(target) != 2:
                    continue
                page, section = str(target[0]), str(target[1])
                label = str(s.get("k", "details"))
                with col:
                    if st.button(
                        f"Open {label.lower()}",
                        key=f"ow_status_open_{index}_{_slugify(label)}",
                        help=f"Open {page} · {section}",
                        type="tertiary",
                        icon=":material/arrow_forward:",
                        width="stretch",
                    ):
                        request_navigation(page, section)


def result_caption(result: QueryResult, note: str = "") -> None:
    """Source + freshness line under any data panel. rec11: the ACCOUNT_USAGE lag
    note is NOT appended here — it printed verbatim under all ~76 panels; it now
    shows once per page in page_header. Source/fetched legitimately vary per panel."""
    bits = []
    if result.source:
        bits.append(f"Source: {result.source}")
    if result.fetched_at:
        bits.append(f"fetched {result.fetched_at.strftime('%H:%M:%S')}")
    if note:
        bits.append(note)
    if bits:
        from app.logic.formulas import md_dollars
        # $-escape at the sink: a note carrying two dollar amounts (or one amount +
        # a USER$-style literal) would otherwise render as a LaTeX math span.
        st.caption(md_dollars(" · ".join(bits)))



def snowsight_profile_column(
    df,
    page: str,
    id_col: str = "QUERY_ID",
    label: str = "Profile",
):
    """Frame + column-config additions that turn QUERY_IDs into Snowsight
    query-profile deep links (owner ask 2026-07-13: "the hyperlinks to the
    query profile are helpful"). The org/account context resolves once per
    session; when it is unavailable the frame returns unchanged and no link
    column renders — an honest degrade, never a dead link."""
    import pandas as pd

    from app.core.query import run
    if df is None or df.empty or id_col not in df.columns:
        return df, {}
    # Cache hits and pre-V064 telemetry rows legitimately have no server query.
    # Avoid both a dead link column and the context probe when every ID is blank.
    def _query_id(value: object) -> str:
        if value is None or bool(pd.isna(value)):
            return ""
        return str(value).strip()

    query_ids = df[id_col].map(_query_id)
    if not query_ids.ne("").any():
        return df, {}
    ctx = st.session_state.get("_ow_snowsight_ctx")
    if ctx is None:
        res = run("SELECT CURRENT_ORGANIZATION_NAME() AS ORG, CURRENT_ACCOUNT_NAME() AS ACCT",
                  page=page, key="snowsight_ctx", tier="metadata", source="session context")
        org = str(res.df.iloc[0].get("ORG", "") or "") if res.usable() else ""
        acct = str(res.df.iloc[0].get("ACCT", "") or "") if res.usable() else ""
        if not org or not acct:
            # R3-3: a failed/empty probe must NOT be cached — writing ('','') pins the
            # not-None guard for the whole session and permanently disables the links.
            # Leave session_state unset so the next rerun re-probes (metadata tier).
            return df, {}
        ctx = (org.lower(), acct.lower())
        st.session_state["_ow_snowsight_ctx"] = ctx
    org, acct = ctx
    if not org or not acct:
        return df, {}
    out = df.copy()
    base = f"https://app.snowflake.com/{org}/{acct}/#/compute/history/queries/"
    out["PROFILE"] = query_ids.map(lambda q: f"{base}{q}/profile" if q else None)
    return out, {"PROFILE": st.column_config.LinkColumn(
        label, display_text="Open ↗", width="small",
        help="Query profile in Snowsight — plan, partitions, spilling.")}


def _mark_served(result, *, live: bool, days: int | None):
    """K1: stamp which leg actually answered, and over what window, onto the
    frame the caller receives. Callers must read it through ``served_days()``.

    WHY it lives on ``df.attrs`` and not on QueryResult: the live builders clamp
    to MAX_LIVE_WINDOW_DAYS (90) while the marts honor 365, so a page that asked
    for 365d and got the live fallback was labeling a 90-day answer "365 days".
    QueryResult is the cached payload shared by both legs; attrs travel with the
    frame through Streamlit's cache copy and through pandas operations, and cost
    nothing when nobody looks."""
    from app.config import clamp_days
    df = getattr(result, "df", None)
    if df is None:
        return result
    df.attrs["_ow_served_live"] = bool(live)
    if days is not None:
        effective = clamp_days(days) if live else int(days)
        # Calendar day zero means "today only" for SQL, but downstream rates
        # still need a non-zero elapsed-period denominator.
        df.attrs["_ow_effective_days"] = max(1, int(effective))
    return result


def served_days(result, requested_days: int) -> int:
    """The window a run_mart_first result ACTUALLY covers (K1 contract).

    The live fallback clamps to MAX_LIVE_WINDOW_DAYS, so captions, per-day
    averages and run-rate math must divide by this, not by the requested days.
    Falls back to ``requested_days`` for any result that did not come through
    run_mart_first — an honest no-op, never a wrong clamp."""
    from app.config import clamp_days
    attrs = getattr(getattr(result, "df", None), "attrs", None) or {}
    effective = attrs.get("_ow_effective_days")
    if isinstance(effective, int) and effective > 0:
        return effective
    if attrs.get("_ow_served_live"):
        return max(1, int(clamp_days(requested_days)))
    return max(1, int(requested_days))


# ---------------------------------------------------------------------------
# Cluster-3 coverage contract (finding #16). One shared acceptance test for
# mart-first reads so a stale / short / gappy mart yields to the (correct) live
# fallback instead of silently answering a long window with incomplete data.
# The freshness/coverage primitives here read the WORST row of a multi-scope
# frame — the coverage law that one fresh database must never mask a stale or
# missing peer (findings #20/#21 use these directly on the storage frames).
# ---------------------------------------------------------------------------


def stalest_day(df, col: str = "LATEST_DAY"):
    """The MINIMUM (oldest) date across all rows of ``col`` — the honest
    freshness of a multi-scope frame. Cluster-3 law: freshness is the WORST
    row, never MAX(), so one fresh database can't hide a stale/missing peer.
    Returns a ``date`` or None (empty frame / absent column)."""
    import pandas as pd
    cols = getattr(df, "columns", [])
    if df is None or getattr(df, "empty", True) or col not in cols:
        return None
    d = pd.to_datetime(df[col], errors="coerce").min()
    return d.date() if pd.notna(d) else None


def min_covered_days(df, col: str = "DAYS_AVERAGED"):
    """The SMALLEST per-scope covered-day count (worst database), so a short
    peer reads as short instead of being masked by a fully-covered one. 0 when
    the column/frame is absent."""
    cols = getattr(df, "columns", [])
    if df is None or getattr(df, "empty", True) or col not in cols:
        return 0
    vals = [safe_float(v) for v in df[col].tolist()]
    return int(min(vals)) if vals else 0


def coverage_contract(days: int, *, day_col: str = "DAY", freshness_days: int = 2):
    """A ``run_mart_first`` acceptance predicate enforcing the Cluster-3
    coverage contract on a DAY-grain mart frame:

      * first observed day <= the requested window start (the mart reaches back
        far enough to answer the asked window),
      * latest observed day is fresh (within ``freshness_days`` of yesterday),
      * no interior day gaps across the observed span.

    Returns a predicate ``df -> bool``. A stale, short, or gappy mart is
    REJECTED so the caller serves the live fallback. Frames WITHOUT ``day_col``
    (pre-aggregated readers with no per-day grain) cannot be evaluated here, so
    the predicate ACCEPTS them — it must never suppress a mart it cannot prove
    incomplete; those readers gate their coverage in SQL instead (e.g.
    mart27_sql.alloc_xdim_attribution's scoped ``cov`` CTE)."""
    from datetime import timedelta

    def _accept(df) -> bool:
        import pandas as pd
        if df is None or getattr(df, "empty", True):
            return False
        if day_col not in getattr(df, "columns", []):
            return True
        d = pd.to_datetime(df[day_col], errors="coerce").dropna()
        if d.empty:
            return True
        present = pd.Index(d.dt.normalize().unique())
        first = present.min().date()
        latest = present.max().date()
        today = account_today()
        window_start = today - timedelta(days=int(days))
        if first > window_start:                                # doesn't reach the window start
            return False
        if latest < today - timedelta(days=1 + int(freshness_days)):   # stale tail
            return False
        span = (latest - first).days + 1                        # interior gaps
        return len(present) >= span                             # every day in the span is present

    return _accept


def run_mart_first(mart_sql: str, live_sql: str, *, page: str, key: str,
                   mart_source: str, live_source: str,
                   mart_tier: str = "hourly", live_tier: str = "historical",
                   max_rows: int | None = None, empty_is_answer: bool = False,
                   mart_accept=None, preloaded=None, days: int | None = None,
                   coverage_gate: bool = False, coverage_day_col: str = "DAY",
                   coverage_freshness_days: int = 2):
    """Fact-first read with the live builder as labeled fallback — the
    Control Room v4.8.2 pattern as one call (wave 2 adoptions). The mart
    result must be usable (ok AND non-empty) or the live path runs under
    its own cache key; callers surface the source via result_caption, so
    a viewer can always tell which path served the panel.

    T2.3: ``preloaded`` accepts the mart leg's result from a run_batch prefetch
    (prefetch-else-run) so a section can issue its independent mart reads as one
    parallel group; mart_accept still applies, and a missing/failed prefetch
    falls through to the serial mart read exactly as before.

    K1: pass ``days`` (the window the caller ASKED for) and read the window that
    was actually served back with ``served_days(result, days)`` — the live legs
    clamp to 90 and the marts do not.

    #16: pass ``coverage_gate=True`` (with ``days``) to apply the shared
    ``coverage_contract`` as the acceptance test when no explicit ``mart_accept``
    is given — a stale/short/gappy DAY-grain mart then yields to the live
    fallback. Left OFF by default so existing callers are unchanged. FOLLOW-UP
    (do not edit here): the day-grain mart-first sites in operations.py,
    optimize.py, overview.py, security.py, contract.py, ai_chargeback.py,
    control_room.py, cost.py, unit_costs.py should opt into ``coverage_gate`` in
    an incremental pass now that the contract exists."""
    from app.core.query import run
    # #16: the coverage contract is the DEFAULT acceptance test when the caller
    # opts in and hasn't supplied its own probe. Aggregated readers (no day_col)
    # accept transparently — they gate coverage in SQL — so this is safe to arm.
    if mart_accept is None and coverage_gate and days is not None:
        mart_accept = coverage_contract(
            days, day_col=coverage_day_col, freshness_days=coverage_freshness_days)
    kwargs: dict = {} if max_rows is None else {"max_rows": max_rows}
    res = preloaded if (preloaded is not None and preloaded.ok) else run(
        mart_sql, page=page, key=f"{key}_fact", tier=mart_tier,
        source=mart_source, **kwargs)
    if res.usable():
        try:
            accepted = mart_accept is None or bool(mart_accept(res.df))
        except Exception as _acc_exc:  # noqa: BLE001 — a coverage probe must never break a page
            # codex#33: a RAISING coverage check must NOT silently accept the mart (that
            # defeats the gate and can serve a mart that doesn't cover the window). Log it
            # and fall through to the live path.
            accepted = False
            try:
                from app.core.query import record_error
                record_error(page, _acc_exc, context="run_mart_first: mart_accept probe raised")
            except Exception:  # noqa: BLE001
                pass
        if accepted:
            return _mark_served(res, live=False, days=days)
        # r11 #2: the mart answered but does not cover enough of the asked
        # window (an accruing mart holds weeks of a 13-month chart). Prefer
        # live; if live cannot answer, the partial mart still beats an empty
        # panel — and the source label says which path served.
        live = run(live_sql, page=page, key=key, tier=live_tier,
                   source=live_source, **kwargs)
        if live.usable():
            return _mark_served(live, live=True, days=days)
        return _mark_served(res, live=False, days=days)
    # Codex r9 #2: for marts whose table only exists once loaded (V035), a
    # SUCCESSFUL empty read means "genuinely nothing" — reviving the live
    # scan would pay 46-56 GB to confirm an answer we already hold. Marts
    # with young-coverage ambiguity keep the default (fallback on empty).
    if empty_is_answer and res.ok:
        return _mark_served(res, live=False, days=days)
    return _mark_served(
        run(live_sql, page=page, key=key, tier=live_tier, source=live_source, **kwargs),
        live=True, days=days)


class _DirectoryUnavailable(Exception):
    """The directory read failed. Raised out of the cache_data-wrapped build so
    the empty-map FAILURE is never pinned for a day (Streamlit does not cache
    exceptions) — the caller still gets {} and the raw-login fallback."""


@st.cache_data(ttl=86400, show_spinner=False, max_entries=4)
def _user_display_map_cached(scope: str, _page: str) -> dict:
    """P4: the directory read is ~11s over 223 rows and sat on the 4h metadata
    TTL, so it cold-started several times a day for a fact that changes when
    someone is HIRED. 24h is the right budget; the Refresh button still clears
    it because ``scope`` carries the refresh salt. ``_page`` is underscored on
    purpose — Streamlit excludes underscored args from the cache key, so every
    page keeps sharing the ONE entry while telemetry still records the caller."""
    from app.core.query import run
    from app.data import directory_sql
    from app.logic.directory import display_name_map
    res = run(directory_sql.user_directory(), page=_page, key="user_directory",
              tier="metadata", source="ACCOUNT_USAGE.USERS (name directory)")
    if not res.usable():
        raise _DirectoryUnavailable(res.error or "user directory unavailable")
    return display_name_map(res.df)


def user_display_map(page: str) -> dict:
    """Cached login -> "First Last" directory from ACCOUNT_USAGE.USERS.

    One small account-global read (metadata tier) shared by every page — the SQL,
    tier and (empty) scope are constant, so all callers hit one cache entry. Pass
    the returned map to logic.directory.attach_display_name / resolve_display so a
    person's name shows next to the login everywhere. Empty on a failed read (the
    resolvers then fall back to the raw login — never blank)."""
    from app.core.query import cache_scope
    from app.data import directory_sql
    try:
        return _user_display_map_cached(cache_scope(directory_sql.user_directory()), page)
    except _DirectoryUnavailable:
        return {}


@st.cache_data(ttl=86400, show_spinner=False, max_entries=4)
def _user_name_parts_map_cached(scope: str, _page: str) -> dict:
    """Exact first/last directory; inner run shares the display-map SQL cache."""
    from app.core.query import run
    from app.data import directory_sql
    from app.logic.directory import name_parts_map

    res = run(directory_sql.user_directory(), page=_page, key="user_directory",
              tier="metadata", source="ACCOUNT_USAGE.USERS (name directory)")
    if not res.usable():
        raise _DirectoryUnavailable(res.error or "user directory unavailable")
    return name_parts_map(res.df)


def user_name_parts_map(page: str) -> dict:
    """Cached login -> (FIRST_NAME, LAST_NAME); empty on directory failure."""
    from app.core.query import cache_scope
    from app.data import directory_sql

    try:
        return _user_name_parts_map_cached(
            cache_scope(directory_sql.user_directory()), page)
    except _DirectoryUnavailable:
        return {}


def with_user_names(df, page: str, *, user_col: str = "USER_NAME", display_col: str = "USER"):
    """Add a "First Last" ``display_col`` (login fallback) beside ``user_col`` using
    the cached directory — the one-liner for detail tables. No-op if the frame has
    no ``user_col``."""
    from app.logic.directory import attach_display_name
    return attach_display_name(df, user_display_map(page), user_col=user_col, display_col=display_col)


def with_user_name_parts(df, page: str, *, user_col: str = "USER_NAME"):
    """Add exact FIRST_NAME/LAST_NAME columns beside a login."""
    from app.logic.directory import attach_name_parts

    return attach_name_parts(df, user_name_parts_map(page), user_col=user_col)


def toggle_cost_hint(key_contains: str) -> str:
    """'What will this cost me?' for heavy on-demand toggles (Codex r7 #15):
    the last observed runtime for a matching query key from THIS session's
    telemetry, or an honest 'first run this session' when unknown."""
    try:
        from app.core.query import query_telemetry
        t = query_telemetry()
        if t.empty:
            return "First run this session — expect a live scan."
        hits = t[t["key"].astype(str).str.contains(key_contains, regex=False)]
        if hits.empty:
            return "First run this session — expect a live scan."
        last_ms = float(hits.iloc[-1]["elapsed_ms"])
        return f"Last run took ~{last_ms / 1000:.1f}s this session (cached repeats are instant)."
    except Exception:  # noqa: BLE001 - a hint must never break a page
        return ""


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
            # rec49: Snowflake compile errors run hundreds of chars with embedded
            # SQL and dominate the panel. Lead with the first line; keep the full
            # text one click away (same idiom as the connection-error screen).
            _err = str(result.error or "").strip()
            _first = _err.splitlines()[0] if _err else ""
            st.error(f"Query failed: {_first}" if _first else "Query failed")
            if _err and _err != _first:
                with st.expander("Error detail"):
                    st.code(_err)
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


def confirm_gate(expected: str, action_label: str, *, key: str, prompt: str = "",
                 object_name: bool = False, enabled: bool = True, **button_kwargs) -> bool:
    """rec42: ONE type-to-confirm gate — renders the confirm input + the action
    button together, returns True only when the button is clicked AND the typed
    value matches ``expected``.

    Matching: an OBJECT NAME (warehouse / database) matches case-INSENSITIVELY —
    typing ``wh_alfa_admin`` satisfies a ``WH_ALFA_ADMIN`` gate — while an ACTION
    VERB (EMERGENCY / CANCEL / RESOLVE) matches EXACT case. ``enabled=False`` keeps
    the button disabled for an ADDITIONAL reason (e.g. a non-operator viewer), so
    the confirm gate and the entitlement gate compose in one place.
    """
    typed = st.text_input(prompt or f"Type {expected} to confirm", key=f"{key}_confirm")
    match = (str(typed).strip().casefold() == str(expected).strip().casefold()) if object_name \
        else (str(typed).strip() == str(expected))
    return st.button(action_label, key=f"{key}_btn",
                     disabled=not (match and enabled), **button_kwargs)


def empty_state(kind: str, message: str, *, hint: str = "") -> None:
    """One vocabulary for the three empty states so COLOR carries meaning
    (house rule 8): 'clean' = verified-clean (green success), 'needs_setup' =
    not installed / configure something (blue info), 'no_data_yet' = nothing has
    loaded yet (quiet caption). Green must never mean 'nothing loaded'."""
    kind = str(kind).lower()
    if kind == "clean":
        st.success(message)
    elif kind == "needs_setup":
        st.info(message)
    else:  # "no_data_yet" + any unknown kind -> the quiet, non-green caption
        st.caption(message)
    if hint:
        st.caption(hint)


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


def confidence_badge(value: object, *, stale: bool = False,
                     coverage_pct: object = None) -> dict[str, str]:
    """Render one evidence-quality treatment wherever a decision is shown."""
    from app.logic.workbench import confidence_state

    confidence = max(0.0, min(safe_float(value), 1.0))
    state = confidence_state(confidence, stale=stale, coverage_pct=coverage_pct)
    pairs = [
        (state["label"], state["state"]),
        (f"{confidence:.0%} confidence", ""),
    ]
    if coverage_pct is not None:
        coverage = max(0.0, min(safe_float(coverage_pct), 100.0))
        pairs.append((f"{coverage:.0f}% coverage", "warn" if coverage < 80 else "ok"))
    status_chips(pairs)
    st.caption(state["reason"])
    return state


def exception_summary(items: list[dict], clean_message: str) -> None:
    """Render only actionable exceptions first, or one compact verified-clean row."""
    rows = []
    for item in items:
        label = html.escape(str(item.get("label") or "Exception"))
        raw_value = item.get("value")
        value = html.escape("" if raw_value is None else str(raw_value))
        detail = html.escape(str(item.get("detail") or ""))
        severity = "bad" if str(item.get("severity") or "").lower() == "bad" else "warn"
        rows.append(
            f'<div class="ow-exception ow-exception--{severity}">'
            f'<span class="ow-exception__label">{label}</span>'
            f'<span class="ow-exception__value">{value}</span>'
            f'<span class="ow-exception__detail">{detail}</span></div>'
        )
    if not rows:
        rows.append(
            '<div class="ow-exception ow-exception--ok">'
            '<span class="ow-exception__label">Checked</span>'
            '<span class="ow-exception__value">Clear</span>'
            f'<span class="ow-exception__detail">{html.escape(clean_message)}</span></div>'
        )
    st.markdown(f'<div class="ow-exceptions">{"".join(rows)}</div>', unsafe_allow_html=True)


def read_model_caption(contract_key: str) -> None:
    """Disclose the summary/evidence split and freshness for a registered surface."""
    from app.logic.read_models import get_contract

    contract = get_contract(contract_key)
    summary_reads = (
        "1 read" if contract.summary_reads == 1
        else f"up to {contract.summary_reads} reads"
    )
    evidence_reads = (
        "1 read" if contract.evidence_reads == 1
        else f"up to {contract.evidence_reads} reads"
    )
    st.caption(
        f"Summary: {contract.summary} ({summary_reads}) · Evidence: {contract.evidence} "
        f"after {contract.trigger} ({evidence_reads}) · {contract.freshness}."
    )


def evidence_gate(contract_key: str, *, key: str, label: str = "Load evidence") -> bool:
    """Registered on-demand evidence gate; summary remains visible above it."""
    read_model_caption(contract_key)
    return bool(st.toggle(label, key=key))


@st.cache_data(ttl=300, show_spinner=False)
def _settings_frame_cached(scope: str):
    from app.core.query import run  # local import: avoid module cycle

    res = run(mart_sql.settings(), page="Settings", key="settings", tier="recent",
              source="SETTINGS")
    if not res.ok:
        # cache_data never caches a raise (Codex r9 #5): a transient failure
        # used to cache an empty frame and pin code defaults for 5 minutes.
        raise RuntimeError(res.error or "settings read failed")
    return res.df


@st.cache_data(ttl=300, show_spinner=False)
def _merged_settings_cached(scope: str) -> dict:
    """The effective settings dict, built ONCE per (salt) scope instead of re-running the
    DEFAULT_SETTINGS+iterrows merge at each of load_settings' ~23 call sites per rerun.
    cache_data returns a fresh copy per call, so callers can mutate safely. A transient
    SETTINGS read failure PROPAGATES (cache_data never caches a raise, so it retries next
    call); an empty/malformed frame yields code defaults (a real, cacheable state)."""
    merged = dict(DEFAULT_SETTINGS)
    # r21 #15: the refresh salt joins the cache key — without it, a manual refresh after
    # editing SETTINGS still served the old frame for up to 5 minutes.
    df = _settings_frame_cached(scope)   # raises on transient failure -> not cached here
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
    else:
        merged["_source"] = "code defaults (SETTINGS not reachable)"
    return merged


def load_settings(page: str) -> dict:
    """Settings from SETTINGS with code defaults as offline fallback."""
    try:
        return _merged_settings_cached(
            f"global:{st.session_state.get('_ow_refresh_salt', '')}")
    except Exception:  # noqa: BLE001 — settings fallback must never break a page
        merged = dict(DEFAULT_SETTINGS)
        merged["_source"] = "code defaults (SETTINGS not reachable)"
        return merged


def daily_spend_wide(page: str) -> QueryResult:
    """PERF #46: ONE FACT_METERING_DAILY scan (150 daily rows, ~few KB) shared by every
    daily-spend caller. Cache identity is (tier, SQL-text, scope) — the caller key is
    telemetry-only — so every caller that routes through here lands on ONE cache entry,
    shared across Brief/Overview/Contract and across pages within a session. Callers slice
    the returned frame with formulas.daily_spend_last_n(df, n).

    tier='hourly' matches FACT_METERING_DAILY's hourly ingest cadence (a 'recent'/300s TTL
    would rescan ~12x/hr for data that changes hourly); the refresh salt + domain salts still
    invalidate immediately on manual refresh / post-write.
    """
    from app.core.query import run  # local import: avoid the app.ui<->app.core cycle
    return run(mart_sql.fact_daily_spend(mart_sql.WIDE_DAILY_SPEND_DAYS),
               page=page, key="fact_daily_wide", tier="hourly",
               source="FACT_METERING_DAILY (150d, shared)")


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

_MINUTE_DURATION_HINTS = {
    "AGE", "AVG", "BILLED", "BLOCKED", "DURATION", "ELAPSED", "EXEC", "EXECUTION",
    "IDLE", "LATE", "LATENCY", "MEDIAN", "MTTA", "MTTR", "OLDEST", "P50", "P90",
    "P95", "P99", "PROVISION", "QUEUE", "QUEUED", "RUNTIME", "SINCE", "TOTAL", "TTD",
    "WAIT", "WALL",
}


def _duration_unit_for_column(col: object) -> str | None:
    """Infer a duration unit from SQL-style column names without mistaking
    ``MIN_CLUSTER_COUNT`` for minutes. The value remains numeric; this only
    selects the display formatter used by tables.
    """
    parts = str(col).upper().split("_")
    if "MS" in parts:
        return "ms"
    if "SEC" in parts or (parts and parts[-1] == "S"):
        return "s"
    if "HOURS" in parts or (parts and parts[-1] == "H" and set(parts) & _MINUTE_DURATION_HINTS):
        return "h"
    if len(parts) > 1 and parts[-1] in {"MIN", "MINS", "MINUTES"}:
        return "min"
    if set(parts) & {"MIN", "MINS", "MINUTES"} and set(parts) & _MINUTE_DURATION_HINTS:
        return "min"
    return None


def _auto_formats(df, skip: set) -> dict:
    """Consistent number display by column-name convention (item: every table
    shows $ and thousands separators without per-site column_config)."""
    from pandas.api import types as ptypes

    from app.logic.formulas import humanize_duration

    fmts: dict = {}
    for col in df.columns:
        if not ptypes.is_numeric_dtype(df[col]):
            continue
        c = str(col).upper()
        duration_unit = _duration_unit_for_column(col)
        if duration_unit:
            # Duration consistency outranks a caller's legacy decimal NumberColumn.
            # Styler changes the display only; the dataframe stays numeric.
            fmts[col] = lambda v, _unit=duration_unit: humanize_duration(v, _unit)
        elif col in skip:
            continue
        elif c.endswith(("_USD", "_PRICE")) or c == "USD":
            fmts[col] = "${:,.2f}"
        elif "CREDITS" in c:
            fmts[col] = "{:,.2f}"
        elif _byte_unit_for_column(col):
            # Reconciliation fix (2026-08-17): a byte column shown as fixed-decimal
            # GB/TB hid sub-unit values ("0.0 GB", "0.000 TB") and never matched
            # Snowsight's MB/GB/TB scale. Humanize like durations (Styler = display
            # only; the frame stays numeric so sort/CSV keep the raw value).
            from app.logic.formulas import humanize_bytes
            _factor = _byte_unit_for_column(col)[1]
            fmts[col] = lambda v, _f=_factor: humanize_bytes(safe_float(v) * _f)
        elif c.endswith(("_PCT", "_SHARE", "_HOURS")) or c == "HIT_PCT":
            fmts[col] = "{:,.1f}"
        elif c.endswith(_COUNT_SUFFIXES):
            fmts[col] = "{:,.0f}"
    # rec33: movement/delta columns always show a LEADING SIGN so direction
    # survives red-green color-blindness (delta_css is color-only). Turn each
    # delta column's chosen format into a signed one (or a signed integer default).
    for col in df.columns:
        if not is_delta_column(col) or col in skip or not ptypes.is_numeric_dtype(df[col]):
            continue
        f = fmts.get(col)
        if callable(f):
            continue  # a duration formatter (rec26) already signs its own negatives
        fmts[col] = f.replace("{:", "{:+", 1) if f else "{:+,.0f}"
    return fmts


# Pandas Styler is per-cell Python. Bound BOTH dimensions: a 350x30 evidence
# table is much more expensive than a 450x4 summary even though the row-only
# rule called the first "small." Above either budget, use Arrow-native formats.
STYLER_MAX_ROWS = 400
STYLER_MAX_CELLS = 6_000
EAGER_CSV_MAX_ROWS = 200
EAGER_CSV_MAX_CELLS = 1_500

# Styler format -> printf equivalent for the large-frame path. Commas are
# lost above the cap (printf has no grouping); that trade is deliberate.
_PRINTF_EQUIV = {"${:,.2f}": "$%.2f", "{:,.2f}": "%.2f", "{:,.1f}": "%.1f", "{:,.0f}": "%.0f",
                 # rec33: signed delta variants so >400-row tables keep the +/- too
                 "${:+,.2f}": "$%+.2f", "{:+,.2f}": "%+.2f", "{:+,.1f}": "%+.1f", "{:+,.0f}": "%+.0f"}

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


def _slugify(text: object, fallback: str = "table") -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-") or fallback


# rec31: a trailing non-duration unit token becomes a parenthesized display unit on
# the header (the CSV keeps the raw name). Duration tokens are removed wherever they
# occur because their values carry the humanized h/min/s/ms unit.
_HEADER_UNITS = {"GB": "GB", "TB": "TB", "MB": "MB", "TIB": "TiB",
                  "PCT": "%", "HOURS": "h", "MIN": "min"}
def _prettify_header(col: object) -> str:
    """rec13/rec31: SQL-shaped UPPER_SNAKE -> Title Case for DISPLAY only (df.to_csv
    keeps the raw name). A trailing unit token renders as "(GB)"/"(%)"/"(h)"; a
    duration token (S/SEC/MS) is dropped because the value itself is humanized to
    H/M/S (rec26). Already-human headers (mixed case, or a space) are left alone."""
    s = str(col)
    if s != s.upper() or " " in s:            # already human
        return s
    parts = s.split("_")
    unit_suffix = ""
    duration_unit = _duration_unit_for_column(col)
    if duration_unit:
        matching = {
            "ms": {"MS"}, "s": {"S", "SEC"},
            "min": {"MIN", "MINS", "MINUTES"}, "h": {"H", "HOURS"},
        }[duration_unit]
        parts = [part for part in parts if part not in matching]
    elif len(parts) > 1:
        tail = parts[-1]
        if tail in _HEADER_UNITS:             # rec31: show the unit in the header
            unit_suffix = f" ({_HEADER_UNITS[tail]})"
            parts = parts[:-1]
    _keep = {"USD", "AI", "ID", "MB", "GB", "TB", "TIB", "MS", "SEC", "PCT", "P95", "P50", "SLA", "CS", "QAS"}
    words = [w if w in _keep else w.capitalize() for w in parts]
    return " ".join(words) + unit_suffix


_BYTE_UNIT_FACTOR = {"MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
                     "TIB": 1024 ** 4, "GIB": 1024 ** 3}


def _byte_unit_for_column(col: object) -> tuple[str, int] | None:
    """('GB', 1024**3) for a byte MAGNITUDE column, else None. Token-based, not
    suffix-based, so both SPILL_REMOTE_GB (suffix) and TB_SCANNED / bare GB
    (prefix) are caught. A 'PER' token means a RATE (SPILL_GB_PER_DAY, $/TiB) —
    never humanized as a magnitude. Drives the Styler humanizer + the large-frame
    printf fallback. (_USD/_PRICE/CREDITS are matched earlier in _auto_formats.)"""
    tokens = str(col).upper().split("_")
    if "PER" in tokens:
        return None
    for tok in tokens:
        if tok in _BYTE_UNIT_FACTOR:
            return tok, _BYTE_UNIT_FACTOR[tok]
    return None


def _duration_display_format(col: object) -> str:
    """rec26: the large-frame (>400 row) Arrow path can't render humanized H/M/S
    (NumberColumn takes a printf, not a callable). Carry the RAW value with its unit
    instead — "850 ms" / "1800.0 s" — so a big duration table is never a unitless bare
    number and the header can drop the token consistently with the Styler path."""
    unit = _duration_unit_for_column(col)
    return {"ms": "%.0f ms", "s": "%.1f s", "min": "%.1f min", "h": "%.1f h"}.get(
        unit, "%.1f s")


def _callable_display_format(col: object) -> str:
    """Large-frame printf fallback for a humanizer callable — a byte column keeps
    its raw value WITH its unit at higher precision ("%.2f GB", better than the
    old "0.0"); everything else falls to the duration formats."""
    byte_unit = _byte_unit_for_column(col)
    if byte_unit:
        return f"%.2f {byte_unit[0]}"
    return _duration_display_format(col)


def _export_filename(seq: int, slug: str | None) -> str:
    """rec14: a self-identifying CSV name — `overwatch-{page}-{table}-{YYYYMMDD}.csv` —
    so a folder of downloads is readable instead of overwatch_table_1/2/3.csv. The
    page comes from the same _ow_dl_page identity the prep-slot uses; the date is
    account-time to match the CSV contents."""
    page = _slugify(st.session_state.get("_ow_dl_page", ""), "overwatch")
    name = _slugify(slug) if slug else f"table-{seq}"
    try:
        day = account_today().strftime("%Y%m%d")
    except Exception:  # noqa: BLE001 - a missing session date just drops the suffix
        day = ""
    parts = ["overwatch", page, name, day]
    return "-".join(p for p in parts if p) + ".csv"


def _render_table(df, *, height: int | None, column_config: dict | None,
                  key: str | None = None, selectable: bool = False,
                  slug: str | None = None, days: int | None = None,
                  size_note: bool = True, sort_label: str = "",
                  totals: tuple = ()) -> int | None:
    if df is None or getattr(df, "empty", True):
        st.dataframe(df, hide_index=True, width="stretch")
        return None
    display_df = df
    try:  # display-timezone conversion is display-only; the CSV keeps account time
        ts_cols = [] if getattr(df, "attrs", {}).get("_ow_tz_converted") else timestampish_columns(df.columns)
        if ts_cols:
            display_df, tz_note = localize_timestamps(df, ts_cols)
            # rec34: the "Times shown in X" note is identical for every converted
            # table — print it ONCE per page (flag reset in page_header), not above
            # all six tables in a section. Note only appears when a NON-account
            # display tz is opted in (the account-tz default renders no note at
            # all); inside an @st.fragment rerun the page-global flag stays set, so
            # the note may not re-render there — cosmetic, and only for that opt-in.
            if tz_note and not st.session_state.get("_ow_tz_note_shown"):
                st.caption(tz_note)
                st.session_state["_ow_tz_note_shown"] = True
    except Exception:  # noqa: BLE001 - conversion is cosmetic
        display_df = df
    data = display_df
    fmts = _auto_formats(df, set(column_config or {}))
    _cell_count = len(df) * max(1, len(df.columns))
    if len(df) <= STYLER_MAX_ROWS and _cell_count <= STYLER_MAX_CELLS:
        try:
            styler = display_df.style
            for col in status_columns_in(list(df.columns)):
                styler = styler.map(lambda v, _c=col: status_css(_c, v), subset=[col])
            # A3: sign-color movement columns (Δ up = red/worse, down = green/better)
            for col in df.columns:
                if is_delta_column(col):
                    styler = styler.map(lambda v: delta_css(v), subset=[col])
            if fmts:
                styler = styler.format(fmts, na_rep="—")  # rec27: one no-value glyph (em-dash)
            data = styler
        except Exception:  # noqa: BLE001 - styling is cosmetic, table must render
            data = display_df
    else:
        # Large frame: skip Styler entirely; carry the number formats through
        # column_config so display stays consistent (minus thousands commas).
        cfg = dict(column_config or {})
        for col, fmt in fmts.items():
            if col in cfg and not callable(fmt):
                continue
            if callable(fmt):
                # rec26 humanizer (a callable) has no printf equivalent; show the raw
                # value WITH its unit so a >400-row duration column is never a unitless
                # bare number (the header drops the token exactly as on the Styler path).
                _existing = cfg.get(col)
                _dlabel = _prettify_header(col)
                _kwargs = {}
                if isinstance(_existing, dict):
                    _dlabel = _existing.get("label") or _dlabel
                    _kwargs = {
                        name: _existing[name]
                        for name in ("width", "help", "disabled", "required", "pinned", "alignment")
                        if _existing.get(name) is not None
                    }
                try:
                    cfg[col] = st.column_config.NumberColumn(
                        _dlabel if _dlabel != str(col) else None,
                        format=_callable_display_format(col), **_kwargs)
                except Exception:  # noqa: BLE001
                    pass
            elif fmt in _PRINTF_EQUIV:
                try:
                    cfg[col] = st.column_config.NumberColumn(format=_PRINTF_EQUIV[fmt])
                except Exception:  # noqa: BLE001
                    break
        column_config = cfg
    # rec13: human-readable headers for SQL-shaped UPPER_SNAKE columns (display only —
    # the CSV keeps raw names). Columns with an explicit CALLER column_config (units,
    # custom labels) or a large-frame printf NumberColumn are left as-is.
    #
    # The first column of a wide (>=8-col) table is ALSO pinned so horizontal scroll
    # keeps the row's identity. r5-bug: pinning used to run AFTER the prettifier had put
    # the first column in the config, so it saw `first in cfg` and bailed — the identity
    # column silently un-pinned. Now the pin and the pretty label go into the SAME Column
    # (built off the CALLER config, not the prettifier's, so a caller override still wins).
    caller_cfg = set(column_config or {})
    _cfg = dict(column_config or {})
    _pin_col = df.columns[0] if len(df.columns) >= 8 and df.columns[0] not in caller_cfg else None
    for _col in df.columns:
        if _col in caller_cfg:
            continue
        _pretty = _prettify_header(_col)
        _label = _pretty if _pretty != str(_col) else None
        _help = COLUMN_HELP.get(str(_col).upper())   # rec32: which-dollar-is-this on the header
        if _label is None and _col != _pin_col and not _help:
            continue
        try:
            _cfg[_col] = st.column_config.Column(_label, pinned=True, help=_help) if _col == _pin_col \
                else st.column_config.Column(_label, help=_help)
        except TypeError:  # runtime predates pinned= : keep the relabel + help, drop the pin
            _cfg[_col] = st.column_config.Column(_label, help=_help)
        except Exception:  # noqa: BLE001 - relabel/pin/help is cosmetic, never break the table
            pass
    column_config = _cfg or None
    if height is None and len(df) > 10:
        height = TABLE_H_XL
    kwargs = {"hide_index": True, "width": "stretch", "column_config": column_config}
    if isinstance(height, int) and height > 0:  # newer Streamlit rejects height=None
        kwargs["height"] = height
    if totals:
        st.caption("Σ " + " · ".join(f"{label}: {value}" for label, value in totals))
    selected: int | None = None
    if selectable and key:
        try:
            event = st.dataframe(data, key=key, on_select="rerun",
                                 selection_mode="single-row", **kwargs)
            rows = list(getattr(getattr(event, "selection", None), "rows", None) or [])
            selected = int(rows[0]) if rows else None
            # st.dataframe's row selection is STICKY: it re-emits its raw positional index
            # on every rerun, even after the frame shrinks (a row resolved, the company/
            # window filter narrowed, or a live frame was re-read smaller). An index past
            # the current end is a stale selection, so report it as no-selection. `data`
            # has the same row count as `df`, so this one clamp immunizes every caller's
            # `frame.iloc[sel]` against an out-of-range IndexError instead of a bounds guard
            # repeated at each of ~40 drill sites.
            if selected is not None and not (0 <= selected < len(df)):
                selected = None
        except TypeError:  # runtime without selection support: render, no selection
            st.dataframe(data, **kwargs)
    else:
        st.dataframe(data, **kwargs)
    try:  # every real table is exportable; tiny KPI-ish frames skip the button
        if len(df) >= 4:
            seq = int(st.session_state.get("_ow_dl_seq", 0))
            st.session_state["_ow_dl_seq"] = seq + 1
            if len(df) <= EAGER_CSV_MAX_ROWS and _cell_count <= EAGER_CSV_MAX_CELLS:
                # N9: keep one-click export, but serialize ONCE per content instead
                # of re-encoding the frame on every 30s rerun (the large-frame path
                # already memoizes; the small path re-shipped the full CSV each pass).
                # The fingerprint guard means a stale slot never serves wrong bytes.
                _fp = _download_fingerprint(df)
                _cache = st.session_state.get("_ow_dlsmall")
                if not isinstance(_cache, dict):
                    _cache = {}
                    st.session_state["_ow_dlsmall"] = _cache
                if len(_cache) > 64:      # bound memory; a rare re-encode is harmless
                    _cache.clear()
                _dlk = f"{key or ''}_{seq}"
                _slot = _cache.get(_dlk)
                if (isinstance(_slot, dict) and _slot.get("fp") == _fp
                        and isinstance(_slot.get("bytes"), (bytes, bytearray))):
                    _csv = bytes(_slot["bytes"])
                else:
                    _csv = df.to_csv(index=False).encode("utf-8")
                    _cache[_dlk] = {"fp": _fp, "bytes": _csv}
                # Downloads are frontend-only, no rerun (r19 #20).
                st.download_button("⬇ CSV", _csv,  # rec30: legible affordance, not a bare glyph
                                   file_name=_export_filename(seq, slug), mime="text/csv",
                                   key=f"ow_dl_{key or ''}_{seq}", type="tertiary",
                                   on_click="ignore",
                                   help="Download this table as CSV (account time).")
            else:
                # r13 #19 + r19 #19: big frames build their CSV ONCE at prep
                # (bytes stored, not a boolean that reserialized every rerun)
                # and the download no longer reruns the app.
                # T1.6: the old positional key (ow_dlprep_<key>_<seq>) collided
                # across pages — _ow_dl_seq resets per page_header, and nothing
                # ever evicted the blob, so a page/section switch served the
                # PREVIOUS table's bytes under a reused seq. Key by PAGE + a
                # content fingerprint of THIS frame, keep ONE slot per session
                # (self-evicting), and serve the ready-branch only on an exact match.
                _fp = _download_fingerprint(df)
                _slot = st.session_state.get("_ow_dlprep")
                _ready = (isinstance(_slot, dict) and _slot.get("fp") == _fp
                          and isinstance(_slot.get("bytes"), (bytes, bytearray)))
                if _ready:
                    st.download_button("⬇ CSV ready", bytes(_slot["bytes"]),
                                       file_name=_export_filename(seq, slug), mime="text/csv",
                                       key=f"ow_dl_{key or ''}_{seq}", type="tertiary",
                                       on_click="ignore")
                elif st.button("⬇ CSV", key=f"ow_dlbtn_{key or ''}_{seq}", type="tertiary",
                               help=f"Prepare {len(df):,} rows as CSV (account time)."):
                    st.session_state["_ow_dlprep"] = {
                        "fp": _fp, "bytes": df.to_csv(index=False).encode("utf-8")}
                    st.rerun()
    except Exception:  # noqa: BLE001 - export is a convenience, never break the table
        pass
    # rec31: declare size only on tables that SCROLL (>10 rows -> height-capped, so
    # the total is not visible on screen). Fully-visible small tables need no
    # caption, and a caller that prints its own count passes size_note=False.
    if size_note and len(df) > 10:
        _cap = f"{len(df):,} rows"
        if sort_label:                       # rec30: never make a reader guess if a list is ranked
            _cap += f" · by {sort_label}"
        if isinstance(days, int) and days > 0:
            _cap += f" · last {days}d"
        st.caption(_cap)
    return selected


def _download_fingerprint(df) -> str:
    """Session-stable identity for the current table: page + shape + columns +
    a content hash (T1.6). Bounds the hash cost and never raises — a bad hash
    just falls back to page+shape, which is still page-scoped."""
    import hashlib

    import pandas as pd
    page = str(st.session_state.get("_ow_dl_page", ""))
    base = f"{page}|{df.shape}|{tuple(map(str, df.columns))}"
    try:
        content = str(int(pd.util.hash_pandas_object(df, index=True).sum()))
    except Exception:  # noqa: BLE001 - fingerprint is best-effort, never break export
        content = ""
    return hashlib.md5(f"{base}|{content}".encode()).hexdigest()[:16]


def styled_table(df, *, height: int | None = None, column_config: dict | None = None,
                 slug: str | None = None, days: int | None = None,
                 size_note: bool = True, sort_label: str = "",
                 totals: tuple = ()) -> None:
    """st.dataframe with semantic status colors, convention-based number
    formats, a height cap, a size caption, and a CSV download. ``slug`` names the
    export file (rec14); ``days`` adds the window to the size caption (rec31);
    ``sort_label`` declares the order in that caption ("by $ desc", rec30);
    ``totals`` renders additive ``(label, display_value)`` pairs above the table;
    ``size_note=False`` suppresses that caption when the caller states its own count."""
    _render_table(df, height=height, column_config=column_config, slug=slug,
                  days=days, size_note=size_note, sort_label=sort_label, totals=totals)


def selectable_table(df, key: str, *, height: int | None = None,
                     column_config: dict | None = None, slug: str | None = None,
                     days: int | None = None, size_note: bool = True,
                     sort_label: str = "") -> int | None:
    """styled_table + single-row click selection; returns the positional row
    index into ``df``, or None. A stale sticky selection whose index is past the
    current frame end is reported as None (not the raw out-of-range index), so a
    caller's ``df.iloc[sel]`` is always in-bounds. Degrades to a plain table on
    runtimes without selections."""
    return _render_table(df, height=height, column_config=column_config,
                         key=key, selectable=True, slug=slug, days=days,
                         size_note=size_note, sort_label=sort_label)


def selectable_nav_table(df, key: str, on_select, *, height: int | None = None,
                         column_config: dict | None = None, slug: str | None = None,
                         days: int | None = None, size_note: bool = True,
                         sort_label: str = "") -> None:
    """selectable_table with the sticky-selection guard built in (rec29).

    st.dataframe's selection is sticky and re-emits on EVERY rerun, so acting on
    a raw selection re-fires the action each pass. This calls ``on_select(row)``
    only when the selection CHANGES from the last one seen — the idiom Overview
    hand-rolled, extracted so no future page rediscovers the rerun loop.
    """
    sel = selectable_table(df, key=key, height=height, column_config=column_config,
                           slug=slug, days=days, size_note=size_note, sort_label=sort_label)
    seen_key = f"_ow_navsel_{key}"
    if sel is not None and sel != st.session_state.get(seen_key):
        st.session_state[seen_key] = sel
        on_select(sel)


def entity_nav_table(df, key: str, *, key_col: str, entity_type: str = "",
                     type_col: str | None = None, height: int | None = None,
                     column_config: dict | None = None, slug: str | None = None,
                     days: int | None = None, size_note: bool = True,
                     sort_label: str = "") -> None:
    """Universal entity drill (UI22): a table whose row click opens that entity
    in Control Room -> Entity 360.

    Any table whose rows ARE entities becomes a deep-link with one call instead
    of a hand-rolled ``selectable_nav_table`` + navigation handler at each site.
    ``entity_type`` is the fixed type for a homogeneous table (every row a
    WAREHOUSE, USER, TASK, ...); pass ``type_col`` when rows carry their own
    ENTITY_TYPE (it takes precedence per row, falling back to ``entity_type``).
    Degrades to a plain ``styled_table`` when ``key_col`` is absent or the
    runtime has no row selection.
    """
    import pandas as pd

    frame = df.reset_index(drop=True) if df is not None else pd.DataFrame()
    if getattr(frame, "empty", True) or key_col not in getattr(frame, "columns", []):
        styled_table(frame, height=height, column_config=column_config, slug=slug,
                     days=days, size_note=size_note, sort_label=sort_label)
        return

    def _open(index: int) -> None:
        from app.core.state import request_navigation

        row = frame.iloc[int(index)]
        per_row = str(row.get(type_col) or "").strip() if type_col and type_col in frame.columns else ""
        kind = per_row or entity_type
        entity_key = str(row.get(key_col) or "").strip()
        if kind and entity_key:
            request_navigation("Control Room", "Entity 360",
                               context={"entity_type": kind, "entity_key": entity_key})

    selectable_nav_table(frame, key=key, on_select=_open, height=height,
                         column_config=column_config, slug=slug, days=days,
                         size_note=size_note, sort_label=sort_label)


def decision_rows(
    df,
    *,
    key: str,
    decision_col: str,
    why_col: str,
    impact_col: str = "",
    confidence_col: str = "",
    owner_col: str = "",
    status_col: str = "",
    next_col: str = "",
    context_cols: tuple[str, ...] = (),
    on_select=None,
    height: int | None = None,
    sort_label: str = "decision priority",
    impact_help: str = "",
    confidence_help: str = "",
    confidence_label: str = "Confidence",
) -> int | None:
    """Compact decision contract: decision, why, impact, confidence, owner and next.

    ``impact_help``/``confidence_help`` document what those columns MEAN on this
    surface (measured vs estimated; evidence-heuristic vs authored belief) — the
    Impact $ and the 0-1 Confidence look identical across surfaces but are not the
    same thing, so each caller states its own basis (the metric_registry ethos).
    ``confidence_label`` (rec28) renames the visible Confidence header so an
    evidence heuristic and an authored belief are told apart at a glance, not only
    on hover — e.g. "Confidence (evidence)" vs "Confidence (authored)"."""
    import pandas as pd

    source = df if df is not None else pd.DataFrame()
    mapping = [
        (decision_col, "Decision"),
        (why_col, "Why"),
        (impact_col, "Impact"),
        (confidence_col, confidence_label),
        (owner_col, "Owner"),
        (status_col, "Status"),
        (next_col, "Next"),
    ]
    selected = [(column, label) for column, label in mapping if column and column in source.columns]
    selected.extend((column, column) for column in context_cols if column in source.columns)
    view = source[[column for column, _ in selected]].copy()
    view.columns = [label for _, label in selected]
    config = {}
    if "Impact" in view.columns:
        config["Impact"] = st.column_config.NumberColumn(
            "Impact", format="$%.2f", help=impact_help or None)
    if confidence_label in view.columns:
        config[confidence_label] = st.column_config.ProgressColumn(
            confidence_label, min_value=0, max_value=1, help=confidence_help or None,
        )
    selection = selectable_table(
        view, key=key, height=height, column_config=config,
        sort_label=sort_label,
    )
    if on_select is not None and selection is not None:
        seen_key = f"_ow_decision_{key}"
        if selection != st.session_state.get(seen_key):
            st.session_state[seen_key] = selection
            on_select(selection)
    return selection


def blast_radius(warehouse: str, page: str) -> None:
    """Who this operator action would hit: last-7d users/roles/tags on the
    warehouse. Rendered above every suspend/resize confirmation so the
    action is informed, not brave. Degrades silently if the scan fails."""
    from app.core.query import run  # local import: avoid module cycle
    from app.data import ops_sql

    wh = str(warehouse or "").strip()
    if not wh:
        return
    try:
        res = run(ops_sql.warehouse_blast_radius(wh, 7), page=page,
                  key=f"blast_{wh}", tier="recent",
                  source="QUERY_HISTORY (7d, this warehouse)")
    except ValueError:
        return  # unusable identifier: no preview, the confirm gate still holds
    if not res.ok:
        st.caption(f"Blast radius unavailable: {res.error}")
        return
    if res.empty:
        st.caption("Blast radius (7d): no queries on this warehouse — low-risk change.")
        return
    df = res.df
    tags = df["SAMPLE_TAG"].dropna().astype(str)
    tagged = tags[tags.str.len() > 0]
    st.warning(
        f"Blast radius (7d): {len(df)} user(s) ran {int(df['QUERIES'].sum()):,} queries here"
        + (f"; scheduled/tooling tags present ({tagged.iloc[0][:40]}…)" if len(tagged) else "")
        + ". Review before executing."
    )
    styled_table(with_user_names(df, page), height=TABLE_H_SM)   # show WHO you'd interrupt


def notify(ok: bool, msg: str) -> None:
    """Operator-action feedback. rec48: a SUCCESS is transient good news — the
    toast (auto-dismiss ~4s) carries it, and the write's own rerun re-renders the
    changed table as the durable receipt, so success no longer ALSO leaves a
    persistent full-width green banner on every write. A FAILURE must NOT vanish
    before it is read (a toast can auto-dismiss before the operator looks back),
    so it still gets a persistent inline st.error alongside the toast.

    Callers that write a NON-idempotent row must st.rerun() on success so the
    changed table is that durable receipt (and the form/button reset) — otherwise
    the toast is the only cue and a second click double-writes."""
    toasted = False
    try:
        st.toast(msg[:120], icon="✅" if ok else "⚠️")
        toasted = True
    except Exception:  # noqa: BLE001 - toast is a nicety
        pass
    if not ok:
        st.error(msg)          # failures ALWAYS get a persistent banner
    elif not toasted:
        st.success(msg)        # success falls back to a banner only if the toast didn't land


def mark_refreshed() -> None:
    """Stamp the session's last data-refresh time (called on load/refresh)."""
    from datetime import datetime
    st.session_state["_ow_refreshed_at"] = datetime.now()


def last_refreshed_note() -> str:
    """Human 'session refreshed Nm ago' for the always-visible sidebar indicator.

    rec48: this tracks the SESSION load / manual Refresh, NOT data freshness
    (per-tier caches mean the data may be newer). 'Updated' overclaimed that —
    say 'Session refreshed' so it does not read as a data-freshness stamp."""
    from datetime import datetime
    ts = st.session_state.get("_ow_refreshed_at")
    if not ts:
        return "Live · cached per tier"
    secs = max(0, int((datetime.now() - ts).total_seconds()))
    if secs < 60:
        return f"Session refreshed {secs}s ago"
    mins = secs // 60
    return f"Session refreshed {mins}m ago" if mins < 60 else f"Session refreshed {mins // 60}h ago"


def download_text_button(label: str, text: str, filename: str) -> None:
    """A real download (the old app's 'copy' button that didn't copy is dead)."""
    st.download_button(label, data=text, file_name=filename, mime="text/plain",
                       on_click="ignore")


def log_ui_event(kind: str, page: str = "") -> None:
    """Product-usage event (V027 rider): saved_view_apply, csv_export,
    remediation_exec, ... — APP_USAGE rows with RENDER_MS NULL. Best-effort;
    silently no-ops pre-V027 (missing columns) or when usage is disabled."""
    import streamlit as st

    from app.core.query import _buffer_write
    from app.core.sqlsafe import sql_literal

    if st.session_state.get("_ow_usage_off") or st.session_state.get("_ow_usage_oldshape"):
        return
    from app.core.identity import identity_sql
    # N12: enqueue into the shared write buffer (flushed once per rerun) instead of
    # one INSERT round trip per UI action. A flush failure downgrades usage to its
    # old shape, matching this function's oldshape guard above (it then no-ops).
    _buffer_write(
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, EVENT_KIND, IS_RERUN, USER_NAME) ",
        f"SELECT {sql_literal(str(page or kind)[:80])}, {sql_literal(str(kind)[:40])}, FALSE, {identity_sql()}",
        off_flag="_ow_usage_off", downgrade_flag="_ow_usage_oldshape")
