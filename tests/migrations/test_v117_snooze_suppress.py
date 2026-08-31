"""V117: SP_ALERT_SCAN snooze-suppress sweep + read-path machine-close exclusion.

A per-event snooze keeps the event's date-banded DEDUPE_KEY, so when the day/week band rolls
the raise arms mint a fresh OPEN for the same rule+entity even though it is snoozed -- a
multi-day snooze silenced nothing past the first day. V117 adds a post-raise sweep that
resolves any OPEN event whose band-independent identity (DEDUPE_KEY minus a trailing bare-date
token, via TRY_TO_DATE) matches an active snooze for the same rule, tagged SNOOZE_SUPPRESSED
(a machine close excluded from precision, like SUPERSEDED/AUTO_CLEARED).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v117_adds_snooze_suppress_sweep() -> None:
    mig = _read("snowflake/migrations/V117__alert_snooze_suppress_sweep.sql")
    assert mig.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN") == 1
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig   # re-derived proc, no schema change
    # the new sweep: resolve OPEN events covered by an ACTIVE snooze for the same rule
    assert "RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'" in mig
    assert "s.STATUS = 'SNOOZED'" in mig and "s.SNOOZED_UNTIL > CURRENT_TIMESTAMP()" in mig
    assert "s.RULE_ID = ev.RULE_ID" in mig
    # band-independent identity via TRY_TO_DATE (no regex): strip a trailing |YYYY-MM-DD
    assert "TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10))" in mig
    assert "TRY_TO_DATE(RIGHT(ev.DEDUPE_KEY, 10))" in mig
    assert "SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'" in mig
    # re-derived from V115: the supersede-ACK fix + the sibling sweeps survive untouched
    assert "WHERE lo.STATUS IN ('OPEN', 'ACK')" in mig
    assert mig.count("RESOLUTION_KIND = 'AUTO_CLEARED'") == 1
    assert mig.count("RESOLUTION_KIND = 'SUPERSEDED'") == 1
    # ordered guard + version stamp
    assert "EXCEPTION (-20117" in mig and "IF (v < 116) THEN" in mig
    assert "SELECT 117 AS VERSION" in mig and "WHERE VERSION = 117)" in mig


def test_v117_read_path_excludes_snooze_suppressed_as_machine_close() -> None:
    # SNOOZE_SUPPRESSED is a MACHINE close (not a human resolution), so it must join
    # SUPERSEDED/AUTO_CLEARED in every read-path exclusion (RESOLVED counts, MTTR, precision) —
    # otherwise the daily snooze-suppressed re-raises would inflate the human-resolution metrics.
    src = _read("app/data/mart_sql.py")
    assert "NOT IN ('SUPERSEDED', 'AUTO_CLEARED')" not in src   # the 2-element form is fully upgraded
    assert src.count("NOT IN ('SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED')") == 4


def test_v117_registered() -> None:
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 117 in _EXPECTED_MIGRATIONS
    assert "BETWEEN 1 AND 88" in _read("snowflake/validate.sql")   # teeth-floor stays 88
