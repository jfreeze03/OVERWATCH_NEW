"""Locks for V064 — the Codex-R2 NEXT-tier owner-migration bundle.

  rec8    SP_NOTIFY_WEBHOOK: OLDEST-first bounded drain. Batches drain the backlog
          forward (capture-once per batch, B9 preserved) so the oldest alerts stop
          starving past the 24h window. Runtime-only (SYSTEM$SEND + ARRAY + the
          LOOP) -> owner smoke-tested (DEPLOYMENT.md); a byte-compare can't prove it.
  rec7    SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE: per-source daily watermarks so
          one table's failure holds only its own mark (was one shared DAILY_FACTS).
  rec20a  SP_ALERT_SCAN_DAILY COST_CONTRACT_BREACH: trailing-30-COMPLETE-day burn
          (was SUM/30 over a partial-current-day span -> could suppress the breach).
  rec18   APP_QUERY_TELEMETRY += SAMPLE_PROB, QUERY_ID (additive, idempotent).

The generator is the source of truth: outputs/gen_v064.py re-derives daily-facts
(from V063), reconcile + alert-scan (from V062) via count-asserted needle edits,
and authors SP_NOTIFY_WEBHOOK in full (a single-shot -> loop restructure). The
first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V64 = (_MIG / "V064__webhook_drain_watermarks_alert_burn_telemetry.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v064_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v064.py")],
                       env={**os.environ, "V064_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V64, (
        "V064 drifted from outputs/gen_v064.py — regenerate, never hand-edit.")


def test_v064_guard_and_version():
    assert "EXCEPTION (-20064" in _V64 and "IF (v < 63) THEN" in _V64
    assert "SELECT 64 AS VERSION" in _V64 and "WHERE VERSION = 64)" in _V64
    # webhook + daily-facts + reconcile + alert-scan
    assert _V64.count("CREATE OR REPLACE PROCEDURE") == 4
    assert _V64.count("$$") == 10                 # 2 (guard) + 8 (four procs)
    assert "CREATE TABLE" not in _V64 and "CREATE TASK" not in _V64   # no new objects


def test_v064_numbering_note():
    # the deliberate V064 (not the deferred T3) is documented, and T3 -> V065
    assert "V065" in _V64 and "contiguous" in _V64


# ---------------------------------------------------------------------------
# rec18 — telemetry columns (additive, idempotent)
# ---------------------------------------------------------------------------
def test_rec18_telemetry_columns_added_idempotent():
    assert "ALTER TABLE DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY" in _V64
    assert "ADD COLUMN IF NOT EXISTS SAMPLE_PROB" in _V64
    assert "ADD COLUMN IF NOT EXISTS QUERY_ID" in _V64


# ---------------------------------------------------------------------------
# rec8 — webhook oldest-first bounded drain, capture-once per batch preserved
# ---------------------------------------------------------------------------
def test_rec8_oldest_first_bounded_drain():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    assert "LOOP" in wh and "END LOOP;" in wh          # the drain loop
    assert "max_batches INT DEFAULT 6" in wh           # bounded batches/route/run
    # oldest-first everywhere; the newest-first V063 ordering is gone
    assert wh.count("ORDER BY f.RAISED_AT ASC") == 1
    assert wh.count("ORDER BY e.RAISED_AT ASC") == 2
    assert "DESC" not in wh
    assert wh.count("EXIT;") == 4   # empty fits, empty msg, send-fail, max_batches


def test_rec8_capture_once_preserved_per_batch():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    assert "fits_ids ARRAY;" in wh
    assert wh.count("ARRAY_AGG(f.EVENT_ID)") == 1                          # capture once per batch
    assert wh.count("ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)") == 3  # message + ledger + NOTIFIED_AT
    assert wh.count("SUM(LEN(REPLACE") == 1                                # escaped-length running sum only in the capture
    assert "OWNER SMOKE TEST" in _V64 or "SMOKE TEST REQUIRED" in _V64


def test_rec8_expired_shares_send_eligibility():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    # the expired-detection now JOINs ALERT_CONFIG and mirrors the send eligibility
    # (family + company + severity), so "flagged expired" == "was eligible but unsent"
    tail = wh.split("Loud, not silent", 1)[1]
    assert "JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID" in tail
    assert "r2.FAMILY = 'ALL' OR c.FAMILY = r2.FAMILY" in tail
    assert "r2.MIN_SEVERITY" in tail
    assert "undelivered_expired" in wh                # the loud tail survives
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in wh   # 24h window kept


# ---------------------------------------------------------------------------
# rec7 — per-source daily watermarks (loader + reconcile in lockstep)
# ---------------------------------------------------------------------------
def test_rec7_loader_per_source_watermarks():
    daily = _proc(_V64, "SP_LOAD_DAILY_FACTS")
    # four per-source advance MERGEs, one per source; the shared key is gone
    for src in ("FACT_METERING_DAILY", "FACT_TASK_DAILY", "FACT_LOGIN_DAILY", "FACT_STORAGE_DAILY"):
        assert f"USING (SELECT '{src}' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s" in daily
    assert "'DAILY_FACTS'" not in daily
    assert daily.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS") == 4
    assert "IF (NOT failed_any) THEN" not in daily      # the single gated advance is gone
    assert ":lo_short::DATE" not in daily               # every window retargeted per-source


def test_rec7_loader_preserves_b34_isolation():
    daily = _proc(_V64, "SP_LOAD_DAILY_FACTS")
    # per-table transaction isolation (B34) and the fail flag survive
    assert daily.count("BEGIN TRANSACTION") == 3 and daily.count("ROLLBACK") == 3
    # 3 sibling handlers + the guarded metering-mark handler (review fix): a
    # transient OW_LOAD_WATERMARKS lock on the metering advance must not abort the
    # proc and starve the siblings -- it logs, holds, and falls through.
    assert daily.count("failed_any := TRUE;") == 4
    assert "FACT_METERING_DAILY watermark advance" in daily   # the guarded metering handler
    assert "SOURCE_FRESHNESS_STATE" in daily            # freshness MERGE untouched
    assert "WITH ERRORS" in daily                       # partial-failure return preserved


def test_rec7_cold_start_seed_from_retained_mark():
    # REVIEW FIX (major): the four new per-source keys are seeded from the retained
    # 'DAILY_FACTS' mark in the migration BODY (before the procs), so a cutover during
    # an outage inherits the held position instead of cold-starting and dropping the
    # backlog. Idempotent: only an absent key, only when DAILY_FACTS exists.
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS (SOURCE, WM_TS)" in _V64
    assert _V64.count("WHERE SOURCE = 'DAILY_FACTS'") == 2   # inherit + guard, in the seed
    for src in ("FACT_METERING_DAILY", "FACT_TASK_DAILY", "FACT_LOGIN_DAILY", "FACT_STORAGE_DAILY"):
        assert f"SELECT '{src}'" in _V64 or f"'{src}' AS SOURCE" in _V64
    assert "NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS w WHERE w.SOURCE = s.SOURCE)" in _V64
    # the seed is in the body, NOT inside the loader proc
    assert "'DAILY_FACTS'" not in _proc(_V64, "SP_LOAD_DAILY_FACTS")


def test_rec7_reconcile_rewinds_new_keys():
    rec = _proc(_V64, "SP_NIGHTLY_RECONCILE")
    # rewinds the 4 new per-source marks (else daily re-coverage silently no-ops)
    assert "'FACT_METERING_DAILY', 'FACT_TASK_DAILY'" in rec
    assert "'FACT_LOGIN_DAILY', 'FACT_STORAGE_DAILY'" in rec
    assert "'DAILY_FACTS'" not in rec
    # still rewinds the extract + hourly marks and re-calls the daily loader
    assert "'QH_EXTRACT', 'HOURLY_FACTS'" in rec
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();" in rec


# ---------------------------------------------------------------------------
# rec20-alert — contract-breach burn matches the canonical app definition
# ---------------------------------------------------------------------------
def test_rec20_alert_complete_day_burn():
    scan = _proc(_V64, "SP_ALERT_SCAN_DAILY")
    assert "NULLIF(COUNT(DISTINCT DAY), 0)" in scan          # divide by actual complete days
    assert "AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN" in scan  # today excluded
    assert "SUM(CREDITS_BILLED), 0) / 30" not in scan        # the partial-day /30 form is gone
    assert "COST_CONTRACT_BREACH" in scan


def test_rec20_alert_matches_app_mart_window():
    # the migration alert block and the app mart must use the SAME canonical window
    from app.data import mart_sql
    app_sql = mart_sql.contract_exhaustion()
    scan = _proc(_V64, "SP_ALERT_SCAN_DAILY")
    for frag in ("DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())",
                 "NULLIF(COUNT(DISTINCT DAY), 0)"):
        assert frag in app_sql and frag in scan, frag
