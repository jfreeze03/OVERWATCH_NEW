"""Turning a rule off must stop its scan (R1-227, V172).

V150's SP_SCAN_CLOUD_SVC_ANOMALY read ``SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr ... AND ENABLED`` and
returned early ``IF (:zthr IS NULL)`` -- which the COALESCE made unreachable: a disabled (or deleted) rule still
scanned, at 3.5 instead of the tuned threshold, and SP_NOTIFY_WEBHOOK delivered what it booked. Disabling a rule
is the operator's main noise control, so two locks over every proc's LATEST definer:

  1. every SP_SCAN_* gates on its ENABLED rule: an ENABLED count read INTO a variable that a ``= 0`` test RETURNs
     on, or (for a scan with no early return) every ALERT_EVENTS insert joins ALERT_CONFIG / cfg with ENABLED;
  2. no proc reads ``COALESCE(<agg>(..), <literal>) INTO :x`` (multi-target INTO included) and later tests
     ``IF (:x IS NULL)`` -- the dead-guard shape itself.
"""

from __future__ import annotations

import re

from tests.test_alert_rule_consistency import _latest_proc_bodies, _mig_files
from tests.test_migration_proc_syntax import _strip_noise


def _code(body: str) -> str:
    """Comments out, string literals KEPT (the RULE_ID literals are what tie a read to its rule)."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body.startswith("--", i):
            j = body.find("\n", i)
            i = n if j < 0 else j
        elif body[i] == "'":
            j = i + 1
            while j < n:
                if body[j] == "'":
                    if body[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(body[i:j])
            i = j
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _split_top(items: str) -> list[str]:
    """Split a SELECT list on top-level commas (parentheses and strings respected)."""
    out, depth, buf, in_str = [], 0, [], False
    for ch in items:
        if in_str:
            buf.append(ch)
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf).strip())
    return out


_SELECT_INTO_RE = re.compile(r"\bSELECT\s+(?P<items>.*?)\s+INTO\s+(?P<targets>:?\w+(?:\s*,\s*:?\w+)*)\s",
                             re.S | re.I)


def _assignments(body: str) -> list[tuple[str, str, str]]:
    """[(select item, target variable, the statement text)] for every scripting SELECT .. INTO, paired by
    position (the multi-target ``INTO :a, :b`` form included)."""
    code = _code(body)
    found: list[tuple[str, str, str]] = []
    for m in _SELECT_INTO_RE.finditer(code):
        items = _split_top(m["items"])
        targets = [t.strip().lstrip(":") for t in m["targets"].split(",")]
        if len(items) != len(targets):
            continue                                   # an INSERT .. SELECT, not a scripting assignment
        stmt = code[m.start():code.find(";", m.end())]
        found += [(item, tgt, stmt) for item, tgt in zip(items, targets, strict=True)]
    return found


def _count_gate_vars(body: str) -> set[str]:
    """Variables assigned an ENABLED-rule count: a COUNT(..) item of a SELECT .. INTO that reads ALERT_CONFIG and
    filters ENABLED (directly or via a c.ENABLED join)."""
    return {tgt for item, tgt, stmt in _assignments(body)
            if re.match(r"COUNT\s*\(", item, re.I) and "ALERT_CONFIG" in stmt and re.search(r"\bENABLED\b", stmt)}


def _returns_on_zero(body: str, var: str) -> bool:
    return bool(re.search(rf"IF\s*\(\s*:?{var}\s*=\s*0\s*\)\s*THEN\s+RETURN\b", _code(body), re.I))


def _inserts_join_enabled_config(body: str) -> bool:
    inserts = re.findall(r"INSERT INTO DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS.*?;", _code(body), re.S)
    return bool(inserts) and all(re.search(r"ALERT_CONFIG|\bcfg\b", s) and re.search(r"\bENABLED\b", s)
                                 for s in inserts)


def _gated(body: str) -> bool:
    return any(_returns_on_zero(body, v) for v in _count_gate_vars(body)) or _inserts_join_enabled_config(body)


def _dead_guards(body: str) -> list[str]:
    """Variables assigned COALESCE(<agg>(..), <literal>) and later tested IS NULL: a guard that can never fire."""
    code = _code(body)
    dead = []
    for item, tgt, stmt in _assignments(body):
        if re.match(r"COALESCE\s*\(\s*(?:MAX|MIN|SUM|AVG|ANY_VALUE)\s*\(.*\)\s*,\s*(?:'[^']*'|-?[0-9.]+)\s*\)$",
                    item, re.I | re.S):
            after = code[code.index(stmt) + len(stmt):]
            if re.search(rf"IF\s*\(\s*:?{tgt}\s+IS\s+NULL\s*\)", after, re.I):
                dead.append(tgt)
    return dead


def _scans() -> dict[str, str]:
    return {n: b for n, b in _latest_proc_bodies().items() if n.startswith("SP_SCAN_")}


def test_every_scan_gates_on_its_enabled_rule():
    scans = _scans()
    assert len(scans) >= 6, sorted(scans)
    ungated = sorted(n for n, b in scans.items() if not _gated(b))
    assert not ungated, f"a disabled rule must stop these scans (ENABLED count + '= 0 THEN RETURN'): {ungated}"


def test_no_proc_tests_a_coalesced_read_for_null():
    dead = {n: d for n, b in _latest_proc_bodies().items() if (d := _dead_guards(b))}
    assert not dead, f"COALESCE(..) INTO :x then IF (:x IS NULL) can never fire: {dead}"


def test_the_gate_checks_have_teeth():
    """V150's body (before V172) fails both locks; V172's passes; the multi-target INTO pairs by position."""
    v150 = next(p for p in _mig_files() if p.name.startswith("V150__")).read_text(encoding="utf-8")
    m = re.search(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_SCAN_CLOUD_SVC_ANOMALY\(.*?\n\$\$;", v150,
                  re.S)
    old = m.group(0)
    assert not _gated(old) and _dead_guards(old) == ["zthr"]
    new = _scans()["SP_SCAN_CLOUD_SVC_ANOMALY"]
    assert _gated(new) and not _dead_guards(new) and _count_gate_vars(new) == {"enabled_cnt"}
    pairs = {(item.split("(")[0], tgt) for item, tgt, _s in _assignments(new)}
    assert {("COUNT", "enabled_cnt"), ("COALESCE", "zthr")} <= pairs
    # the other scans pass on the count gate itself, not by accident
    for name in ("SP_SCAN_SCHEMA_DRIFT", "SP_SCAN_RECON_ERRORS", "SP_SCAN_REF_GAPS", "SP_SCAN_ETL_CYCLE",
                 "SP_SCAN_SLEEP_POLLING"):
        body = _scans()[name]
        assert any(_returns_on_zero(body, v) for v in _count_gate_vars(body)), name
    assert "ENABLED" in _strip_noise(new)
