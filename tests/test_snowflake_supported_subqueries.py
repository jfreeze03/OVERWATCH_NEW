"""Snowflake-supported correlated subqueries (V173): every correlated subquery has a decorrelation key.

INCIDENT: prod APP_ERROR_LOG 2026-10-02 from 07:08 Central, every hourly run: AlertScan ``rule_block_failed`` "SQL
compilation error: Unsupported subquery type cannot be evaluated", CONTEXT "rule SEC_NEW_ADMIN_NETWORK - other rules
unaffected". V168 gave arm [18]'s dedupe guard one correlated NOT EXISTS whose every reference to the outer row sat
under an OR::

    WHERE NOT EXISTS (SELECT 1 FROM ALERT_EVENTS e
                      WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
                         OR (e.RULE_ID = b.RULE_ID AND ... AND (e.DEDUPE_KEY = ... OR (... = ...))))

Snowflake turns a correlated EXISTS / IN / scalar subquery into a (semi / anti) join, which needs an equality between
the subquery's own column and the outer row at the top of its WHERE. With none, the statement does not compile. The
in-memory sqlite harness runs a correlated OR row by row, so CI passed it. V173 rewrote the guard as three AND-ed
NOT EXISTS with plain-equality keys.

THE RULE, over the LATEST definer of every procedure, function, view and task the migrations create (the same set as
tests/test_sql_division_guards.py), every statement parsed by sqlglot (Snowflake dialect) and scoped:

  R1  a correlated expression subquery (under EXISTS / IN / ANY / ALL, or a scalar subquery inside an expression)
      has at least one DECORRELATION KEY: a top-level AND-ed conjunct of its WHERE that is ``inner = outer`` with
      one side a bare column of the subquery's own sources and the other an expression of outer columns only. No
      waiver: a subquery without one is rewritten (split the OR into AND-ed NOT EXISTS, or precompute the inner side
      in a CTE as V173 does).
  R2  every OTHER conjunct, select item or join condition that reads the outer row -- a correlation under OR, a
      non-equality (<>, <, >, LIKE, ...), an outer-only filter, an equality with an expression on both sides or with
      inner and outer columns on one side -- is a RESIDUAL. Snowflake applies a residual as a join filter beside the
      key, and each shape below is proven in production; a new one is listed in _PROVEN with its evidence (and the
      count it occurs) or rewritten.
  R3  every statement of a latest definer that contains SELECT parses; a waiver is a reasoned _UNPARSED entry.

A correlation is a QUALIFIED reference to an outer source (law 8 qualifies them all): without the tables' schemas the
parser cannot tell an unqualified column's scope, so it is read as the subquery's own.

Precedents the V173 rewrite leans on (all R1 keys): a correlated NOT EXISTS over a CTE (V105 SP_LOAD_SECURITY_FACTS,
``SELECT 1 FROM current_findings c WHERE c.SCANNER_ID = p.SCANNER_ID``); a bare inner column equal to an outer
EXPRESSION (V157 arm [10], ``h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')``; V150's anomaly scans,
``e.DEDUPE_KEY = 'COST_..._ANOMALY|' || l.SERIES || ...``). Not covered: SQL a body builds in string literals and
runs with EXECUTE IMMEDIATE (SP_SCAN_REF_GAPS's checks): it is data until run time.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from typing import NamedTuple

import pytest

from tests._source import ROOT
from tests.test_sql_division_guards import _close, latest_definers, mask

sqlglot = pytest.importorskip("sqlglot")
from sqlglot import exp  # noqa: E402
from sqlglot.optimizer.scope import traverse_scope  # noqa: E402

_MIG = ROOT / "snowflake" / "migrations"

# ---------------------------------------------------------------------------------------------------------------
# Statements of an object's CREATE text
# ---------------------------------------------------------------------------------------------------------------
_KEYWORD = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|SELECT|WITH)\b", re.I)


def _segments(m: str, a: int, b: int) -> list[tuple[int, int]]:
    """[a, b) split on ';' at parenthesis depth 0 (masked text: strings and comments carry no ';' or paren)."""
    out, start, depth = [], a, 0
    for i in range(a, b):
        depth += (m[i] == "(") - (m[i] == ")")
        if m[i] == ";" and depth == 0:
            out.append((start, i))
            start = i + 1
    out.append((start, b))
    return out


def statements(create: str) -> list[str]:
    """The SQL statements of a CREATE: a $$ body (procedure, function) split into its statements past the scripting
    keywords, a task's AS statement, or a view's whole CREATE. A statement inside parentheses (``x := (SELECT ...)``,
    ``IF (EXISTS (SELECT ...)) THEN``) is that parenthesised query; the scan goes on after it."""
    m = mask(create)
    if "$$" in m:
        a, b = m.index("$$") + 2, m.rindex("$$")
    elif re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:SECURE\s+)?VIEW\b", m, re.I):
        a, b = 0, len(m)
    else:
        hit = re.search(r"\bAS\b", m)
        assert hit, create[:120]
        a, b = hit.end(), len(m)
    out = []
    for s, e in _segments(m, a, b):
        pos = s
        while k := _KEYWORD.search(m, pos, e):
            opens: list[int] = []
            for i in range(s, k.start()):
                if m[i] == "(":
                    opens.append(i)
                elif m[i] == ")":
                    opens.pop()
            if not opens:
                out.append(create[k.start():e])
                break
            close = _close(m, opens[-1])
            out.append(create[k.start():close - 1])
            pos = close
    return out


_BIND = re.compile(r"(?<![:\w$]):([A-Za-z_]\w*)")
_INTO = re.compile(r"\bINTO\s+:[A-Za-z_]\w*(?:\s*,\s*:[A-Za-z_]\w*)*", re.I)


def _prep(sql: str) -> str:
    """Scripting to plain SQL for the parser: ``SELECT ... INTO :v`` loses its INTO, a ``:bind`` reads ``$bind``."""
    if re.match(r"\s*(SELECT|WITH)\b", sql, re.I):
        sql = _INTO.sub("", sql)
    return _BIND.sub(r"$\1", sql)


# ---------------------------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------------------------
class Subquery(NamedTuple):
    text: str                       # the subquery as sqlglot renders it (for messages only)
    keys: tuple[str, ...]           # R1 decorrelation keys
    residuals: tuple[str, ...]      # R2 signatures


_EXPR_PARENTS = (exp.Condition, exp.Alias, exp.Where, exp.Having, exp.Tuple)


def _is_expression_subquery(node: exp.Expression) -> bool:
    """Under EXISTS / IN / ANY / ALL, or a scalar subquery in an expression or a select list -- never a FROM / JOIN
    / MERGE source, a CTE, a UNION branch or a LATERAL table function (sqlglot also scopes those, and an UNPIVOT
    alias it does not model reads as 'external')."""
    p = node.parent
    if isinstance(p, exp.Exists | exp.In | exp.Any | exp.All):
        return True
    if not isinstance(p, exp.Subquery) or isinstance(p.parent, exp.From | exp.Join):
        return False
    return isinstance(p.parent, _EXPR_PARENTS) or (isinstance(p.parent, exp.Select) and p.arg_key == "expressions")


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def _name(c: exp.Column) -> str:
    return f"{c.text('table')}.{c.name}"


def _signature(kind: str, node: exp.Expression, outer: set[int]) -> str:
    cols = list(node.find_all(exp.Column))
    o = sorted({_name(c) for c in cols if id(c) in outer})
    i = sorted({_name(c) for c in cols if id(c) not in outer})
    return f"{kind}: outer {', '.join(o)} | inner {', '.join(i) or '-'}"


def _key(cj: exp.Expression, outer: set[int]) -> bool:
    """``inner_column = outer_only_expression`` (either side)."""
    if not isinstance(cj, exp.EQ):
        return False
    for inner, other in ((cj.left, cj.right), (cj.right, cj.left)):
        other_cols = list(other.find_all(exp.Column))
        if (isinstance(inner, exp.Column) and id(inner) not in outer and other_cols
                and all(id(c) in outer for c in other_cols)):
            return True
    return False


def classify(sql: str) -> list[Subquery]:
    """Every correlated expression subquery of one statement."""
    out = []
    for tree in sqlglot.parse(_prep(sql), read="snowflake"):
        if tree is None:
            continue
        for scope in traverse_scope(tree):
            node = scope.expression
            ext = [c for c in scope.external_columns if c.text("table")]
            if not ext or not _is_expression_subquery(node):
                continue
            outer = {id(c) for c in ext}
            keys: list[str] = []
            residuals: list[str] = []
            seen: set[int] = set()
            where = node.args.get("where") if isinstance(node, exp.Select) else None
            for cj in _conjuncts(where.this) if where is not None else []:
                refs = [c for c in cj.find_all(exp.Column) if id(c) in outer]
                if not refs:
                    continue
                seen |= {id(c) for c in refs}
                if _key(cj, outer):
                    keys.append(cj.sql())
                else:
                    kind = type(cj).__name__ if not isinstance(cj, exp.EQ) else "EQ (not inner column = outer)"
                    residuals.append(_signature(kind, cj, outer))
            rest = [c for c in ext if id(c) not in seen]
            if rest:
                residuals.append("outside WHERE: outer " + ", ".join(sorted({_name(c) for c in rest})))
            out.append(Subquery(node.sql()[:240], tuple(keys), tuple(residuals)))
    return out


@functools.cache
def _scan_object(create: str) -> tuple[tuple[Subquery, ...], tuple[str, ...]]:
    """(correlated subqueries, statements that failed to parse) of one CREATE text; cached by text."""
    subs: list[Subquery] = []
    unparsed: list[str] = []
    for stmt in statements(create):
        if not re.search(r"\bSELECT\b", mask(stmt), re.I):
            continue
        try:
            subs += classify(stmt)
        except sqlglot.errors.ParseError:
            unparsed.append(" ".join(stmt.split())[:120])
    return tuple(subs), tuple(unparsed)


# ---------------------------------------------------------------------------------------------------------------
# The reasoned allow-list: residual correlations Snowflake has run in production beside a decorrelation key.
# ---------------------------------------------------------------------------------------------------------------
class Proven(NamedTuple):
    count: int
    since: int          # the migration that first shipped this shape to production
    evidence: str


_SCAN, _DAILY = "SP_ALERT_SCAN()", "SP_ALERT_SCAN_DAILY()"
_PROVEN: dict[tuple[str, str], Proven] = {
    (_SCAN, "Or: outer b.EXP_TS | inner e.DETAIL, e.RESOLVED_AT"): Proven(
        1, 157, "arm [10] SEC_CRED_EXPIRY's dedupe (V157, live since 2026-09-26, every 4h): key e.DEDUPE_KEY = "
                "b.DEDUPE_KEY; the cycle-id OR (RESOLVED_AT IS NULL OR DETAIL LIKE 'Rotate before <expiry>%') rides "
                "as a residual. No rule_block_failed for it (V173 PREFLIGHT P173.1 shows any)."),
    (_SCAN, "Like: outer b.DEDUPE_KEY | inner -"): Proven(
        2, 157, "an outer-only filter beside two keys (h.RULE_ID = b.RULE_ID, h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, "
                "...)): arm [10]'s never-mint-EXPIRING-while-EXPIRED guard (V157) and arm [26] SEC_LOGIN_TAKEOVER's "
                "never-mint-WARN-after-CRIT guard (V162, the same shape, hourly since the wave-4 apply)"),
    (_SCAN, "NEQ: outer lo.DEDUPE_KEY | inner hi.DEDUPE_KEY"): Proven(
        1, 67, "the V067 #40 escalation supersede sweep (hourly since V067): key hi.RULE_ID = lo.RULE_ID"),
    (_SCAN, "Or: outer lo.DEDUPE_KEY, lo.RAISED_AT, lo.RULE_ID | inner hi.DEDUPE_KEY, hi.RAISED_AT"): Proven(
        1, 67, "the same sweep's band-swap OR (six REPLACE equalities since V067 / V096). V168 added a seventh "
               "disjunct (failures-only SEC_NEW_ADMIN_NETWORK, a LIKE and a 48h range on lo) inside the same "
               "residual, beside the same key; its failure would log supersede_sweep_failed (P173.1 shows any)"),
    (_SCAN, "EQ (not inner column = outer): outer s.DEDUPE_KEY | inner s2.DEDUPE_KEY"): Proven(
        1, 117, "the V117 snooze carry-forward sweep (hourly since V117): the date-stripped identity CASE = CASE "
                "rides beside the key s2.RULE_ID = s.RULE_ID"),
    (_SCAN, "NEQ: outer s.EVENT_ID | inner s2.EVENT_ID"): Proven(
        1, 117, "the V117 snooze carry-forward sweep: s2.EVENT_ID <> s.EVENT_ID beside the key s2.RULE_ID = s.RULE_ID"),
    (_SCAN, "GT: outer s.RAISED_AT | inner s2.RAISED_AT"): Proven(
        1, 117, "the V117 snooze carry-forward sweep: s2.RAISED_AT > s.RAISED_AT beside the key s2.RULE_ID = s.RULE_ID"),
    ("SP_NOTIFY_WEBHOOK()", "EQ (not inner column = outer): outer e.DEDUPE_KEY | inner s.DEDUPE_KEY"): Proven(
        1, 164, "the V164 escalation's snoozed-sibling probe (the V117 identity CASE = CASE) beside the key "
                "s.RULE_ID = e.RULE_ID; the escalation pass ran in production on 2026-10-01"),
    ("SP_NOTIFY_WEBHOOK()", "LT: outer e.RAISED_AT | inner s.RAISED_AT"): Proven(
        1, 164, "the V164 escalation's snoozed-sibling probe: s.RAISED_AT < e.RAISED_AT beside s.RULE_ID = e.RULE_ID"),
    ("SP_NOTIFY_WEBHOOK()", "GTE: outer e.RAISED_AT | inner s.RESOLVED_AT"): Proven(
        1, 164, "the V164 escalation's snoozed-sibling probe: s.RESOLVED_AT >= e.RAISED_AT beside s.RULE_ID = "
                "e.RULE_ID"),
    ("SP_INCIDENT_AUTODECLARE()", "EQ (not inner column = outer): outer c.FAMILY | inner a.DEDUPE_KEY, a.EVENT_ID"):
        Proven(1, 32, "the auto-declare family match SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = "
                      "c.FAMILY: hourly since V032 (the only correlation until V099 added the key i.COMPANY = "
                      "c.COMPANY), re-derived through V154 / V162"),
}
# A statement sqlglot cannot parse is a hole in the lint: list it here with why it holds no correlated subquery.
_UNPARSED: dict[tuple[str, str], str] = {}


def violations(objects: dict[str, str]) -> list[str]:
    """R1-R3 over {object key: CREATE text}, the allow-list applied (an entry for an object outside ``objects``
    is not checked)."""
    out: list[str] = []
    used: Counter[tuple[str, str]] = Counter()
    for label, create in sorted(objects.items()):
        subs, unparsed = _scan_object(create)
        out.extend(f"{label}: R3 does not parse: {stmt}" for stmt in unparsed if (label, stmt) not in _UNPARSED)
        for sub in subs:
            if not sub.keys:
                out.append(f"{label}: R1 no decorrelation key (inner column = outer expression at the top of the "
                           f"WHERE): {sub.text}")
                continue
            for sig in sub.residuals:
                if (label, sig) in _PROVEN:
                    used[(label, sig)] += 1
                else:
                    out.append(f"{label}: R2 residual correlation not proven in production: {sig} -- {sub.text}")
    for (label, sig), entry in _PROVEN.items():
        if label in objects and used[(label, sig)] != entry.count:
            out.append(f"{label}: proven residual '{sig}' occurs {used[(label, sig)]}x, expected {entry.count}x "
                       "(re-check the evidence, then update _PROVEN)")
    return out


def _objects() -> dict[str, str]:
    return {k: body for k, (_v, body) in latest_definers().items()}


# ---------------------------------------------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------------------------------------------
def test_every_correlated_subquery_has_a_decorrelation_key():
    found = violations(_objects())
    assert not found, (
        "A correlated subquery Snowflake may not decorrelate ('Unsupported subquery type cannot be evaluated' -- the "
        "2026-10-02 SEC_NEW_ADMIN_NETWORK outage; sqlite runs it fine). Give it a top-level 'inner column = outer "
        "expression' conjunct (split an OR into AND-ed NOT EXISTS, precompute an inner expression in a CTE), or, for "
        "a residual beside such a key, add a _PROVEN entry with production evidence:\n  " + "\n  ".join(found))


def test_the_lint_has_reach():
    """The scan sees the subqueries it must: 74 correlated expression subqueries in the latest definers at V173 (the
    UNPIVOT and LATERAL FLATTEN sources sqlglot also scopes are not among them), the V173 legs, the V105 CTE
    precedent and the V067 sweep inside an UPDATE."""
    objs = _objects()
    total = Counter()
    for label, create in objs.items():
        total[label] = len(_scan_object(create)[0])
    assert sum(total.values()) >= 70, sum(total.values())
    assert total[_SCAN] >= 20 and total[_DAILY] >= 15 and total["SP_LOAD_SECURITY_FACTS(FLOAT)"] >= 1
    scan = list(_scan_object(objs[_SCAN])[0])
    legs = [s for s in scan if "FROM recent AS r" in s.text]
    assert len(legs) == 2 and all(s.keys and not s.residuals for s in legs), legs
    assert sorted(len(s.keys) for s in legs) == [1, 2]
    sweep = [s for s in scan if "hi.RULE_ID = lo.RULE_ID" in s.text]
    assert len(sweep) == 1 and sweep[0].keys == ("hi.RULE_ID = lo.RULE_ID",)
    cte = [s for s in _scan_object(objs["SP_LOAD_SECURITY_FACTS(FLOAT)"])[0] if "current_findings" in s.text]
    assert cte and cte[0].keys == ("c.SCANNER_ID = p.SCANNER_ID",)
    assert latest_definers()[_SCAN][0] >= 173 and latest_definers()[_DAILY][0] >= 173


def test_every_proven_entry_carries_evidence():
    for (label, sig), entry in _PROVEN.items():
        assert label in _objects(), label
        assert entry.count >= 1 and entry.since < 173 and len(entry.evidence) > 60, (label, sig)
        assert re.fullmatch(r"(Or|NEQ|GT|GTE|LT|LTE|Like|ILike|EQ \(not inner column = outer\)): outer [^|]+ \| "
                            r"inner .+", sig), sig


# -- teeth: the incident ---------------------------------------------------------------------------------------
def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    return text[s:text.index("$$;", text.index("$$", s) + 2) + 3]


_V168 = (_MIG / "V168__alert_scan_hourly_keys_and_sweeps.sql").read_text(encoding="utf-8")


def test_the_lint_fails_on_v168_arm_18():
    """V168's SP_ALERT_SCAN (prod-live until V173 is applied): exactly arm [18]'s guard fails R1, and nothing else."""
    found = violations({_SCAN: _proc(_V168, "SP_ALERT_SCAN()")})
    assert len(found) == 1 and found[0].startswith(f"{_SCAN}: R1 no decorrelation key"), found
    assert "e.DEDUPE_KEY = b.DEDUPE_KEY OR (e.RULE_ID = b.RULE_ID" in found[0]
    # V173 is V168 with that guard replaced: the same allow-list counts hold for both bodies
    v173 = _objects()[_SCAN]
    assert not violations({_SCAN: v173})


def test_the_lint_fails_when_one_v173_leg_loses_its_key():
    v173 = _objects()[_SCAN]
    leg3 = ("            WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)\n"
            "              AND r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)\n")
    assert v173.count(leg3) == 1
    # the V168 leg-3 shape (an expression on both sides) is not a key: precompute it, as the recent CTE does
    both_sides = ("            WHERE LENGTH(r.DEDUPE_KEY) = LENGTH(b.DEDUPE_KEY)\n"
                  "              AND LEFT(r.DEDUPE_KEY, LENGTH(r.DEDUPE_KEY) - 10) = LEFT(b.DEDUPE_KEY, "
                  "LENGTH(b.DEDUPE_KEY) - 10)\n")
    found = violations({_SCAN: v173.replace(leg3, both_sides)})
    assert len(found) == 1 and "R1 no decorrelation key" in found[0], found
    # an OR'ed pair of keys is no key either
    ored = ("            WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)\n"
            "               OR r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)\n")
    found = violations({_SCAN: v173.replace(leg3, ored)})
    assert len(found) == 1 and "R1 no decorrelation key" in found[0], found


def test_the_allow_list_counts_are_exact():
    v173 = _objects()[_SCAN]
    sweep_neq = "                 AND hi.DEDUPE_KEY <> lo.DEDUPE_KEY\n"
    assert v173.count(sweep_neq) == 1
    found = violations({_SCAN: v173.replace(sweep_neq, "")})
    assert found == [f"{_SCAN}: proven residual 'NEQ: outer lo.DEDUPE_KEY | inner hi.DEDUPE_KEY' occurs 0x, "
                     "expected 1x (re-check the evidence, then update _PROVEN)"], found


@pytest.mark.parametrize(("sql", "want"), [
    # R1: the incident shape and its fixes
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = b.k OR (e.r = b.r AND e.t > 0))", "R1"),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = b.k) AND NOT EXISTS (SELECT 1 FROM e "
     "WHERE e.r = b.r AND e.t > 0)", ""),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE LEFT(e.k, 3) = LEFT(b.k, 3))", "R1"),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = LEFT(b.k, 3))", ""),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.ts > b.ts)", "R1"),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k || b.x = 'a')", "R1"),
    ("SELECT 1 FROM b WHERE b.k IN (SELECT e.k FROM e WHERE e.ts > b.ts)", "R1"),
    ("SELECT (SELECT MAX(e.v) FROM e WHERE e.ts < b.ts) FROM b", "R1"),
    ("SELECT (SELECT MAX(e.v) FROM e WHERE e.k = b.k) FROM b", ""),
    # R2: a residual beside a key needs proof
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = b.k AND e.ts > b.ts)", "R2"),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = b.k AND b.k LIKE '%x')", "R2"),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = b.k AND e.k = b.k || e.x)", "R2"),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e JOIN f ON f.k = b.k WHERE e.k = b.k)", "R2"),
    # an unqualified column reads as the subquery's own (law 8 qualifies every correlation)
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE e.k = b.k AND z = 1)", ""),
    ("SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM e WHERE k = b.k)", ""),
    # R3: a statement the parser cannot read is reported, never skipped
    ("SELECT 1 FROM b WHERE (((", "R3"),
    # uncorrelated subqueries and FROM sources are not checked
    ("SELECT 1 FROM b WHERE b.k NOT IN (SELECT k FROM e WHERE k IS NOT NULL)", ""),
    ("SELECT 1 FROM (SELECT * FROM e) x JOIN LATERAL FLATTEN(input => x.v) f", ""),
    ("WITH r AS (SELECT k FROM e) SELECT 1 FROM b WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.k = b.k)", ""),
])
def test_the_rule(sql, want):
    found = violations({"X": "CREATE OR REPLACE VIEW X AS " + sql})
    assert [f.split(": ", 1)[1][:2] for f in found] == ([want] if want else []), found


def test_statement_extraction_reads_scripting():
    body = """CREATE OR REPLACE PROCEDURE P() RETURNS VARCHAR LANGUAGE SQL AS
$$
DECLARE
    n NUMBER;
BEGIN
    n := (SELECT COUNT(*) FROM t WHERE ';' = 'x');
    IF (EXISTS (SELECT 1 FROM t)) THEN
        INSERT INTO u SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM v WHERE v.a = t.a);
    END IF;
    SELECT MAX(a) INTO :n FROM t;
    -- a comment; with SELECT in it
    RETURN 'done; SELECT';
END;
$$;"""
    got = [" ".join(s.split()) for s in statements(body)]
    assert got == ["SELECT COUNT(*) FROM t WHERE ';' = 'x'", "SELECT 1 FROM t",
                   "INSERT INTO u SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM v WHERE v.a = t.a)",
                   "SELECT MAX(a) INTO :n FROM t"], got
    subs, unparsed = _scan_object(body)
    assert not unparsed and [s.keys for s in subs] == [("v.a = t.a",)]
