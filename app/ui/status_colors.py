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
}

# Columns that carry status semantics; True-is-good ones invert boolean colors.
STATUS_COLUMNS = (
    "SEVERITY", "STATUS", "STATE", "LAST_STATE", "EXECUTION_STATUS",
    "ROLE_IN_GRAPH", "KIND", "GOT_WORSE", "CANDIDATE", "FLAGGED",
    "STALE", "IS_ANOMALY", "SLA_MET", "ENABLED", "VERDICT",
)
_TRUE_IS_GOOD = {"SLA_MET", "ENABLED"}
_VERDICTS = {"BETTER": _OK, "WORSE": _BAD, "FLAT": _MUTED, "N/A": _MUTED}


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
    bg, fg = pair
    return f"background-color: {bg}; color: {fg}; font-weight: 600;"


def status_columns_in(columns: list[str] | tuple[str, ...]) -> list[str]:
    upper = {str(c).upper(): c for c in columns}
    return [upper[c] for c in STATUS_COLUMNS if c in upper]
