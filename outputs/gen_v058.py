#!/usr/bin/env python3
"""Forward-generate V058__task_node_timing.sql — per-node loader-timing observability.

Performance round, T3 (reconcile-scheduling) — the OBSERVABILITY prerequisite.

The reconcile-scheduling changes (serialize the 5-way daily fan-out, retarget the
nightly reconcile, de-collide the 06:30-06:50 root tasks) cannot be safely sized
from code alone: MART_TASK_GRAPH_DAILY stores only per-PIPELINE AVG/P95_WALL_SEC
and DISCARDS SCHEDULED_TIME, so the per-node queue delay and exec duration that
would quantify the XSMALL contention and size a serialized chain do not exist
anywhere. V058 produces that signal, then those changes become data-driven.

V058 is additive + contained:
  * NEW table MART_TASK_NODE_DAILY (grain DAY x DATABASE x SCHEMA x TASK_NAME):
    RUNS, FAILED, AVG/P95/MAX_QUEUE_SEC (SCHEDULED_TIME->QUERY_START_TIME dispatch
    delay), AVG/P95/MAX_EXEC_SEC (QUERY_START_TIME->COMPLETED_TIME), FIRST_START,
    LAST_COMPLETED, LOAD_TS.
  * SP_LOAD_MARTS_V27 re-derived from its CURRENT (V057) definition with ONE
    enumerated edit: a NEW standalone guarded arm [6b] inserted after the existing
    task-graph arm [6], scanning TASK_HISTORY once at the same -:d window and
    MERGE-ing per-node rows. It touches NO existing statement (every other arm and
    the end-of-HOURLY freshness MERGE stay byte-identical) and has its own
    BEGIN...EXCEPTION so a fault is contained to this arm.

NO task-graph surgery: CREATE OR REPLACE PROCEDURE does not require suspending the
calling task, and no SCHEDULE / AFTER edge changes. The table fills on the existing
hourly cadence (TASK_LOAD_MARTS_V27_HOURLY calls 'HOURLY',2; reconcile 'HOURLY',3),
both of which reach back over that morning's 06:40/06:45 runs.

Per-node CREDITS are deliberately NOT included (arm [6] already carries pipeline-
grain WH_CREDITS); queue + exec timing is the signal the deferred schedule work
needs. Adding per-node credits (a QUERY_ATTRIBUTION_HISTORY join like arm [6]'s)
is a clean fast-follow if the reconcile-retargeting analysis needs it.

Derivation law: SP_LOAD_MARTS_V27 re-derived from V057 (the LATEST def — V057
re-derived it from V056 with the four EXECUTION_STATUS 'FAILED'->'FAIL' fixes, which
this edit preserves) + the single arm insertion; tests/test_v058_task_node.py
byte-compares and asserts the four '= FAIL' tokens survive.
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
    matches = pat.findall(text)
    assert matches, (path, name)
    return matches[-1]


def apply_once(body: str, old: str, new: str, name: str) -> str:
    n = body.count(old)
    assert n == 1, f"{name}: needle x{n} (want 1): {old[:70]!r}"
    return body.replace(old, new)


# --- the new arm [6b]: per-node task timing, inserted after arm [6] (task-graph).
# Own guarded BEGIN...EXCEPTION; MERGE keyed on (DAY, DATABASE_NAME, SCHEMA_NAME,
# TASK_NAME) so it is idempotent under the hourly + reconcile re-runs.
NEW_ARM = """
        -- [6b] per-node task timing (queue + exec delay) -> MART_TASK_NODE_DAILY
        -- Observability for the deferred reconcile-scheduling work: the
        -- SCHEDULED_TIME->QUERY_START_TIME dispatch delay (which the pipeline-grain
        -- arm [6] discards) quantifies the 06:40/06:45 XSMALL contention. Own
        -- guarded arm; touches no existing statement; one TASK_HISTORY scan at the
        -- same -:d window; MERGE on (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME).
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY t
            USING (
                SELECT DATE(QUERY_START_TIME) AS DAY,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       NAME AS TASK_NAME,
                       COUNT(*) AS RUNS,
                       COUNT_IF(STATE = 'FAILED') AS FAILED,
                       ROUND(AVG(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS AVG_QUEUE_SEC,
                       ROUND(APPROX_PERCENTILE(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0), 0.95) / 1000, 2) AS P95_QUEUE_SEC,
                       ROUND(MAX(GREATEST(DATEDIFF('millisecond', SCHEDULED_TIME, QUERY_START_TIME), 0)) / 1000, 2) AS MAX_QUEUE_SEC,
                       ROUND(AVG(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS AVG_EXEC_SEC,
                       ROUND(APPROX_PERCENTILE(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME), 0.95) / 1000, 2) AS P95_EXEC_SEC,
                       ROUND(MAX(DATEDIFF('millisecond', QUERY_START_TIME, COMPLETED_TIME)) / 1000, 2) AS MAX_EXEC_SEC,
                       MIN(QUERY_START_TIME) AS FIRST_START,
                       MAX(COMPLETED_TIME) AS LAST_COMPLETED
                FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND STATE IN ('SUCCEEDED', 'FAILED')
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.TASK_NAME = s.TASK_NAME
               AND COALESCE(t.DATABASE_NAME, '') = COALESCE(s.DATABASE_NAME, '')
               AND COALESCE(t.SCHEMA_NAME, '') = COALESCE(s.SCHEMA_NAME, '')
            WHEN MATCHED THEN UPDATE SET
                RUNS = s.RUNS, FAILED = s.FAILED,
                AVG_QUEUE_SEC = s.AVG_QUEUE_SEC, P95_QUEUE_SEC = s.P95_QUEUE_SEC, MAX_QUEUE_SEC = s.MAX_QUEUE_SEC,
                AVG_EXEC_SEC = s.AVG_EXEC_SEC, P95_EXEC_SEC = s.P95_EXEC_SEC, MAX_EXEC_SEC = s.MAX_EXEC_SEC,
                FIRST_START = s.FIRST_START, LAST_COMPLETED = s.LAST_COMPLETED, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, RUNS, FAILED,
                 AVG_QUEUE_SEC, P95_QUEUE_SEC, MAX_QUEUE_SEC,
                 AVG_EXEC_SEC, P95_EXEC_SEC, MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED)
            VALUES (s.DAY, s.DATABASE_NAME, s.SCHEMA_NAME, s.TASK_NAME, s.RUNS, s.FAILED,
                    s.AVG_QUEUE_SEC, s.P95_QUEUE_SEC, s.MAX_QUEUE_SEC,
                    s.AVG_EXEC_SEC, s.P95_EXEC_SEC, s.MAX_EXEC_SEC, s.FIRST_START, s.LAST_COMPLETED);
            loaded := loaded || 'task_node ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_TASK_NODE_DAILY - other marts unaffected', CURRENT_ROLE();
        END;
"""

_ANCHOR = (
    "                SELECT 'MartLoader', 'mart_load_failed', :emsg, "
    "'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();\n"
    "        END;\n"
    "\n"
    "        -- [8] incident timeline (rolling 48h window rebuild) -----------------")

marts = apply_once(
    extract_proc("V057__fail_status_token.sql", "SP_LOAD_MARTS_V27"),
    _ANCHOR,
    _ANCHOR.replace(
        "        END;\n\n        -- [8] incident timeline",
        "        END;\n" + NEW_ARM + "\n        -- [8] incident timeline"),
    "SP_LOAD_MARTS_V27")

# guardrails (critique must-fixes): V057's FAILED->FAIL fix survives, signature
# unchanged, the new arm is present and self-contained, no existing arm harmed.
assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4, "V057 FAIL fix must survive"
assert "EXECUTION_STATUS = 'FAILED'" not in marts, "the dead 'FAILED' token must not reappear"
assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts, "signature must be unchanged"
assert marts.count("MART_TASK_NODE_DAILY - other marts unaffected") == 1, "new arm has its own guard"
assert marts.count("loaded := loaded || 'graphs ';") == 1, "arm [6] untouched"
assert marts.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY") == 1


out = f"""-- V058__task_node_timing.sql — per-node loader-timing observability (perf T3).
--
--   The reconcile-scheduling changes (serialize the daily fan-out, retarget the
--   nightly reconcile, de-collide the 06:30-06:50 root tasks) cannot be sized
--   safely from code alone: MART_TASK_GRAPH_DAILY keeps only per-PIPELINE wall
--   time and discards SCHEDULED_TIME. V058 adds the missing per-NODE signal so
--   those changes become data-driven, and touches NO task schedule or edge.
--
--   Additive + contained:
--     * NEW MART_TASK_NODE_DAILY (DAY x DATABASE x SCHEMA x TASK_NAME): RUNS,
--       FAILED, AVG/P95/MAX_QUEUE_SEC (SCHEDULED_TIME->QUERY_START_TIME dispatch
--       delay), AVG/P95/MAX_EXEC_SEC, FIRST_START, LAST_COMPLETED, LOAD_TS.
--     * SP_LOAD_MARTS_V27 re-derived from V057 with ONE new standalone guarded
--       arm [6b] after the task-graph arm [6]; one TASK_HISTORY scan at the same
--       -:d window; every existing statement byte-identical.
--
--   No task-graph surgery (CREATE OR REPLACE PROCEDURE needs no SUSPEND); the
--   table fills on the existing hourly cadence. Idempotent; safe to re-run.
--   Apply AFTER V057.
--
-- Derivation law: SP_LOAD_MARTS_V27 re-derived from its CURRENT (V057) definition
-- + the single arm insertion; the test byte-compares and asserts V057's
-- 'FAILED'->'FAIL' fix survives.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20058, 'V058 requires V057 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 57) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- New per-node timing mart. Additive: no existing table/task/edge is altered.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY (
    DAY DATE NOT NULL,
    DATABASE_NAME VARCHAR(200),
    SCHEMA_NAME VARCHAR(200),
    TASK_NAME VARCHAR(300) NOT NULL,
    RUNS NUMBER(12,0),
    FAILED NUMBER(12,0),
    AVG_QUEUE_SEC NUMBER(18,2),
    P95_QUEUE_SEC NUMBER(18,2),
    MAX_QUEUE_SEC NUMBER(18,2),
    AVG_EXEC_SEC NUMBER(18,2),
    P95_EXEC_SEC NUMBER(18,2),
    MAX_EXEC_SEC NUMBER(18,2),
    FIRST_START TIMESTAMP_NTZ,
    LAST_COMPLETED TIMESTAMP_NTZ,
    LOAD_TS TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- >>> derived:SP_LOAD_MARTS_V27
{marts}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 58 AS VERSION,
       'Per-node loader-timing observability (perf T3): MART_TASK_NODE_DAILY (queue + exec delay per task node) + a new contained arm in SP_LOAD_MARTS_V27; enables data-driven fan-out serialization / reconcile / de-collision. No task-graph change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 58);
"""
target = Path(os.environ.get("V058_OUT") or (MIG / "V058__task_node_timing.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
