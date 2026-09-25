"""Proc lineage guard: a re-derived proc must be derived from its IMMEDIATELY PREVIOUS definer.

"Proc" here means any re-definable DBA_MAINT_DB.OVERWATCH object: PROCEDURE, VIEW or FUNCTION
(SECURE optional). Views and UDFs joined in wave 2a: V_SECURITY_EXCEPTION_QUEUE and
MART_SOURCE_FRESHNESS are re-derived exactly like procs, so a wrong-base view silently drops an
intervening version's predicate the same way (V088 re-derived the view "from V075" past V080).

Round 13's defect (fixed by V148): V123 re-derived SP_REFRESH_EXEC_BOARD "from V073" while the
live definer was V079, silently dropping V079's CoCo/CoWork AI-rate predicate and mispricing
Cortex Code on the exec board. Each migration's own byte-lock compares against the base the
AUTHOR named, so a wrong base passes its own test. This guard derives the true lineage from the
migration set itself (proc -> ordered defining versions) and checks every DECLARED base.

Claim sources, in precedence order (first non-empty wins, per proc per file):
  1. the structured marker   ``-- >>> derived:<PROC>  (from Vnnn; ...)`` or ``(Vnnn with ...)``
  2. proc-named prose        ``Re-derives <PROC> from Vnnn`` / ``re-derive <PROC> (from its CURRENT (Vnnn)``
  3. unnamed prose           ``re-derived from Vnnn`` -- only when the file re-defines exactly ONE
                             pre-existing proc (unambiguous). A chain ``V122->V132`` counts its LAST hop.
Escape hatch: a ``-- LINEAGE-WAIVER: <PROC> <reason>`` line in the migration (new files), or an
entry in _HISTORICAL_WAIVERS (applied files are never edited).
Fail closed ABOVE _FAIL_CLOSED_ABOVE: a V151+ file that re-defines an existing proc MUST carry a
parseable source-1 marker for it (or a waiver). <= V150 files without any claim are grandfathered.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"

_FAIL_CLOSED_ABOVE = 150   # the migration horizon when this guard landed (v4.588)

# (version, proc) -> why the declared base is allowed to differ. Applied migrations are never edited,
# so their waivers live here. Each entry must still be a REAL mismatch (stale-waiver check below).
_HISTORICAL_WAIVERS: dict[tuple[int, str], str] = {
    (123, "SP_REFRESH_EXEC_BOARD"): (
        "declared V073, true previous definer V079 -- the round-13 defect: dropped V079's CoCo/CoWork "
        "AI predicate; repaired forward by V148 (re-derived from V123 with the predicate restored)."),
    (88, "V_SECURITY_EXCEPTION_QUEUE"): (
        "declared V075, true previous definer V080 -- deliberate: V088 generalized V080's fixed 18-role "
        "list to TF_* (a supersede, nothing dropped)."),
}

# wave 2a: VIEW / FUNCTION (SECURE optional) are guarded like procs. A view opens "AS", a proc/UDF "(".
_PROC_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+(?:SECURE\s+)?(?:PROCEDURE|VIEW|FUNCTION)\s+DBA_MAINT_DB\.OVERWATCH\.(\w+)\s*(?:\(|AS\b)")
_MARK_RE = re.compile(r"^-- >>> derived:(\w+)(.*)$", re.M)
_MARK_BASE_RE = re.compile(r"\bfrom\s+V(\d{3})\b|^\s*\(\s*V(\d{3})\b")
_CHAIN = r"V(\d{3})((?:\s*->\s*V\d{3})*)"
_NAMED_RE = re.compile(
    r"re-?deriv(?:e|es|ed|ing)\s+(SP_\w+)\s*\(?\s*from\s+"
    r"(?:its\s+(?:CURRENT|LATEST)\s+(?:def(?:inition)?\s*)?\(?\s*)?" + _CHAIN, re.I)
_UNNAMED_RE = re.compile(
    r"re-?deriv\w*\b[^.;]{0,120}?\bfrom\s+(?:its\s+|the\s+)?(?:(?:CURRENT|LATEST)\s+)?"
    r"(?:def(?:inition)?s?\s+)?(?:\(\s*)?(?:the\s+)?" + _CHAIN, re.I | re.S)
_WAIVER_RE = re.compile(r"^-- LINEAGE-WAIVER:\s*(\w+)\s+\S", re.M)


def _version(path: Path) -> int:
    m = re.match(r"V(\d+)", path.name)
    assert m, path.name
    return int(m.group(1))


def _migrations() -> dict[int, str]:
    return {_version(p): p.read_text(encoding="utf-8") for p in _MIG.glob("V*.sql")}


def _last_hop(first: str, tail: str) -> int:
    hops = re.findall(r"V(\d{3})", tail or "")
    return int(hops[-1]) if hops else int(first)


def _definers(texts: dict[int, str]) -> dict[str, list[int]]:
    """{PROC: ascending versions whose file CREATE OR REPLACEs it}."""
    defs: dict[str, list[int]] = {}
    for v in sorted(texts):
        for name in dict.fromkeys(_PROC_RE.findall(texts[v])):
            defs.setdefault(name.upper(), []).append(v)
    return defs


def _lineage(texts: dict[int, str]) -> list[dict]:
    """One row per (file, re-defined proc): version, proc, true previous definer, claim source,
    declared bases, waived-in-file flag."""
    defs = _definers(texts)
    rows: list[dict] = []
    for v in sorted(texts):
        t = texts[v]
        names = [n.upper() for n in dict.fromkeys(_PROC_RE.findall(t))]
        prev = {n: max((x for x in defs[n] if x < v), default=None) for n in names}
        redefined = [n for n in names if prev[n] is not None]
        waived = {w.upper() for w in _WAIVER_RE.findall(t)}
        for n in redefined:
            claims: list[int] = []
            src = "marker"
            for m in _MARK_RE.finditer(t):
                if m.group(1).upper() == n:
                    b = _MARK_BASE_RE.search(m.group(2))
                    if b:
                        claims.append(int(b.group(1) or b.group(2)))
            if not claims:
                src = "named"
                claims = [_last_hop(m.group(2), m.group(3)) for m in _NAMED_RE.finditer(t)
                          if m.group(1).upper() == n]
            if not claims and len(redefined) == 1:
                src = "unnamed"
                claims = [_last_hop(m.group(1), m.group(2)) for m in _UNNAMED_RE.finditer(t)]
            if not claims:
                src = "none"
            rows.append({"v": v, "proc": n, "prev": prev[n], "src": src,
                         "claims": sorted(set(claims)), "waived": n in waived})
    return rows


def _violations(texts: dict[int, str], waivers: dict[tuple[int, str], str]) -> list[str]:
    out: list[str] = []
    for r in _lineage(texts):
        if r["waived"] or (r["v"], r["proc"]) in waivers:
            continue
        bad = [c for c in r["claims"] if c != r["prev"]]
        if bad:
            out.append(f"V{r['v']:03d} {r['proc']}: declares base V{bad[0]:03d} but the immediately "
                       f"previous definer is V{r['prev']:03d} ({r['src']} claim)")
        elif r["v"] > _FAIL_CLOSED_ABOVE and r["src"] != "marker":
            out.append(f"V{r['v']:03d} {r['proc']}: re-defines a proc (previous definer "
                       f"V{r['prev']:03d}) without a '-- >>> derived:{r['proc']}  (from Vnnn; ...)' "
                       "marker -- add one, or a '-- LINEAGE-WAIVER: <PROC> <reason>' line")
    return out


# ---------------------------------------------------------------------------
def test_every_declared_rederivation_base_is_the_previous_definer():
    v = _violations(_migrations(), _HISTORICAL_WAIVERS)
    assert not v, ("A migration re-derives a proc from a base that is NOT its immediately previous "
                   "definer -- the intervening version's changes are silently dropped (the V123 class). "
                   "Re-derive from the latest definition, or record a LINEAGE-WAIVER:\n  " + "\n  ".join(v))


def test_historical_waivers_are_real_and_not_stale():
    rows = {(r["v"], r["proc"]): r for r in _lineage(_migrations())}
    for key in _HISTORICAL_WAIVERS:
        assert key in rows, f"waiver {key} names no re-definition -- drop it"
        r = rows[key]
        assert any(c != r["prev"] for c in r["claims"]), (
            f"waiver {key} no longer covers a mismatch (declared {r['claims']}, prev {r['prev']}) -- drop it")


def test_lineage_parser_is_not_vacuous():
    """Guard the guard: the parser must see the house's re-derivations and their claims."""
    rows = _lineage(_migrations())
    claimed = [r for r in rows if r["claims"]]
    assert len(claimed) >= 60, f"only {len(claimed)} claimed re-derivations parsed -- regex drift?"
    by = {(r["v"], r["proc"]): r for r in rows}
    assert by[(148, "SP_REFRESH_EXEC_BOARD")]["claims"] == [123]
    assert by[(148, "SP_REFRESH_EXEC_BOARD")]["prev"] == 123
    assert by[(123, "SP_REFRESH_EXEC_BOARD")]["claims"] == [73]
    assert by[(123, "SP_REFRESH_EXEC_BOARD")]["prev"] == 79
    assert by[(149, "SP_LOAD_QH_EXTRACT")]["prev"] == 94
    assert by[(133, "SP_ANOMALY_SWEEP")]["claims"] == [132]      # chain "V122->V132": last hop
    assert by[(91, "SP_ALERT_SCAN")]["claims"] == [87]           # marker beats the carried V087 header prose
    # wave 2a: views and UDFs are tracked (V080's view marker is right; V088's is the waived supersede)
    assert by[(80, "V_SECURITY_EXCEPTION_QUEUE")]["claims"] == [75]
    assert by[(80, "V_SECURITY_EXCEPTION_QUEUE")]["prev"] == 75
    assert by[(88, "V_SECURITY_EXCEPTION_QUEUE")]["claims"] == [75]
    assert by[(88, "V_SECURITY_EXCEPTION_QUEUE")]["prev"] == 80
    assert by[(45, "MART_SOURCE_FRESHNESS")]["prev"] == 43
    assert by[(44, "COMPANY_FOR_USER")]["prev"] == 19


_BASE = ("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_X()\nRETURNS VARCHAR\n"
         "LANGUAGE SQL\nAS\n$$\nBEGIN RETURN 'x'; END;\n$$;\n")


def test_synthetic_wrong_base_is_caught():
    texts = {10: _BASE, 20: _BASE, 30: "-- Re-derives SP_X from V010 with one fix.\n" + _BASE}
    v = _violations(texts, {})
    assert v and "V030 SP_X" in v[0] and "V010" in v[0] and "V020" in v[0]


def test_synthetic_marker_beats_prose_and_waiver_escapes():
    ok = {10: _BASE, 20: "-- >>> derived:SP_X  (from V010; fix)\n" + _BASE}
    assert not _violations(ok, {})
    wrong = {10: _BASE, 20: _BASE, 30: "-- >>> derived:SP_X  (from V010; fix)\n" + _BASE}
    assert _violations(wrong, {})
    waived = {10: _BASE, 20: _BASE,
              30: "-- LINEAGE-WAIVER: SP_X V020 was reverted by hand\n-- >>> derived:SP_X  (from V010)\n" + _BASE}
    assert not _violations(waived, {})


def test_synthetic_views_and_functions_are_guarded():
    """wave 2a: VIEW, SECURE VIEW and FUNCTION re-derivations get the same base check and the same
    post-horizon fail-closed marker rule as procs."""
    shapes = {
        "V_X": "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.V_X AS\nSELECT 1 AS A;\n",
        "V_S": "CREATE OR REPLACE SECURE VIEW DBA_MAINT_DB.OVERWATCH.V_S\nAS SELECT 1 AS A;\n",
        "F_X": "CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.F_X(A VARCHAR)\nRETURNS VARCHAR\nAS 'A';\n",
    }
    hi = _FAIL_CLOSED_ABOVE + 1
    for name, body in shapes.items():
        assert _PROC_RE.findall(body) == [name]
        wrong = {10: body, 20: body, 30: f"-- >>> derived:{name}  (from V010; fix)\n" + body}
        v = _violations(wrong, {})
        assert v and f"V030 {name}" in v[0] and "V020" in v[0]
        assert not _violations({10: body, 20: body, 30: f"-- >>> derived:{name}  (from V020; fix)\n" + body}, {})
        assert _violations({10: body, hi: "-- touched\n" + body}, {}), f"{name}: post-horizon needs a marker"
    # the full (greedy) name is captured when AS follows it; a TABLE is never a tracked definer
    assert _PROC_RE.findall("CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.ASSET_V AS SELECT 1") == ["ASSET_V"]
    assert not _PROC_RE.findall("CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.T_X AS SELECT 1")


def test_synthetic_fail_closed_above_horizon():
    hi = _FAIL_CLOSED_ABOVE + 1
    bare = {10: _BASE, hi: "-- touched SP_X\n" + _BASE}
    assert _violations(bare, {}), "a post-horizon re-definition with no marker must fail closed"
    prose_only = {10: _BASE, hi: "-- Re-derives SP_X from V010.\n" + _BASE}
    assert _violations(prose_only, {}), "post-horizon files need the structured marker, prose is not enough"
    marked = {10: _BASE, hi: "-- >>> derived:SP_X  (from V010; fix)\n" + _BASE}
    assert not _violations(marked, {})
    grandfathered = {10: _BASE, _FAIL_CLOSED_ABOVE: "-- touched SP_X\n" + _BASE}
    assert not _violations(grandfathered, {})
