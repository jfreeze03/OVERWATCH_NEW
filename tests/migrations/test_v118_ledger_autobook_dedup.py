"""Locks for V118 — SP_LEDGER_AUTOBOOK stops double-counting a warehouse's saving
when >1 cost lever changes on it in the same measured window (LBA-1, round-4 hunt).

BASELINE/AFTER_CREDITS_PER_DAY are WAREHOUSE-level, but the registry holds one row
per (warehouse, SETTING) change — so V038 booked the full warehouse delta on EACH
co-occurring lever. V118 attributes the delta to ONE primary lever per measured
window; co-occurring levers settle VERIFIED at $0."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V118__ledger_autobook_dedup.sql").read_text(encoding="utf-8")


def test_v118_guard_shape_and_chain():
    assert "EXCEPTION (-20118" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                 # the V035 lesson holds
    assert "IF (v < 117) THEN" in _MIG and "SELECT 118 AS VERSION" in _MIG
    # $$-only dollar quoting (the V089 lesson); no tagged $tag$ quotes.
    assert "$_" not in _MIG


def test_recreates_the_proc_and_keeps_the_forward_only_settle():
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK()" in _MIG
    body = _MIG.split("SP_LEDGER_AUTOBOOK()", 1)[1]
    assert "l.STATE = 'ESTIMATED'" in body                 # settled items still never rewrite
    assert "r.VERDICT <> 'PENDING'" in body                # only measured verdicts settle
    assert "* :rate * 30" in body                          # measured credits/day x rate x 30
    assert "CREDIT_PRICE_USD" in _MIG                       # rate from SETTINGS, not hardcoded


def test_dedup_attributes_the_warehouse_delta_once():
    # The measured-window signature partitions co-occurring levers; RN=1 is primary.
    assert "PARTITION BY r.WAREHOUSE_NAME, r.BASELINE_CREDITS_PER_DAY," in _MIG
    assert "r.AFTER_CREDITS_PER_DAY, r.AFTER_DAYS" in _MIG
    assert "ROW_NUMBER() OVER (" in _MIG and "FIRST_VALUE(r.CHANGE_ID) OVER (" in _MIG
    # primary carries the full saving; co-occurring levers settle VERIFIED at $0.
    assert "WHEN s.RN = 1 THEN ROUND(s.WH_SAVED_MONTHLY_USD, 2)" in _MIG
    assert "ELSE 0" in _MIG
    assert "co-attributed" in _MIG
    assert "WH_SAVED_MONTHLY_USD >= 5" in _MIG              # $5/mo noise floor preserved
    # MPROC-1 (round-6): the ranking population is restricted to BOOKED levers (those with
    # a ledger row), so a non-saving co-occurring change can't win RN=1 as a phantom
    # primary and settle the real saving lever at $0. Both settle blocks carry the guard.
    assert _MIG.count("EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l2\n"
                      "                            WHERE l2.SOURCE_CHANGE_ID = r.CHANGE_ID)") == 1
    assert "l3.SOURCE_CHANGE_ID = reg.CHANGE_ID" in _MIG


def test_one_time_correction_is_idempotent_and_conservative():
    corr = _MIG.split("Step 2", 1)[1]
    assert "g.RN > 1" in corr                               # only non-primary duplicates
    assert "l.STATE = 'VERIFIED'" in corr                   # only already-booked rows
    assert "COALESCE(l.VERIFIED_USD, 0) > 0" in corr        # that carry dollars
    assert "NOT LIKE '%LBA-1 co-attributed%'" in corr       # sentinel -> re-run is a no-op
    # only correct when the group's primary is itself a real (VERIFIED) saving
    assert "p.SOURCE_CHANGE_ID = g.PRIMARY_CHANGE_ID" in corr and "p.STATE = 'VERIFIED'" in corr


def test_validate_floor_tracks_the_tip():
    # floor moved to V119 when the auto-clear-hysteresis migration landed (round 6).
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V123 applied" in val
    assert "VERSION BETWEEN 1 AND 123) = 123" in val
