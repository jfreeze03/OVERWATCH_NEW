"""Locks for V071 — task-graph re-chain + root retry policy (DAG surgery on applied tasks).

Snowflake runs sibling child tasks in PARALLEL, and readers were hung off the ROOTS as
siblings of the extract/reconcile that FEED them, so they raced their own data. V071
re-points them with ALTER TASK ADD/REMOVE AFTER (never a CREATE OR REPLACE TASK, so every
schedule/warehouse/body is preserved), sets an additive retry policy on both roots, and
widens SCHEMA_VERSION.DESCRIPTION at the top. Idempotency is a STATE CHECK, not an
error-string guess: each re-point snapshots the task's predecessors from SHOW TASKS and issues
ADD (before REMOVE) only when the new predecessor is absent + REMOVE only when the old is
present, with NO exception swallowing — so a matched re-run is a no-op and any genuine ALTER
failure aborts loudly (leaving the graph suspended) rather than silently orphaning a task.

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V71 = (_MIG / "V071__task_graph_rechain_retry.sql").read_text(encoding="utf-8")
_FQ = "DBA_MAINT_DB.OVERWATCH."


def test_v071_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v071.py")],
                       env={**os.environ, "V071_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V71, (
        "V071 drifted from outputs/gen_v071.py — regenerate, never hand-edit.")


def test_v071_guard_version_and_registry_insert():
    assert "EXCEPTION (-20071" in _V71 and "IF (v < 70) THEN" in _V71
    assert "SELECT 71 AS VERSION" in _V71 and "WHERE VERSION = 71)" in _V71


def test_v071_never_redefines_a_task_body_and_adds_no_objects():
    # the whole point: ADD/REMOVE AFTER + SET only, so schedules/warehouses/bodies survive
    assert "CREATE OR REPLACE TASK" not in _V71, "V071 must not re-define a task body"
    assert "CREATE TASK" not in _V71, "V071 creates no task (#5 finalizer deferred; no new objects)"
    for ddl in ("CREATE TABLE", "CREATE TRANSIENT TABLE", "CREATE VIEW",
                "CREATE OR REPLACE VIEW", "CREATE STREAM", "CREATE OR REPLACE PROCEDURE"):
        assert ddl not in _V71, ddl


def test_v071_42_schema_widen_is_at_the_very_top_before_the_guard():
    # #42: the widen must run unconditionally, ABOVE the version guard, so any install
    # advancing the chain gets it even when the manual preflight is skipped.
    assert ("ALTER TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION\n"
            "    ALTER COLUMN DESCRIPTION SET DATA TYPE VARCHAR(4000);") in _V71
    assert _V71.index("SET DATA TYPE VARCHAR(4000)") < _V71.index("EXCEPTION (-20071"), \
        "the widen must precede the version guard"


# (task, old_predecessor, new_predecessor) — #3 hourly readers, #4 daily readers
_REPOINTS = [
    ("TASK_REFRESH_EXEC_BOARD", "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT"),
    ("TASK_ALERT_SCAN", "TASK_LOAD_HOURLY", "TASK_QH_EXTRACT"),
    ("TASK_LOAD_MARTS_V27_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE"),
    ("TASK_PLATFORM_SCORE_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE"),
    ("TASK_ALERT_SCAN_DAILY", "TASK_LOAD_DAILY", "TASK_NIGHTLY_RECONCILE"),
]


def test_v071_readers_repoint_onto_extract_and_reconcile():
    # #3: exec board + alert scan move AFTER the root -> AFTER the query-fact extract.
    # #4: the three daily readers move AFTER the root -> AFTER the nightly reconcile.
    for task, old, new in _REPOINTS:
        assert f"ALTER TASK {_FQ}{task} ADD AFTER {_FQ}{new};" in _V71, task
        assert f"ALTER TASK {_FQ}{task} REMOVE AFTER {_FQ}{old};" in _V71, task


def test_v071_repoints_are_exactly_five_and_fully_qualified():
    # 2 hourly (#3) + 3 daily (#4) = 5 re-points, each one ADD + one REMOVE, all FQ names
    assert _V71.count("REMOVE AFTER DBA_MAINT_DB.OVERWATCH.") == 5
    assert _V71.count("ADD AFTER DBA_MAINT_DB.OVERWATCH.") == 5


def test_v071_notify_and_correct_qh_children_are_not_repointed():
    # NOTIFY trails ALERT_SCAN and only moves with it — its own link is never touched
    assert "REMOVE AFTER DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN;" not in _V71
    # the two QH children V041 already chained correctly are suspended for the surgery but
    # never re-pointed (they are not a new predecessor of anything, nor is their link removed)
    for t in ("TASK_LOAD_MARTS_V27_HOURLY", "TASK_OPS_DIAG_HOURLY"):
        assert f"ADD AFTER DBA_MAINT_DB.OVERWATCH.{t}" not in _V71, t
        assert f"REMOVE AFTER DBA_MAINT_DB.OVERWATCH.{t}" not in _V71, t


def test_v071_repoints_are_state_checked_add_before_remove_no_swallow():
    # Codex HIGH fix: idempotency is a STATE CHECK, not an error-string guess, with NO
    # exception swallowing — so a genuine ALTER failure (busy graph / privilege / not-found)
    # aborts LOUDLY (leaving the graph suspended) instead of a silent green partial apply.
    assert "WHEN OTHER" not in _V71, "no EXCEPTION WHEN OTHER swallowing anywhere in V071"
    assert "does not exist" not in _V71, "no error-string matching — the state check gates instead"
    # each re-point snapshots the live predecessors via the repo's SHOW + RESULT_SCAN idiom
    assert _V71.count("SHOW TASKS LIKE '") == 5
    assert _V71.count("RESULT_SCAN(LAST_QUERY_ID())") == 5
    assert _V71.count("EXECUTE IMMEDIATE\n$$") == 6, "version guard + 5 state-checked re-point blocks"
    for task, old, new in _REPOINTS:
        add = f"ALTER TASK {_FQ}{task} ADD AFTER {_FQ}{new};"
        rem = f"ALTER TASK {_FQ}{task} REMOVE AFTER {_FQ}{old};"
        # ADD BEFORE REMOVE (Codex fix A): a failed ADD aborts before REMOVE, so the task
        # keeps its ORIGINAL predecessor and is never orphaned with zero predecessors.
        assert _V71.index(add) < _V71.index(rem), f"{task}: ADD must precede REMOVE"
        # ADD only when the new predecessor is ABSENT; REMOVE only when the old is PRESENT
        assert f"IF (preds NOT ILIKE '%{new}%') THEN\n        {add}" in _V71, f"{task}: ADD is state-gated"
        assert f"IF (preds ILIKE '%{old}%') THEN\n        {rem}" in _V71, f"{task}: REMOVE is state-gated"


def test_v071_43_retry_policy_on_both_roots():
    assert _V71.count("SET TASK_AUTO_RETRY_ATTEMPTS = 1, SUSPEND_TASK_AFTER_NUM_FAILURES = 10;") == 2
    assert ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY\n"
            "    SET TASK_AUTO_RETRY_ATTEMPTS = 1") in _V71
    assert ("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY\n"
            "    SET TASK_AUTO_RETRY_ATTEMPTS = 1") in _V71


def test_v071_resumes_the_whole_tree_no_dependent_left_suspended():
    # both graphs end with SYSTEM$TASK_DEPENDENTS_ENABLE(root) — the repo's guard against the
    # "children left suspended" alert-outage class (V041 hardening)
    assert _V71.count("SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');") == 1
    assert _V71.count("SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY');") == 1
    # roots are suspended for the surgery and resumed last
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;" in _V71
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY SUSPEND;" in _V71


def test_v071_in_migration_registry():
    # V071 is registered in the admin contract. (The validate.sql floor is the MOVING tip,
    # owned by tests/test_v451_trust.py + tests/test_perf_budgets.py — pinning it here would
    # fail the moment V072 lands; see test_v068/69/70 for the same boundary.)
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 71 in _EXPECTED_MIGRATIONS
