"""Freshness coverage guard (Next-Fifty #10c, V152): every loaded table must carry a loader-owned stamp.

The app judges pipeline health ONLY through SOURCE_FRESHNESS_STATE (health strip, Brief stale count,
Control Room / Admin freshness boards, NATIVE_ALERT_STALE_FACTS). A FACT_/MART_ table a loader fills
but never stamps is invisible there: its loader can stall for weeks while every board reads green.
Before V152, MART_CLOUD_SVC_DAILY (the V150 COST_CLOUD_SVC_ANOMALY baseline) and MART_TASK_NODE_DAILY
were exactly that. These locks keep the class closed:

  (a) every FACT_/MART_ MERGE/INSERT target in the latest proc bodies is a quoted SOURCE_NAME in some
      latest-body ``MERGE INTO ...SOURCE_FRESHNESS_STATE ... ;`` block (allowlist UNSTAMPED_OK, empty);
  (b) SP_LOAD_MARTS_V27's token chain: per scope block, the ``loaded := loaded || '<tok> '`` tokens
      equal that block's srcmap tokens, each on its arm's success path, and every srcmap SOURCE_NAME is
      a ``SELECT '<name>'`` row of the latest MART_SOURCE_FRESHNESS view -- a token alone stamps
      nothing and a view row alone stamps nothing (the stamp JOINs the view to the srcmap);
  (c) name-rule safety: the shared cadence rule judges a SOURCE_NAME with DAILY/METERING at 30h and
      anything else at 3h, so every stamped name WITHOUT those words must really load hourly.
"""

from __future__ import annotations

import re

from tests.test_alert_rule_consistency import _latest_proc_bodies, _mig_files

# Loader targets that may legitimately go unstamped, with the reason. Empty since V152; an entry must
# name a table that is REALLY unstamped (stale-entry check below).
UNSTAMPED_OK: dict[str, str] = {}

# Stamped sources the shared name rule judges at 3h (no DAILY/METERING in the name). Each one must be
# loaded by the hourly graph (TASK_LOAD_HOURLY and its children). Wave 2b adds ALERT_SCAN_HOURLY.
HOURLY_NAMED = frozenset({
    "OW_QH_EXTRACT", "FACT_QUERY_HOURLY",                      # SP_LOAD_QH_EXTRACT
    "FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY",       # SP_LOAD_MARTS_V27('HOURLY')
    "MART_INCIDENT_TIMELINE",                                   # SP_LOAD_MARTS_V27('HOURLY')
    "MART_OPS_DIAG_HOURLY",                                     # SP_LOAD_OPS_DIAG
    "MART_EXEC_BOARD",                                          # SP_REFRESH_EXEC_BOARD
    "FACT_SECURITY_CHANGE", "SECURITY_TRUST_SNAPSHOT",          # SP_LOAD_SECURITY_FACTS
})

_NAME_RE = re.compile(r"^(FACT|MART|OW|SECURITY|ALERT)_[A-Z0-9_]+$")   # drops 'OK'/'ERROR' STATUS literals
_TARGET_RE = re.compile(
    r"(?:MERGE\s+INTO|INSERT\s+(?:OVERWRITE\s+)?INTO)\s+DBA_MAINT_DB\.OVERWATCH\.((?:FACT|MART)_\w+)")
_STAMP_RE = re.compile(r"MERGE\s+INTO\s+DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE\b.*?;", re.S)
_TOKEN_RE = re.compile(r"loaded := loaded \|\| '(\w+) ';")
_SRCMAP_RE = re.compile(r"FROM VALUES\s*\n(.*?)AS srcmap\(SOURCE_NAME, TOKEN\)", re.S)
_PAIR_RE = re.compile(r"\('(\w+)', '(\w+)'\)")
_DAILY_SPLIT = "    IF (UPPER(:SCOPE) = 'DAILY') THEN\n"


def _stamped() -> dict[str, set[str]]:
    """{SOURCE_NAME: {procs whose latest body stamps it}}."""
    out: dict[str, set[str]] = {}
    for proc, body in _latest_proc_bodies().items():
        for block in _STAMP_RE.findall(body):
            for lit in re.findall(r"'([A-Z][A-Z0-9_]*)'", block):
                if _NAME_RE.match(lit):
                    out.setdefault(lit, set()).add(proc)
    return out


def _targets() -> dict[str, set[str]]:
    """{FACT_/MART_ table: {procs whose latest body MERGEs / INSERTs into it}}."""
    out: dict[str, set[str]] = {}
    for proc, body in _latest_proc_bodies().items():
        for t in _TARGET_RE.findall(body):
            out.setdefault(t, set()).add(proc)
    return out


def _latest_view() -> str:
    pat = re.compile(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.MART_SOURCE_FRESHNESS AS\n.*?;\n", re.S)
    latest = ""
    for f in _mig_files():
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            latest = m.group(0)
    assert latest, "no MART_SOURCE_FRESHNESS definition found"
    return latest


def _scope_blocks() -> dict[str, str]:
    body = _latest_proc_bodies()["SP_LOAD_MARTS_V27"]
    hourly, daily = body.split(_DAILY_SPLIT, 1)
    assert "    IF (UPPER(:SCOPE) = 'HOURLY') THEN\n" in hourly
    return {"HOURLY": hourly, "DAILY": daily}


# (a) -----------------------------------------------------------------------------------------------
def test_every_loaded_fact_and_mart_carries_a_loader_owned_freshness_stamp():
    stamped = _stamped()
    missing = {t: sorted(p) for t, p in _targets().items() if t not in stamped and t not in UNSTAMPED_OK}
    assert not missing, (
        "a loader fills these FACT_/MART_ tables but nothing stamps SOURCE_FRESHNESS_STATE for them, so a "
        "stalled load is invisible to every freshness board and to NATIVE_ALERT_STALE_FACTS. Add the name "
        "to the loader's freshness MERGE (and, for a token-gated SP_LOAD_MARTS_V27 arm, BOTH a srcmap row "
        f"and a MART_SOURCE_FRESHNESS view row): {missing}")


def test_unstamped_allowlist_is_not_stale():
    stamped, targets = _stamped(), _targets()
    for name in UNSTAMPED_OK:
        assert name in targets, f"UNSTAMPED_OK[{name!r}] names no loader target -- drop it"
        assert name not in stamped, f"UNSTAMPED_OK[{name!r}] is stamped now -- drop it"


def test_v152_closed_the_two_known_gaps():
    stamped = _stamped()
    assert stamped.get("MART_CLOUD_SVC_DAILY") == {"SP_LOAD_QH_EXTRACT"}
    assert stamped.get("MART_TASK_NODE_DAILY") == {"SP_LOAD_MARTS_V27"}


def test_name_filter_drops_status_literals():
    """SP_LOAD_SECURITY_FACTS' stamp carries 'OK' / 'ERROR' STATUS literals -- never source names."""
    stamped = _stamped()
    assert "OK" not in stamped and "ERROR" not in stamped
    assert "SECURITY_TRUST_SNAPSHOT" in stamped and "FACT_SECURITY_CHANGE" in stamped


# (b) -----------------------------------------------------------------------------------------------
def test_marts_loader_token_chain_matches_its_srcmaps_per_scope():
    view_names = set(re.findall(r"SELECT '(\w+)'", _latest_view()))
    for scope, block in _scope_blocks().items():
        maps = _SRCMAP_RE.findall(block)
        assert len(maps) == 1, f"{scope}: expected one freshness srcmap, got {len(maps)}"
        pairs = _PAIR_RE.findall(maps[0])
        map_tokens = {tok for _, tok in pairs}
        tokens = _TOKEN_RE.findall(block)
        assert len(tokens) == len(set(tokens)), f"{scope}: a token is appended twice"
        assert set(tokens) == map_tokens, (
            f"{scope}: loaded-token set != srcmap token set -- unmapped {sorted(set(tokens) - map_tokens)}, "
            f"never-loaded {sorted(map_tokens - set(tokens))}. A token alone stamps nothing.")
        unviewed = sorted({name for name, _ in pairs} - view_names)
        assert not unviewed, (f"{scope}: srcmap names with no MART_SOURCE_FRESHNESS row never stamp "
                              f"(the stamp JOINs the view): {unviewed}")


def _mask(sql: str) -> str:
    """Same-length copy with -- comments and '...' literals blanked, so block keywords in prose or
    strings never count and positions still line up with the original text."""
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql.startswith("''", j):
                    j += 2
                    continue
                if sql[j] == "'":
                    j += 1
                    break
                j += 1
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


# BEGIN (never BEGIN TRANSACTION) opens a block, EXCEPTION (never a DECLARE'd `x EXCEPTION (...)`) switches
# it to its handler, END; closes it (END IF; / END LOOP; do not match).
_BLOCK_KW_RE = re.compile(r"\bBEGIN\b(?!\s+TRANSACTION)|\bEXCEPTION\b(?!\s*\()|\bEND\s*;")


def _in_handler_at(body: str) -> list[tuple[int, bool]]:
    """[(position, inside-any-EXCEPTION-handler)] at every block keyword of a Snowflake Scripting body."""
    stack: list[str] = []
    marks: list[tuple[int, bool]] = []
    for m in _BLOCK_KW_RE.finditer(_mask(body)):
        kw = m.group(0)
        if kw == "BEGIN":
            stack.append("body")
        elif kw == "EXCEPTION":
            assert stack, f"EXCEPTION outside any block at {m.start()}"
            stack[-1] = "handler"
        else:
            assert stack, f"unbalanced END; at {m.start()}"
            stack.pop()
        marks.append((m.end(), "handler" in stack))
    assert not stack, "block scanner drifted: BEGIN/END; do not balance over the proc body"
    return marks


def _block_spans(body: str) -> list[tuple[int, int, int, int]]:
    """[(body_start, body_end, full_end, depth)] for every BEGIN...END; block (depth 0 = the proc's outer
    block). body_end is where the block's EXCEPTION handler starts (or its END; when it has none)."""
    stack: list[list[int]] = []
    spans: list[tuple[int, int, int, int]] = []
    for m in _BLOCK_KW_RE.finditer(_mask(body)):
        kw = m.group(0)
        if kw == "BEGIN":
            stack.append([m.end(), -1, len(stack)])
        elif kw == "EXCEPTION":
            assert stack, f"EXCEPTION outside any block at {m.start()}"
            stack[-1][1] = m.start()
        else:
            assert stack, f"unbalanced END; at {m.start()}"
            start, exc, depth = stack.pop()
            spans.append((start, exc if exc >= 0 else m.start(), m.start(), depth))
    assert not stack, "block scanner drifted: BEGIN/END; do not balance over the proc body"
    return spans


def _token_violations(body: str, pairs: list[tuple[str, str]]) -> list[str]:
    """Each ``loaded := loaded || '<tok> '`` must sit in a NESTED arm block's body (before its handler, not in
    the proc's outer block - there it would advance freshness even when the arm's load failed, the V066 #11
    class), and that arm must itself MERGE/INSERT into every table the srcmap maps the token to (a token
    appended by the wrong arm stamps the wrong source fresh)."""
    names_by_token: dict[str, set[str]] = {}
    for name, tok in pairs:
        names_by_token.setdefault(tok, set()).add(name)
    spans = _block_spans(body)
    problems: list[str] = []
    for m in _TOKEN_RE.finditer(body):
        pos, tok = m.start(), m.group(1)
        enclosing = [s for s in spans if s[0] <= pos < s[2]]
        if not enclosing:
            problems.append(f"{tok}: outside every block")
            continue
        start, body_end, _full, depth = max(enclosing, key=lambda s: s[0])      # innermost block
        if depth < 1:
            problems.append(f"{tok}: appended in the proc's outer block, not inside its arm")
            continue
        if pos >= body_end:
            problems.append(f"{tok}: appended inside an EXCEPTION handler")
            continue
        arm = body[start:body_end]
        for name in sorted(names_by_token.get(tok, ())):
            loads = rf"(?:MERGE\s+INTO|INSERT\s+(?:OVERWRITE\s+)?INTO)\s+DBA_MAINT_DB\.OVERWATCH\.{name}\b"
            if not re.search(loads, arm):
                problems.append(f"{tok}: its arm never loads {name}, the table the srcmap maps it to")
    return problems


def test_every_token_is_appended_on_its_arms_success_path():
    body = _latest_proc_bodies()["SP_LOAD_MARTS_V27"]
    split = body.index(_DAILY_SPLIT)
    stamps = [m.start() for m in re.finditer(r"MERGE INTO DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE t", body)]
    assert len(stamps) == 2 and stamps[0] < split < stamps[1]
    pairs = [p for block in _scope_blocks().values() for p in _PAIR_RE.findall(_SRCMAP_RE.findall(block)[0])]
    problems = _token_violations(body, pairs)
    assert not problems, problems
    for m in _TOKEN_RE.finditer(body):
        stamp = stamps[0] if m.start() < split else stamps[1]
        assert m.start() < stamp, f"token {m.group(1)!r} is appended after its scope's freshness stamp"


def test_token_guard_catches_the_v066_class_and_a_wrong_mapping():
    """Review fix (wave 2a): a token moved OUT of its arm (appended even when the arm's MERGE failed) and a
    token appended by an arm that loads a different table must both be caught."""
    arm = ("BEGIN\n  BEGIN\n    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_A t USING x ON 1=1;\n{inside}"
           "  EXCEPTION\n    WHEN OTHER THEN\n      NULL;\n  END;\n{outside}END;\n")
    ok = arm.format(inside="    loaded := loaded || 'a ';\n", outside="")
    moved_out = arm.format(inside="", outside="  loaded := loaded || 'a ';\n")
    assert _token_violations(ok, [("MART_A", "a")]) == []
    assert any("outer block" in p for p in _token_violations(moved_out, [("MART_A", "a")]))
    assert any("never loads MART_B" in p for p in _token_violations(ok, [("MART_B", "a")]))


def test_block_scanner_sees_handlers():
    """Guard the guard: the scanner must flag a token appended in a handler and clear a nested one."""
    bad = ("BEGIN\n  BEGIN\n    x := 1;\n  EXCEPTION\n    WHEN OTHER THEN\n"
           "      loaded := loaded || 'oops ';\n  END;\nEND;\n")
    good = ("DECLARE\n  e EXCEPTION (-20001, 'x');\nBEGIN\n  BEGIN\n    BEGIN TRANSACTION;\n    BEGIN\n"
            "      y := 1;   -- END; in a comment is ignored\n    EXCEPTION WHEN OTHER THEN z := 'END;';\n"
            "    END;\n    COMMIT;\n    loaded := loaded || 'fine ';\n  EXCEPTION\n    WHEN OTHER THEN\n"
            "      ROLLBACK;\n  END;\nEND;\n")
    for text, want in ((bad, True), (good, False)):
        pos = _TOKEN_RE.search(text).start()
        assert [h for p, h in _in_handler_at(text) if p <= pos][-1] is want


def test_token_chain_counts():
    blocks = _scope_blocks()
    hourly, daily = (set(_TOKEN_RE.findall(blocks[s])) for s in ("HOURLY", "DAILY"))
    assert "task_node" in hourly and len(hourly) == 10
    assert daily == {"posture", "ai_code", "ai_functions"}


# (c) -----------------------------------------------------------------------------------------------
def test_sources_judged_hourly_by_the_name_rule_really_load_hourly():
    judged_3h = {n for n in _stamped() if "DAILY" not in n and "METERING" not in n}
    assert judged_3h == HOURLY_NAMED, (
        "a source judged at 3h by the shared name rule must actually load hourly; name it *_DAILY if it "
        f"loads daily. New: {sorted(judged_3h - HOURLY_NAMED)}; gone: {sorted(HOURLY_NAMED - judged_3h)}")
