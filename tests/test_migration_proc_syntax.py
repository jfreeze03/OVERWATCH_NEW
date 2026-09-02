"""Structural syntax coverage for stored-procedure ($$) bodies.

The parse gate (test_migrations_parse.py) SKIPS $$ / EXECUTE IMMEDIATE blocks because
sqlglot cannot read Snowflake Scripting, and the only end-to-end proc gate
(snowflake-smoke) is opt-in + continue-on-error — so 118 of 122 migrations carry their
most-patched, highest-defect SQL (SP_ALERT_SCAN, SP_LEDGER_AUTOBOOK, SP_LOAD_*) entirely
UNCHECKED, and a syntax error inside a body ships to Snowsight and is caught by hand.

These robust structural checks catch the two classes that have actually recurred: a
truncated / malformed $$ delimiter (a body that ships unparseable) and unbalanced
parentheses in a body. They are NOT a full parser — a keyword typo still needs the
(owner-gated) blocking clone-smoke — but they convert ZERO coverage to catching the most
common structural failure. Verified zero-false-positive across all 122 live migrations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MIGRATIONS = sorted(
    (Path(__file__).resolve().parents[1] / "snowflake" / "migrations").glob("V[0-9]*.sql")
)


def _strip_noise(sql: str) -> str:
    """Remove line/block comments AND single-quoted string literals in ONE pass, so a
    '--' inside a string is not mistaken for a comment (V081 builds dynamic SQL with
    embedded '--') and a quote inside a comment is not mistaken for a string. A doubled
    '' is a Snowflake escaped quote. What remains is code structure only."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif sql[i] == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


@pytest.mark.parametrize("path", _MIGRATIONS, ids=lambda p: p.name)
def test_dollar_quote_delimiters_are_balanced(path: Path) -> None:
    """Every $$ opens and closes — an odd count is a truncated/malformed scripting body
    (the class that ships unparseable to Snowsight). Counted after stripping comments and
    strings so a $$ inside a comment (V029) or a string does not fool it."""
    clean = _strip_noise(path.read_text(encoding="utf-8"))
    assert clean.count("$$") % 2 == 0, (
        f"{path.name}: unbalanced $$ delimiter — a truncated or malformed proc body")


@pytest.mark.parametrize("path", _MIGRATIONS, ids=lambda p: p.name)
def test_proc_body_parentheses_are_balanced(path: Path) -> None:
    """Parentheses balance inside each $$ scripting body (comments/strings stripped) — an
    unbalanced paren in a proc body is a syntax error the $$-skipping parse gate misses."""
    clean = _strip_noise(path.read_text(encoding="utf-8"))
    for k, body in enumerate(clean.split("$$")[1::2]):
        opens, closes = body.count("("), body.count(")")
        assert opens == closes, (
            f"{path.name}: proc body #{k} has unbalanced parentheses ({opens} '(' vs {closes} ')')")


def test_checks_have_teeth() -> None:
    """Prove the structural checks FAIL on a malformed body, not merely pass on clean
    files — a green suite must mean 'checked', not 'skipped'."""
    truncated = "CREATE PROCEDURE X() AS $$ BEGIN NULL; END;"        # opens $$, never closes
    assert _strip_noise(truncated).count("$$") % 2 == 1
    unbalanced = "$$ BEGIN INSERT INTO T SELECT f( ; END; $$"        # unclosed paren in body
    bad_body = _strip_noise(unbalanced).split("$$")[1]
    assert bad_body.count("(") != bad_body.count(")")
    # a '--' inside a string must NOT be treated as a comment (the V081 class)
    assert _strip_noise("SELECT 'a -- b', (1)").count("(") == 1
    # a well-formed body passes both checks
    ok = "$$ BEGIN INSERT INTO T SELECT f(x) FROM s; END; $$"
    ok_body = _strip_noise(ok).split("$$")[1]
    assert _strip_noise(ok).count("$$") % 2 == 0
    assert ok_body.count("(") == ok_body.count(")")


def test_scripting_bodies_are_actually_covered() -> None:
    """Guard the guard: confirm the checks see the $$ bodies they claim to, so a future
    refactor cannot silently turn the coverage above into a no-op (no silent caps)."""
    with_bodies = [p.name for p in _MIGRATIONS
                   if "$$" in _strip_noise(p.read_text(encoding="utf-8"))]
    assert len(with_bodies) >= 100, (
        f"expected 100+ scripting migrations with $$ bodies, saw {len(with_bodies)} — "
        "the structural proc-syntax coverage may have silently stopped extracting bodies")
