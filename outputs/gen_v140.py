"""Generate V140 (change-impact scan: exclude OVERWATCH's own DBA_MAINT_DB objects).

Re-derives SP_CHANGE_IMPACT_SCAN from V061 by adding ONE DBA_MAINT_DB exclusion to each
registration arm (procedures 1a, tasks 1b). Everything else is byte-identical to V061's proc;
test_v140 proves it. Also emits a one-time cleanup that resolves the open change-impact alerts
for DBA_MAINT_DB objects and drops their registry rows (the existing false positives).

Run: python outputs/gen_v140.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V061 = (MIG / "V061__ai_loader_alert_score_purge_fixes.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


proc = extract_proc(V061, "SP_CHANGE_IMPACT_SCAN()")

# --- Edit 1: procedure registration (1a) — skip OVERWATCH's own procs. -------
_old1 = ("          AND PROCEDURE_CATALOG IS NOT NULL\n"
         "          AND LAST_ALTERED >= DATEADD('day', -3, CURRENT_TIMESTAMP())")
_new1 = ("          AND PROCEDURE_CATALOG IS NOT NULL\n"
         "          AND PROCEDURE_CATALOG <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own procs are self-monitored (freshness + per-loader error log), not change-impact-tracked\n"
         "          AND LAST_ALTERED >= DATEADD('day', -3, CURRENT_TIMESTAMP())")
assert proc.count(_old1) == 1, "edit 1 anchor not unique/found"
proc = proc.replace(_old1, _new1)

# --- Edit 2: task registration (1b) — skip OVERWATCH's own tasks. ------------
_old2 = ("            WHERE CHANGE_SEEN_AT >= DATEADD('day', -3, CURRENT_TIMESTAMP())\n"
         "              AND PREV_DEFINITION IS NOT NULL")
_new2 = ("            WHERE CHANGE_SEEN_AT >= DATEADD('day', -3, CURRENT_TIMESTAMP())\n"
         "              AND DATABASE_NAME <> 'DBA_MAINT_DB'   -- V140: OVERWATCH's own tasks are self-monitored, not change-impact-tracked\n"
         "              AND PREV_DEFINITION IS NOT NULL")
assert proc.count(_old2) == 1, "edit 2 anchor not unique/found"
proc = proc.replace(_old2, _new2)

HEADER = """\
-- V140__change_impact_exclude_self_procs.sql
--
-- Harden the change-impact regression detector so it never tracks OVERWATCH's OWN objects.
-- SP_CHANGE_IMPACT_SCAN registers every changed PROCEDURE/TASK into OBJECT_CHANGE_REGISTRY and
-- alerts when runtime/credits regress after the change. But it also registered OVERWATCH's own
-- maintenance procs -- which get CREATE OR REPLACE'd on every migration AND often run a one-time
-- apply-time backfill CALL. That one heavy call inflates the "after" p95/credits vs the daily
-- baseline, so a correctness fix (e.g. V120's SP_LOAD_PATTERN_COST fanout fix on 2026-09-02, which
-- re-stamped 90 days at apply) trips a false "PROCEDURE ... regressed after <date>" CRITICAL -- the
-- oldest-open critical dragging the responsiveness KPI. OVERWATCH's own plumbing is already
-- monitored the right way (SOURCE_FRESHNESS_STATE staleness + per-loader error logging + the OPS
-- scan-health tally), so change-impact self-tracking is pure noise. (V139's SP_LOAD_OBJECT_COST
-- used the same CREATE-OR-REPLACE + apply-time-backfill pattern, so it would have tripped the
-- identical false alert in a few days -- this prevents that too.)
--
-- Fix: re-derive SP_CHANGE_IMPACT_SCAN from V061 with a single DBA_MAINT_DB exclusion in EACH
-- registration arm (procedures 1a, tasks 1b). Everything else is byte-identical (test_v140 proves
-- proc == V061 modulo those two lines). Then one-time: resolve the open change-impact alerts for
-- DBA_MAINT_DB objects and drop their registry rows so the existing false critical clears. Proc +
-- data cleanup; no schema change. Apply AFTER V139. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20140, 'V140 requires V139 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 139) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_CHANGE_IMPACT_SCAN (from V061 + DBA_MAINT_DB self-exclusion, V140)
"""

CLEANUP = """\

-- One-time: clear the alerts + registry rows the OLD scan raised for OVERWATCH's own objects.
-- These are false positives (see header); the hardened scan above never re-creates them.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'EXPECTED'
 WHERE STATUS IN ('OPEN', 'ACK')
   AND DEDUPE_KEY LIKE 'PERF_CHANGE_REGRESSION|DBA_MAINT_DB.%';

DELETE FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB';
"""

SCHEMA_INSERT = """\

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 140 AS VERSION,
       'Change-impact detector hardening: SP_CHANGE_IMPACT_SCAN no longer registers OVERWATCH''s own DBA_MAINT_DB procedures/tasks into OBJECT_CHANGE_REGISTRY (they are self-monitored via SOURCE_FRESHNESS_STATE + per-loader error logging), so a maintenance-proc redeploy plus a one-time apply-time backfill can no longer trip a false PERF_CHANGE_REGRESSION alert (e.g. SP_LOAD_PATTERN_COST after V120). Re-derived from V061 with a DBA_MAINT_DB exclusion in each registration arm; one-time resolves the open self-object change-impact alerts and drops their registry rows.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 140);
"""

out = HEADER + proc + "\n" + CLEANUP + SCHEMA_INSERT
(MIG / "V140__change_impact_exclude_self_procs.sql").write_text(out, encoding="utf-8")
print("wrote V140; proc chars:", len(proc), "| edit1+edit2 applied")
