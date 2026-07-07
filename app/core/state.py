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


def filters() -> dict:
    init_filters()
    return {
        "company": str(st.session_state["flt_company"]),
        "environment": str(st.session_state["flt_environment"]),
        "days": int(st.session_state["flt_days"]),
        "warehouse_contains": str(st.session_state["flt_warehouse_contains"]),
        "user_contains": str(st.session_state["flt_user_contains"]),
    }


def filters_signature() -> str:
    f = filters()
    return (
        f"co={f['company']}|env={f['environment']}|d={f['days']}"
        f"|wh={f['warehouse_contains']}|u={f['user_contains']}"
    )


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
        st.query_params[_PAGE_PARAM] = str(page).lower().replace(" ", "-")
    except Exception:
        pass
