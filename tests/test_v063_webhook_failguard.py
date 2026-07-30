"""Locks for V063 — the two correctness/robustness fixes deferred from V062.

  B9   SP_NOTIFY_WEBHOOK capture-once: the fitting EVENT_IDs are frozen into ONE
       ARRAY; the message, the ALERT_DELIVERIES ledger, and the NOTIFIED_AT update
       all derive from that array (ARRAY_CONTAINS), so the sent set == the recorded
       set by construction (no send-vs-ledger race). Runtime ARRAY binding is
       owner-smoke-tested (DEPLOYMENT.md); a byte-compare cannot prove it.
  B34  SP_LOAD_DAILY_FACTS fail-guard: a per-table failure sets failed_any, holds
       the DAILY_FACTS watermark, and returns a non-success string (was: swallowed
       -> advanced -> false success). Per-table isolation preserved.

The generator is the source of truth: outputs/gen_v063.py re-derives both procs
(SP_NOTIFY_WEBHOOK from V034 — V062 didn't touch it; SP_LOAD_DAILY_FACTS from V062)
via count-asserted needle edits. The first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V63 = (_MIG / "V063__webhook_capture_once_daily_facts_failguard.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v063_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v063.py")],
                       env={**os.environ, "V063_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V63, (
        "V063 drifted from outputs/gen_v063.py — regenerate, never hand-edit.")


def test_v063_guard_and_version():
    assert "EXCEPTION (-20063" in _V63 and "IF (v < 62) THEN" in _V63
    assert "SELECT 63 AS VERSION" in _V63 and "WHERE VERSION = 63)" in _V63
    assert _V63.count("CREATE OR REPLACE PROCEDURE") == 2   # SP_NOTIFY_WEBHOOK + SP_LOAD_DAILY_FACTS
    assert _V63.count("$$") == 6
    assert "CREATE TABLE" not in _V63 and "CREATE TASK" not in _V63   # no new objects


# ---------------------------------------------------------------------------
# B9 — capture-once: message, ledger, and NOTIFIED_AT share ONE frozen array
# ---------------------------------------------------------------------------
def test_b9_capture_once_single_frozen_set():
    wh = _proc(_V63, "SP_NOTIFY_WEBHOOK")
    assert "fits_ids ARRAY;" in wh                          # the frozen set is declared
    assert wh.count("ARRAY_AGG(f.EVENT_ID)") == 1           # captured EXACTLY once
    # the message, the ledger INSERT, and the NOTIFIED_AT UPDATE all read the SAME
    # array — three consumers, one immutable set (this is what kills the race).
    assert wh.count("ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)") == 3
    # the ledger no longer re-derives the eligible/CUM_LEN set independently
    assert wh.count("SUM(LEN(REPLACE") == 1                 # the escaped-length running sum lives ONLY in the capture
    assert "OWNER SMOKE TEST" in _V63 or "SMOKE TEST REQUIRED" in _V63  # runbook flag is loud


def test_b9_preserves_delivery_semantics():
    wh = _proc(_V63, "SP_NOTIFY_WEBHOOK")
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in wh   # 24h window kept
    assert "NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d" in wh  # per-route already-delivered filter
    assert "undelivered_expired" in wh                          # the loud expired-undelivered tail survives


# ---------------------------------------------------------------------------
# B34 — fail-guard: partial failure holds the watermark + returns non-success
# ---------------------------------------------------------------------------
def test_b34_failguard_holds_watermark():
    daily = _proc(_V63, "SP_LOAD_DAILY_FACTS")
    assert "failed_any BOOLEAN DEFAULT FALSE;" in daily
    assert daily.count("failed_any := TRUE;") == 3          # set in all 3 per-table handlers
    assert "IF (NOT failed_any) THEN" in daily              # watermark advance gated on success
    # the DAILY_FACTS watermark MERGE is the thing being gated
    assert "OW_LOAD_WATERMARKS" in daily and "'DAILY_FACTS'" in daily
    # per-table isolation preserved: NO re-raise added (siblings still load)
    # (the 3 transaction wraps stay intact)
    assert daily.count("BEGIN TRANSACTION") == 3 and daily.count("ROLLBACK") == 3


def test_b34_returns_non_success_on_partial_failure():
    daily = _proc(_V63, "SP_LOAD_DAILY_FACTS")
    # a partial-failure run must NOT return the plain success string unconditionally
    assert "IF (failed_any)" in daily or "failed_any" in daily.split("RETURN", 1)[-1] \
        or "WITH ERRORS" in daily or "watermark held" in daily


# ---------------------------------------------------------------------------
# T3 deferral — perf-loader restructures are NOT in V063 (isolated to V064)
# ---------------------------------------------------------------------------
def test_v063_excludes_deferred_t3():
    assert "SP_LOAD_MARTS_V27" not in _V63       # T3.1-T3.3 (V064)
    assert "SP_NIGHTLY_RECONCILE" not in _V63    # T3.4 (V064)
    assert "V064" in _V63                        # the deferral is documented
