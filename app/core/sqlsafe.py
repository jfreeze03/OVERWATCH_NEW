"""The single source of SQL-safety primitives.

The old app maintained four divergent copies of these functions; every
generated-SQL call site in this app imports from here and nowhere else.
Pure module: no Streamlit.
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
_FILTER_RE = re.compile(r"^[A-Za-z0-9_%@.\- ]{0,128}$")
_CONTROL_RE = re.compile(
    r"(;|--|/\*|\*/|\bUNION\b|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b"
    r"|\bDROP\b|\bALTER\b|\bGRANT\b|\bREVOKE\b|\bCALL\b|\bCREATE\b|\bMERGE\b)",
    re.IGNORECASE,
)


def sql_literal(value: object, max_len: int = 8000) -> str:
    """Quote a value as a SQL string literal (NULL for None)."""
    if value is None:
        return "NULL"
    text = str(value).replace("\x00", "")[:max_len]
    return "'" + text.replace("'", "''") + "'"


def sql_number(value: object, default: float = 0.0) -> str:
    """Render a numeric literal; never interpolates raw text."""
    try:
        return repr(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return repr(float(default))


def safe_identifier(value: str, allow_qualified: bool = False) -> str:
    """Validate a Snowflake identifier before embedding in generated SQL."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Identifier cannot be blank")
    parts = raw.split(".") if allow_qualified else [raw]
    for part in parts:
        if not _IDENT_RE.match(part):
            raise ValueError(f"Unsafe Snowflake identifier: {raw!r}")
    return ".".join(parts)


def clean_filter_text(value: object, max_len: int = 128) -> str:
    """Sanitize free-text 'contains' filters from the UI.

    Returns '' (filter off) rather than raising, because a hostile value in a
    search box should degrade to no-filter, not to a page error.
    """
    text = str(value or "").strip()[:max_len]
    if not text:
        return ""
    text = re.sub(r"[^A-Za-z0-9_%@.\- ]", "", text)
    if not text or _CONTROL_RE.search(text) or not _FILTER_RE.match(text):
        return ""
    return text


def assert_no_control_tokens(clause: str) -> str:
    """Fail closed if a composed WHERE fragment smuggles SQL control tokens.

    String literals are masked first so legitimate quoted values never trip it.
    """
    masked = re.sub(r"'(?:''|[^'])*'", "''", str(clause or ""))
    if _CONTROL_RE.search(masked):
        raise ValueError("Unsafe SQL filter clause rejected")
    return str(clause or "")


def in_list(column: str, values: list[str] | tuple[str, ...]) -> str:
    """``UPPER(col) IN (...)`` for exact (non-wildcard) values; '' if empty."""
    items = [str(v).strip() for v in values if str(v or "").strip()]
    if not items:
        return ""
    column = safe_identifier(column, allow_qualified=True)
    literals = ", ".join(sql_literal(v.upper(), 300) for v in items)
    return f"UPPER({column}) IN ({literals})"


def not_in_list(column: str, values: list[str] | tuple[str, ...], allow_null: bool = True) -> str:
    """``UPPER(col) NOT IN (...)``, NULL-tolerant by default; '' if empty."""
    items = [str(v).strip() for v in values if str(v or "").strip()]
    if not items:
        return ""
    column = safe_identifier(column, allow_qualified=True)
    literals = ", ".join(sql_literal(v.upper(), 300) for v in items)
    predicate = f"UPPER({column}) NOT IN ({literals})"
    return f"({column} IS NULL OR {predicate})" if allow_null else predicate


def like_any(column: str, patterns: list[str] | tuple[str, ...]) -> str:
    """OR-joined ILIKE for wildcard patterns, exact IN for the rest; '' if empty."""
    items = [str(v).strip() for v in patterns if str(v or "").strip()]
    if not items:
        return ""
    column = safe_identifier(column, allow_qualified=True)
    exact = [v for v in items if "%" not in v]
    wild = [v for v in items if "%" in v]
    parts: list[str] = []
    if exact:
        literals = ", ".join(sql_literal(v.upper(), 300) for v in exact)
        parts.append(f"UPPER({column}) IN ({literals})")
    parts.extend(f"{column} ILIKE {sql_literal(v, 300)}" for v in wild)
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def contains_filter(column: str, raw_value: object) -> str:
    """Case-insensitive contains clause from sanitized UI text; '' when off.

    LIKE metacharacters in the user's text are escaped so 'WH_' matches the
    literal underscore rather than any character ('~' escape avoids
    backslash-in-string-literal semantics entirely).
    """
    text = clean_filter_text(raw_value)
    if not text:
        return ""
    column = safe_identifier(column, allow_qualified=True)
    escaped = text.replace("~", "~~").replace("%", "~%").replace("_", "~_")
    return f"{column} ILIKE {sql_literal('%' + escaped + '%', 300)} ESCAPE '~'"
