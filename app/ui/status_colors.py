"""Semantic status colors for tables and chips (pure module, tested).

One palette for the whole app: red = act now, amber = watch, green = healthy,
sky = informational, slate = neutral. Backgrounds are tints with dark text so
contrast stays readable on the dark theme.
"""

from __future__ import annotations

# value (upper) -> (background, text)
_BAD = ("#7f1d1d", "#fecaca")     # deep red bg, light red text
_WARN = ("#78350f", "#fde68a")    # amber
_OK = ("#14532d", "#bbf7d0")      # green
_INFO = ("#0c4a6e", "#bae6fd")    # sky
_MUTED = ("#1e293b", "#94a3b8")   # slate

# The pairs above are dark-theme tuned (deep bg, light text). Light theme
# gets pastel backgrounds with dark text; detection falls back to the dark
# pairs so a failed lookup never changes today's look.
_LIGHT_EQUIV = {
    ("#7f1d1d", "#fecaca"): ("#fee2e2", "#991b1b"),
    ("#78350f", "#fde68a"): ("#fef3c7", "#92400e"),
    ("#14532d", "#bbf7d0"): ("#dcfce7", "#166534"),
    ("#0c4a6e", "#bae6fd"): ("#e0f2fe", "#075985"),
    ("#1e293b", "#94a3b8"): ("#f1f5f9", "#475569"),
}


def _theme_is_light() -> bool:
    try:
        import streamlit as _st

        ctx_theme = getattr(getattr(_st, "context", None), "theme", None)
        if ctx_theme is not None and getattr(ctx_theme, "type", None):
            return str(ctx_theme.type).lower() == "light"
        return str(_st.get_option("theme.base") or "").lower() == "light"
    except Exception:  # noqa: BLE001 - theming is cosmetic; default to dark pairs
        return False

STATUS_COLOR_MAP = {
    # severities
    "CRITICAL": _BAD, "HIGH": _BAD, "MEDIUM": _WARN, "LOW": _MUTED, "INFO": _MUTED,
    # lifecycle states
    "OPEN": _WARN, "ACK": _INFO, "IN_PROGRESS": _INFO, "RESOLVED": _OK,
    "DONE": _OK, "DROPPED": _MUTED,
    # ledger states
    "ESTIMATED": _WARN, "VERIFIED": _OK, "REJECTED": _MUTED,
    # execution / task states
    "FAIL": _BAD, "FAILED": _BAD, "SUCCESS": _OK, "SUCCEEDED": _OK, "RUNNING": _INFO,
    "CANCELLED": _MUTED, "SKIPPED": _MUTED,
    # graph roles
    "ROOT CAUSE": _BAD, "CASCADE": _WARN,
    # booleans (rendered by pandas as True/False strings)
    "TRUE": _WARN, "FALSE": _MUTED,
    # triage kinds
    "ALERT": _BAD, "TASK FAILURE": _WARN, "SPEND ANOMALY": _INFO,
    # credential expiry
    "EXPIRED": _BAD, "EXPIRING": _WARN,
    # client driver versions (Security -> Clients)
    "BEHIND": _WARN, "CURRENT": _OK,
    "ELEVATED": _BAD, "WATCH": _WARN, "NORMAL": _OK, "STALE": _WARN, "ACTIVE": _OK,
}

# Columns that carry status semantics; True-is-good ones invert boolean colors.
STATUS_COLUMNS = (
    "SEVERITY", "STATUS", "STATE", "LAST_STATE", "EXECUTION_STATUS",
    "ROLE_IN_GRAPH", "KIND", "GOT_WORSE", "CANDIDATE", "FLAGGED",
    "STALE", "IS_ANOMALY", "SLA_MET", "ENABLED", "VERDICT",
)
_TRUE_IS_GOOD = {"SLA_MET", "ENABLED"}
_VERDICTS = {
    "BETTER": _OK, "WORSE": _BAD, "FLAT": _MUTED, "N/A": _MUTED,
    # change-impact registry (V010)
    "REGRESSED": _BAD, "IMPROVED": _OK, "NEUTRAL": _MUTED, "PENDING": _INFO,
    "NO_BASELINE": _MUTED, "INSUFFICIENT_AFTER": _MUTED,
}


def status_css(column: str, value: object) -> str:
    """Return a CSS string for a cell, or '' for no styling."""
    text = str(value if value is not None else "").strip().upper()
    if not text:
        return ""
    column = str(column).upper()
    if column == "VERDICT":
        pair = _VERDICTS.get(text)
    elif column in _TRUE_IS_GOOD and text in ("TRUE", "FALSE"):
        pair = _OK if text == "TRUE" else _BAD
    else:
        pair = STATUS_COLOR_MAP.get(text)
    if not pair:
        return ""
    if _theme_is_light():
        pair = _LIGHT_EQUIV.get(pair, pair)
    bg, fg = pair
    return f"background-color: {bg}; color: {fg}; font-weight: 600;"


def status_columns_in(columns: list[str] | tuple[str, ...]) -> list[str]:
    upper = {str(c).upper(): c for c in columns}
    return [upper[c] for c in STATUS_COLUMNS if c in upper]
