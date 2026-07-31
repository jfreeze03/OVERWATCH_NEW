"""Global filter state and query-param navigation."""

from __future__ import annotations

import streamlit as st

from app.companies import COMPANIES, DEFAULT_COMPANY, DEFAULT_ENVIRONMENT, ENVIRONMENTS
from app.config import DAY_WINDOW_OPTIONS, DEFAULT_DAY_WINDOW

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
    if st.session_state["flt_environment"] not in ENVIRONMENTS:
        st.session_state["flt_environment"] = DEFAULT_ENVIRONMENT
    if st.session_state["flt_days"] not in DAY_WINDOW_OPTIONS:
        st.session_state["flt_days"] = DEFAULT_DAY_WINDOW
    # A database selection from another company OR environment scope resets to All
    # (a PROD scope must not keep a lingering DEV database pin). Bug round 2 B7:
    # validate with the SAME live-inventory classification the picker uses, not the
    # static company/environment tuples — otherwise DBA_MAINT_DB and any new
    # ALFA_*/TRXS_* database (offered by the SHOW-DATABASES picker) is silently
    # un-picked on the next run, and applied saved views lose their DB scope.
    from app.companies import classify_databases  # local import: tiny, avoids cycles
    _db = str(st.session_state["flt_database"] or "").strip()
    if _db and _db.upper() not in classify_databases(
            (_db,), st.session_state["flt_company"], st.session_state["flt_environment"]):
        st.session_state["flt_database"] = ""


def filters() -> dict:
    init_filters()
    return {
        "company": str(st.session_state["flt_company"]),
        "environment": str(st.session_state["flt_environment"]),
        "days": int(st.session_state["flt_days"]),
        "warehouse_contains": str(st.session_state["flt_warehouse_contains"]),
        "user_contains": str(st.session_state["flt_user_contains"]),
        "database": str(st.session_state["flt_database"]),
        "schema_contains": str(st.session_state["flt_schema_contains"]),
    }


def apply_filters(**kwargs) -> None:
    """Set top-bar filters programmatically (deep links, saved views).

    Values are validated the same way the widgets validate them; days snaps
    to the nearest allowed window so select_slider never sees a bad value.
    """
    from app.config import DAY_WINDOW_OPTIONS

    mapping = {
        "company": "flt_company", "environment": "flt_environment", "days": "flt_days",
        "warehouse_contains": "flt_warehouse_contains", "user_contains": "flt_user_contains",
        "database": "flt_database", "schema_contains": "flt_schema_contains",
    }
    for name, value in kwargs.items():
        key = mapping.get(name)
        if key is None or value is None:
            continue
        if name == "days":
            options = list(DAY_WINDOW_OPTIONS)
            try:
                value = min(options, key=lambda o: abs(int(o) - int(value)))
            except (TypeError, ValueError):
                continue
        st.session_state[key] = value


def request_navigation(page: str, section: str = "", filters: dict | None = None) -> None:
    """Queue a cross-page jump; consumed at the top of the NEXT run, before
    any widget instantiates (Streamlit forbids touching a live widget's key)."""
    # Clamp off-profile targets HERE (B8 also clamps on consume), then NO-OP a jump
    # that resolves to the current page with no section change. A sticky st.dataframe
    # selection re-fires request_navigation every rerun; without this, an EXECUTIVE
    # clicking a Top-action row (whose Control Room jump clamps back to Overview) spun
    # forever — request_navigation -> rerun -> selection re-emits -> request_navigation.
    if page:
        from app.config import PAGES_BY_PROFILE, resolve_role_profile
        from app.core.session import current_role
        allowed = PAGES_BY_PROFILE.get(resolve_role_profile(current_role()), ())
        if allowed and page not in allowed:
            page = "Overview"  # offered by every profile
    if page == st.session_state.get("_ow_page") and not section and not filters:
        return
    st.session_state["_ow_nav_pending"] = {
        "page": page, "section": section, "filters": dict(filters or {}),
    }
    st.rerun()


def consume_pending_navigation() -> None:
    """Call first thing in main(): applies a queued jump pre-instantiation."""
    pending = st.session_state.pop("_ow_nav_pending", None)
    if not pending:
        return
    # B6: reset the jump box ONLY when we actually consumed a jump. The old
    # unconditional clear ran every rerun and erased the user's pick on the very
    # rerun that delivered it (before _global_jump could read _ow_jump and fire
    # request_navigation) — the whole Jump-to box was a silent no-op.
    st.session_state["_ow_jump"] = None
    from app.logic.navigate import PAGE_SECTION_KEYS

    page = str(pending.get("page") or "")
    # B8: never route a viewer to a page their profile does not offer — landing on
    # a dead page or forcing an off-profile selection. Clamp to the viewer's pages.
    if page:
        from app.config import PAGES_BY_PROFILE, resolve_role_profile
        from app.core.session import current_role
        allowed = PAGES_BY_PROFILE.get(resolve_role_profile(current_role()), ())
        if allowed and page not in allowed:
            page = "Overview"  # offered by every profile
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


def requested_page(valid_pages: tuple[str, ...]) -> str | None:
    """Page requested via ?page= deep link, when the runtime supports it."""
    try:
        value = st.query_params.get(_PAGE_PARAM)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            for page in valid_pages:
                if page.lower().replace(" ", "-") == str(value).lower():
                    return page
    except Exception:
        pass  # SiS runtimes without query-param support
    return None


def remember_page(page: str) -> None:
    try:
        value = str(page).lower().replace(" ", "-")
        if st.query_params.get(_PAGE_PARAM) != value:  # r21 #19: no no-op writes
            st.query_params[_PAGE_PARAM] = value
    except Exception:
        pass
