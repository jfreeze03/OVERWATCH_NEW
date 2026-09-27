"""SYSTEM$WAIT (sleep polling) signature + argument parser — ONE definition shared by the
cloud-services driver classifier (cs_driver), the per-query advisor (query_advisor) and the
billed-family SQL builder (mart_sql.cloud_svc_billed_families, via SLEEP_SQL_PATTERN), so the
SQL window totals and the Python classification can never disagree about what a sleep is.

A SYSTEM$WAIT statement compiles in well under 0.1 s and then holds the statement open for the
requested interval; its cloud-services credits accrue for the whole wait (owner DIAG 2026-09-26:
~0.55 CS credits per hour slept on this account). Compile-ranked views therefore never show it.

Two predicates, deliberately different:

* ``is_sleep_statement`` — COUNTED as a sleep (SQL SLEEP_FLAG, hours slept, the sleep totals). The
  statement itself must BE the sleep: ``SELECT SYSTEM$WAIT(`` or ``CALL SYSTEM$WAIT(`` (after optional
  leading whitespace and comments), and not DDL or a multi-statement parent. A statement that only
  mentions the call (a string literal, a comment, an ILIKE '%system$wait(%' search) is not a sleep,
  and neither is a scripting block that wraps the call in a loop: Snowflake records each wrapped call
  as its own child statement, which is counted, so every sleep is counted once, on the child.
* ``polls_with_system_wait`` — LABELLED sleep polling (driver class, owner, next step, advisor). Also
  true for the wrapping block, so its owner gets the polling fix instead of "platform noise"; it never
  adds hours or credits to the sleep totals.

Pure: ``re`` only — no pandas, Streamlit or Snowflake.
"""

from __future__ import annotations

import math
import re

# The sleep statement shape for Snowflake REGEXP_INSTR over UPPER(text): POSIX ERE, anchored at the
# start; leading whitespace, /* block */, -- and // line comments allowed; then SELECT or CALL, then the
# call at a token boundary (SYSTEM$WAIT_FOR_SERVICES has "_" next, so it never matches). No backslash
# and no quote: it is embedded in a SQL string literal (the line comment ends at a literal newline).
SLEEP_SQL_PATTERN = (
    "^[[:space:]]*((/[*]([^*]|[*]+[^*/])*[*]+/|--[^\n]*\n|//[^\n]*\n)[[:space:]]*)*"
    "(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(]"
)
# QUERY_TYPEs never counted as a sleep, whatever their text: DDL that defines one, and the parent row
# of a multi-statement request (its child statements carry the sleep). The SQL SLEEP_FLAG mirrors these
# as NOT LIKE '<prefix>%' (tests lock the parity).
SLEEP_EXCLUDED_TYPE_PREFIXES = ("CREATE", "ALTER", "MULTI_STATEMENT")
# The SAME pattern for Python (POSIX [[:space:]] == \s here); tests lock the parity.
_PY_SHAPE = SLEEP_SQL_PATTERN.replace("[[:space:]]", r"\s")
_SHAPE_RE = re.compile(_PY_SHAPE, re.IGNORECASE)
# The shape plus its argument list: a non-negative number (optionally quoted), then an optional quoted
# unit. A bind variable (? / :1), an expression or a truncated call does not match -> None. Named groups:
# the shared shape carries its own (unnamed) groups.
_ARG_RE = re.compile(
    _PY_SHAPE + r"\s*'?\s*(?P<amount>\d+(?:\.\d*)?|\.\d+)\s*'?\s*(?:,\s*'\s*(?P<unit>[A-Za-z]+)\s*'\s*)?\)",
    re.IGNORECASE)
# The call anywhere, at a token boundary (for the one-call check and the wrapper label).
_CALL_RE = re.compile(r"(?:^|[^A-Z0-9_$])SYSTEM\$WAIT\s*\(", re.IGNORECASE)
# String literals (doubled '' and backslash escapes) and Snowflake comments (--, //, /* */), blanked
# before counting calls or looking for a call a block really executes.
_LITERAL_OR_COMMENT_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'?|--[^\n]*|//[^\n]*|/\*.*?(?:\*/|$)", re.DOTALL)
_DDL_PREFIXES = ("CREATE", "ALTER")
_DDL_TEXT_RE = re.compile(r"^\s*(?:CREATE|ALTER)\b", re.IGNORECASE)
# Snowflake's SYSTEM$WAIT time units (singular after stripping a trailing S); default SECONDS.
_UNIT_SECONDS = {"DAY": 86400.0, "HOUR": 3600.0, "MINUTE": 60.0, "SECOND": 1.0,
                 "MILLISECOND": 1e-3, "MICROSECOND": 1e-6, "NANOSECOND": 1e-9}


def _type_starts(query_type: object, prefixes: tuple[str, ...]) -> bool:
    qtype = query_type.strip().upper() if isinstance(query_type, str) else ""
    return qtype.startswith(prefixes)


def is_sleep_text(text: object) -> bool:
    """True when ``text`` IS a sleep statement: SELECT / CALL SYSTEM$WAIT(...) (any case/spacing,
    optional leading comments). A statement that only mentions the call is not."""
    return isinstance(text, str) and _SHAPE_RE.search(text) is not None


def is_sleep_statement(text: object, query_type: object = "") -> bool:
    """COUNTED as a sleep: the sleep shape, and a QUERY_TYPE (when known) that is not DDL or a
    multi-statement parent."""
    return is_sleep_text(text) and not _type_starts(query_type, SLEEP_EXCLUDED_TYPE_PREFIXES)


def polls_with_system_wait(text: object, query_type: object = "") -> bool:
    """LABELLED sleep polling: a sleep statement, or a non-DDL statement that executes a SYSTEM$WAIT
    call outside string literals and comments (a scripting block polling in a loop, a multi-statement
    parent). Never used for hours or totals — the wrapped calls are counted on their own child rows."""
    if is_sleep_statement(text, query_type):
        return True
    if not isinstance(text, str) or _type_starts(query_type, _DDL_PREFIXES) or _DDL_TEXT_RE.match(text):
        return False
    return _CALL_RE.search(_LITERAL_OR_COMMENT_RE.sub(" ", text)) is not None


def wait_seconds(text: object) -> float | None:
    """The requested wait, in seconds, of a sleep statement.

    None when ``text`` is not a sleep statement or holds more than one call, the argument is a bind
    variable (? / :1) or an expression, the unit is unknown, or the text is truncated. The unit defaults
    to SECONDS; singular and plural spellings are both accepted, case-insensitively. A negative amount
    never matches (Snowflake rejects it)."""
    # one call outside literals / comments (a comment that mentions the call is not a second call)
    if not isinstance(text, str) or len(_CALL_RE.findall(_LITERAL_OR_COMMENT_RE.sub(" ", text))) > 1:
        return None
    m = _ARG_RE.search(text)
    if m is None:
        return None
    amount = float(m.group("amount"))
    unit = (m.group("unit") or "SECONDS").upper()
    if unit.endswith("S"):
        unit = unit[:-1]
    factor = _UNIT_SECONDS.get(unit)
    if factor is None or not math.isfinite(amount):
        return None
    return amount * factor
