"""Division-by-zero lock (V173 hotfix): every SQL division and modulo guards its own divisor.

INCIDENT: prod APP_ERROR_LOG 2026-10-01 06:49 Central, AlertScan ``rule_block_failed`` "Division by zero", CONTEXT
"rule COST_IDLE_OPPORTUNITY - other rules unaffected". SP_ALERT_SCAN_DAILY arm [24] computed
``ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1)`` behind only ``HAVING SUM(CREDITS_TOTAL) > 0``. Snowflake does
not promise to apply a WHERE / HAVING / JOIN / QUALIFY filter before it computes a projection, and
MART_WAREHOUSE_EFFICIENCY_DAILY holds CREDITS_TOTAL = 0 rows (the mart loader's FULL OUTER JOIN: a warehouse with
queries but no metering), so a zero reached the division. V169 (applied 2026-10-02) carried the text unchanged; V173
re-derives SP_ALERT_SCAN_DAILY from V169 and guards both [24] divisions in the expression itself. The read-only
PREFLIGHT grids V157 and V169 shipped with the same arm text (P157, P169.6) are history: they stay as written, allowed
only while the live arm is V173's guarded text (each entry's proof checks the scanned SP_ALERT_SCAN_DAILY).

THE RULE. In
  * the LATEST definer of every PROCEDURE / FUNCTION / VIEW / TASK / DYNAMIC TABLE the migrations create (one per
    name and argument types, so each overload counts; a later DROP retires it), including the dynamic SQL a body
    builds in string literals (a literal that carries SQL call syntax ``NAME(`` or a ``::`` cast, in any case), and
  * the read-only PREFLIGHT, PART B and owner REPAIR text every generator in outputs/ writes on request,
every divisor -- the denominator of a ``/`` and the second operand of a modulo (``a % b``, ``MOD(a, b)``) -- is, in
the expression itself, one of:
  * a non-zero numeric constant expression (literals, + - * /, POWER);
  * ``NULLIF(x, 0)``;
  * ``GREATEST(..., c)`` with a positive constant ``c``;
  * a parenthesised product of such factors;
  * inside the branch of an IFF / searched CASE whose condition compares that same expression with a constant so
    the branch never sees 0 (the polarity is checked: ``IFF(d = 0, 0, x / d)`` passes, ``IFF(d = 0, x / d, 0)``
    fails);
or it is an entry of _NEVER_ZERO: (object, divisor) -> how many times it occurs, a proof that must hold on that
object's text, and why it can never be 0 (or is never executed). A filter elsewhere in the statement is NOT a guard.
DIV0 / DIV0NULL have no ``/`` and pass by construction.

A trailing cast to FLOAT / DOUBLE / REAL keeps a guard. A cast to an integer type or to NUMBER / DECIMAL / NUMERIC
rounds to its scale (0 places when it has none), which can turn a small non-zero value into 0 (``0.4::INT`` = 0,
``0.001::NUMBER(10, 2)`` = 0): it keeps a constant or a GREATEST floor only when the rounded value is still non-zero
(a tie such as ``0.5::INT`` counts as 0, so the lock does not lean on the tie rule), and never keeps NULLIF
(``NULLIF(b, 0)::INT`` divides by 0 for b = 0.3) or a product. Any other cast voids the guard. A divisor that cannot
be read is reported, inside dynamic SQL too (a statement concatenated at run time, ``'ROUND(x / ' || col || ', 2)'``,
or a search needle): it needs a _NEVER_ZERO entry.
"""

from __future__ import annotations

import ast
import functools
import operator
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from decimal import ROUND_HALF_DOWN, Decimal, localcontext
from pathlib import Path
from typing import NamedTuple

import pytest

from tests._source import ROOT

_MIG = ROOT / "snowflake" / "migrations"
_OUTPUTS = ROOT / "outputs"

# ---------------------------------------------------------------------------------------------------------------
# Lexing: a same-length copy of the SQL with comments blanked and string / quoted-identifier interiors masked, so
# every position maps back to the original text and a '/' left in the copy is an operator. A double-quoted
# identifier is one token (an apostrophe in "it's" opens no string); it never spans a line, so a stray '"' cannot
# swallow the code after it.
# ---------------------------------------------------------------------------------------------------------------
_NOISE_RE = re.compile(r"--[^\n]*|//[^\n]*|/\*.*?\*/|\"(?:[^\"\n]|\"\")*\"|'(?:[^'\\]|\\.|'')*'", re.S)


def mask(sql: str) -> str:
    out: list[str] = []
    last = 0
    for m in _NOISE_RE.finditer(sql):
        out.append(sql[last:m.start()])
        tok = m.group(0)
        if tok[0] in "'\"":
            out.append(tok[0] + re.sub(r"[^\n]", "_", tok[1:-1]) + tok[0])
        else:
            out.append(re.sub(r"[^\n]", " ", tok))
        last = m.end()
    out.append(sql[last:])
    masked = "".join(out)
    assert len(masked) == len(sql)
    return masked


def _close(text: str, i: int) -> int:
    """Index just past the parenthesis group that opens at text[i]."""
    assert text[i] == "(", text[i:i + 20]
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k + 1
    raise ValueError(f"unbalanced parenthesis at {text[i:i + 40]!r}")


_IDENT_RE = re.compile(r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*")
_NUM_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_BIND_RE = re.compile(r":[A-Za-z_]\w*")
_POSTFIX_RE = re.compile(r"\s*::\s*[A-Za-z_]\w*(?:\s*\([\d\s,]*\))?|:[A-Za-z_]\w*(?::[A-Za-z_]\w*)*")


def _operand(m: str, i: int) -> tuple[int, int]:
    """[start, end) of the operand right after a '/' at i - 1 (the denominator): one primary (a parenthesised
    group, a number, a :bind, a name, a function call or a CASE ... END) plus its casts / path access. '/' and '*'
    bind tighter than '+' / '-', and are left-associative, so ``a / b * c`` divides by ``b`` only."""
    n = len(m)
    while i < n and m[i].isspace():
        i += 1
    start = i
    if i < n and m[i] in "+-":
        i += 1
        while i < n and m[i].isspace():
            i += 1
    if i < n and m[i] == "(":
        i = _close(m, i)
    elif i < n and (b := _BIND_RE.match(m, i)):
        i = b.end()
    elif num := _NUM_RE.match(m, i):
        i = num.end()
    elif ident := _IDENT_RE.match(m, i):
        i = ident.end()
        if ident.group(0).upper() == "CASE":
            depth = 1
            for t in re.finditer(r"\b(CASE|END)\b", m[i:], re.I):
                depth += 1 if t.group(1).upper() == "CASE" else -1
                if depth == 0:
                    i += t.end()
                    break
            else:
                raise ValueError("CASE without END")
        else:
            j = i
            while j < n and m[j] in " \t":
                j += 1
            if j < n and m[j] == "(":
                i = _close(m, j)
    else:
        raise ValueError(f"no denominator after '/': {m[start:start + 40]!r}")
    while post := _POSTFIX_RE.match(m, i):
        i = post.end()
    return start, i


def _divisions(m: str) -> list[int]:
    """The position of every division operator in the masked text."""
    return [k for k, c in enumerate(m) if c == "/"]


def _modulo_ops(m: str) -> list[int]:
    """The position of every ``%`` modulo operator in the masked text: a '%' right after an operand (a name, a number,
    a closing parenthesis or quote). A LIKE wildcard in a search needle such as ``'%NAME(%'`` follows none."""
    out = []
    for k, c in enumerate(m):
        if c == "%":
            j = k - 1
            while j >= 0 and m[j].isspace():
                j -= 1
            if j >= 0 and (m[j].isalnum() or m[j] in "_$)]'\""):
                out.append(k)
    return out


_MOD_RE = re.compile(r"\bMOD\s*\(", re.I)


def _mod_divisor(m: str, o: int) -> tuple[int, int]:
    """[start, end) of the divisor (the second argument) of the MOD( whose parenthesis opens at m[o]."""
    e = _close(m, o)
    depth, commas = 0, []
    for i in range(o + 1, e - 1):
        depth += (m[i] == "(") - (m[i] == ")")
        if m[i] == "," and depth == 0:
            commas.append(i)
    if len(commas) != 1:
        raise ValueError(f"MOD without two arguments: {m[o:e][:40]!r}")
    s, end = commas[0] + 1, e - 1
    while s < end and m[s].isspace():
        s += 1
    while end > s and m[end - 1].isspace():
        end -= 1
    return s, end


# ---------------------------------------------------------------------------------------------------------------
# Guard recognition
# ---------------------------------------------------------------------------------------------------------------
def _norm(s: str) -> str:
    return " ".join(s.split())


_EXACT_CASTS = frozenset({"FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL"})
_INTEGER_CASTS = frozenset({"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"})
_FIXED_CASTS = frozenset({"NUMBER", "NUMERIC", "DECIMAL"})
_CAST_RE = re.compile(r"\s*::\s*([A-Za-z_]\w*)\s*(?:\(\s*\d+\s*(?:,\s*(\d+)\s*)?\))?\s*$")


def _casts(d: str) -> tuple[str, tuple[int, ...] | None]:
    """``d`` without its trailing casts, and the decimal places those casts round it to, innermost first. FLOAT /
    DOUBLE / REAL keep a non-zero value non-zero and add nothing; NUMBER / NUMERIC / DECIMAL round to their scale (0
    places when none is given) and the integer types to 0 places, so a small non-zero value can become 0
    (0.4::INT = 0). None in place of the places: a cast to any other type, whose value this lock does not reason
    about."""
    scales: list[int] = []
    d = d.strip()
    while c := _CAST_RE.search(d):
        name = c.group(1).upper()
        if name in _INTEGER_CASTS:
            scales.append(0)
        elif name in _FIXED_CASTS:
            scales.append(int(c.group(2) or 0))
        elif name not in _EXACT_CASTS:
            return d, None
        d = d[:c.start()].strip()
    return d, tuple(reversed(scales))


def _rounded(v: float, scales: tuple[int, ...]) -> float:
    """``v`` after casts that round it to these decimal places in turn. A tie (exactly half a unit) rounds toward 0,
    so the zero test never leans on the cast's tie rule."""
    for s in scales:
        if abs(v) < 1e15:                                   # a larger value never rounds to 0
            with localcontext() as ctx:
                ctx.prec = 100
                v = float(Decimal(repr(v)).quantize(Decimal(1).scaleb(-s), rounding=ROUND_HALF_DOWN))
    return v


def _strip_exact_casts(d: str) -> str:
    """``d`` without trailing FLOAT / DOUBLE / REAL casts. A rounding or other cast stays: it is part of the value,
    and ``b > 0`` does not rule out ``b::INT`` = 0."""
    while (c := _CAST_RE.search(d)) and c.group(1).upper() in _EXACT_CASTS:
        d = d[:c.start()]
    return d.strip()


def _wrapped(d: str) -> bool:
    try:
        return d.startswith("(") and d.endswith(")") and _close(d, 0) == len(d)
    except ValueError:                                      # unbalanced: a string fragment
        return False


def _bare(d: str) -> str:
    d = _norm(_strip_exact_casts(d))
    while _wrapped(d):
        d = _norm(_strip_exact_casts(d[1:-1]))
    return d.upper()


def _split_top(text: str, seps: str) -> list[str]:
    """Split on separator characters at parenthesis depth 0."""
    parts, cur, depth = [], [], 0
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and ch in seps:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _const_value(d: str) -> float | None:
    """The value of a numeric constant expression (literals, + - * /, parentheses, POWER) after its trailing casts
    (``0.4::INT`` is 0), else None."""
    d, scales = _casts(d)
    if scales is None:
        return None
    expr = re.sub(r"\bPOWER?\s*\(", "pow(", d, flags=re.I)
    if re.search(r"[A-Za-z_]", expr.replace("pow(", "")):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "pow"
                and len(node.args) == 2 and not node.keywords):
            return float(ev(node.args[0]) ** ev(node.args[1]))
        raise ValueError(ast.dump(node))

    try:
        return _rounded(ev(tree), scales)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def denominator_kind(d: str, rounds: tuple[int, ...] = ()) -> str | None:
    """How the denominator text itself rules out a zero: 'const' / 'nullif' / 'greatest' / 'product', else None.
    ``rounds``: the decimal places that casts OUTSIDE ``d`` round its value to, innermost first."""
    d, scales = _casts(_norm(d))
    if scales is None:
        return None
    rounds = scales + rounds
    value = _const_value(d)
    if value is not None:
        return "const" if _rounded(value, rounds) != 0 else None
    if _wrapped(d):
        inner = _norm(d[1:-1])
        if not rounds and len(_split_top(inner, "+-/")) == 1:   # a pure product: every factor non-zero-or-NULL
            factors = _split_top(inner, "*")
            if len(factors) > 1 and all(denominator_kind(f) for f in factors):
                return "product"
        return denominator_kind(inner, rounds) if len(_split_top(inner, "+-*/")) == 1 else None
    call = re.match(r"([A-Za-z_]\w*)\s*\(", d)
    try:
        whole = call is not None and _close(d, call.end() - 1) == len(d)
    except ValueError:                                      # a parenthesis inside a string argument
        whole = False
    if call and whole:
        name = call.group(1).upper()
        args = [_norm(a) for a in _split_top(d[call.end():-1], ",")]
        if name == "NULLIF" and not rounds and len(args) == 2 and _const_value(args[1]) == 0:
            return "nullif"                                 # NULLIF(0.3, 0)::INT is 0: no rounding cast
        floors = [v for a in args if (v := _const_value(a)) is not None and v > 0]
        if name == "GREATEST" and floors and _rounded(max(floors), rounds) > 0:
            return "greatest"
    return None


_ATOM_RE = re.compile(r"(.+?)\s*(<>|!=|>=|<=|=|>|<)\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.S)
_ATOM_REV_RE = re.compile(r"(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(<>|!=|>=|<=|=|>|<)\s*(.+)", re.S)
_FLIP = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "=": "=", "<>": "<>", "!=": "!="}


def _atom(cond: str, den: str) -> tuple[str, float] | None:
    """``<den> <op> <number>`` (either side) -> (op, number) with the denominator on the left, else None."""
    c = _norm(cond)
    while _wrapped(c):
        c = _norm(c[1:-1])
    if (m := _ATOM_RE.fullmatch(c)) and _bare(m.group(1)) == _bare(den):
        return m.group(2), float(m.group(3))
    if (m := _ATOM_REV_RE.fullmatch(c)) and _bare(m.group(3)) == _bare(den):
        return _FLIP[m.group(2)], float(m.group(1))
    return None


def _true_rules_out_zero(op: str, n: float) -> bool:
    """The comparison holds -> the denominator is not 0."""
    return {">": n >= 0, ">=": n > 0, "<": n <= 0, "<=": n < 0, "<>": n == 0, "!=": n == 0, "=": n != 0}[op]


def _false_rules_out_zero(op: str, n: float) -> bool:
    """The comparison is FALSE -> the denominator is not 0 (a NULL denominator divides to NULL, never raises)."""
    return {"=": n == 0, "<=": n >= 0, "<": n > 0, ">": n < 0, ">=": n <= 0, "<>": False, "!=": False}[op]


def _split_bool(cond: tuple[str, str], word: str) -> list[str]:
    """Top-level AND / OR split: positions from the masked condition, slices of the original."""
    orig, msk = cond
    out, depth, last = [], 0, 0
    for t in re.finditer(rf"\(|\)|\b{word}\b", msk, re.I):
        if t.group(0) == "(":
            depth += 1
        elif t.group(0) == ")":
            depth -= 1
        elif depth == 0:
            out.append(orig[last:t.start()])
            last = t.end()
    out.append(orig[last:])
    return out


def _when_true_guards(cond: tuple[str, str], den: str) -> bool:
    return any((a := _atom(p, den)) and _true_rules_out_zero(*a) for p in _split_bool(cond, "AND"))


def _when_false_guards(cond: tuple[str, str], den: str) -> bool:
    return any((a := _atom(p, den)) and _false_rules_out_zero(*a) for p in _split_bool(cond, "OR"))


def _iff_guard(sql: str, m: str, k: int, den: str) -> bool:
    for call in re.finditer(r"\bIFF\s*\(", m, re.I):
        o = call.end() - 1
        if o > k:
            break
        try:
            e = _close(m, o)
        except ValueError:                                  # an unclosed IFF( in a string fragment
            continue
        if not o < k < e:
            continue
        spans, depth, last = [], 0, o + 1
        for i in range(o + 1, e - 1):
            depth += (m[i] == "(") - (m[i] == ")")
            if m[i] == "," and depth == 0:
                spans.append((last, i))
                last = i + 1
        spans.append((last, e - 1))
        if len(spans) != 3:
            continue
        cond = (sql[spans[0][0]:spans[0][1]], m[spans[0][0]:spans[0][1]])
        if spans[1][0] <= k < spans[1][1] and _when_true_guards(cond, den):
            return True
        if spans[2][0] <= k < spans[2][1] and _when_false_guards(cond, den):
            return True
    return False


def _case_guard(sql: str, m: str, k: int, den: str) -> bool:
    for case in re.finditer(r"\bCASE\b", m, re.I):
        s = case.start()
        if s > k:
            break
        depth_case, depth_par, marks, end = 1, 0, [], None
        for t in re.finditer(r"\(|\)|\b(?:CASE|WHEN|THEN|ELSE|END)\b", m[case.end():], re.I):
            tok, pos = t.group(0).upper(), case.end() + t.start()
            if tok == "(":
                depth_par += 1
            elif tok == ")":
                depth_par -= 1
            elif tok == "CASE":
                depth_case += 1
            elif tok == "END":
                depth_case -= 1
                if depth_case == 0:
                    end = pos
                    break
            elif depth_case == 1 and depth_par == 0:
                marks.append((tok, pos, pos + len(tok)))
        if end is None or not s < k < end or not marks or marks[0][0] != "WHEN":
            continue                                         # a simple CASE x WHEN v tests no denominator
        conds: list[tuple[str, str]] = []
        branches: list[tuple[int | None, int, int]] = []
        for i, (tok, _a, b) in enumerate(marks):
            nxt = marks[i + 1][1] if i + 1 < len(marks) else end
            if tok == "WHEN":
                conds.append((sql[b:nxt], m[b:nxt]))
            elif tok == "THEN":
                branches.append((len(conds) - 1, b, nxt))
            elif tok == "ELSE":
                branches.append((None, b, nxt))
        for idx, a, b in branches:
            if not a <= k < b:
                continue
            earlier = conds if idx is None else conds[:idx]
            if any(_when_false_guards(c, den) for c in earlier):
                return True
            if idx is not None and _when_true_guards(conds[idx], den):
                return True
    return False


_DYNAMIC_SQL_RE = re.compile(r"\b[A-Z][A-Z0-9_]*\(|::\s*[A-Z]", re.I)
_DIVIDES_RE = re.compile(r"[/%]|\bMOD\s*\(", re.I)


def unguarded(sql: str) -> list[str]:
    """Every divisor in ``sql`` that does not guard itself (allow-list not applied), in text order: the normalised
    denominator of a '/', or ``% <divisor>`` for a modulo (``a % b``, ``MOD(a, b)``). A divisor that cannot be read is
    reported as ``<unreadable: ...>`` (``% <unreadable: ...>``). A string literal carrying SQL call syntax, in any
    case, is dynamic SQL and is checked the same way, recursively; an unreadable divisor there is reported too: a
    piece of a statement concatenated at run time (``'ROUND(x / ' || col``) or a search needle is not proved safe."""
    m = mask(sql)
    sites: list[tuple[int, str, Callable[[], tuple[int, int]]]] = []
    sites += [(k, "", functools.partial(_operand, m, k + 1)) for k in _divisions(m)]
    sites += [(k, "% ", functools.partial(_operand, m, k + 1)) for k in _modulo_ops(m)]
    sites += [(c.start(), "% ", functools.partial(_mod_divisor, m, c.end() - 1)) for c in _MOD_RE.finditer(m)]
    out: list[str] = []
    for k, op, span in sorted(sites, key=operator.itemgetter(0)):
        try:
            s, e = span()
        except ValueError as exc:
            out.append(f"{op}<unreadable: {exc}>")
            continue
        den = _norm(sql[s:e])
        if not (denominator_kind(den) or _iff_guard(sql, m, k, den) or _case_guard(sql, m, k, den)):
            out.append(op + den)
    for lit in re.finditer(r"'(_*(?:\n_*)*)'", m):
        body = sql[lit.start() + 1:lit.end() - 1].replace("''", "'")
        if _DIVIDES_RE.search(body) and _DYNAMIC_SQL_RE.search(body):
            out += unguarded(body)
    return out


# ---------------------------------------------------------------------------------------------------------------
# What is scanned
# ---------------------------------------------------------------------------------------------------------------
_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:SECURE\s+)?(PROCEDURE|FUNCTION|VIEW|TASK|DYNAMIC\s+TABLE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?((?:\w+\.)*\w+)", re.I)
_DROP_RE = re.compile(
    r"\bDROP\s+(PROCEDURE|FUNCTION|VIEW|TASK|DYNAMIC\s+TABLE)\s+(?:IF\s+EXISTS\s+)?((?:\w+\.)*\w+)\s*(\([^)]*\))?",
    re.I)


def _statements(m: str) -> list[tuple[int, int]]:
    """Top-level statements of a masked migration: split on ';' outside $$ bodies."""
    out, start, inside, i = [], 0, False, 0
    while i < len(m):
        if m.startswith("$$", i):
            inside = not inside
            i += 2
            continue
        if m[i] == ";" and not inside:
            out.append((start, i + 1))
            start = i + 1
        i += 1
    out.append((start, len(m)))
    return out


def _base_type(tok: str) -> str:
    return re.match(r"[A-Za-z_]+", tok).group(0).upper()


def _key(kind: str, name: str, sig_types: list[str] | None) -> str:
    base = name.split(".")[-1].upper()
    return f"{base}({','.join(sig_types)})" if kind in ("PROCEDURE", "FUNCTION") else base


@functools.cache
def _code_events() -> tuple[tuple[int, int, str, str, str | None], ...]:
    """Every CREATE and DROP of a code object, (version, position, 'create' | 'drop', key, CREATE text) in replay
    order; cached, read once per run."""
    events: list[tuple[int, int, str, str, str | None]] = []
    for p in _MIG.glob("V*.sql"):
        v = int(re.match(r"V(\d+)", p.name).group(1))
        text = p.read_text(encoding="utf-8")
        m = mask(text)
        for a, b in _statements(m):
            lead = a + (len(m[a:b]) - len(m[a:b].lstrip()))
            c = _CREATE_RE.match(m, lead)
            if not c or c.start() >= b:
                continue
            kind = re.sub(r"\s+", " ", c.group(1).upper())
            types = None
            if kind in ("PROCEDURE", "FUNCTION"):
                o = m.index("(", c.end())
                args = [x for x in _split_top(m[o + 1:_close(m, o) - 1], ",") if x.strip()]
                types = [_base_type(x.split()[1]) for x in args]
            events.append((v, lead, "create", _key(kind, c.group(2), types), text[lead:b]))
        for d in _DROP_RE.finditer(m):
            kind = re.sub(r"\s+", " ", d.group(1).upper())
            types = None
            if kind in ("PROCEDURE", "FUNCTION"):
                types = [_base_type(x.split()[0]) for x in (d.group(3) or "()")[1:-1].split(",") if x.strip()]
            events.append((v, d.start(), "drop", _key(kind, d.group(2), types), None))
    return tuple(sorted(events, key=lambda e: (e[0], e[1])))


@functools.cache
def _latest_definers() -> tuple[tuple[str, tuple[int, str]], ...]:
    """Replay every CREATE and DROP of a code object in (version, position) order."""
    defs: dict[str, tuple[int, str]] = {}
    for v, _pos, what, key, body in _code_events():
        if what == "create":
            assert body is not None
            defs[key] = (v, body)
        else:
            defs.pop(key, None)
    return tuple(defs.items())


def latest_definers() -> dict[str, tuple[int, str]]:
    """{object key: (version, CREATE statement text)} for every object still defined at the migration tip."""
    return dict(_latest_definers())


def definer_history(key: str) -> list[tuple[int, str]]:
    """[(version, CREATE statement text)] of every definition of one object, oldest first."""
    return [(v, body) for v, _pos, what, k, body in _code_events() if what == "create" and k == key and body]


def _runbox_generators() -> dict[str, list[str]]:
    """{generator file: the runbox env vars it honours} for every outputs/gen_v*.py that writes runbox text."""
    out = {}
    for g in sorted(_OUTPUTS.glob("gen_v*.py")):
        src = g.read_text(encoding="utf-8")
        envs = sorted(set(re.findall(r"\"(PREFLIGHT_OUT|PART_B_OUT|PARTB_OUT|REPAIR_OUT)\"", src)))
        if envs:
            out[g.name] = envs
    return out


_KIND = {"PREFLIGHT_OUT": "PREFLIGHT", "PART_B_OUT": "PART B", "PARTB_OUT": "PART B", "REPAIR_OUT": "REPAIR"}


def _run_generator(gen: str, envs: list[str], tmp: Path) -> dict[str, str]:
    src = (_OUTPUTS / gen).read_text(encoding="utf-8")
    mig_env = re.search(r"os\.environ\.get\(\"(V\d+_OUT)\"\)", src).group(1)
    env = {k: v for k, v in os.environ.items()
           if k not in (*_KIND, mig_env) and not re.fullmatch(r"V\d+_OUT", k)}
    env[mig_env] = str(tmp / f"{gen}.migration.sql")
    paths = {e: tmp / f"{gen}.{e}.sql" for e in envs}
    env.update({e: str(p) for e, p in paths.items()})
    res = subprocess.run([sys.executable, str(_OUTPUTS / gen)], env=env, cwd=tmp, capture_output=True, text=True)
    assert res.returncode == 0, (gen, res.stderr[-2000:])
    assert all(p.exists() for p in paths.values()), (gen, [e for e, p in paths.items() if not p.exists()])
    stem = gen.removesuffix(".py")
    return {f"{stem} {_KIND[e]}": p.read_text(encoding="utf-8") for e, p in paths.items()}


@pytest.fixture(scope="module")
def runbox_texts(tmp_path_factory) -> dict[str, str]:
    tmp = tmp_path_factory.mktemp("runbox_division_guards")
    texts: dict[str, str] = {}
    for gen, envs in _runbox_generators().items():
        texts.update(_run_generator(gen, envs, tmp))
    return texts


def _scan_set(runbox: dict[str, str]) -> dict[str, str]:
    texts = {k: body for k, (_v, body) in latest_definers().items()}
    assert not set(texts) & set(runbox)
    return {**texts, **runbox}


# ---------------------------------------------------------------------------------------------------------------
# The reasoned allow-list: divisors that are never 0 by construction, or text that is never executed, each with a
# proof on its own text. ``holds_while``: a condition on the whole scanned set (history allowed only while it is
# superseded).
# ---------------------------------------------------------------------------------------------------------------
class NeverZero(NamedTuple):
    count: int
    proof: Callable[[str], bool]
    reason: str
    holds_while: Callable[[dict[str, str]], bool] | None = None


# V173's arm [24] divisions, as the live SP_ALERT_SCAN_DAILY carries them
_ARM24_GUARDED = ("ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) AS IDLE_PCT",
                  ":credit_price / NULLIF(s.COVERED_DAYS, 0) * 30, 2) AS MONTHLY_USD")


def _arm24_live_text_is_guarded(texts: dict[str, str]) -> bool:
    live = texts.get("SP_ALERT_SCAN_DAILY()", "")
    return all(live.count(g) == 1 for g in _ARM24_GUARDED)


_ARM24_HISTORY = ("HISTORY, not run again: the read-only grid that previewed arm [24] COST_IDLE_OPPORTUNITY before "
                  "its apply, carrying the arm's text as it shipped (the 2026-10-01 'Division by zero' text). Allowed "
                  "only while the live SP_ALERT_SCAN_DAILY is V173's NULLIF-guarded arm; V173 PREFLIGHT P173.5 is the "
                  "current preview")


def _days_in_month_proof(text: str) -> bool:
    return text.count("AS DAYS_IN_MONTH") == text.count("DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH") >= 1


_DAYS_IN_MONTH = ("DAYS_IN_MONTH = DAY(LAST_DAY(CURRENT_DATE())), the length of this month (28-31), in the mtd CTE: an "
                  "aggregate with no GROUP BY, so always exactly one row")

_NEVER_ZERO: dict[tuple[str, str], NeverZero] = {
    ("SP_ALERT_SCAN()", "DAY(LAST_DAY(CURRENT_DATE()))"): NeverZero(
        1, lambda t: "(DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(CURRENT_DATE())) AS TIME_SHARE" in t,
        "COST_DEPT_BUDGET TIME_SHARE: DAY(LAST_DAY(d)) is the length of d's month, 28-31 (the division by "
        "BUDGET_USD * TIME_SHARE is NULLIF-guarded)"),
    ("SP_ALERT_SCAN_DAILY()", "m.DAYS_IN_MONTH"): NeverZero(
        3, _days_in_month_proof,
        "[08] COST_BUDGET_PACE TITLE (inside the NULLIF), DETAIL and JOIN predicate: " + _DAYS_IN_MONTH),
    ("SP_ALERT_SCAN_DAILY()", "n.CAP_CR"): NeverZero(
        2, lambda t: t.count("AS CAP_CR") == 1 and (
            "SELECT COALESCE(NULLIF(GREATEST(COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'COCO_DAILY_CAP_CREDITS', VALUE, "
            "NULL))), 15), 0), 0), 15) AS CAP_CR,") in t and "CROSS JOIN knob n" in t,
        "[28] COST_AI_USER_RUNAWAY TITLE and METRIC_VALUE: knob.CAP_CR = COALESCE(NULLIF(GREATEST(x, 0), 0), 15) -- "
        "a negative, zero or unreadable setting reads as 15 -- in a SETTINGS aggregate with no GROUP BY (one row)"),
    ("SP_LOAD_OBJECT_COST(FLOAT)", "c.N"): NeverZero(
        1, lambda t: ("WITH counts AS (\n        SELECT QUERY_ID, COUNT(*) AS N\n"
                      "        FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE\n        GROUP BY QUERY_ID\n    )") in t
        and "    JOIN counts c ON c.QUERY_ID = qa.QUERY_ID\n" in t and "LEFT JOIN counts" not in t,
        "the equal split SUM(qa.CREDITS / c.N): N = COUNT(*) per QUERY_ID group, and the INNER JOIN pairs a query "
        "only with an existing group, which has at least one row"),
    ("gen_v169 PREFLIGHT", "m.DAYS_IN_MONTH"): NeverZero(
        2, _days_in_month_proof,
        "P169.1 OLD_PACE_RATIO / NEW_PACE_RATIO, inside their NULLIF, on the arm's own mtd CTE: " + _DAYS_IN_MONTH),
    ("gen_v157 PREFLIGHT", "i.TOTAL_CREDITS"): NeverZero(
        1, lambda t: t.count("ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT") == 1,
        "PREFLIGHT_WAVE2B (V157): " + _ARM24_HISTORY, _arm24_live_text_is_guarded),
    ("gen_v157 PREFLIGHT", "s.COVERED_DAYS"): NeverZero(
        1, lambda t: t.count("(SELECT CREDIT_PRICE FROM px) / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD") == 1,
        "PREFLIGHT_WAVE2B (V157): " + _ARM24_HISTORY, _arm24_live_text_is_guarded),
    ("gen_v169 PREFLIGHT", "i.TOTAL_CREDITS"): NeverZero(
        1, lambda t: t.count("ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT") == 1,
        "P169.6 (V169): " + _ARM24_HISTORY, _arm24_live_text_is_guarded),
    ("gen_v169 PREFLIGHT", "s.COVERED_DAYS"): NeverZero(
        1, lambda t: t.count("/ s.COVERED_DAYS * 30, 2) AS MONTHLY_USD") == 1,
        "P169.6 (V169): " + _ARM24_HISTORY, _arm24_live_text_is_guarded),
    ("gen_v172 PART B", "<unreadable: unbalanced parenthesis at '(t.AFTER_CALLS'>"): NeverZero(
        2, lambda t: t.count("TOTAL_CR / NULLIF(t.AFTER_CALLS") == 2 and (
            "SELECT 'V172.1 SP_CHANGE_IMPACT_SCAN DDL lacks: TOTAL_CR / NULLIF(t.AFTER_CALLS', IFF(NOT CONTAINS("
            "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()'), 'TOTAL_CR / NULLIF(t.AFTER_CALLS'"
            "), 'OK', 'FAIL: still the old body')") in t,
        "V172.1 search text, never executed: the needle CONTAINS looks for in GET_DDL (the V140 AFTER_CREDITS_PER_CALL "
        "line V172 replaces) and the same words in the row's CHECK label"),
}


def _shown(den: str) -> str:
    """A reported divisor with its operator: '/ x', or '% x' for a modulo."""
    return den if den.startswith("% ") else f"/ {den}"


def violations(texts: dict[str, str]) -> list[str]:
    out: list[str] = []
    used: Counter[tuple[str, str]] = Counter()
    for label, sql in sorted(texts.items()):
        for den in unguarded(sql):
            if (label, den) in _NEVER_ZERO:
                used[(label, den)] += 1
            else:
                out.append(f"{label}: '{_shown(den)}'")
    for (label, den), entry in _NEVER_ZERO.items():
        if label not in texts:
            continue
        if used[(label, den)] != entry.count:
            out.append(f"{label}: allow-listed '{_shown(den)}' occurs {used[(label, den)]}x, expected {entry.count}x "
                       "(re-check the reason, then update _NEVER_ZERO)")
        if not entry.proof(texts[label]):
            out.append(f"{label}: the proof for allow-listed '{_shown(den)}' no longer holds ({entry.reason})")
        if entry.holds_while is not None and not entry.holds_while(texts):
            out.append(f"{label}: allow-listed '{_shown(den)}' is history no longer superseded ({entry.reason})")
    return out


# ---------------------------------------------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------------------------------------------
def test_every_division_guards_its_own_denominator(runbox_texts):
    found = violations(_scan_set(runbox_texts))
    assert not found, (
        "A SQL division can see a zero denominator. A WHERE / HAVING / JOIN filter does not order evaluation in "
        "Snowflake (the 2026-10-01 COST_IDLE_OPPORTUNITY 'Division by zero'): wrap the denominator in NULLIF(x, 0) "
        "(or an IFF / CASE zero test), or, when it is 0-free by construction, add a reasoned _NEVER_ZERO entry with "
        "a proof:\n  " + "\n  ".join(found))


def test_every_allow_list_entry_names_a_scanned_text(runbox_texts):
    texts = _scan_set(runbox_texts)
    assert {label for label, _ in _NEVER_ZERO} <= set(texts)
    for (_label, den), entry in _NEVER_ZERO.items():
        assert entry.count >= 1 and len(entry.reason) > 40 and denominator_kind(den) is None, den


def test_the_scan_covers_the_current_definers_and_every_runbox_generator(runbox_texts):
    from tests.test_proc_lineage import _definers, _migrations
    defs = latest_definers()
    by_name = _definers(_migrations())                  # the lineage guard's own view, overloads merged
    for key in ("SP_ALERT_SCAN_DAILY()", "SP_ALERT_SCAN()", "SP_LOAD_OBJECT_COST(FLOAT)", "SP_DAILY_DIGEST()"):
        assert defs[key][0] == max(by_name[key.split("(")[0]]), key
    # each overload is its own object, and a dropped object leaves the scan
    assert {k for k in defs if k.startswith("SP_INCIDENT_DECLARE(")} == {
        "SP_INCIDENT_DECLARE(VARCHAR,VARCHAR,VARCHAR,VARCHAR)",
        "SP_INCIDENT_DECLARE(VARCHAR,VARCHAR,VARCHAR,VARCHAR,VARCHAR)"}
    assert "SP_BACKUP_OPERATOR_TABLES()" not in defs and "TASK_BACKUP_OPERATOR" not in defs
    assert "MART_SPEND_ROLLUP_DT" not in defs
    assert not [k for k in defs if k.startswith("SP_ACTION_LIFECYCLE(") and k.count(",") == 7]
    kinds = Counter(re.match(r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:SECURE\s+)?(\w+)", body, re.I).group(1).upper()
                    for _v, body in defs.values())
    assert kinds["PROCEDURE"] >= 40 and kinds["TASK"] >= 30 and kinds["VIEW"] >= 1, kinds
    assert defs["SP_ALERT_SCAN_DAILY()"][0] >= 173 and defs["SP_ALERT_SCAN()"][0] >= 173      # V173 re-derived both
    gens = _runbox_generators()
    assert {"gen_v157.py", "gen_v169.py", "gen_v172.py", "gen_v173.py"} <= set(gens)
    for gen in gens:
        assert any(label.startswith(gen.removesuffix(".py") + " ") for label in runbox_texts), gen
    assert {"gen_v169 PREFLIGHT", "gen_v169 PART B", "gen_v169 REPAIR", "gen_v157 PREFLIGHT", "gen_v173 PREFLIGHT",
            "gen_v173 PART B"} <= set(runbox_texts)
    # the V173 preview runs the guarded arm: no allow-list entry, no finding
    assert not unguarded(runbox_texts["gen_v173 PREFLIGHT"]) and not unguarded(runbox_texts["gen_v173 PART B"])
    assert runbox_texts["gen_v173 PREFLIGHT"].count("NULLIF(i.TOTAL_CREDITS, 0)") == 1
    # dynamic SQL in string literals is read too (V155 builds its operator-stats INSERT in strings)
    body = defs["SP_LOAD_QUERY_OPERATOR_STATS(FLOAT)"][1]
    assert "'/ NULLIF(OPERATOR_STATISTICS:input_rows::FLOAT, 0), 3), '" in body
    assert not unguarded(body) and unguarded(body.replace("'/ NULLIF(OPERATOR_STATISTICS:input_rows::FLOAT, 0), 3), '",
                                                          "'/ OPERATOR_STATISTICS:input_rows::FLOAT, 3), '"))
    # modulo divisors are read too: the hourly scan's cadence gates are MOD(ct_hour, 4) and MOD(ct_hour, 3)
    scan = defs["SP_ALERT_SCAN()"][1]
    before = unguarded(scan)                            # its one allow-listed '/', no modulo
    assert scan.count("IF (MOD(ct_hour, 3) = 2) THEN") == 1 and not [d for d in before if d.startswith("% ")]
    after = unguarded(scan.replace("IF (MOD(ct_hour, 3) = 2)", "IF (MOD(ct_hour, n_slots) = 2)"))
    assert sorted(after) == sorted([*before, "% n_slots"])
    # an unreadable divisor in a string is reported, not dropped: V172.1's GET_DDL needle is allow-listed by name
    assert unguarded(runbox_texts["gen_v172 PART B"]) == [
        "<unreadable: unbalanced parenthesis at '(t.AFTER_CALLS'>"] * 2


# -- teeth: the incident text, and the V163 / V169 text, fail the lock -------------------------------------------

_OLD_24 = (("i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100", "i.IDLE_CREDITS / i.TOTAL_CREDITS * 100"),
           (":credit_price / NULLIF(s.COVERED_DAYS, 0) * 30", ":credit_price / s.COVERED_DAYS * 30"))


def _unfix(text: str) -> str:
    for new, old in _OLD_24:
        assert text.count(new) == 1, new
        text = text.replace(new, old)
    return text


def test_the_lock_fails_when_the_live_arm_loses_its_guards(runbox_texts):
    """V173's arm [24] without its two NULLIFs: both divisions are reported, and the historical P157 / P169.6 grids
    lose their allowance (their text is history only while the live arm is guarded)."""
    texts = _scan_set(runbox_texts)
    texts["SP_ALERT_SCAN_DAILY()"] = _unfix(texts["SP_ALERT_SCAN_DAILY()"])
    found = violations(texts)
    assert found[:2] == ["SP_ALERT_SCAN_DAILY(): '/ i.TOTAL_CREDITS'", "SP_ALERT_SCAN_DAILY(): '/ s.COVERED_DAYS'"]
    assert sorted(f.split(" is history")[0] for f in found[2:]) == sorted(
        f"gen_v{v} PREFLIGHT: allow-listed '/ {d}'" for v in (157, 169) for d in ("i.TOTAL_CREDITS", "s.COVERED_DAYS"))


def test_the_historical_grids_are_allowed_exactly_as_shipped(runbox_texts):
    """The P169.6 grid stays V169's text (history, never edited); the allowance counts exactly its two divisions."""
    texts = _scan_set(runbox_texts)
    pf = texts["gen_v169 PREFLIGHT"]
    assert pf.count("ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT") == 1
    texts["gen_v169 PREFLIGHT"] = pf + "\nSELECT a / b FROM t;\n"
    assert violations(texts) == ["gen_v169 PREFLIGHT: '/ b'"]
    texts["gen_v169 PREFLIGHT"] = pf.replace("ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT",
                                             "ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT, "
                                             "ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT_2")
    found = violations(texts)
    assert len(found) == 2 and all(f.startswith("gen_v169 PREFLIGHT: ") for f in found), found
    assert "occurs 2x, expected 1x" in found[0] and "no longer holds" in found[1], found


def _body(rel: str) -> str:
    text = (_MIG / rel).read_text(encoding="utf-8")
    s = text.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    return text[s:text.index("$$;", text.index("$$", s) + 2) + 3]


@pytest.mark.parametrize("rel", ["V163__ai_runaway_trust_regression.sql",
                                 "V169__alert_scan_daily_windows_and_keys.sql"])
def test_the_lock_catches_the_prod_bodies_v173_replaces(rel):
    """V163's body failed at 06:49 on 2026-10-01; V169 (applied 2026-10-02) carried the same two divisions."""
    assert violations({"SP_ALERT_SCAN_DAILY()": _body(rel)}) == [
        "SP_ALERT_SCAN_DAILY(): '/ i.TOTAL_CREDITS'", "SP_ALERT_SCAN_DAILY(): '/ s.COVERED_DAYS'"]


@pytest.mark.parametrize(("sql", "bad"), [
    # the incident shape: a HAVING (or WHERE / JOIN) filter is not a guard
    ("SELECT a / b FROM t GROUP BY 1 HAVING SUM(b) > 0", ["b"]),
    ("SELECT x.a / x.b FROM t x WHERE x.b > 0", ["x.b"]),
    ("SELECT a / NULLIF(b, 0) FROM t", []),
    ("SELECT a / NULLIF(b, 1) FROM t", ["NULLIF(b, 1)"]),
    ("SELECT a / 0 FROM t", ["0"]),
    ("SELECT a / (60 - 60) FROM t", ["(60 - 60)"]),
    ("SELECT a / POWER(1024, 3), a / 86400.0, a / (60 * 60) FROM t", []),
    ("SELECT a / GREATEST(b, 0.5), a / GREATEST(b, 0) FROM t", ["GREATEST(b, 0)"]),
    ("SELECT a / (NULLIF(b, 0) * 60) FROM t", []),
    ("SELECT a / b::FLOAT FROM t", ["b::FLOAT"]),
    ("SELECT a / NULLIF(b, 0)::FLOAT FROM t", []),
    ("SELECT a / :n FROM t", [":n"]),
    # IFF / CASE polarity
    ("SELECT IFF(SUM(q) = 0, 0, SUM(f) / SUM(q) * 100) FROM t", []),
    ("SELECT IFF(SUM(q) = 0, SUM(f) / SUM(q), 0) FROM t", ["SUM(q)"]),
    ("SELECT IFF(b > 0, a / b, NULL) FROM t", []),
    ("SELECT IFF(b >= 0, a / b, NULL) FROM t", ["b"]),
    ("SELECT IFF(b < 20, 0, a / b) FROM t", []),
    ("SELECT IFF(c > 0, a / b, 0) FROM t", ["b"]),
    ("SELECT CASE WHEN n < 5 THEN NULL WHEN d.MAD > 0 THEN x / d.MAD ELSE 0 END FROM t", []),
    ("SELECT CASE WHEN d.MAD > 0 THEN 0 ELSE x / d.MAD END FROM t", ["d.MAD"]),
    ("SELECT CASE WHEN p = 0 THEN 999 ELSE (c / p - 1) * 100 END FROM t", []),
    ("SELECT CASE WHEN p = 0 OR q = 0 THEN 0 ELSE c / p END FROM t", []),
    ("SELECT CASE WHEN p = 0 AND q = 0 THEN 0 ELSE c / p END FROM t", ["p"]),
    ("SELECT CASE WHEN p > 0 AND q > 0 THEN c / p END FROM t", []),
    ("SELECT CASE k WHEN 0 THEN 0 ELSE c / k END FROM t", ["k"]),
    # strings and comments are not divisions; dynamic SQL in a literal is
    ("SELECT 'n/a', 'America/Chicago' -- a / b\n/* c / d */ FROM t", []),
    ("SELECT 'ROUND(x / y, 2)' FROM t", ["y"]),
    ("SELECT 'ROUND(x / NULLIF(y, 0), 2)' FROM t", []),
    ("SELECT 'x / y is prose' FROM t", []),
    ("SELECT 'select round(x / y, 2) from t' FROM t", ["y"]),            # lower-case dynamic SQL is read too
    # an unreadable divisor inside dynamic SQL is reported: a statement concatenated at run time is not proved safe
    ("SELECT 'ROUND(x / ' || col || ', 2)' FROM t", ["<unreadable: no denominator after '/': ''>"]),
    # a double-quoted identifier is one token: its apostrophe opens no string, its '/' divides nothing
    ("SELECT \"it's\", a / b, 'z' FROM t", ["b"]),
    ("SELECT \"a/b\" FROM t", []),
    ("SELECT 1 AS \"x\n, a / b FROM t", ["b"]),                          # a stray quote ends at its line
    # a cast that rounds can turn a guarded non-zero value into 0 (Snowflake: 0.4::INT = 0)
    ("SELECT a / NULLIF(b, 0)::INT, c / GREATEST(d, 0.4)::NUMBER(10,0), e / 0.4::INT FROM t",
     ["NULLIF(b, 0)::INT", "GREATEST(d, 0.4)::NUMBER(10,0)", "0.4::INT"]),
    ("SELECT a / NULLIF(b, 0)::INTEGER, a / NULLIF(b, 0)::BIGINT, a / NULLIF(b, 0)::NUMBER FROM t",
     ["NULLIF(b, 0)::INTEGER", "NULLIF(b, 0)::BIGINT", "NULLIF(b, 0)::NUMBER"]),
    ("SELECT a / NULLIF(b, 0)::NUMBER(10, 2), a / NULLIF(b, 0)::DECIMAL(38, 0) FROM t",   # 0.001 -> 0.00
     ["NULLIF(b, 0)::NUMBER(10, 2)", "NULLIF(b, 0)::DECIMAL(38, 0)"]),
    ("SELECT a / (NULLIF(b, 0))::INT, a / (NULLIF(b, 0) * 60)::INT, a / NULLIF(b, 0)::FLOAT::INT FROM t",
     ["(NULLIF(b, 0))::INT", "(NULLIF(b, 0) * 60)::INT", "NULLIF(b, 0)::FLOAT::INT"]),
    ("SELECT a / NULLIF(b, 0)::VARCHAR FROM t", ["NULLIF(b, 0)::VARCHAR"]),
    ("SELECT a / NULLIF(b, 0)::DOUBLE, a / NULLIF(b, 0)::REAL, a / (NULLIF(b, 0))::FLOAT FROM t", []),
    ("SELECT a / GREATEST(d, 0.6)::INT, a / GREATEST(d, 0.006)::NUMBER(10, 2), a / 1.4::INT, a / 0.6::INT FROM t", []),
    ("SELECT a / GREATEST(d, 0.004)::NUMBER(10, 2), a / GREATEST(d, 0.4::INT), a / (0.4)::INT, a / (0.4::INT) FROM t",
     ["GREATEST(d, 0.004)::NUMBER(10, 2)", "GREATEST(d, 0.4::INT)", "(0.4)::INT", "(0.4::INT)"]),
    ("SELECT a / 0.5::INT, a / GREATEST(d, 0.005)::NUMBER(10, 2) FROM t",        # a tie is not leaned on
     ["0.5::INT", "GREATEST(d, 0.005)::NUMBER(10, 2)"]),
    ("SELECT IFF(b > 0, a / b::INT, NULL), IFF(b::INT > 0, a / b::INT, NULL), IFF(b::FLOAT > 0, a / b, NULL) FROM t",
     ["b::INT"]),
    ("SELECT CASE WHEN b > 0 THEN a / b::NUMBER(10, 2) END FROM t", ["b::NUMBER(10, 2)"]),
    # a modulo divisor is held to the same rule
    ("SELECT MOD(a, b), a % b, MOD(a, 60), a % 24, a % (b - 1) FROM t", ["% b", "% b", "% (b - 1)"]),
    ("SELECT MOD(a, NULLIF(b, 0)), IFF(b > 0, a % b, 0), CASE WHEN b <> 0 THEN MOD(a, b) END FROM t", []),
    ("SELECT MOD(a, b::INT), MOD(a) FROM t", ["% b::INT", "% <unreadable: MOD without two arguments: '(a)'>"]),
    ("SELECT 'MOD(a, b)', 'SELECT ROUND(x % y) FROM t', 'SELECT x % y FROM t' FROM t",   # no call syntax: prose
     ["% b", "% y"]),
    ("SELECT CONTAINS(GET_DDL('PROCEDURE', 'P()'), 'a / NULLIF(b, 0)') OR x ILIKE '%SP_X(%' FROM t", []),
])
def test_the_rule(sql, bad):
    assert unguarded(sql) == bad


def test_mask_keeps_positions():
    sql = "SELECT 'it''s / a' AS s, \"x's / y\" AS q, a / b -- c / d\nFROM t /* e / f */"
    m = mask(sql)
    assert len(m) == len(sql) and m.count("/") == 1 and m.index("/") == sql.index("a / b") + 2
