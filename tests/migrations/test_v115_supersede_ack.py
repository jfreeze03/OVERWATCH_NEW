"""V115: SP_ALERT_SCAN escalation-supersede includes ACK'd events.

The V067 #40 supersede sweep resolved the lower-band event only when BOTH sides were
STATUS='OPEN'. But the open-count convention is STATUS IN ('OPEN','ACK') -- an ACK'd
event still counts as open -- so an acknowledged-then-escalated incident (a WARN ACKed,
then a CRIT raised) double-counted as two open alerts with a doubled score penalty.
V115 broadens both sides of the sweep to OPEN/ACK; the sibling auto-clear sweep stays
OPEN-only by design (an ACK there means a human is actively working it).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v115_supersede_includes_ack() -> None:
    mig = _read("snowflake/migrations/V115__alert_supersede_includes_ack.sql")
    # re-derives SP_ALERT_SCAN only; no schema change
    assert mig.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN") == 1
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    # the fix: both sides of the supersede sweep now match OPEN or ACK
    assert "WHERE lo.STATUS IN ('OPEN', 'ACK')" in mig
    assert "WHERE hi.STATUS IN ('OPEN', 'ACK')" in mig
    assert "lo.STATUS = 'OPEN'" not in mig and "hi.STATUS = 'OPEN'" not in mig
    # the auto-clear sweep (ev alias) stays OPEN-only by design
    assert "WHERE ev.STATUS = 'OPEN'" in mig
    # SUPERSEDED kind + V110's EXH / terminal-EXPIRING arms survive (proves re-derived from V110)
    assert mig.count("RESOLUTION_KIND = 'SUPERSEDED'") == 1
    assert "REPLACE(lo.DEDUPE_KEY, '|CRIT|', '|EXH|')" in mig
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))" in mig
    assert "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" in mig
    # ordered guard + version stamp
    assert "EXCEPTION (-20115" in mig and "IF (v < 114) THEN" in mig
    assert "SELECT 115 AS VERSION" in mig and "WHERE VERSION = 115)" in mig


def test_v115_registered() -> None:
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 115 in _EXPECTED_MIGRATIONS
    # the MOVING validate tip (V001..Vtip applied / BETWEEN 1 AND tip) is asserted by the
    # 7 designated tip tests, which bump every migration -- not here (this would break at V116).
    assert "BETWEEN 1 AND 88" in _read("snowflake/validate.sql")   # teeth-floor is stable
