"""Global filter state and query-param navigation."""

from __future__ import annotations

import streamlit as st

from app.companies import COMPANIES, DEFAULT_COMPANY, DEFAULT_ENVIRONMENT
from app.config import DEFAULT_DAY_WINDOW, TRIAGE_WINDOW_OPTIONS

_PAGE_PARAM = "page"

FILTER_DEFAULTS = {
    "flt_company": DEFAULT_COMPANY,
    "flt_environment": DEFAULT_ENVIRONMENT,
    "flt_days": DEFAULT_DAY_WINDOW,
    "flt_warehouse_contains": "",
    "flt_user_contains": "",
    "flt_database": "",
    "flt_schema_contains": "",
}


def init_filters() -> None:
    for key, default in FILTER_DEFAULTS.items():
        st.session_state.setdefault(key, default)
    if st.session_state["flt_company"] not in COMPANIES:
        st.session_state["flt_company"] = DEFAULT_COMPANY
    # Environment is retained only as a saved-view compatibility field. It no
    # longer has a visible control and must never invisibly narrow Database.
    st.session_state["flt_environment"] = DEFAULT_ENVIRONMENT
    if st.session_state["flt_days"] not in TRIAGE_WINDOW_OPTIONS:
        st.session_state["flt_days"] = DEFAULT_DAY_WINDOW
    # A database selection from another company resets to All. Bug round 2 B7:
    # validate with the SAME live-inventory classification the picker uses, not the
    # static company/environment tuples — otherwise DBA_MAINT_DB and any new
    # ALFA_*/TRXS_* database (offered by the SHOW-DATABASES picker) is silently
    # un-picked on the next run, and applied saved views lose their DB scope.
    from app.companies import classify_databases  # local import: tiny, avoids cycles
    _db = str(st.session_state["flt_database"] or "").strip()
    if _db and _db.upper() not in classify_databases(
            (_db,), st.session_state["flt_company"]):
        st.session_state["flt_database"] = ""


def filters() -> dict:
    init_filters()
    from app.logic.date_windows import (
        resolve_window_days,
        window_bounds,
        window_scope_label,
    )

    window = st.session_state["flt_days"]
    return {
        "company": str(st.session_state["flt_company"]),
        "environment": str(st.session_state["flt_environment"]),
        "days": resolve_window_days(window),
        "window": window,
        # (start, end_exclusive) account-clock dates for the three calendar presets, else None
        # (trailing windows). Only LAST_MONTH is a complete prior calendar month; CURRENT_MONTH /
        # CURRENT_YEAR are partial period-to-date windows (first of period .. tomorrow-exclusive).
        # Gate any "vs prior calendar month" comparison on date_windows.is_prior_month_window(bounds),
        # never on `bounds is not None`. Builders that support it pass this to
        # resolve_effective_window(..., bounds=...) to emit an explicit calendar range instead of
        # the trailing today-anchored one.
        "bounds": window_bounds(window),
        "window_label": window_scope_label(window),
        "warehouse_contains": str(st.session_state["flt_warehouse_contains"]),
        "user_contains": str(st.session_state["flt_user_contains"]),
        "database": str(st.session_state["flt_database"]),
        "schema_contains": str(st.session_state["flt_schema_contains"]),
    }


def apply_filters(**kwargs) -> None:
    """Set top-bar filters programmatically (deep links, saved views).

    Values are validated the same way the widgets validate them. Existing
    integer-only saved views remain compatible; new views preserve MTD/YTD.
    """
    from app.logic.date_windows import normalize_window

    requested_window = kwargs.get("window")
    if requested_window is not None:
        st.session_state["flt_days"] = normalize_window(requested_window)
    elif kwargs.get("days") is not None:
        st.session_state["flt_days"] = normalize_window(kwargs["days"])

    mapping = {
        "company": "flt_company",
        "warehouse_contains": "flt_warehouse_contains", "user_contains": "flt_user_contains",
        "database": "flt_database", "schema_contains": "flt_schema_contains",
    }
    for name, value in kwargs.items():
        key = mapping.get(name)
        if key is None or value is None:
            continue
        st.session_state[key] = value


def clamp_page(page: str, allowed: tuple[str, ...] | list[str]) -> str:
    """Where a navigation to ``page`` may land for a profile offering ``allowed`` (B8).

    ``page`` itself when the profile offers it -- or when no page was asked or the profile is
    unreadable (main.py's last-line deny still holds then). Otherwise Overview when the profile has
    it (every profile but MONITOR), else the profile's own landing page (v4.610.0: MONITOR offers
    only Cost Intelligence and Operations, so a jump to Alerts lands on Cost Intelligence)."""
    if not page or not allowed or page in allowed:
        return page
    return "Overview" if "Overview" in allowed else allowed[0]


def request_navigation(page: str, section: str = "", filters: dict | None = None,
                       context: dict | None = None, *,
                       capture_origin: bool = True) -> None:
    """Queue a cross-page jump; consumed at the top of the NEXT run, before
    any widget instantiates (Streamlit forbids touching a live widget's key).

    C9: a cross-PAGE jump also captures its ORIGIN (the page + active section it
    left), stamped with the destination, so the destination's header can offer a
    one-hop "Back to <origin>". The return jump itself passes
    ``capture_origin=False`` so returning never creates a boomerang origin."""
    # v4.597 (Option C): a retired target (e.g. the old Decision Studio sections) is remapped to its
    # new home FIRST — defensively, before the clamp below would send it to Overview.
    from app.logic.navigate import remap_legacy_target
    page, section = remap_legacy_target(page, section)
    # Clamp off-profile targets HERE (B8 also clamps on consume), then NO-OP a jump
    # that resolves to the current page with no section change. A sticky st.dataframe
    # selection re-fires request_navigation every rerun; without this, an EXECUTIVE
    # clicking a Top-action row (whose Control Room jump clamps back to Overview) spun
    # forever — request_navigation -> rerun -> selection re-emits -> request_navigation.
    if page:
        from app.config import PAGES_BY_PROFILE
        from app.core.session import active_profile, current_role
        allowed = PAGES_BY_PROFILE.get(active_profile(current_role()), ())
        clamped = clamp_page(page, allowed)
        if clamped != page:
            # the off-profile target's section and drill context belong to a page this viewer never
            # reaches; dropping them also lets the no-op below catch a clamp back onto the current page
            page, section, context = clamped, "", None
    if page == st.session_state.get("_ow_page") and not section and not filters and not context:
        return
    # C9: origin only for a genuine page change — a section-only hop within the
    # current page has nothing to "return" from.
    _cur = str(st.session_state.get("_ow_page") or "")
    origin = None
    if capture_origin and page and _cur and page != _cur:
        from app.logic.navigate import PAGE_SECTION_KEYS
        _skey = PAGE_SECTION_KEYS.get(_cur)
        origin = {"page": _cur,
                  "section": str(st.session_state.get(_skey) or "") if _skey else "",
                  "dest": page}
    st.session_state["_ow_nav_pending"] = {
        "page": page, "section": section, "filters": dict(filters or {}),
        "context": dict(context or {}), "origin": origin,
    }
    st.rerun()


def consume_pending_navigation() -> None:
    """Call first thing in main(): applies a queued jump pre-instantiation."""
    pending = st.session_state.pop("_ow_nav_pending", None)
    if not pending:
        return
    # Legacy remap (rec8, then v4.597 Option C): a stale saved-view / default-landing naming a
    # retired target — the old {Control Room, Decision Studio} section, or any Decision Studio
    # page/section — is rewritten to its new home (navigate.LEGACY_TARGETS) BEFORE the profile
    # clamp, so it lands on the real page instead of Overview or a first section.
    from app.logic.navigate import remap_legacy_target
    _lp, _ls = remap_legacy_target(str(pending.get("page") or ""), str(pending.get("section") or ""))
    pending = {**pending, "page": _lp, "section": _ls}
    # B6 (bug round 2) used to reset a fixed `_ow_jump` key here, after the early return
    # above. Since C3 (v4.313) the Jump-to box is keyed `_ow_jump_{nonce}` and clears itself
    # by remounting under a bumped nonce (main._global_jump), so there is nothing to reset.
    from app.logic.navigate import PAGE_SECTION_KEYS

    page = str(pending.get("page") or "")
    # B8: never route a viewer to a page their profile does not offer — landing on
    # a dead page or forcing an off-profile selection. Clamp to the viewer's pages.
    if page:
        from app.config import PAGES_BY_PROFILE
        from app.core.session import active_profile, current_role
        allowed = PAGES_BY_PROFILE.get(active_profile(current_role()), ())
        clamped = clamp_page(page, allowed)
        if clamped != page:   # as in request_navigation: the target's section / context do not travel
            page, pending = clamped, {**pending, "section": "", "context": {}}
    if page:
        st.session_state["_ow_page"] = page
        # rec14: the grouped-nav radios re-derive their selection from _ow_page, so
        # clear their per-group widget keys (this runs before the sidebar renders)
        # or a stale group selection would override the forced navigation.
        for _k in [k for k in st.session_state if str(k).startswith("_ow_nav_")]:
            st.session_state.pop(_k, None)
    section = str(pending.get("section") or "")
    section_key = PAGE_SECTION_KEYS.get(page)
    if section and section_key:
        st.session_state[section_key] = section
    apply_filters(**pending.get("filters", {}))
    # Page-local drill identity is deliberately separate from global filters.
    # A selected action/query/entity should survive the navigation rerun without
    # silently reshaping every metric on the destination page.
    st.session_state["_ow_nav_context"] = dict(pending.get("context") or {})
    # C9: persist the jump's origin for the destination's one-hop return; a jump
    # without one (same-page hop, or the return itself) clears any stale origin.
    if pending.get("origin"):
        st.session_state["_ow_nav_origin"] = dict(pending["origin"])
    else:
        st.session_state.pop("_ow_nav_origin", None)


def nav_return_target(current_page: str) -> dict | None:
    """C9: the origin to offer a one-hop return to, or None.

    Valid only while the viewer is still ON the jump's destination — wandering
    off via the sidebar drops the origin, so a stale "Back to Alerts" can never
    linger on unrelated pages."""
    origin = st.session_state.get("_ow_nav_origin")
    if not isinstance(origin, dict) or not str(origin.get("page") or ""):
        return None
    if str(origin.get("dest") or "") != str(current_page or ""):
        st.session_state.pop("_ow_nav_origin", None)
        return None
    return dict(origin)


def pop_nav_origin() -> None:
    """C9: consume the origin (the return button fires exactly once)."""
    st.session_state.pop("_ow_nav_origin", None)


def navigation_context() -> dict:
    """Return page-local identity carried by the most recent cross-page jump.

    Read-only: a destination that spends one key (an alert drawer's event_id, a filter
    note, a deep-link fingerprint) strips just that key itself, so the other drill
    identities alongside it survive."""
    return dict(st.session_state.get("_ow_nav_context") or {})


def requested_page(valid_pages: tuple[str, ...]) -> str | None:
    """Page requested via ?page= deep link, when the runtime supports it.

    v4.597 (Option C): a slug that matches no page is tried as a RETIRED deep link
    (``?page=decision-studio[&section=<old slug>]``, navigate.legacy_deep_link). On a hit the
    target section is seeded into the target page's section key (pre-widget) and the new page is
    returned. It fires once: the sidebar's remember_page rewrites ?page= in the same run."""
    try:
        value = st.query_params.get(_PAGE_PARAM)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            for page in valid_pages:
                if page.lower().replace(" ", "-") == str(value).lower():
                    return page
            from app.logic.navigate import PAGE_SECTION_KEYS, legacy_deep_link
            sec = st.query_params.get("section")
            if isinstance(sec, list):
                sec = sec[0] if sec else None
            hit = legacy_deep_link(str(value), str(sec or ""), valid_pages)
            if hit is not None:
                page, section = hit
                section_key = PAGE_SECTION_KEYS.get(page)
                if section and section_key:
                    st.session_state[section_key] = section
                return page
    except Exception:
        pass  # SiS runtimes without query-param support
    return None


def viewer_pages() -> tuple[str, ...]:
    """The pages this viewer's profile offers — () when the profile cannot be resolved."""
    try:
        from app.config import PAGES_BY_PROFILE
        from app.core.session import active_profile, current_role
        return tuple(PAGES_BY_PROFILE.get(active_profile(current_role()), ()))
    except Exception:  # a cross-link gate is chrome, never break the page
        return ()


def can_open(page: str) -> bool:
    """Does this viewer's profile offer ``page``? Gate every cross-page affordance on it (AGENTS.md:
    a click toward an off-profile page would silently clamp to Overview). Fail-OPEN on an unreadable
    profile, like the since-last-visit opener — the navigation clamp still holds either way."""
    pages = viewer_pages()
    return not pages or page in pages


def remember_page(page: str) -> None:
    try:
        value = str(page).lower().replace(" ", "-")
        if st.query_params.get(_PAGE_PARAM) != value:  # r21 #19: no no-op writes
            st.query_params[_PAGE_PARAM] = value
    except Exception:
        pass
