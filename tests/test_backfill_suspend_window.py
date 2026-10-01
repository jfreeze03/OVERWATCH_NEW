"""backfill_365.sql can never strand the hourly task graph on an error (R1-231).

The backfill SUSPENDs TASK_LOAD_HOURLY (B12: so the minute-7 extract trim cannot shrink the 90d fill
mid-run) and RESUMEs it only at the end. Snowsight's Run All halts at the first failing statement, and
six bare CALLs sat inside that window -- SP_LOAD_OPS_DIAG and SP_LOAD_PLATFORM_SCORE have no EXCEPTION
handler at all -- so one error left every hourly load, the alert scan and Teams delivery suspended (the
V041 stranding class). Now every statement inside the window is a guarded single-CALL block whose
handler logs and RETURNs, the last pane counts this run's failures, and a loud note covers what no
handler can catch (a statement timeout or Stop). The rebuild copy (04_backfill_365.sql) is byte-locked
to this file by test_rebuild_bundle.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BF = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
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


def test_recovery_note_and_last_pane_counts_this_runs_failures():
    stmts = _statements(_BF)
    sus, _res = _window(stmts)
    note = _BF[:_BF.index("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND")]
    assert "IF THIS WORKSHEET STOPS BEFORE THE END" in note
    assert "loader_chain_check.sql step 0" in note and "LAST TWO statements" in note
    assert stmts[sus - 1] == "SET backfill_started = CURRENT_TIMESTAMP()"
    last = stmts[-1]
    assert last.startswith("SELECT COUNT(*) AS BACKFILL_CALLS_FAILED")
    assert "PAGE = 'Backfill365'" in last and "LOGGED_AT >= $backfill_started::TIMESTAMP_NTZ" in last


def test_the_old_shape_is_what_this_lock_rejects():
    """Teeth: the pre-fix window (bare CALLs) is flagged as halting."""
    old = ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;\n"
           "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(90);\n"
           "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;\n")
    stmts = _statements(old)
    sus, res = _window(stmts)
    assert [s for s in stmts[sus + 1:res] if not _guarded(s)] == ["CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(90)"]
