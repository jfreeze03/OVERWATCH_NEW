#!/usr/bin/env python3
r"""Forward-generate V068__standalone_mart_freshness_stamps.sql.

Live-screenshot finding (2026-07-31): the Brief "stalest telemetry" card showed
MART_LOCK_WAIT_DAILY 448.5h (~18.7 days) stale. Diagnosis (adversarial trace): the loader
chain is INTACT — the freshness STAMP is orphaned. V041 retired the V040 10-minute
SP_SNAPSHOT_FRESHNESS sweep (which refreshed every SOURCE_FRESHNESS_STATE row) and moved
stamping into each loader — but only the loaders it touched. The two marts with their OWN
standalone tasks were never given a stamp:

  MART_LOCK_WAIT_DAILY    (SP_LOAD_LOCK_WAIT_MART / TASK_LOCK_WAIT_DAILY, V035)
  MART_PATTERN_COST_DAILY (SP_LOAD_PATTERN_COST  / TASK_PATTERN_COST_DAILY, V036/37/47)

Their freshness rows froze at apply/rebuild time (~2026-07-09, exactly 448.5h before the
screenshot; pattern-cost is equally frozen but hidden — the card shows only the worst
source). Stacked second defect: any stamp derived from MAX(LOAD_TS) OF THE MART reads
"stale" forever on an account with zero lock waits (no-news-vs-no-load ambiguity), and
V061's purge could even empty the mart back to "never loaded".

Fix — both procs re-derived from their LATEST defs (SP_LOAD_LOCK_WAIT_MART from V035,
SP_LOAD_PATTERN_COST from V047) with the standard V041-R6 loader-owned freshness MERGE
appended before RETURN, with one deliberate difference: LAST_LOAD_TS = CURRENT_TIMESTAMP()
(a RUN stamp — "the loader ran and observed its source through now"), NOT MAX(LOAD_TS)
(a row stamp), so zero-event windows read fresh, as they are. The migration tail CALLs both
procs once so the two rows heal immediately instead of waiting for the next task fire.

Derivation law (see gen_v061..67): tests/test_v068_*.py byte-compares (regenerate via
V068_OUT). Idempotent; apply AFTER V067. No new objects.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def _apply(body: str, old: str, new: str, count: int, name: str) -> str:
    n = body.count(old)
    assert n == count, f"{name}: needle x{n} (want {count}): {old[:70]!r}"
    return body.replace(old, new)


def derive(name: str) -> str:
    base, edits = EDITS[name]
    body = extract_proc(base, name)
    for old, new, cnt in edits:
        body = _apply(body, old, new, cnt, name)
    for g in GUARDS.get(name, []):
        assert g in body, f"{name}: guardrail vanished: {g[:70]!r}"
    return body


def _stamp(source: str, mart: str) -> str:
    """The V041-R6 loader-owned freshness MERGE, with a RUN stamp (see header)."""
    return f"""
    -- V068: loader-owned freshness stamp (V041-R6 pattern; this standalone-task loader
    -- was missed in the V041 handoff, freezing its SOURCE_FRESHNESS_STATE row at apply
    -- time). LAST_LOAD_TS is a RUN stamp (CURRENT_TIMESTAMP()), not MAX(LOAD_TS) of the
    -- mart, so a window with ZERO source events still reads fresh - no news is not
    -- no load.
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT '{source}' AS SOURCE_NAME, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS,
               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.{mart}) AS ROW_COUNT
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');
    RETURN 'OK';"""


_TAIL = "    RETURN 'OK';"

EDITS = {
    'SP_LOAD_LOCK_WAIT_MART': ("V035__lock_wait_mart.sql", [
        (_TAIL, _stamp("MART_LOCK_WAIT_DAILY", "MART_LOCK_WAIT_DAILY"), 1),
    ]),
    'SP_LOAD_PATTERN_COST': ("V047__pattern_cost_qas.sql", [
        (_TAIL, _stamp("MART_PATTERN_COST_DAILY", "MART_PATTERN_COST_DAILY"), 1),
    ]),
}

GUARDS = {
    'SP_LOAD_LOCK_WAIT_MART': [
        "MART_LOCK_WAIT_DAILY", "LOCK_WAIT_HISTORY",
    ],
    'SP_LOAD_PATTERN_COST': [
        "MART_PATTERN_COST_DAILY", "QUERY_ATTRIBUTION_HISTORY",
        # V047's QAS credits must survive
        "CREDITS_USED_QUERY_ACCELERATION",
    ],
}

lockwait = derive("SP_LOAD_LOCK_WAIT_MART")
pattern = derive("SP_LOAD_PATTERN_COST")

# ---- correctness assertions ----
for name, body in (("lock_wait", lockwait), ("pattern", pattern)):
    assert body.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE") == 1, f"{name}: one stamp"
    assert body.count("RETURN 'OK';") == 1, f"{name}: single RETURN (stamp inserted before it)"
    assert "CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS LAST_LOAD_TS" in body, f"{name}: RUN stamp, not MAX(LOAD_TS)"
    assert body.index("SOURCE_FRESHNESS_STATE") < body.index("RETURN 'OK';"), f"{name}: stamp before RETURN"
assert "'MART_LOCK_WAIT_DAILY' AS SOURCE_NAME" in lockwait
assert "'MART_PATTERN_COST_DAILY' AS SOURCE_NAME" in pattern

out = f"""-- V068__standalone_mart_freshness_stamps.sql
--
-- Live finding (Brief card screenshot 2026-07-31): MART_LOCK_WAIT_DAILY read 448.5h stale.
-- The loader chain is fine - the freshness STAMP was orphaned: V041 retired the 10-minute
-- SP_SNAPSHOT_FRESHNESS sweep and moved stamping into each loader, but the two marts with
-- their OWN standalone tasks (lock-wait V035, pattern-cost V036/37/47) never got one, so
-- their SOURCE_FRESHNESS_STATE rows froze at apply time. Both loaders are re-derived from
-- their latest defs (V035 / V047) via outputs/gen_v068.py with the standard V041-R6
-- freshness MERGE appended - stamping LAST_LOAD_TS = CURRENT_TIMESTAMP() (a RUN stamp) so
-- an account with zero lock waits reads FRESH, not stale (no-news is not no-load).
-- The tail CALLs both procs once so the rows heal immediately.
-- Idempotent; apply AFTER V067. No new objects.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20068, 'V068 requires V067 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 67) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_LOCK_WAIT_MART  (freshness run-stamp appended)
{lockwait}
-- >>> derived:SP_LOAD_PATTERN_COST  (freshness run-stamp appended)
{pattern}
-- Heal the two frozen rows now (cheap 3-day windows - the same cadence their tasks use);
-- the next scheduled task fires keep them current from here on.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_LOCK_WAIT_MART(3);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(3);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 68 AS VERSION,
       'Standalone-mart freshness stamps (screenshot finding): SP_LOAD_LOCK_WAIT_MART + SP_LOAD_PATTERN_COST gain the V041-R6 loader-owned SOURCE_FRESHNESS_STATE stamp they were never given when V041 retired the snapshot sweep - their rows had been frozen since apply time (the Brief card showed MART_LOCK_WAIT_DAILY 448h stale). Stamped as a RUN timestamp so zero-event windows read fresh; migration tail heals both rows immediately. Re-derived from V035/V047; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 68);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 2, "two procs re-defined"
assert "CREATE TABLE" not in out and "CREATE TASK" not in out, "no new objects"
assert "EXCEPTION (-20068" in out and "IF (v < 67) THEN" in out, "version guard"
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_") == 2, "both heal CALLs present"

target = Path(os.environ.get("V068_OUT") or (MIG / "V068__standalone_mart_freshness_stamps.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
