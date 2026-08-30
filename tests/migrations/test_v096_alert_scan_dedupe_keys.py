"""V096 locks: alert-scan dedupe/clear keys that encode the state they need.

Re-derives SP_ALERT_SCAN (from V091) so the auto-clear sweep matches a RAISED_AT recency
window instead of DEDUPE_KEY LIKE today (next-day-cleared conditions auto-resolve) and the
supersede sweep also maps |HIGH|->|CRIT| (SLO burn) and |EXPIRING|->|EXPIRED| (cred expiry);
and SP_SLO_BREACH_SCAN (from V085) so its dedupe key gains a burn band token (a same-day
HIGH->CRITICAL escalation is no longer swallowed by the NOT EXISTS guard). Byte-locked to
outputs/gen_v096.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V096 = (_MIG / "V096__alert_scan_dedupe_keys.sql").read_text(encoding="utf-8")
_V091 = (_MIG / "V091__alert_auto_clear.sql").read_text(encoding="utf-8")
_V085 = (_MIG / "V085__slo_breach_alert.sql").read_text(encoding="utf-8")


def test_v096_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v096.py")],
        env={**os.environ, "V096_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V096, (
        "V096 drifted from its forward-generation — edit outputs/gen_v096.py, "
        "not the .sql, then regenerate."
    )


def test_v096_is_two_guarded_proc_redefinitions():
    assert "EXCEPTION (-20096" in _V096 and "IF (v < 95) THEN" in _V096
    assert "SELECT 96 AS VERSION" in _V096 and "WHERE VERSION = 96)" in _V096
    assert _V096.count("CREATE OR REPLACE PROCEDURE") == 2
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in _V096
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SLO_BREACH_SCAN" in _V096
    assert "CREATE TABLE" not in _V096 and "ALTER TABLE" not in _V096
    assert "CREATE OR REPLACE VIEW" not in _V096 and "CREATE TASK" not in _V096


def test_v096_autoclear_matches_recency_not_date_in_key():
    # defect (1): candidate set is a RAISED_AT recency window, not today's date-in-key
    assert "AND ev.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())" in _V096
    assert "ev.DEDUPE_KEY LIKE '%|'" not in _V096      # the stranding filter is gone
    # the >=1h dwell upper bound and the below-CLEAR hysteresis NOT-IN survive
    assert "AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())" in _V096
    assert _V096.count("COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)") == 3
    # V091 (base) still carries the date-in-key bug, confirming V096 supersedes it
    assert "AND ev.DEDUPE_KEY LIKE '%|' || CURRENT_DATE()" in _V091


def test_v096_supersede_sweep_covers_slo_burn_and_cred_expiry():
    # defects (2b)+(3): the OR-list gains |HIGH|->|CRIT| and |EXPIRING|->|EXPIRED|
    assert "REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')" in _V096
    assert "REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')" in _V096
    assert "REPLACE(lo.DEDUPE_KEY, '|HIGH|', '|CRIT|')" in _V096
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" in _V096


def test_v096_slo_dedupe_key_carries_a_burn_band_token():
    # defect (2): the SLO dedupe key gains a burn band segment matching the severity escalation
    assert ("c.RULE_ID || '|' || e.SLO_ID || '|' "
            "|| IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRIT', 'HIGH') "
            "|| '|' || TO_VARCHAR(CURRENT_DATE())") in _V096
    # the severity escalation predicate is unchanged and identical to the band predicate
    assert "IFF(COALESCE(e.BURN_MULTIPLE, 0) >= 2, 'CRITICAL', c.SEVERITY)" in _V096
    # V085 (base) still has the un-banded key, confirming V096 supersedes it
    assert "c.RULE_ID || '|' || e.SLO_ID || '|' || TO_VARCHAR(CURRENT_DATE())" in _V085


def test_v096_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V096):
        sqlglot.parse(statement, dialect="snowflake")
