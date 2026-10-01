"""backfill_365.sql carries an OPT-IN, commented SP_LOAD_OBJECT_COST(365) block (V166 round, HEAL-OBJ-COST; R2-013).

FACT_OBJECT_COST_DAILY is first-filled with 14 days (V048..V139; teardown drops it, so a rebuild restarts it there)
and the daily task reloads 3, so the Cost > Storage & waste panel discloses how few of a long window's days it covers
(R2-013, v4.608). The year can be reloaded with one CALL, but it is heavy (ACCESS_HISTORY x QUERY_ATTRIBUTION_HISTORY)
and not older-days-only, so it ships COMMENTED, after the hourly-graph RESUME:
  * commented only -- no live statement mentions SP_LOAD_OBJECT_COST, and the suspend-window CALL list is unchanged;
  * placement -- after the RESUME pair (it must never hold the hourly graph suspended) and before the verify pane;
  * guarded once uncommented -- the same single-CALL shape as the blocks inside the window (logs Backfill365,
    RETURNs FAILED, never re-raises);
  * verdict teeth -- its own ``rv <> 'OK'`` predicate against the CURRENT definer's RETURN literals (the shared
    ILIKE predicate would read 'FAILED: object-cost load rolled back ...' as ok);
  * the year survives -- SP_PURGE_FACTS keeps daily facts at least 365 days and purges this fact on that window.
"""

from __future__ import annotations

import re

from tests._source import read
from tests.test_backfill_suspend_window import (
    _LOADS,
    _failure_verdict,
    _guarded,
    _latest_proc,
    _statements,
    _window,
)

_BF = read("snowflake/backfill_365.sql")
_CALL = "--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(365);"
_RESUME_PAIR = ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;\n"
                "SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');\n")
_VERIFY = "SELECT COUNT_IF(PAGE = 'Backfill365')"


def _block() -> str:
    """The commented block, raw: from its EXECUTE IMMEDIATE line through its closing $$; line."""
    start = _BF.index("-- EXECUTE IMMEDIATE $$\n")
    end = _BF.index("-- $$;\n", start) + len("-- $$;\n")
    return _BF[start:end]


def _uncommented() -> str:
    lines = _block().splitlines()
    assert all(ln.startswith("-- ") for ln in lines), [ln for ln in lines if not ln.startswith("-- ")]
    return "\n".join(ln[3:] for ln in lines) + "\n"


def _verdict(rv: str | None) -> bool:
    """The block's own failure predicate, evaluated: ``IF (rv IS NULL OR rv <> 'OK') THEN``."""
    m = re.search(r"^\s*IF \((rv IS NULL OR rv <> '([^']*)')\) THEN$", _uncommented(), re.M)
    assert m, "the block lost its own verdict predicate"
    return rv is None or rv != m.group(2)


def test_the_object_cost_reload_is_commented_only():
    assert _BF.count(_CALL) == 1
    live = _statements(_BF)
    assert not [s for s in live if "SP_LOAD_OBJECT_COST" in s]
    sus, res = _window(live)
    calls = [re.search(r"CALL DBA_MAINT_DB\.OVERWATCH\.([^;\n]+);", s).group(1) for s in live[sus + 1:res]]
    assert calls == list(_LOADS)                                # the suspend window's own loads are unchanged
    assert not re.search(r"^\s*CALL DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_OBJECT_COST", _BF, re.M)


def test_it_sits_after_the_resume_pair_and_before_the_verify_pane():
    at = _BF.index(_CALL)
    assert _BF.count(_RESUME_PAIR) == 1
    assert _BF.index(_RESUME_PAIR) + len(_RESUME_PAIR) < _BF.index("-- EXECUTE IMMEDIATE $$\n") < at
    assert at < _BF.index(_VERIFY)
    # the last pane also lists the reload's own rollback row (PAGE ObjectCost, object_cost_load_failed)
    last = _statements(_BF)[-1]
    assert last.startswith(_VERIFY)
    pages = set(re.findall(r"'(\w+)'", re.search(r"PAGE IN \(([^)]*)\)", last).group(1)))
    assert "ObjectCost" in pages and "ERROR_TYPE ILIKE '%_failed%'" in last
    body = _latest_proc("SP_LOAD_OBJECT_COST")
    assert re.search(r"SELECT 'ObjectCost', 'object_cost_load_failed', :emsg", body)


def test_uncommented_it_is_one_guarded_call():
    stmts = _statements(_uncommented())
    assert len(stmts) == 1 and _guarded(stmts[0])
    block = stmts[0]
    assert len(re.findall(r"^\s*CALL DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_OBJECT_COST\(365\);$", block, re.M)) == 1
    lines = [ln.strip() for ln in block.split("\nEXCEPTION\n", 1)[0].splitlines()]
    at = lines.index("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(365);")
    assert lines[at + 1] == "SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));"
    assert "RETURN 'ok: SP_LOAD_OBJECT_COST(365) -> ' || rv;" in lines
    assert "'SP_LOAD_OBJECT_COST(365)', CURRENT_ROLE();" in block and "'Backfill365'" in block
    assert "RAISE" not in block.split("\nEXCEPTION\n", 1)[1]


def test_its_verdict_has_teeth_against_the_current_definer():
    body = _latest_proc("SP_LOAD_OBJECT_COST")
    assert "SP_LOAD_OBJECT_COST(DAYS_BACK FLOAT)" in body
    literals = set(re.findall(r"^\s*RETURN '([^']*)';", body, re.M))
    assert literals == {"OK", "FAILED: object-cost load rolled back - see APP_ERROR_LOG"}
    assert not _verdict("OK")
    assert _verdict("FAILED: object-cost load rolled back - see APP_ERROR_LOG") and _verdict(None)
    # the shared predicate the in-window blocks use would read the FAILED verdict as ok -- hence its own
    assert not _failure_verdict("FAILED: object-cost load rolled back - see APP_ERROR_LOG")


def test_a_reloaded_year_survives_the_purge():
    purge = _latest_proc("SP_PURGE_FACTS")
    assert "daily_days := GREATEST(daily_days, 365);" in purge
    assert ("DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY\n"
            "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());") in purge
