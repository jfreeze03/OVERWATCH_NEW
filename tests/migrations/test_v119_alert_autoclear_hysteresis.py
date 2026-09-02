"""Locks for V119 — SP_ALERT_SCAN auto-clear hysteresis fix (MPROC-2, round-6 hunt).

The auto-clear sweep resolves an OPEN perf event unless its scope is STILL firing at
the CLEAR floor. V117 minted the "still firing" keys with CURRENT_DATE() (today) while
the OPEN event carries its RAISE-day date in its own DEDUPE_KEY, so across midnight an
event held open by hysteresis could never match and was falsely auto-cleared. V119
compares on the date-stripped RULE_ID|scope identity on both sides."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V119__alert_autoclear_hysteresis_fix.sql").read_text(encoding="utf-8")


def test_v119_guard_shape_and_chain():
    assert "EXCEPTION (-20119" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                 # the V035 lesson holds
    assert "IF (v < 118) THEN" in _MIG and "SELECT 119 AS VERSION" in _MIG
    assert "$_" not in _MIG                                # $$-only dollar quoting (V089 lesson)
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()" in _MIG


def test_autoclear_compares_the_date_stripped_identity():
    # LEFT side: the event's key is stripped to RULE_ID|scope (2nd token = scope).
    assert "(ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)) NOT IN (" in _MIG
    # RIGHT side: the still-firing set emits the date-free identity, NOT ...|CURRENT_DATE().
    assert "SELECT c.RULE_ID || '|' || q.COMPANY AS DEDUPE_KEY" in _MIG
    # Scope the date-free check to the auto-clear NOT-IN block ONLY — the RAISE arms
    # elsewhere in the proc legitimately keep CURRENT_DATE() in their minted keys.
    span = _MIG.split("SPLIT_PART(ev.DEDUPE_KEY, '|', 2)) NOT IN (", 1)[1].split("EXCEPTION", 1)[0]
    assert "CURRENT_DATE()" not in span, "auto-clear still-firing set is still date-stamped"
    # the -48h..-1h resolve window (V096) is preserved
    assert "DATEADD('hour', -48, CURRENT_TIMESTAMP())" in _MIG
    assert "DATEADD('hour', -1, CURRENT_TIMESTAMP())" in _MIG


def test_raise_arms_still_date_stamp_their_keys():
    # ONLY the auto-clear still-firing set drops the date; the raise arms must KEEP
    # CURRENT_DATE() in the minted DEDUPE_KEY (their per-day dedup identity).
    assert _MIG.count("CURRENT_DATE()") >= 20      # raise arms retain their day-stamped keys


def test_validate_floor_and_docs_track_v119():
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V122 applied" in val and "VERSION BETWEEN 1 AND 122) = 122" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V119__alert_autoclear_hysteresis_fix.sql" in (_ROOT / rel).read_text(encoding="utf-8")
