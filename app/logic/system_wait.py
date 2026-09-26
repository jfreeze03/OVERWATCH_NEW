"""SYSTEM$WAIT (sleep polling) signature + argument parser — ONE definition shared by the
cloud-services driver classifier (cs_driver), the per-query advisor (query_advisor) and the
billed-family SQL builder (mart_sql.cloud_svc_billed_families, via SLEEP_SQL_PATTERN), so the
SQL window totals and the Python classification can never disagree about what a sleep is.

A SYSTEM$WAIT statement compiles in well under 0.1 s and then holds the statement open for the
requested interval; its cloud-services credits accrue for the whole wait (owner DIAG 2026-09-26:
~0.55 CS credits per hour slept on this account). Compile-ranked views therefore never show it.

Pure: ``re`` only — no pandas, Streamlit or Snowflake.
"""

from __future__ import annotations

import math
import re

# The call itself, at a token boundary: SYSTEM$WAIT( or SYSTEM$WAIT (…). Not SYSTEM$WAIT_FOR_SERVICES
# (the next char is "_") nor a longer identifier ending in SYSTEM$WAIT (the boundary class).
_CALL_RE = re.compile(r"(?:^|[^A-Z0-9_$])SYSTEM\$WAIT\s*\(", re.IGNORECASE)
# The same signature for Snowflake REGEXP_INSTR over UPPER(text): POSIX ERE, no backslash and no
# quote (it is embedded in a SQL string literal). tests/test_system_wait.py locks the parity.
SLEEP_SQL_PATTERN = "(^|[^A-Z0-9_$])SYSTEM[$]WAIT[[:space:]]*[(]"
# The argument list of ONE call: a non-negative number (optionally quoted), then an optional quoted
# unit. A bind variable (? / :1), an expression or a truncated call does not match -> None.
_ARG_RE = re.compile(
    r"(?:^|[^A-Z0-9_$])SYSTEM\$WAIT\s*\(\s*'?\s*(\d+(?:\.\d*)?|\.\d+)\s*'?\s*"
    r"(?:,\s*'\s*([A-Za-z]+)\s*'\s*)?\)",
    re.IGNORECASE)
# Snowflake's SYSTEM$WAIT time units (singular after stripping a trailing S); default SECONDS.
_UNIT_SECONDS = {"DAY": 86400.0, "HOUR": 3600.0, "MINUTE": 60.0, "SECOND": 1.0,
                 "MILLISECOND": 1e-3, "MICROSECOND": 1e-6, "NANOSECOND": 1e-9}
# Defining a task or procedure whose body sleeps is not itself a sleep.
_DDL_PREFIXES = ("CREATE", "ALTER")
_DDL_TEXT_RE = re.compile(r"^\s*(?:CREATE|ALTER)\b", re.IGNORECASE)


def is_sleep_text(text: object) -> bool:
    """True when ``text`` contains a SYSTEM$WAIT call (SELECT or CALL form, any case/spacing)."""
    return isinstance(text, str) and _CALL_RE.search(text) is not None


def is_sleep_statement(text: object, query_type: object = "") -> bool:
    """A sleep statement: sleep text that is not DDL. QUERY_TYPE decides when present
    (CREATE_TASK / ALTER_TASK / CREATE_PROCEDURE …); with no type, the text's first keyword does."""
    if not is_sleep_text(text):
        return False
    qtype = str(query_type or "").strip().upper() if isinstance(query_type, str) else ""
    if qtype:
        return not qtype.startswith(_DDL_PREFIXES)
    return _DDL_TEXT_RE.match(str(text)) is None


def wait_seconds(text: object) -> float | None:
    """The requested wait, in seconds, of the ONE SYSTEM$WAIT call in ``text``.

    None when there is no call, more than one call (a loop body), a bind variable (? / :1), an
    unknown unit, or truncated text. The unit defaults to SECONDS; singular and plural spellings
    are both accepted, case-insensitively. A negative amount never matches (Snowflake rejects it)."""
    if not isinstance(text, str) or len(_CALL_RE.findall(text)) != 1:
        return None
    m = _ARG_RE.search(text)
    if m is None:
        return None
    try:
        amount = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "SECONDS").upper()
    if unit.endswith("S"):
        unit = unit[:-1]
    factor = _UNIT_SECONDS.get(unit)
    if factor is None or not math.isfinite(amount):
        return None
    return amount * factor
