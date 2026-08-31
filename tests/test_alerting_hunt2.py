"""Alerting-layer bug-hunt #2 locks (2026-08-30, v4.368.0).

Second adversarial alerting pass (6 finders). 13 surfaced, 9 confirmed (6 distinct), 4 refuted. All
six are migration-bearing, shipped as V110 (SP_ALERT_SCAN x4), V111 (SP_ALERT_SCAN_DAILY), V112
(SP_DAILY_DIGEST). (Distinct from the v4.347.0 alerting hunt locked in test_alerting_hunt.py.)
  - [MED] SEC_NEW_ADMIN_NETWORK omitted the built-in ACCOUNTADMIN (false all-clear on a new-network admin login).
  - [MED] cred-expiry EXPIRING->EXPIRED supersede was a no-op after V104 made the band token terminal.
  - [LOW] contract-breach CRIT/WARN->EXHAUSTED had no supersede arm (stale CRIT lingered).
  - [LOW] PERF_QUERY_FAIL_PCT DETAIL interpolated WINDOW_HOURS while the window is a hardcoded 24h.
  - [LOW] COST_BUDGET_PACE counted today as fully elapsed (under-fired early in the month).
  - [LOW] the daily digest could reach a CRITICAL-only paging route.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _mig(name: str) -> str:
    return (_ROOT / "snowflake" / "migrations" / name).read_text(encoding="utf-8")


def test_v110_sp_alert_scan_fixes() -> None:
    mig = _mig("V110__alerting_hunt_sp_alert_scan_fixes.sql")
    # A: built-in ACCOUNTADMIN added to the watched-admin role set
    assert "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" in mig
    assert "AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" not in mig
    # B: EXPIRING supersede matches the terminal token (no trailing pipe); the old broken form is gone
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))" in mig
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" not in mig
    # C: CRIT/WARN -> EXH supersede arms for the V108 contract EXHAUSTED band
    assert "REPLACE(lo.DEDUPE_KEY, '|CRIT|', '|EXH|')" in mig
    assert "REPLACE(lo.DEDUPE_KEY, '|WARN|', '|EXH|')" in mig
    # F: PERF DETAIL hardcodes 24h to match the aggregation
    assert "' queries failed in last 24h.'" in mig
    assert "c.WINDOW_HOURS || 'h.'" not in mig
    # re-derives the one proc only; ordered-apply guard + version stamp
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in mig
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    assert "EXCEPTION (-20110" in mig and "IF (v < 109) THEN" in mig
    assert "SELECT 110 AS VERSION" in mig and "WHERE VERSION = 110)" in mig


def test_v111_cost_budget_pace_completed_days() -> None:
    mig = _mig("V111__cost_budget_pace_completed_days.sql")
    assert mig.count("(m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH") == 3
    assert " m.DAY_OF_MONTH / m.DAYS_IN_MONTH" not in mig   # no full-day share left
    assert "AND m.DAY_OF_MONTH > 1" in mig                  # day-1 guard
    assert "(m.DAYS_IN_MONTH - m.DAY_OF_MONTH)" in mig      # forecast remaining-days math untouched
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" in mig
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    assert "EXCEPTION (-20111" in mig and "IF (v < 110) THEN" in mig
    assert "SELECT 111 AS VERSION" in mig and "WHERE VERSION = 111)" in mig


def test_v112_daily_digest_skips_paging_routes() -> None:
    mig = _mig("V112__daily_digest_skips_paging_routes.sql")
    assert "UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'" in mig
    assert "r.ENABLED AND r.DELIVER_DIGEST" in mig   # existing digest-eligible filter preserved
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST" in mig
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    assert "EXCEPTION (-20112" in mig and "IF (v < 111) THEN" in mig
    assert "SELECT 112 AS VERSION" in mig and "WHERE VERSION = 112)" in mig
