"""backfill_365.sql can never strand the hourly task graph on an error (R1-231).

The backfill SUSPENDs TASK_LOAD_HOURLY (B12: so the minute-7 extract trim cannot shrink the 90d fill
mid-run) and RESUMEs it only at the end. Snowsight's Run All halts at the first failing statement, and
six bare CALLs sat inside that window -- SP_LOAD_OPS_DIAG and SP_LOAD_PLATFORM_SCORE have no EXCEPTION
handler at all -- so one error left every hourly load, the alert scan and Teams delivery suspended (the
V041 stranding class). Now every statement inside the window is a guarded single-CALL block whose
handler logs and RETURNs, the last pane counts this run's failures, and a loud note covers what no
handler can catch (a statement timeout or Stop). The rebuild copy (04_backfill_365.sql) is byte-locked
to this file by test_rebuild_bundle.

Review follow-up: the first guarded version RETURNed a constant 'ok: <call>', dropping each loader's own
verdict. The loaders mostly log and RETURN rather than RAISE -- SP_LOAD_MARTS_V27 says 'MARTS WITH
ERRORS: ...' (V066 #10), SP_LOAD_QH_EXTRACT says '(extract committed: false)' -- so a failed backfill
read 'ok' and a 0 failure count. Each block now reads the CALL's return value (RESULT_SCAN(LAST_QUERY_ID()),
the V064 reconcile idiom), turns a failure verdict into a FAILED row, and the last pane also counts the
arm failures the loaders log and swallow. The recovery note names the RESUME pair by what it is ("just
above the final verify SELECT") instead of "the LAST TWO statements", which stopped being true when the
verify SELECT was appended.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BF = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
_MIGRATIONS = sorted((_ROOT / "snowflake" / "migrations").glob("V[0-9]*.sql"))
_VERIFY = "SELECT COUNT_IF(PAGE = 'Backfill365') AS BACKFILL_CALLS_FAILED"
_LOADS = ("SP_LOAD_QH_EXTRACT(90)", "SP_LOAD_MARTS_V27('HOURLY', 90)", "SP_LOAD_OPS_DIAG(90)",
          "SP_LOAD_MARTS_V27('DAILY', 365)", "SP_LOAD_PLATFORM_SCORE(120)", "SP_LOAD_SECURITY_FACTS(90)")


def _statements(text: str) -> list[str]:
    """Top-level statements as Run All executes them: ';' ends one except inside a 'string', a -- comment
    or a $$ block; comment-only fragments dropped, leading comment lines stripped."""
    out, buf, i, n = [], [], 0, len(text)
    in_str = in_cmt = in_dollar = False
    while i < n:
        if in_dollar or (not in_str and not in_cmt and text.startswith("$$", i)):
            if text.startswith("$$", i):
                in_dollar = not in_dollar
                buf.append("$$")
                i += 2
                continue
        elif in_cmt:
            in_cmt = text[i] != "\n"
        elif in_str:
            in_str = text[i] != "'"
        elif text.startswith("--", i):
            in_cmt = True
        elif text[i] == "'":
            in_str = True
        elif text[i] == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(text[i])
        i += 1
    out.append("".join(buf))
    live = []
    for stmt in out:
        lines = [ln for ln in stmt.splitlines() if ln.strip()]
        while lines and lines[0].lstrip().startswith("--"):
            lines.pop(0)
        if lines:
            live.append("\n".join(lines).strip())
    return live


def _guarded(stmt: str) -> bool:
    """One CALL inside an anonymous block whose WHEN OTHER handler logs and RETURNs (never re-raises)."""
    if not (stmt.startswith("EXECUTE IMMEDIATE $$") and stmt.endswith("$$")):
        return False
    body, _, handler = stmt.partition("\nEXCEPTION\n")
    return (len(re.findall(r"^\s*CALL DBA_MAINT_DB\.OVERWATCH\.", body, re.M)) == 1
            and "WHEN OTHER THEN" in handler and "RETURN 'FAILED: " in handler and "RAISE" not in handler
            and "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG" in handler and "'Backfill365'" in handler)


def _window(stmts: list[str]) -> tuple[int, int]:
    sus = [i for i, s in enumerate(stmts) if re.fullmatch(r"ALTER TASK IF EXISTS DBA_MAINT_DB\.OVERWATCH\."
                                                           r"TASK_LOAD_HOURLY SUSPEND", s)]
    res = [i for i, s in enumerate(stmts) if re.fullmatch(r"ALTER TASK IF EXISTS DBA_MAINT_DB\.OVERWATCH\."
                                                           r"TASK_LOAD_HOURLY RESUME", s)]
    assert len(sus) == 1 and len(res) == 1 and sus[0] < res[0]
    return sus[0], res[0]


def test_no_statement_in_the_suspend_window_can_halt_run_all_on_an_error():
    stmts = _statements(_BF)
    sus, res = _window(stmts)
    window = stmts[sus + 1:res]
    halting = [s.splitlines()[0][:80] for s in window if not _guarded(s)]
    assert not halting, f"an error here strands TASK_LOAD_HOURLY suspended: {halting}"
    calls = [re.search(r"CALL DBA_MAINT_DB\.OVERWATCH\.([^;\n]+);", s).group(1) for s in window]
    assert calls == list(_LOADS)                      # the extract fills first (V041); nothing dropped
    assert stmts[res + 1] == "SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY')"


def test_no_bare_call_anywhere_and_each_block_logs_its_own_call():
    stmts = _statements(_BF)
    assert not [s for s in stmts if s.upper().startswith("CALL ")]
    for s in stmts:
        if s.startswith("EXECUTE IMMEDIATE $$"):
            call = re.search(r"CALL DBA_MAINT_DB\.OVERWATCH\.([^;\n]+);", s).group(1)
            assert f"'{call.replace(chr(39), chr(39) * 2)}', CURRENT_ROLE()" in s, call


def test_recovery_note_names_the_resume_pair_and_the_last_pane_counts_this_runs_failures():
    stmts = _statements(_BF)
    sus, res = _window(stmts)
    note = _BF[:_BF.index("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND")]
    flat = " ".join(note.replace("--", " ").split())
    assert "IF THIS WORKSHEET STOPS BEFORE THE END" in note
    assert "loader_chain_check.sql step 0" in flat
    # The note names the RESUME pair by WHAT it is and WHERE it sits -- and the file agrees: the pair is
    # exactly the two statements before the final verify SELECT. "The LAST TWO statements" was wrong once
    # the verify SELECT was appended (selecting them skips the RESUME and errors on $backfill_started).
    assert ("run the ALTER TASK ... RESUME and SYSTEM$TASK_DEPENDENTS_ENABLE statements just above this "
            "file's final verify SELECT") in flat
    assert "last two statements" not in flat.lower()
    assert res == len(stmts) - 3 and stmts[-1].startswith(_VERIFY)
    assert stmts[sus - 1] == "SET backfill_started = CURRENT_TIMESTAMP()"
    last = stmts[-1]
    assert "PAGE = 'Backfill365'" in last and "LOGGED_AT >= $backfill_started::TIMESTAMP_NTZ" in last
    doc = " ".join((_ROOT / "docs" / "FULL_REBUILD.md").read_text(encoding="utf-8").split())
    assert "last two statements" not in doc.lower()
    assert ("run the `ALTER TASK ... RESUME` and `SYSTEM$TASK_DEPENDENTS_ENABLE` statements just above the "
            "file's final verify SELECT") in doc
    assert "`BACKFILL_CALLS_FAILED` and `LOADER_ARMS_FAILED`" in doc


# ---------------------------------------------------------------------------
# The loader's own verdict survives the guard (review follow-up).
# ---------------------------------------------------------------------------

def _latest_proc(name: str) -> str:
    """The CURRENT definition: the body from the last migration that CREATE OR REPLACEs the proc."""
    pat = re.compile(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\." + name + r"\(")
    for mig in reversed(_MIGRATIONS):
        text = mig.read_text(encoding="utf-8")
        hit = pat.search(text)
        if hit:
            return text[hit.start():text.index("\n$$;", hit.start())]
    raise AssertionError(f"no migration defines {name}")


def _ilike(value: str, pattern: str) -> bool:
    """Snowflake ILIKE: % = any run, _ = one char, case-insensitive, whole-string."""
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value, re.I | re.S) is not None


def _blocks() -> list[str]:
    return [s for s in _statements(_BF) if s.startswith("EXECUTE IMMEDIATE $$")]


def _failure_verdict(rv: str | None) -> bool:
    """Evaluate the blocks' own verdict predicate (identical in every block) against a return value."""
    preds = set()
    for b in _blocks():
        m = re.search(r"^\s*IF \((rv IS NULL(?: OR rv ILIKE '[^']*')+)\) THEN$", b, re.M)
        assert m, b.splitlines()[5]
        preds.add(m.group(1))
    assert len(preds) == 1, preds
    patterns = re.findall(r"rv ILIKE '([^']*)'", preds.pop())
    return rv is None or any(_ilike(rv, p) for p in patterns)


def test_each_block_keeps_the_loaders_own_verdict():
    blocks = _blocks()
    assert len(blocks) == len(_LOADS)
    for block, call in zip(blocks, _LOADS, strict=True):
        q = call.replace("'", "''")
        lines = [ln.strip() for ln in block.split("\nEXCEPTION\n", 1)[0].splitlines()]
        at = lines.index(f"CALL DBA_MAINT_DB.OVERWATCH.{call};")
        # the very next statement reads THIS call's return value (nothing may run in between)
        assert lines[at + 1] == "SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));", call
        # the pane shows the verdict on success; a failure verdict becomes a logged FAILED row
        assert f"RETURN 'ok: {q} -> ' || rv;" in lines, call
        assert not [ln for ln in lines if re.fullmatch(r"RETURN 'ok: [^']*(''[^']*)*';", ln)], call
        branch = block[block.index("    IF (rv IS NULL"):block.index("    END IF;")]
        assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG" in branch and "'Backfill365'" in branch
        assert f"'{q}', CURRENT_ROLE();" in branch and f"RETURN 'FAILED: {q} -> '" in branch


def test_the_verdict_predicate_matches_the_loaders_real_return_strings():
    # Lock the verdict strings to the CURRENT definers, so a migration that rewords one fails here first.
    marts = _latest_proc("SP_LOAD_MARTS_V27")
    assert "RETURN 'MARTS WITH ERRORS: ' || :req_fail || ' required, ' || :opt_fail || ' optional ('" in marts
    assert "RETURN 'MARTS OK (' || :SCOPE || ', ' || :d || 'd): ' || :loaded" in marts
    extract = _latest_proc("SP_LOAD_QH_EXTRACT")
    assert "RETURN 'qh extract + query facts loaded (extract committed: ' || :ok || ')';" in extract
    # failure verdicts (Snowflake renders a BOOLEAN as 'false' in a || concat)
    assert _failure_verdict("MARTS WITH ERRORS: 2 required, 1 optional (HOURLY, 90d): MART_A,MART_B")
    assert _failure_verdict("qh extract + query facts loaded (extract committed: false)")
    assert _failure_verdict(None)
    # success verdicts -- an OPTIONAL arm failure is counted by LOADER_ARMS_FAILED, not as a failed CALL
    assert not _failure_verdict("MARTS OK (DAILY, 365d): MART_A,MART_B[1 optional failed]")
    assert not _failure_verdict("qh extract + query facts loaded (extract committed: true)")
    for name in ("SP_LOAD_OPS_DIAG", "SP_LOAD_PLATFORM_SCORE", "SP_LOAD_SECURITY_FACTS"):
        literals = re.findall(r"^\s*RETURN '([^']*)'", _latest_proc(name), re.M)
        assert literals, name
        for lit in literals:
            assert not _failure_verdict(f"{lit}90d"), (name, lit)


def test_the_last_pane_counts_the_failures_the_loaders_swallow():
    last = _statements(_BF)[-1]
    assert last.startswith(_VERIFY)
    assert "COUNT_IF(PAGE <> 'Backfill365') AS LOADER_ARMS_FAILED" in last
    pages = re.search(r"PAGE IN \(([^)]*)\)", last)
    type_pat = re.search(r"ERROR_TYPE ILIKE '([^']*)'", last)
    assert pages and type_pat, "the last pane no longer counts loader-logged arm failures"
    counted_pages = set(re.findall(r"'(\w+)'", pages.group(1)))
    # every (PAGE, ERROR_TYPE) the backfilled loaders (and the procs they CALL) log: each *_failed one is
    # counted, a skipped / unavailable note is not
    logged: set[tuple[str, str]] = set()
    todo, seen = [c.split("(")[0] for c in _LOADS], set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        body = _latest_proc(name)
        logged |= set(re.findall(r"SELECT\s+'(\w+)',\s*'(\w+)'", body))
        todo += re.findall(r"CALL DBA_MAINT_DB\.OVERWATCH\.(\w+)\(", body)
    assert ("MartLoader", "mart_load_failed") in logged and ("ExtractLoader", "extract_load_failed") in logged
    for page, etype in sorted(logged):
        counted = page in counted_pages and _ilike(etype, type_pat.group(1))
        assert counted == etype.endswith("_failed"), (page, etype)


def test_the_old_shape_is_what_this_lock_rejects():
    """Teeth: the pre-fix window (bare CALLs) is flagged as halting."""
    old = ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;\n"
           "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(90);\n"
           "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;\n")
    stmts = _statements(old)
    sus, res = _window(stmts)
    assert [s for s in stmts[sus + 1:res] if not _guarded(s)] == ["CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(90)"]
