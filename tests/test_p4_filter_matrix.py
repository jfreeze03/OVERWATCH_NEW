"""Phase 4 — the company filter matrix, derived from the code, not transcribed.

Every scoping lock in this suite so far names its builders by hand. That is a
drift hazard, and it has already bitten: test_injection_fuzz.TARGETS calls
itself "every filter-accepting builder" and is the file we would hand a pen
tester — it lists 28 entries against 105 builders that actually take a company.
A builder added tomorrow is not covered by it, and nothing fails to say so.

So this file enumerates the builders by INTROSPECTION. Add a company-taking
builder to app/data/*_sql.py and it is in the matrix on the next run, whether
or not anyone remembered to add it here. The invariants, per builder, per
company in COMPANIES:

  1. it produces SQL that parses as Snowflake
  2. the filter is live      — a named company changes the SQL vs 'ALL'
  3. the scopes are distinct — ALFA, Trexis, and UNKNOWN do not collapse
  4. hostile input stays inside a quoted literal (company and free-text filters)
  5. the day window is clamped — no unbounded ACCOUNT_USAGE scan

UNKNOWN is in COMPANIES and is a real selectable scope (V044: a warehouse or
user with no company evidence classifies UNKNOWN and surfaces on Chargeback).
Invariant 2 is what stops it, or any other scope, from quietly failing OPEN and
showing every company's data to someone who asked for one.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable

import pytest

from app.companies import COMPANIES

sqlglot = pytest.importorskip("sqlglot")

_SQL_MODULES = (
    "change_impact_sql", "chargeback_sql", "cortex_sql", "cost_sql", "etl_sql",
    "graph_sql", "insights_sql", "mart27_sql", "mart_sql", "ops_sql",
    "prefs_sql", "recheck_sql", "security_sql",
)

# Valid values for every REQUIRED non-company argument. A builder that grows a
# new required argument raises KeyError here — deliberately. The matrix should
# fail loudly and be taught the new argument, never silently skip the builder.
_REQUIRED_ARGS: dict[str, object] = {
    "days": 7,
    "day": "2026-06-14",
    "dimension": "USER",
    "a_start": "2026-06-01", "a_end": "2026-06-07",
    "b_start": "2026-06-08", "b_end": "2026-06-14",
    "release_date": "2026-06-14",
    "window_days": 3,
    "month": "2026-06",
    "proc_name": "MY_PROC",
}

# Free-text filter arguments — the other injection surface besides company.
_TEXT_FILTERS = ("database", "schema_contains", "warehouse_contains",
                 "user_contains", "proc_name")

_PAYLOADS = (
    "ZZINJZZ'",
    "ZZINJZZ''--",
    "ZZINJZZ' OR '1'='1",
    "ZZINJZZ%;DROP TABLE X",
    "ZZINJZZ_~pattern",
    'ZZINJZZ"double',
    "ZZINJZZ\\backslash",
    "ZZINJZZ; SELECT 1 --",
    "ZZINJZZ\nnewline'",
)

_NAMED = tuple(c for c in COMPANIES if c != "ALL")


def _strip_literals_and_comments(sql: str) -> str:
    """Remove quoted literals AND SQL comments, in one left-to-right pass.

    A regex that only strips ``'...'`` is not good enough, and the difference is
    not academic. insights_sql.procedure_costs_usd carries the comment

        -- (children carry the CALL's id as ROOT_QUERY_ID)

    whose apostrophe a literal-only regex reads as an opening quote; it then eats
    forward to the next quote and leaves a bare ``'`` in the residue, reporting an
    injection in SQL that is perfectly clean.

    Stripping comments first is equally wrong: the payload ``ZZINJZZ''--`` puts a
    ``--`` INSIDE a literal, and a comment-first pass would swallow the rest of
    the statement and hide a real escape. Whichever construct opens first wins,
    so both are consumed in a single scan.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":                                   # string literal
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # '' escape, still inside
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")                              # collapse to whitespace
        elif ch == "-" and sql[i:i + 2] == "--":         # line comment
            while i < n and sql[i] != "\n":
                i += 1
        elif ch == "/" and sql[i:i + 2] == "/*":         # block comment
            i += 2
            while i < n and sql[i:i + 2] != "*/":
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _company_builders() -> list[tuple[str, Callable[..., str]]]:
    found = []
    for mod_name in _SQL_MODULES:
        mod = importlib.import_module(f"app.data.{mod_name}")
        for name, fn in vars(mod).items():
            if name.startswith("_") or not inspect.isfunction(fn):
                continue
            if fn.__module__ != mod.__name__:      # re-exported import, not ours
                continue
            if "company" in inspect.signature(fn).parameters:
                found.append((f"{mod_name}.{name}", fn))
    return sorted(found)


BUILDERS = _company_builders()
_IDS = [name for name, _ in BUILDERS]


def _build(fn: Callable[..., str], company: str, **overrides: object) -> str:
    """Call a builder with a real company and valid values for everything else."""
    kwargs: dict[str, object] = {"company": company}
    for pname, param in inspect.signature(fn).parameters.items():
        if pname == "company":
            continue
        if pname in overrides:
            kwargs[pname] = overrides[pname]
        elif param.default is inspect.Parameter.empty:
            if pname not in _REQUIRED_ARGS:
                raise KeyError(
                    f"{fn.__module__}.{fn.__name__} needs a new required arg "
                    f"{pname!r}; add it to _REQUIRED_ARGS so the matrix covers it")
            kwargs[pname] = _REQUIRED_ARGS[pname]
    return fn(**kwargs)


def _escapes_a_literal(sql: str) -> bool:
    """True if any hostile residue survives OUTSIDE a quoted string literal.

    Strip every literal and comment; whatever is left is the statement's own
    structure. A surviving marker, or a stray quote, means the payload broke out.
    """
    residue = _strip_literals_and_comments(sql)
    return "ZZINJZZ" in residue or "'" in residue


# ---------------------------------------------------------------------------
# 0. The registry itself
# ---------------------------------------------------------------------------

def test_the_matrix_actually_found_the_builders():
    # Guards the introspection: if a refactor moves the builders, this fails
    # loudly rather than passing an empty matrix and proving nothing.
    assert len(BUILDERS) >= 100, f"only found {len(BUILDERS)} company builders"


def test_unknown_is_a_real_scope_not_a_sentinel():
    # Invariant 2 is only meaningful because UNKNOWN is selectable (V044).
    assert "UNKNOWN" in COMPANIES and "ALL" in COMPANIES
    assert _NAMED == ("ALFA", "Trexis", "UNKNOWN")


def test_the_escape_detector_is_not_vacuous():
    """Positive control. A detector that has never caught an escape proves
    nothing about the 105 builders it just waved through."""
    # Unquoted interpolation — the marker lands in the statement's structure.
    assert _escapes_a_literal("SELECT * FROM T WHERE C = ZZINJZZ' OR 1=1")
    # Correctly escaped: the same marker, doubled-quoted, stays inside its literal.
    assert not _escapes_a_literal("SELECT * FROM T WHERE C = 'ZZINJZZ'' OR 1=1'")
    # An apostrophe inside a comment is prose, not a literal. A literal-only
    # regex reads it as an open quote and cries injection on clean SQL — which
    # is exactly what insights_sql.procedure_costs_usd does.
    assert not _escapes_a_literal("SELECT 1 -- children carry the CALL's id\nFROM T")
    # ...and a `--` inside a literal is data, not a comment. Consuming it as a
    # comment would blind the scan to the rest of the statement — and hide the
    # marker sitting in plain structure right after it.
    assert _escapes_a_literal("SELECT * FROM T WHERE C = 'a--b' OR ZZINJZZ")


# ---------------------------------------------------------------------------
# 1-3. Per builder: parses, filter is live, scopes stay distinct
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "fn"), BUILDERS, ids=_IDS)
def test_every_company_produces_parseable_snowflake_sql(name, fn):
    for company in COMPANIES:
        sql = _build(fn, company)
        assert sql.strip(), f"{name} returned empty SQL for {company}"
        sqlglot.parse(sql, dialect="snowflake")


@pytest.mark.parametrize(("name", "fn"), BUILDERS, ids=_IDS)
def test_the_company_filter_is_live(name, fn):
    """A named company must change the SQL. Identical to 'ALL' means the scope
    is inert and the page shows every company's data to someone who picked one."""
    unscoped = _build(fn, "ALL")
    for company in _NAMED:
        assert _build(fn, company) != unscoped, (
            f"{name}: company={company!r} produces the same SQL as 'ALL' — "
            "the filter does nothing and the scope fails OPEN")


@pytest.mark.parametrize(("name", "fn"), BUILDERS, ids=_IDS)
def test_named_scopes_do_not_collapse_into_each_other(name, fn):
    rendered = {c: _build(fn, c) for c in _NAMED}
    for a, b in ((x, y) for x in _NAMED for y in _NAMED if x < y):
        assert rendered[a] != rendered[b], (
            f"{name}: {a} and {b} render identically — one company is seeing "
            "the other's rows")


# ---------------------------------------------------------------------------
# 4. Injection — across EVERY company builder, not a hand-picked 28
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", _PAYLOADS)
def test_hostile_company_never_escapes_a_literal(payload):
    for name, fn in BUILDERS:
        try:
            sql = _build(fn, payload)
        except ValueError:
            continue                     # a builder that refuses input passes
        assert not _escapes_a_literal(sql), f"{name}: company payload escaped"


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_hostile_text_filters_never_escape_a_literal(payload):
    for name, fn in BUILDERS:
        params = inspect.signature(fn).parameters
        for filt in (f for f in _TEXT_FILTERS if f in params):
            try:
                sql = _build(fn, "ALFA", **{filt: payload})
            except ValueError:
                continue
            assert not _escapes_a_literal(sql), f"{name}: {filt} payload escaped"


# ---------------------------------------------------------------------------
# 5. Windows stay bounded
# ---------------------------------------------------------------------------

def test_every_day_window_is_clamped():
    """An unclamped `days` is an unbounded ACCOUNT_USAGE scan — the perf budgets
    exist precisely because those are expensive. The raw value must never survive."""
    checked = 0
    for name, fn in BUILDERS:
        if "days" not in inspect.signature(fn).parameters:
            continue
        sql = _build(fn, "ALFA", days=999999)
        assert "999999" not in sql, f"{name}: days=999999 reached the SQL unclamped"
        checked += 1
    assert checked >= 70, f"only {checked} day-window builders checked"
