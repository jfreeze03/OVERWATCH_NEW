#!/usr/bin/env python3
r"""Forward-generate V071__task_graph_rechain_retry.sql.

DAG surgery on ALREADY-APPLIED tasks (an applied migration can never be edited), fixing a
class of race Snowflake creates by running sibling child tasks in PARALLEL. The graph was
grown by hanging readers off the ROOT loaders instead of chaining them behind the
extract/reconcile that FEED them, so readers race their own data (Codex #3/#4, verified
against the live DAG by grep of the creating migrations):

  HOURLY (root TASK_LOAD_HOURLY):
    TASK_QH_EXTRACT (AFTER root, V041) refreshes FACT_QUERY_HOURLY/DAILY.
      TASK_LOAD_MARTS_V27_HOURLY / TASK_OPS_DIAG_HOURLY already run AFTER it (V041, correct).
    TASK_REFRESH_EXEC_BOARD (AFTER root, V003)  <-- #3 RACE: reads the query facts QH_EXTRACT
    TASK_ALERT_SCAN         (AFTER root, V004)  <-- #3 RACE   refreshes, but runs as its sibling
      TASK_ALERT_NOTIFY     (AFTER TASK_ALERT_SCAN, V018) -- moves with its parent, untouched
  DAILY (root TASK_LOAD_DAILY):
    TASK_NIGHTLY_RECONCILE     (AFTER root, V041) deletes+reloads D-2/D-3 facts.
    TASK_LOAD_MARTS_V27_DAILY  (AFTER root, V027)  <-- #4 RACE: read facts reconcile is
    TASK_PLATFORM_SCORE_DAILY  (AFTER root, V041)  <-- #4 RACE   deleting/reloading, as its
    TASK_ALERT_SCAN_DAILY      (AFTER root, V062)  <-- #4 RACE   siblings

WHAT V071 DOES (only ADD/REMOVE AFTER + SET retry params + a schema widen -- NEVER a
CREATE OR REPLACE TASK, so every existing schedule/warehouse/body is preserved):
  #3  re-point TASK_REFRESH_EXEC_BOARD + TASK_ALERT_SCAN: AFTER TASK_LOAD_HOURLY -> AFTER
      TASK_QH_EXTRACT (they now read the refreshed query facts). NOTIFY trails ALERT_SCAN.
  #4  re-point TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY + TASK_ALERT_SCAN_DAILY:
      AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE (they now read reconciled data).
      Reconcile is NOT internally atomic (Codex #7 deferred -- its child loaders own their own
      transactions/DDL); serializing readers AFTER it is strictly better than racing it and is
      safe. V071 does NOT attempt reconcile atomicity.
  #43 additive retry/auto-suspend policy on the two ROOTS: TASK_AUTO_RETRY_ATTEMPTS = 1,
      SUSPEND_TASK_AFTER_NUM_FAILURES = 10 (set while suspended, in the surgery window).
  #42 a standalone idempotent widen of SCHEMA_VERSION.DESCRIPTION to VARCHAR(4000) at the VERY
      TOP (before the version guard), so any install advancing the chain gets the widen
      unconditionally -- closes the manual-preflight gap.

MECHANISM (the delicate part; idempotent + safe to re-run):
  Re-pointing a task's predecessor requires the task SUSPENDED, and a DAG modification requires
  the root suspended. Per graph we suspend the WHOLE graph (root + every dependent), which is
  what SYSTEM$TASK_DEPENDENTS_ENABLE reverses, then re-point, then resume. Idempotency is a
  STATE CHECK, NOT an error-string guess (we cannot verify Snowflake's exact ALTER TASK
  ADD/REMOVE AFTER error strings from here): each re-point snapshots the task's current
  predecessors from SHOW TASKS and issues ADD only when the new predecessor is ABSENT and
  REMOVE only when the old one is PRESENT. A matched re-run issues NEITHER ALTER, so there is
  no idempotency error to tolerate -- and there is NO exception handler, so ANY genuine ALTER
  failure (busy graph / privilege / not-found) propagates loudly and ABORTS before the
  VERSION 71 insert. ADD runs BEFORE REMOVE (Codex fix A): the graph is suspended so the
  transient two-parent state is inert, and if ADD aborts the task keeps its ORIGINAL
  predecessor (racy = pre-V071 status quo) rather than being orphaned with zero predecessors.
  On abort the graph's trailing RESUME/TASK_DEPENDENTS_ENABLE never runs, so the graph is left
  SUSPENDED -- a loud, obvious outage the owner sees immediately, which self-heals on re-run.
  That is the correct trade for the riskiest migration: loud-suspended beats
  silently-orphaned-but-green. SUSPEND / RESUME stay plain top-level `ALTER TASK IF EXISTS`
  statements (inherently idempotent) + `SYSTEM$TASK_DEPENDENTS_ENABLE(root)` -- the exact
  proven idiom of V041/V027/V062 that this repo hardened against the "children left suspended"
  alert-outage class.

#5 (finalizer / green-on-failure): DEFERRED. Loaders return "WITH ERRORS" strings instead of
  raising, so task history is green on partial failure; the clean fix is a FINALIZE task per
  root. It is a NEW object with its own warehouse + suspend/resume handling, and a correct
  verdict needs correlating APP_ERROR_LOG rows to *this* graph run -- materially more scope and
  risk than a focused, idempotent re-chain. Deferred with TODO(#5); see the report. The #3/#4
  re-chain is the correctness win; #5 is observability.

This migration is DDL/ALTER (not a re-derived proc), so this generator AUTHORS the statements
directly with correctness asserts and byte-writes the .sql -- still the single source of truth
(no hand-edit of the .sql). tests/test_v071_task_graph.py byte-compares (regenerate via
V071_OUT). Idempotent; apply AFTER V070. No new objects.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
FQ = "DBA_MAINT_DB.OVERWATCH."


def _suspend(tasks: list[str]) -> str:
    return "\n".join(f"ALTER TASK IF EXISTS {FQ}{t} SUSPEND;" for t in tasks)


def _resume(children_root_last: list[str], root: str) -> str:
    """Resume children first, root last, then whole-tree enable -- the V041 order. A child
    can be resumed while the root is suspended; enabling the tree last guarantees no
    dependent is stranded suspended (the r07-r12 alert-outage class this repo guards)."""
    lines = [f"ALTER TASK IF EXISTS {FQ}{t} RESUME;" for t in children_root_last]
    lines.append(f"SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('{FQ}{root}');")
    return "\n".join(lines)


def _retry(root: str) -> str:
    return (f"ALTER TASK IF EXISTS {FQ}{root}\n"
            f"    SET TASK_AUTO_RETRY_ATTEMPTS = 1, SUSPEND_TASK_AFTER_NUM_FAILURES = 10;")


def _repoint(task: str, old: str, new: str) -> str:
    """State-checked, ADD-before-REMOVE re-point in ONE scripting block, with NO error
    swallowing.

    Idempotency is a STATE CHECK, not an error-string guess (we cannot verify Snowflake's
    exact ALTER TASK ADD/REMOVE AFTER error strings from here): snapshot the current
    predecessors from SHOW TASKS, then issue ADD only when the new predecessor is ABSENT and
    REMOVE only when the old one is PRESENT. A re-run whose state already matches issues
    NEITHER ALTER, so there is nothing to tolerate -- and because there is no EXCEPTION
    handler, ANY genuine ALTER failure (busy graph / privilege / not-found) propagates loudly
    and ABORTS the migration before the VERSION 71 insert. On abort the graph's trailing
    RESUME/TASK_DEPENDENTS_ENABLE never runs, so the graph is left SUSPENDED -- a loud,
    obvious outage the owner sees at once, which self-heals on re-run. That is the correct
    trade for the riskiest migration: loud-suspended beats silently-orphaned-but-green.

    ADD FIRST, then REMOVE (Codex fix A): a task may hold multiple predecessors transiently
    and the graph is suspended, so the two-parent intermediate state never executes. If ADD
    fails it aborts before REMOVE runs, so the task keeps its ORIGINAL predecessor (racy =
    the pre-V071 status quo) and is NEVER left with zero predecessors + no schedule.

    The membership test is a bare-task-name ILIKE over TO_VARCHAR(predecessors): it is
    format-agnostic (the SHOW predecessors shape -- array vs string, short vs fully-qualified
    -- varies) and collision-free HERE because no predecessor name involved is a substring of
    another (TASK_LOAD_HOURLY vs TASK_QH_EXTRACT; TASK_LOAD_DAILY vs TASK_NIGHTLY_RECONCILE;
    and TASK_LOAD_*_DAILY does not contain 'TASK_LOAD_DAILY')."""
    return (
        f"-- Re-point {task}: AFTER {old} -> AFTER {new}. STATE-CHECKED, ADD-before-REMOVE, no\n"
        f"-- error swallowing: ADD only if {new} is absent, REMOVE only if {old} is present, so\n"
        f"-- a matched re-run is a no-op and any genuine ALTER failure aborts loudly (see header).\n"
        f"EXECUTE IMMEDIATE\n$$\n"
        f"DECLARE\n"
        f"    preds VARCHAR;\n"
        f"BEGIN\n"
        f"    SHOW TASKS LIKE '{task}' IN SCHEMA DBA_MAINT_DB.OVERWATCH;\n"
        f"    SELECT COALESCE(MAX(TO_VARCHAR(\"predecessors\")), '') INTO :preds\n"
        f"      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE \"name\" = '{task}';\n"
        f"    -- ADD the new predecessor first (transient two-parent state is inert while the\n"
        f"    -- graph is suspended); if this fails it aborts before the REMOVE below.\n"
        f"    IF (preds NOT ILIKE '%{new}%') THEN\n"
        f"        ALTER TASK {FQ}{task} ADD AFTER {FQ}{new};\n"
        f"    END IF;\n"
        f"    -- then drop the old predecessor, only if it is still attached.\n"
        f"    IF (preds ILIKE '%{old}%') THEN\n"
        f"        ALTER TASK {FQ}{task} REMOVE AFTER {FQ}{old};\n"
        f"    END IF;\n"
        f"END;\n$$;")


# ---- the two graphs (fully-qualified names verified against the creating migrations) ----
# HOURLY: root + qh + the two already-correct qh children + the two re-pointed readers + notify
_HOURLY_ALL = [
    "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT", "TASK_LOAD_MARTS_V27_HOURLY",
    "TASK_OPS_DIAG_HOURLY", "TASK_REFRESH_EXEC_BOARD", "TASK_ALERT_SCAN", "TASK_ALERT_NOTIFY",
]
# resume children first (deepest last-resumed root), root last
_HOURLY_CHILDREN = [
    "TASK_ALERT_NOTIFY", "TASK_ALERT_SCAN", "TASK_REFRESH_EXEC_BOARD",
    "TASK_OPS_DIAG_HOURLY", "TASK_LOAD_MARTS_V27_HOURLY", "TASK_QH_EXTRACT", "TASK_LOAD_HOURLY",
]
_DAILY_ALL = [
    "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE", "TASK_LOAD_MARTS_V27_DAILY",
    "TASK_PLATFORM_SCORE_DAILY", "TASK_ALERT_SCAN_DAILY",
]
_DAILY_CHILDREN = [
    "TASK_ALERT_SCAN_DAILY", "TASK_PLATFORM_SCORE_DAILY", "TASK_LOAD_MARTS_V27_DAILY",
    "TASK_NIGHTLY_RECONCILE", "TASK_LOAD_DAILY",
]

_hourly = f"""-- ===========================================================================
-- HOURLY graph re-chain (#3). TASK_REFRESH_EXEC_BOARD + TASK_ALERT_SCAN read the query
-- facts TASK_QH_EXTRACT refreshes, but were hung AFTER the root as QH_EXTRACT's SIBLINGS
-- (V003 / V004, pre-dating the V041 extract), so Snowflake ran them in PARALLEL with the
-- extract and they raced their own data. Re-point both to run AFTER TASK_QH_EXTRACT.
-- TASK_ALERT_NOTIFY stays AFTER TASK_ALERT_SCAN and simply moves with its parent.
-- ===========================================================================
-- Suspend the whole hourly graph for the surgery (SUSPEND on a suspended task is a no-op).
{_suspend(_HOURLY_ALL)}

-- #43: additive retry + auto-suspend policy on the HOURLY root (set while suspended).
{_retry("TASK_LOAD_HOURLY")}

{_repoint("TASK_REFRESH_EXEC_BOARD", "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT")}

{_repoint("TASK_ALERT_SCAN", "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT")}

-- Resume the whole hourly tree (children first, root last, then whole-tree enable).
{_resume(_HOURLY_CHILDREN, "TASK_LOAD_HOURLY")}"""

_daily = f"""-- ===========================================================================
-- DAILY graph re-chain (#4). TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY +
-- TASK_ALERT_SCAN_DAILY read facts TASK_NIGHTLY_RECONCILE deletes+reloads for D-2/D-3, but
-- were hung AFTER the root as reconcile's SIBLINGS (V027 / V041 / V062), so they ran in
-- PARALLEL with the reconcile and could read half-reloaded facts. Re-point all three to run
-- AFTER TASK_NIGHTLY_RECONCILE. (Reconcile is not itself atomic -- Codex #7, deferred; its
-- child loaders own their transactions/DDL -- but serializing the readers AFTER it is
-- strictly better than racing it, and safe. V071 does not touch reconcile's body.)
-- ===========================================================================
-- Suspend the whole daily graph for the surgery.
{_suspend(_DAILY_ALL)}

-- #43: additive retry + auto-suspend policy on the DAILY root (set while suspended).
{_retry("TASK_LOAD_DAILY")}

{_repoint("TASK_LOAD_MARTS_V27_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE")}

{_repoint("TASK_PLATFORM_SCORE_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE")}

{_repoint("TASK_ALERT_SCAN_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE")}

-- Resume the whole daily tree (children first, root last, then whole-tree enable).
{_resume(_DAILY_CHILDREN, "TASK_LOAD_DAILY")}"""

out = f"""-- V071__task_graph_rechain_retry.sql
--
-- DAG surgery on already-applied tasks (verified against the live graph, Codex #3/#4/#43/#42).
-- Snowflake runs sibling child tasks in PARALLEL; the graph was grown by hanging readers off
-- the ROOT loaders instead of chaining them behind the extract/reconcile that FEED them, so
-- readers raced their own data. V071 re-points them and hardens the two roots. It only
-- ADD/REMOVE AFTER + SETs retry params -- it NEVER re-defines a task body (no CREATE OR
-- REPLACE TASK), so every existing schedule/warehouse/body is preserved.
--
--   #3  TASK_REFRESH_EXEC_BOARD + TASK_ALERT_SCAN: AFTER TASK_LOAD_HOURLY -> AFTER
--       TASK_QH_EXTRACT (they read the query facts the extract refreshes). NOTIFY trails SCAN.
--   #4  TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY + TASK_ALERT_SCAN_DAILY:
--       AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE (they read reconciled facts,
--       not the ones reconcile is mid-reload on). Reconcile atomicity (#7) stays deferred.
--   #43 additive retry/auto-suspend policy on both roots (TASK_AUTO_RETRY_ATTEMPTS = 1,
--       SUSPEND_TASK_AFTER_NUM_FAILURES = 10), set inside the surgery window.
--   #42 SCHEMA_VERSION.DESCRIPTION widened to VARCHAR(4000) at the very top, before the guard.
--
-- IDEMPOTENT + SAFE TO RE-RUN, via a STATE CHECK (not an error-string guess): each re-point
-- snapshots the task's predecessors from SHOW TASKS and issues ADD only if the new predecessor
-- is absent + REMOVE only if the old one is present, ADD BEFORE REMOVE. A matched re-run
-- issues neither ALTER, and there is NO exception handler, so any genuine ALTER failure (busy
-- graph / privilege) propagates loudly and ABORTS before the VERSION 71 insert. On abort the
-- graph's trailing RESUME/TASK_DEPENDENTS_ENABLE does not run, so the graph is left SUSPENDED
-- -- a loud outage that self-heals on re-run, deliberately chosen over silently orphaning a
-- task (ADD-before-REMOVE also guarantees a failed re-point keeps its original predecessor,
-- never zero). SUSPEND/RESUME are IF EXISTS (idempotent) + SYSTEM$TASK_DEPENDENTS_ENABLE(root)
-- so a clean run leaves no dependent suspended. #5 finalizer DEFERRED (see gen_v071.py header +
-- the owner smoke test in DEPLOYMENT.md). Apply AFTER V070.
--
-- OWNER SMOKE TEST (read-only, REQUIRED -- the re-point + resume are runtime-only; a
-- byte-compare cannot prove the graph resumed, and a genuine abort leaves it suspended):
--   SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
--   -- TASK_REFRESH_EXEC_BOARD.predecessors + TASK_ALERT_SCAN.predecessors = TASK_QH_EXTRACT
--   -- TASK_LOAD_MARTS_V27_DAILY / TASK_PLATFORM_SCORE_DAILY / TASK_ALERT_SCAN_DAILY
--   --   .predecessors = TASK_NIGHTLY_RECONCILE ; and state = 'started' for ALL 12 tasks.
--   -- If any task is `suspended`, re-run V071 (idempotent) or SYSTEM$TASK_DEPENDENTS_ENABLE
--   -- both roots.

-- #42 (very top, before the version guard): widen SCHEMA_VERSION.DESCRIPTION unconditionally,
-- so any install advancing the chain gets the widen even if the manual preflight was skipped.
-- GROW-ONLY: widening a VARCHAR to an equal/greater length is always allowed and idempotent;
-- this is safe to re-run only because it never SHRINKS (were a later migration to widen
-- DESCRIPTION beyond 4000, re-running V071 would be a rejected shrink -- keep this <= any
-- later width).
ALTER TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
    ALTER COLUMN DESCRIPTION SET DATA TYPE VARCHAR(4000);

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20071, 'V071 requires V070 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 70) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{_hourly}

{_daily}

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 71 AS VERSION,
       'Task-graph re-chain + root retry policy (DAG surgery on applied tasks): Snowflake runs sibling child tasks in parallel, and readers were hung off the roots as siblings of the extract/reconcile that feed them, so they raced their own data. TASK_REFRESH_EXEC_BOARD and TASK_ALERT_SCAN re-pointed AFTER TASK_LOAD_HOURLY -> AFTER TASK_QH_EXTRACT so they read the refreshed query facts (#3); TASK_LOAD_MARTS_V27_DAILY, TASK_PLATFORM_SCORE_DAILY and TASK_ALERT_SCAN_DAILY re-pointed AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE so they read reconciled data rather than racing the delete+reload (#4; reconcile atomicity #7 stays deferred). Both roots gain TASK_AUTO_RETRY_ATTEMPTS=1 + SUSPEND_TASK_AFTER_NUM_FAILURES=10 (#43). SCHEMA_VERSION.DESCRIPTION widened to VARCHAR(4000) at the top before the guard (#42). Only ADD/REMOVE AFTER + SET retry params -- no task body re-defined, so schedules/warehouses/bodies are preserved; each re-point is state-checked (ADD only if the new predecessor is absent, REMOVE only if the old is present, ADD before REMOVE) with no error swallowing, so a matched re-run is a no-op and any genuine ALTER failure aborts loudly leaving the graph suspended rather than silently orphaned, and a clean run ends each graph with SYSTEM$TASK_DEPENDENTS_ENABLE. Green-on-failure finalizer (#5) deferred. No new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 71);
"""

# ---- correctness assertions on the generated migration ----
# #42 schema widen is at the very top, ABOVE the version guard
assert "ALTER COLUMN DESCRIPTION SET DATA TYPE VARCHAR(4000)" in out, "#42 widen present"
assert out.index("SET DATA TYPE VARCHAR(4000)") < out.index("EXCEPTION (-20071"), \
    "#42 widen precedes the version guard"
# version guard (-20071, v < 70) + registry insert 71
assert "EXCEPTION (-20071" in out and "IF (v < 70) THEN" in out, "version guard"
assert "SELECT 71 AS VERSION" in out and "WHERE VERSION = 71)" in out, "registry insert"
# NEVER a task-body re-definition -- bodies/schedules/warehouses are preserved
assert "CREATE OR REPLACE TASK" not in out, "V071 never re-defines a task body"
assert "CREATE TASK" not in out, "V071 creates no task (no new objects; #5 finalizer deferred)"
for ddl in ("CREATE TABLE", "CREATE TRANSIENT TABLE", "CREATE VIEW",
            "CREATE OR REPLACE VIEW", "CREATE STREAM", "CREATE OR REPLACE PROCEDURE"):
    assert ddl not in out, f"no new objects: {ddl}"
# #3 + #4: exactly five re-points, each = one ADD AFTER + one REMOVE AFTER (executable form)
assert out.count("REMOVE AFTER DBA_MAINT_DB.OVERWATCH.") == 5, "5 REMOVE AFTER (2 hourly + 3 daily)"
assert out.count("ADD AFTER DBA_MAINT_DB.OVERWATCH.") == 5, "5 ADD AFTER (2 hourly + 3 daily)"


def _add(task: str, new: str) -> str:
    return f"ALTER TASK {FQ}{task} ADD AFTER {FQ}{new};"


def _remove(task: str, old: str) -> str:
    return f"ALTER TASK {FQ}{task} REMOVE AFTER {FQ}{old};"


# #3 the hourly readers re-point onto the extract; #4 the three daily readers onto the reconcile
_REPOINTS = [
    ("TASK_REFRESH_EXEC_BOARD", "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT"),
    ("TASK_ALERT_SCAN", "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT"),
    ("TASK_LOAD_MARTS_V27_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE"),
    ("TASK_PLATFORM_SCORE_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE"),
    ("TASK_ALERT_SCAN_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE"),
]
for task, old, new in _REPOINTS:
    assert _add(task, new) in out, f"{task}: ADD AFTER {new}"
    assert _remove(task, old) in out, f"{task}: REMOVE AFTER {old}"
    # Codex fix A: ADD runs BEFORE REMOVE, so a failed ADD never orphans the task
    assert out.index(_add(task, new)) < out.index(_remove(task, old)), f"{task}: ADD must precede REMOVE"
    # both ALTERs are STATE-GATED -- ADD only when new is absent, REMOVE only when old is present
    assert f"IF (preds NOT ILIKE '%{new}%') THEN\n        {_add(task, new)}" in out, f"{task}: ADD gated"
    assert f"IF (preds ILIKE '%{old}%') THEN\n        {_remove(task, old)}" in out, f"{task}: REMOVE gated"
# idempotency is a STATE CHECK, not an error-string guess: NO exception swallowing anywhere
assert "WHEN OTHER" not in out, "no error swallowing -- genuine ALTER failures must abort loudly"
assert "does not exist" not in out, "no error-string matching (state check gates instead)"
# each re-point reads the live predecessors via the repo's SHOW + RESULT_SCAN idiom
assert out.count("SHOW TASKS LIKE '") == 5, "one predecessor snapshot per re-point"
assert out.count("RESULT_SCAN(LAST_QUERY_ID())") == 5, "read the SHOW output per re-point"
assert out.count("EXECUTE IMMEDIATE\n$$") == 6, "version guard + 5 state-checked re-point blocks"
# #43 retry policy on both roots, set while suspended
assert out.count("SET TASK_AUTO_RETRY_ATTEMPTS = 1, SUSPEND_TASK_AFTER_NUM_FAILURES = 10;") == 2, \
    "#43 retry policy on both roots"
assert ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY\n"
        "    SET TASK_AUTO_RETRY_ATTEMPTS = 1") in out, "#43 on the hourly root"
assert ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY\n"
        "    SET TASK_AUTO_RETRY_ATTEMPTS = 1") in out, "#43 on the daily root"
# both graphs are resumed via the whole-tree enable (no dependent left suspended)
assert out.count("SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');") == 1
assert out.count("SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');") == 1
# NOTIFY is only ever suspended/resumed -- never re-pointed (it moves with ALERT_SCAN)
assert "REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN;" not in out, "notify's link is untouched"
assert out.count("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY SUSPEND;") == 1
# the two already-correct QH children are suspended/resumed for the surgery but NOT re-pointed
for t in ("TASK_LOAD_MARTS_V27_HOURLY", "TASK_OPS_DIAG_HOURLY"):
    assert f"ADD AFTER DBA_MAINT_DB.OVERWATCH.{t}" not in out, f"{t} is not a new predecessor"
    assert f"REMOVE AFTER DBA_MAINT_DB.OVERWATCH.{t}" not in out, f"{t} link untouched"

target = Path(os.environ.get("V071_OUT") or (MIG / "V071__task_graph_rechain_retry.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
