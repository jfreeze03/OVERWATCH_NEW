"""Locks for V065 — alert run-rate window fixes (bug round 5).

Two pre-existing bugs in SP_ALERT_SCAN_DAILY, both re-derived from V064 (the latest
def) via outputs/gen_v065.py + count-asserted needle edits:

  rank2 (MEDIUM)  COST_FORECAST_BREACH / COST_BUDGET_PACE mtd CTE: DAILY_RATE_USD was
        MTD$ / DAY(CURRENT_DATE()) -- month-to-date dollars (incl. today's PARTIAL day)
        over the full day-of-month understated the run-rate -> under-projected month-end
        -> could suppress the budget-breach alert. Now a COMPLETE-days-only rate. Both
        mtd CTEs carry the identical expression; [09] projects with it (the real bug),
        [08] computes it dead -> both fixed for consistency.
  rank3 (LOW)     COST_AI_CREEP: THIS_WK spanned 8 days (DAY >= today-7 INCLUDES today)
        vs PRIOR_WK 7 -> inflated WoW. The scan window now excludes today, so both weeks
        are equal 7-complete-day windows.

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V65 = (_MIG / "V065__alert_run_rate_windows.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v065_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v065.py")],
                       env={**os.environ, "V065_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V65, (
        "V065 drifted from outputs/gen_v065.py — regenerate, never hand-edit.")


def test_v065_guard_and_version():
    assert "EXCEPTION (-20065" in _V65 and "IF (v < 64) THEN" in _V65
    assert "SELECT 65 AS VERSION" in _V65 and "WHERE VERSION = 65)" in _V65
    # exactly one proc re-defined, no new objects
    assert _V65.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE TABLE" not in _V65 and "CREATE TASK" not in _V65
    assert "ALTER TABLE" not in _V65


# ---------------------------------------------------------------------------
# rank2 — complete-days-only forecast run-rate (both mtd CTEs)
# ---------------------------------------------------------------------------
def test_v065_rank2_complete_day_run_rate():
    scan = _proc(_V65, "SP_ALERT_SCAN_DAILY")
    # the partial-day divisor is gone from BOTH blocks; the complete-day divisor replaces it
    assert "/ NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD" not in scan
    assert scan.count("NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD") == 2
    # the numerator now sums only complete days (DAY < today), still AI-weighted two ways
    assert scan.count("SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE") == 2
    assert scan.count("SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE") == 2
    # MTD_USD stays the additive base (whole month-to-date, incl today) in both blocks
    assert scan.count("* :ai_credit_price AS MTD_USD,") == 2
    # the projection still adds the rate over the remaining full days (shape unchanged)
    assert "m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH)" in scan
    assert "COST_FORECAST_BREACH" in scan and "COST_BUDGET_PACE" in scan


# ---------------------------------------------------------------------------
# rank3 — COST_AI_CREEP compares equal, today-excluded 7-day windows
# ---------------------------------------------------------------------------
def test_v065_rank3_ai_creep_today_excluded():
    scan = _proc(_V65, "SP_ALERT_SCAN_DAILY")
    creep = scan.split("COST_AI_CREEP", 1)[1].split("-- [", 1)[0]  # bound to the AI-creep block
    # the scan window now excludes today, making both weeks 7 complete days
    assert "WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())\n              AND DAY < CURRENT_DATE()" in scan
    # the week boundaries are unchanged (still split at today-7); today-exclusion does the work
    assert "IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS THIS_WK_CR" in creep
    assert "IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS PRIOR_WK_CR" in creep


# ---------------------------------------------------------------------------
# derived-from-V064: everything V064 fixed in this proc must survive untouched
# ---------------------------------------------------------------------------
def test_v065_preserves_v064_contract_burn():
    scan = _proc(_V65, "SP_ALERT_SCAN_DAILY")
    # V064 rec20-alert trailing-30-complete-day contract burn is carried through
    assert "NULLIF(COUNT(DISTINCT DAY), 0)" in scan
    assert "AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN" in scan
    assert "COST_CONTRACT_BREACH" in scan


def test_v065_lockstep_floor_and_registry():
    # validate.sql floor and the admin contract both moved to 65 (generic floor test in
    # test_v451_trust / test_perf_budgets also guards this; this pins the intent here).
    v = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V065 applied" in v and "BETWEEN 1 AND 65) = 65" in v
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 65 in _EXPECTED_MIGRATIONS
