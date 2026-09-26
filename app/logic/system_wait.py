"""SYSTEM$WAIT (sleep polling) signature + argument parser — ONE definition shared by the
cloud-services driver classifier (cs_driver), the per-query advisor (query_advisor) and the
billed-family SQL builder (mart_sql.cloud_svc_billed_families, via SLEEP_SQL_PATTERN), so the
SQL window totals and the Python classification can never disagree about what a sleep is.

A SYSTEM$WAIT statement compiles in well under 0.1 s and then holds the statement open for the
requested interval; its cloud-services credits accrue for the whole wait (owner DIAG 2026-09-26:
~0.55 CS credits per hour slept on this account). Compile-ranked views therefore never show it.

A sleep is recognised by its STATEMENT SHAPE, not by the text merely containing the call: the
statement itself must be ``SELECT SYSTEM$WAIT(`` or ``CALL SYSTEM$WAIT(`` (after optional leading
whitespace and comments). So a query that only mentions the call — in a string literal, a comment,
an ILIKE '%system$wait(%' search — is not a sleep, and neither is task / procedure DDL or a
scripting block that wraps the call in a loop (Snowflake records each wrapped call as its own child
statement, which IS classified, so the sleep is counted once, on the child).

Pure: ``re`` only — no pandas, Streamlit or Snowflake.
"""

from __future__ import annotations

import math
import re

# The sleep statement shape for Snowflake REGEXP_INSTR over UPPER(text): POSIX ERE, anchored at the
# start; leading whitespace, /* block */ and -- line comments allowed; then SELECT or CALL, then the
# call at a token boundary (SYSTEM$WAIT_FOR_SERVICES has "_" next, so it never matches). No backslash
# and no quote: it is embedded in a SQL string literal (the line comment ends at a literal newline).
SLEEP_SQL_PATTERN = (
    "^[[:space:]]*((/[*]([^*]|[*]+[^*/])*[*]+/|--[^\n]*\n)[[:space:]]*)*"
    "(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(]"
)
# The SAME pattern for Python (POSIX [[:space:]] == \s here); tests lock the parity.
_PY_SHAPE = SLEEP_SQL_PATTERN.replace("[[:space:]]", r"\s")
_SHAPE_RE = re.compile(_PY_SHAPE, re.IGNORECASE)
# The shape plus its argument list: a non-negative number (optionally quoted), then an optional quoted
# unit. A bind variable (? / :1), an expression or a truncated call does not match -> None. Named groups:
# the shared shape carries its own (unnamed) groups.
_ARG_RE = re.compile(
    _PY_SHAPE + r"\s*'?\s*(?P<amount>\d+(?:\.\d*)?|\.\d+)\s*'?\s*(?:,\s*'\s*(?P<unit>[A-Za-z]+)\s*'\s*)?\)",
    re.IGNORECASE)
# Snowflake's SYSTEM$WAIT time units (singular after stripping a trailing S); default SECONDS.
_UNIT_SECONDS = {"DAY": 86400.0, "HOUR": 3600.0, "MINUTE": 60.0, "SECOND": 1.0,
                 "MILLISECOND": 1e-3, "MICROSECOND": 1e-6, "NANOSECOND": 1e-9}
# Defensive: a DDL QUERY_TYPE is never a sleep, whatever its text (the shape already excludes DDL text).
_DDL_PREFIXES = ("CREATE", "ALTER")


def is_sleep_text(text: object) -> bool:
    """True when ``text`` IS a sleep statement: SELECT / CALL SYSTEM$WAIT(...) (any case/spacing,
    optional leading comments). A statement that only mentions the call is not."""
    return isinstance(text, str) and _SHAPE_RE.search(text) is not None


def is_sleep_statement(text: object, query_type: object = "") -> bool:
    """A sleep statement whose QUERY_TYPE (when known) is not DDL."""
    if not is_sleep_text(text):
        return False
    qtype = query_type.strip().upper() if isinstance(query_type, str) else ""
    return not qtype.startswith(_DDL_PREFIXES) if qtype else True


def wait_seconds(text: object) -> float | None:
    """The requested wait, in seconds, of a sleep statement.

    None when ``text`` is not a sleep statement, the argument is a bind variable (? / :1) or an
    expression, the unit is unknown, or the text is truncated. The unit defaults to SECONDS; singular
    and plural spellings are both accepted, case-insensitively. A negative amount never matches
    (Snowflake rejects it)."""
    if not isinstance(text, str):
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
