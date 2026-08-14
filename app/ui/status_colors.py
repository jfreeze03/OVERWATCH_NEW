"""Semantic status colors for tables and chips (pure module, tested).

One palette for the whole app: red = act now, amber = watch, green = healthy,
sky = informational, slate = neutral. Backgrounds are tints with dark text so
contrast stays readable on the dark theme.

DARK PAIRS ONLY (v4.155, owner color review 2026-08-14): the app's chrome is
pinned dark by inject_theme() regardless of runtime, but ``st.context.theme``
reports the VIEWER'S browser/host preference — in SiS/Snowsight-light it says
"light" while the cards behind the table are still dark. The old detection
then swapped every status cell to pastel light-theme pairs on dark chrome
(observed live: washed-out pale-yellow/green cells). The browser theme is
irrelevant to a hardcoded-dark design system, so it is no longer consulted.
"""

from __future__ import annotations

import re

from app.ui import palette

# value (upper) -> (background, text)
_BAD = ("#7f1d1d", "#fecaca")     # deep red bg, light red text
_HIGH = ("#7c2d12", "#fed7aa")    # deep orange — HIGH, distinct from CRITICAL red (r4)
_WARN = ("#78350f", "#fde68a")    # amber
_OK = ("#14532d", "#bbf7d0")      # green
_INFO = ("#0c4a6e", "#bae6fd")    # sky
_MUTED = ("#1e293b", palette.MUTED)   # slate (text hue = rec50 single source)

STATUS_COLOR_MAP = {
    # severities
    "CRITICAL": _BAD, "HIGH": _HIGH, "MEDIUM": _WARN, "LOW": _MUTED, "INFO": _MUTED,
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
    # change-registration states (Security -> Changes) and generic verdicts
    "UNKNOWN": _MUTED,
    "REGISTERED": _OK, "UNREGISTERED": _WARN, "NOT_APPLICABLE": _MUTED,
    "PENDING": _INFO,
    "NEW": _WARN, "SPIKE": _BAD,
    "PASS": _OK, "INSUFFICIENT": _MUTED,
    "ELEVATED": _BAD, "WATCH": _WARN, "NORMAL": _OK, "STALE": _WARN, "ACTIVE": _OK,
}

# Columns that carry status semantics; True-is-good ones invert boolean colors.
STATUS_COLUMNS = (
    "SEVERITY", "STATUS", "STATE", "LAST_STATE", "EXECUTION_STATUS",
    "ROLE_IN_GRAPH", "KIND", "GOT_WORSE", "CANDIDATE", "FLAGGED",
    "STALE", "IS_ANOMALY", "SLA_MET", "ENABLED", "VERDICT",
    "CHANGE_REGISTRATION", "BEHAVIOR", "DECISION", "SLO_STATE",
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
    bg, fg = pair
    return f"background-color: {bg}; color: {fg}; font-weight: 600;"


def status_columns_in(columns: list[str] | tuple[str, ...]) -> list[str]:
    upper = {str(c).upper(): c for c in columns}
    return [upper[c] for c in STATUS_COLUMNS if c in upper]


# A3: movement/delta columns — the primary scan target of a cost command center.
# A column whose sign carries meaning (DELTA_*, D_*, Δ ...). Deliberately narrow so a
# timestamp like CHANGE_SEEN_AT is never mistaken for a movement (it'd no-op anyway,
# but the wasted styler pass is avoided).
_DELTA_RE = re.compile(r"(?:^|_)delta(?:_|$)|Δ|^d_", re.IGNORECASE)


def is_delta_column(name: object) -> bool:
    return bool(_DELTA_RE.search(str(name or "")))


def delta_css(value: object) -> str:
    """Sign color for a movement/delta cell — the cost-tool convention: an INCREASE
    in cost/latency/failures is worse (red), a DECREASE is better (green). Text color
    only (no fill) so a table of deltas stays scannable, not a wall of blocks. A ±0
    or non-numeric value gets no color."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v > 0:
        col = palette.BAD    # up = worse
    elif v < 0:
        col = palette.OK     # down = better
    else:
        return ""
    return f"color: {col}; font-weight: 600;"
