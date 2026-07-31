"""Locks for V066 — bug round 6's 5 migration-proc fixes.

Three procs re-derived (SP_ALERT_SCAN + SP_LOAD_MARTS_V27 from V062, SP_ALERT_SCAN_DAILY
from V065) via count-asserted needle edits in outputs/gen_v066.py:

  #1  PIPE_COPY_FAILURES     -- daily dedupe key gains a CRIT/WARN severity band
  #6  COST_SERVERLESS_CREEP  -- scan window excludes today (equal 7-complete-day weeks)
  #11 COST_DEPT_BUDGET_PACE  -- daily dedupe key gains a HIGH/MED severity band
  #2  COST_CONTRACT_BREACH   -- weekly dedupe key gains a CRIT/WARN band (AI_CREEP left alone)
  #3  MART_INCIDENT_TIMELINE arm [8] -- DELETE+INSERT wrapped in one transaction

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V66 = (_MIG / "V066__alert_escalation_serverless_window_timeline_atomicity.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v066_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v066.py")],
                       env={**os.environ, "V066_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V66, (
        "V066 drifted from outputs/gen_v066.py — regenerate, never hand-edit.")


def test_v066_guard_and_version():
    assert "EXCEPTION (-20066" in _V66 and "IF (v < 65) THEN" in _V66
    assert "SELECT 66 AS VERSION" in _V66 and "WHERE VERSION = 66)" in _V66
    assert _V66.count("CREATE OR REPLACE PROCEDURE") == 3   # 3 procs re-derived
    assert "CREATE TABLE" not in _V66 and "CREATE TASK" not in _V66


# ---------------------------------------------------------------------------
# escalation dedupe bands (SP_ALERT_SCAN #1/#11, SP_ALERT_SCAN_DAILY #2)
# each band must MATCH its rule's own severity-escalation threshold
# ---------------------------------------------------------------------------
def test_pipe_copy_dedupe_band_matches_critical_threshold():
    scan = _proc(_V66, "SP_ALERT_SCAN")
    # SEVERITY escalates at FAILED_FILES >= 10; the dedupe band must flip on the same test
    assert "IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY)" in scan     # severity (unchanged)
    assert "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())" in scan
    assert "p.TBL || '|' || TO_VARCHAR(CURRENT_DATE())" not in scan        # bare key gone


def test_dept_pace_dedupe_band_matches_high_threshold():
    scan = _proc(_V66, "SP_ALERT_SCAN")
    assert "IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', c.SEVERITY)" in scan   # severity
    assert "IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', 'MED') || '|' || TO_VARCHAR(CURRENT_DATE())" in scan
    assert "d.DEPARTMENT || '|' || TO_VARCHAR(CURRENT_DATE())" not in scan        # bare key gone


def test_contract_breach_band_leaves_ai_creep_alone():
    daily = _proc(_V66, "SP_ALERT_SCAN_DAILY")
    assert "IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY)" in daily              # severity
    assert "IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN') || '|' || TO_VARCHAR(DATE_TRUNC('week'" in daily
    # exactly one bare weekly key remains — COST_AI_CREEP, which has no within-bucket escalation
    assert daily.count("c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))") == 1


# ---------------------------------------------------------------------------
# #6 serverless creep — today excluded (equal 7-complete-day weeks)
# ---------------------------------------------------------------------------
def test_serverless_creep_excludes_today():
    scan = _proc(_V66, "SP_ALERT_SCAN")
    assert ("WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())\n"
            "              AND USAGE_DATE < CURRENT_DATE()") in scan
    # the week boundaries are unchanged; today-exclusion does the work
    assert "SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) AS THIS_WK" in scan


# ---------------------------------------------------------------------------
# #3 incident-timeline arm [8] — atomic DELETE+INSERT
# ---------------------------------------------------------------------------
def test_incident_timeline_arm_is_atomic():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    arm = marts.split("[8] incident timeline", 1)[1].split("loaded := loaded || 'timeline ';", 1)[0]
    assert "BEGIN TRANSACTION;" in arm                                   # opens before the DELETE
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE" in arm
    assert marts.count("CURRENT_TIMESTAMP());\n            COMMIT;\n            loaded := loaded || 'timeline ';") == 1
    assert "ROLLBACK;   -- V066 #3" in marts                             # undo on a failed rebuild


def test_v066_preserves_v065_run_rate_windows():
    daily = _proc(_V66, "SP_ALERT_SCAN_DAILY")
    # V065's forecast + AI-creep fixes ride through untouched
    assert "NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD" in daily
    assert "AND DAY < CURRENT_DATE()   -- V065 rank3" in daily
    assert "NULLIF(COUNT(DISTINCT DAY), 0)" in daily        # V064 contract burn


def test_v066_in_migration_registry():
    # V066 is registered in the admin contract. (The validate.sql floor is the MOVING tip,
    # guarded generically by test_v451_trust — a per-migration test must not pin it.)
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 66 in _EXPECTED_MIGRATIONS
