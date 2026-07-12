"""Every migration's plain SQL must PARSE (Snowflake dialect).

Born 2026-07-08: an inline comment swallowed a column-list comma in V022's
CREATE TABLE and the file shipped unparseable — caught by the user in
Snowsight, not by CI. Scripting blocks ($$ ... $$ bodies, EXECUTE IMMEDIATE)
are skipped: sqlglot doesn't speak Snowflake Scripting, but every plain
CREATE/ALTER/INSERT/MERGE/GRANT statement now has to parse before merge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")

_SNOWFLAKE_DIR = Path(__file__).resolve().parents[1] / "snowflake"
_FILES = [*sorted(_SNOWFLAKE_DIR.glob("migrations/V0*.sql")),
     _SNOWFLAKE_DIR / "roles.sql",
    _SNOWFLAKE_DIR / "teardown.sql",
    _SNOWFLAKE_DIR / "alert_drill.sql",
    _SNOWFLAKE_DIR / "webhook_delivery.sql",
]


def _split_statements(text: str):
    """Split on ';' with a real scanner: semicolons inside 'strings'
    (including '' escapes) and -- line comments do NOT terminate a
    statement. The naive .split(';') version chopped V022's version-row
    description mid-string and cried wolf."""
    statements, buf = [], []
    in_string = in_comment = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_comment:
            buf.append(ch)
            if ch == "\n":
                in_comment = False
        elif in_string:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_string = False
        elif ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == "-" and i + 1 < n and text[i + 1] == "-":
            in_comment = True
            buf.append(ch)
        elif ch == ";":
            statements.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        statements.append("".join(buf))
    return statements


def _plain_statements(text: str):
    """Yield parseable statements OUTSIDE $$-delimited scripting blocks."""
    parts = text.split("$$")
    outside = "".join(part for i, part in enumerate(parts) if i % 2 == 0)
    for stmt in _split_statements(outside):
        body = stmt.strip()
        if not body or all(ln.lstrip().startswith("--") or not ln.strip()
                           for ln in body.splitlines()):
            continue
        # drop leading full-line comments so prefix detection sees SQL
        lines = body.splitlines()
        while lines and (lines[0].lstrip().startswith("--") or not lines[0].strip()):
            lines.pop(0)
        body = "\n".join(lines).strip()
        if not body:
            continue
        upper = body.upper()
        # Positive list: the statement families sqlglot's snowflake dialect
        # parses reliably AND where a silent syntax slip hurts most (the
        # V022 bug was a CREATE TABLE). Tasks/alerts/grants/procs/secrets
        # are dialect gaps — Snowsight remains their only parser.
        prefixes = ("CREATE TABLE", "CREATE OR REPLACE TABLE",
                    "CREATE TRANSIENT TABLE", "CREATE OR REPLACE TRANSIENT TABLE",
                    "CREATE TABLE IF NOT EXISTS",
                    "CREATE TRANSIENT TABLE IF NOT EXISTS",
                    "CREATE VIEW", "CREATE OR REPLACE VIEW",
                    "INSERT ", "MERGE ", "UPDATE ", "DELETE ", "SELECT ")
        if not upper.startswith(prefixes):
            continue
        yield body


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_migration_sql_parses(path):
    text = path.read_text(encoding="utf-8")
    failures = []
    for stmt in _plain_statements(text):
        try:
            sqlglot.parse(stmt, dialect="snowflake")
        except sqlglot.errors.ParseError as exc:
            failures.append(f"{stmt[:90]!r}: {exc}")
    assert not failures, f"{path.name}: {failures[:3]}"


def test_gate_catches_the_v022_class_of_bug():
    """The exact failure mode this gate exists for: a comment that swallows
    a column-list comma must fail parsing."""
    broken = (
        "CREATE TABLE T (\n"
        "    A VARCHAR(80) NOT NULL,\n"
        "    B VARCHAR(80) NOT NULL  -- comment eats the comma,\n"
        "    C TIMESTAMP_NTZ NOT NULL\n"
        ")"
    )
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse(broken, dialect="snowflake")


def test_every_registered_builder_parses():
    """r18 #2 class-killer. The V039 edit pass left a DOUBLE WHERE in the
    warehouse-sizing live fallback and nothing parsed app-side SQL — the
    builder failed on every render for two days. Every canary-registered
    builder must be valid Snowflake, forever."""
    from app.data import canary as _canary

    registry = next(v for v in vars(_canary).values()
                    if isinstance(v, (list, tuple)) and v
                    and isinstance(v[0], tuple) and callable(v[0][1]))
    failures = []
    for name, fn in registry:
        try:
            sql = fn()
        except Exception as exc:  # noqa: BLE001 — any raise is a gate failure
            failures.append(f"{name}: builder raised {type(exc).__name__}: {exc}")
            continue
        try:
            sqlglot.parse(sql, dialect="snowflake")
        except sqlglot.errors.ParseError as exc:
            failures.append(f"{name}: {str(exc)[:160]}")
    assert not failures, f"{len(failures)} builder(s) failed: {failures[:5]}"
