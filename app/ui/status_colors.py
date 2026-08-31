"""Semantic status colors for tables and chips (pure module, tested).

One palette for the whole app: red = act now, amber = watch, green = healthy,
sky = informational, slate = neutral. Backgrounds are tints with dark text so
contrast stays readable on the dark theme.
"""

from __future__ import annotations

import re

from app.ui import palette

# value (upper) -> (background, text)
_BAD = ("#5f1b1b", "#fecaca")     # deep red bg, light red text
_HIGH = ("#613112", "#fed7aa")    # deep orange — HIGH, distinct from CRITICAL red (r4)
_WARN = ("#5f3b0b", "#fde68a")    # amber
_OK = ("#123e2c", "#bbf7d0")      # green
_INFO = ("#183a5a", "#bfdbfe")    # softened blue
_MUTED = ("#273244", palette.MUTED)   # slate (text hue = rec50 single source)

# The pairs above are dark-theme tuned (deep bg, light text). Light theme
# gets pastel backgrounds with dark text; detection falls back to the dark
# pairs so a failed lookup never changes today's look.
_LIGHT_EQUIV = {
    ("#5f1b1b", "#fecaca"): ("#fee2e2", "#991b1b"),
    ("#613112", "#fed7aa"): ("#ffedd5", "#9a3412"),   # HIGH orange (r4)
    ("#5f3b0b", "#fde68a"): ("#fef3c7", "#92400e"),
    ("#123e2c", "#bbf7d0"): ("#dcfce7", "#166534"),
    ("#183a5a", "#bfdbfe"): ("#dbeafe", "#1d4ed8"),
    ("#273244", "#94a3b8"): ("#f1f5f9", "#475569"),
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
    # stored-procedure regression advisor (F7) — REGRESSED/IMPROVED reused above
    "SLOWER": _WARN, "STABLE": _MUTED, "FASTER BUT FAILING": _BAD,
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


# A3: movement/delta columns — the primary scan target of a cost command center.
# A column whose sign carries meaning (DELTA_*, D_*, Δ ...). Deliberately narrow so a
# timestamp like CHANGE_SEEN_AT is never mistaken for a movement (it'd no-op anyway,
# but the wasted styler pass is avoided).
_DELTA_RE = re.compile(r"(?:^|_)delta(?:_|$)|Δ|^d_", re.IGNORECASE)


def is_delta_column(name: object) -> bool:
    return bool(_DELTA_RE.search(str(name or "")))


# Delta columns whose metric is GOOD when it rises — cache/hit rate, coverage,
# verified savings, throughput, realization, precision, uptime. For these an
# increase is green, inverting the default cost-tool polarity (cost/latency/
# failures up = red). Matched as a substring of the column name; deliberately
# narrow so an ambiguous name falls through to the safe bad-up default.
_GOOD_UP_DELTA_TOKENS = (
    "HIT", "CACHE", "COVERAGE", "VERIFIED", "THROUGHPUT", "REALIZATION",
    "REALIZED", "PRECISION", "RECALL", "UPTIME", "AVAILABILITY", "ACCURACY",
)


def delta_up_is_good(column: object) -> bool:
    """True when an INCREASE in this delta column is the healthy direction."""
    up = str(column or "").upper()
    return any(tok in up for tok in _GOOD_UP_DELTA_TOKENS)


def delta_css(value: object, column: object = "") -> str:
    """Sign color for a movement/delta cell. Default cost-tool convention: an
    INCREASE in cost/latency/failures is worse (red), a DECREASE is better (green).
    A good-up column (cache rate, coverage, verified savings, throughput ...) inverts
    that so a positive delta reads green, not red — generic sign coloring miscodes
    those. Text color only (no fill). A ±0 or non-numeric value gets no color."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v == 0:
        return ""
    is_good = (v > 0) == delta_up_is_good(column)
    if is_good:
        col = "#15803d" if _theme_is_light() else palette.OK     # healthy direction
    else:
        col = "#b91c1c" if _theme_is_light() else palette.BAD    # wrong direction
    return f"color: {col}; font-weight: 600;"
