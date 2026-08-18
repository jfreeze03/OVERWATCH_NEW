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

_ROOT = Path(__file__).resolve().parents[2]
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
    # #26 adds exactly one control table (OW_SENDER_LEASE, the single-flight lease);
    # no other new objects. A control table is the only robust way to express the guard.
    assert _V64.count("CREATE TABLE") == 1 and "OW_SENDER_LEASE" in _V64
    assert "CREATE TASK" not in _V64


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
    # the expired-detection JOINs ALERT_CONFIG + ALERT_ROUTES and mirrors the send
    # eligibility (family + company + severity), so "flagged expired" == "was eligible
    # to that route but unsent"
    tail = wh.split("Loud, not silent", 1)[1]
    assert "JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID" in tail
    assert "r2.FAMILY = 'ALL' OR c.FAMILY = r2.FAMILY" in tail
    assert "r2.MIN_SEVERITY" in tail
    assert "undelivered_expired" in wh                # the loud tail survives
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in wh   # 24h window kept


# ---------------------------------------------------------------------------
# #27 — PER-ROUTE expiry: a pair is expired when past 24h AND undelivered to THAT
#       route (NOT EXISTS on event+route), not keyed on ALERT_EVENTS.NOTIFIED_AT
#       (which is stamped after the FIRST route delivers, hiding route-B expiry).
# ---------------------------------------------------------------------------
def test_27_per_route_expiry_not_notified_at():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    tail = wh.split("Loud, not silent", 1)[1]
    # the expired tail no longer keys on the event-level NOTIFIED_AT flag
    assert "NOTIFIED_AT IS NULL" not in tail
    # it enumerates the eligible routes and flags a pair with NO delivery for THAT route
    assert "JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r2\n      ON r2.ENABLED" in tail
    assert ("NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n"
            "                      WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = r2.ROUTE_ID)") in tail
    # 24h window + 7d floor preserved
    assert "e.RAISED_AT < DATEADD('hour', -24, CURRENT_TIMESTAMP())" in tail
    assert "e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())" in tail


# ---------------------------------------------------------------------------
# #40 — expired-log inflation: each (event,route) episode is logged at most once
#       per 24h, so a persistent backlog can't make a 30d KPI count RUNS not events.
# ---------------------------------------------------------------------------
def test_40_expired_log_once_per_episode():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    tail = wh.split("Loud, not silent", 1)[1]
    # gated on NOT EXISTS a prior 'undelivered_expired' row with the same event/route
    # key (CONTEXT) inside a 24h window
    assert ("NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG a\n"
            "                      WHERE a.ERROR_TYPE = 'undelivered_expired'") in tail
    assert "AND a.LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())" in tail
    # the same (event,route) key is both WRITTEN as CONTEXT and MATCHED in the guard
    assert tail.count("'event ' || e.EVENT_ID || ' route ' || r2.ROUTE_ID") == 2
    # per-pair insert, no aggregate single-row count form
    assert "open event(s) aged past the 24h delivery window with no successful send" not in tail


# ---------------------------------------------------------------------------
# #26 — single-flight guard: acquire a sentinel lease at entry, bail if held,
#       release at exit. Send-then-ledger ordering is intentionally preserved.
# ---------------------------------------------------------------------------
def test_26_single_flight_lease():
    # the lease control table is created (idempotent) in the migration body
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE" in _V64
    assert "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE" in _V64  # idempotent seed
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    # acquire at entry: UPDATE the sentinel, bail if not acquired.
    # V064 #9 adds the outer-handler fenced release, so there are now THREE lease
    # references: acquire + normal-path release + outer-handler release.
    assert wh.count("DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE") == 3   # acquire + 2 releases
    assert "HELD = FALSE OR ACQUIRED_AT < DATEADD('hour', -1, CURRENT_TIMESTAMP())" in wh  # stale reclaim
    assert "IF (SQLROWCOUNT = 0) THEN\n        RETURN 'skipped - another SP_NOTIFY_WEBHOOK run holds the sender lease';" in wh
    # release at exit — FENCED to the holding session (review): an unconditional release
    # would let a >1h-stalled run clear a successor's reclaimed lease and permit two
    # concurrent senders. The fence makes release a no-op unless we still hold the lease.
    assert "SET HELD = FALSE, HOLDER = NULL" in wh
    assert ("WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'\n"
            "       AND HOLDER = CURRENT_SESSION();") in wh
    # ordering is still send-first (the intentional at-least-once paging bias)
    assert wh.index("SYSTEM$SEND_SNOWFLAKE_NOTIFICATION") < wh.index(
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES")


# ---------------------------------------------------------------------------
# #9 — OUTER LEASE HANDLER: an unhandled error mid-proc must fenced-release the
#      sender lease, log the error, and RE-RAISE (not leave HELD=TRUE until the 1h
#      reclaim, and not swallow). The normal-path release stays (idempotent).
# ---------------------------------------------------------------------------
def test_9_outer_lease_handler_releases_and_reraises():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    # the proc body is wrapped by an outer EXCEPTION WHEN OTHER
    assert "EXCEPTION\n    -- V064 #9 OUTER LEASE HANDLER" in wh
    assert "WHEN OTHER THEN" in wh
    # THREE lease references now: acquire + normal release + outer-handler release
    assert wh.count("DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE") == 3
    # BOTH releases are fenced to the holding session (no-op unless we still hold it)
    assert wh.count("HOLDER = CURRENT_SESSION();") == 2
    # the handler logs the unhandled error and RE-RAISEs so the task run FAILs
    assert "'NotifyWebhook', 'webhook_run_failed', :emsg," in wh
    assert wh.count("RAISE;") == 1
    assert wh.rstrip().endswith("RAISE;\nEND;\n$$;")
    # the inner per-route send handler is unchanged (send failure caught, not re-raised)
    assert "send_failed := TRUE;" in wh


# ---------------------------------------------------------------------------
# #10-slice — undelivered CRITICALs get a 7d send window (24h floor for the rest).
#      Cheap slice only: oldest-first drain + per-route NOT EXISTS + expiry
#      watchdog all stay intact; no dead-letter/replay machine.
# ---------------------------------------------------------------------------
def test_10_critical_seven_day_send_window():
    wh = _proc(_V64, "SP_NOTIFY_WEBHOOK")
    assert ("AND e.RAISED_AT >= CASE WHEN e.SEVERITY = 'CRITICAL'\n"
            "                                          THEN DATEADD('day', -7, CURRENT_TIMESTAMP())\n"
            "                                          ELSE DATEADD('hour', -24, CURRENT_TIMESTAMP()) END") in wh
    # the drain machinery is untouched
    assert "LOOP" in wh and "max_batches INT DEFAULT 6" in wh
    assert wh.count("ARRAY_AGG(f.EVENT_ID)") == 1
    # the expiry watchdog + its 24h/7d bounds still exist
    tail = wh.split("Loud, not silent", 1)[1]
    assert "undelivered_expired" in tail
    assert "e.RAISED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())" in tail


# ---------------------------------------------------------------------------
# #6 — reconcile RUN scoping: the logged-failure count is fenced to the reconcile's
#      OWN child-loader pages, so a concurrent unrelated failure is not counted.
# ---------------------------------------------------------------------------
def test_6_reconcile_failure_count_scoped_to_own_pages():
    rec = _proc(_V64, "SP_NIGHTLY_RECONCILE")
    assert "AND PAGE IN ('ExtractLoader', 'DailyFacts', 'MartLoader')" in rec
    # still scoped by time + the children's failure tokens
    assert "WHERE LOGGED_AT >= :recon_start" in rec
    assert "ERROR_TYPE ILIKE '%_failed%'" in rec
    # unrelated pages that also log '%_failed%' are NOT in the scope
    assert "'AlertScan'" not in rec.split("SELECT COUNT(*) INTO :logged_fails", 1)[1].split(";", 1)[0]


# ---------------------------------------------------------------------------
# #7 (DEFERRED) + #4-drop (KEEP) — atomicity deferral is flagged in-proc; the
#      daily re-call is documented as the re-cover mechanism, not a redundant dup.
# ---------------------------------------------------------------------------
def test_7_atomicity_deferred_and_4drop_kept():
    rec = _proc(_V64, "SP_NIGHTLY_RECONCILE")
    assert "TODO(#7 staging-swap)" in rec
    assert "V064 #4-drop ANALYSIS (KEEP -- verified NOT redundant)" in rec
    # the daily loader re-call is STILL present (kept, not dropped)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();" in rec


# ---------------------------------------------------------------------------
# #9 — reconcile captures each child loader's return and reports a verdict.
# ---------------------------------------------------------------------------
def test_9_reconcile_reports_child_verdicts():
    rec = _proc(_V64, "SP_NIGHTLY_RECONCILE")
    # each of the 5 child CALLs has its return captured (secondary signal)
    assert rec.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_") == 5
    assert rec.count("SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));") == 5
    assert rec.count("fails := fails + 1;") == 5
    assert "rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%'" in rec
    # AUTHORITATIVE signal (review): return-string parsing catches only the two
    # machine-verdict children; the other three SWALLOW arm failures and return benign
    # 'loaded' strings. So the verdict must count APP_ERROR_LOG '%_failed%' rows written
    # DURING this run (scoped to recon_start), which every child writes on an arm failure.
    assert "recon_start := CURRENT_TIMESTAMP();" in rec
    assert "SELECT COUNT(*) INTO :logged_fails" in rec
    assert "WHERE LOGGED_AT >= :recon_start" in rec
    assert "ERROR_TYPE ILIKE '%_failed%'" in rec
    # either signal (a machine verdict OR a logged failure) flags the run
    assert "IF (fails > 0 OR logged_fails > 0) THEN" in rec
    # machine-readable parent verdict: OK vs WITH ERRORS
    assert "RETURN 'RECONCILE WITH ERRORS: ' || :logged_fails ||" in rec
    assert "RETURN 'RECONCILE OK" in rec
    # the DAILY_FACTS child call (the rec7 lockstep guard) survives
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_DAILY_FACTS();" in rec


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
