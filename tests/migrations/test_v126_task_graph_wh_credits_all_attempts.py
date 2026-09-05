"""V126: pipeline WH_CREDITS on the task-graph mart SUMs every attempt's compute.

MART_TASK_GRAPH_DAILY.WH_CREDITS (loaded by SP_LOAD_MARTS_V27) collapsed task
auto-retries to the terminal attempt BEFORE joining QUERY_ATTRIBUTION_HISTORY, so a
failed retry's compute dropped from WH_CREDITS (V102 documented this as "accepted").
The live twin graph_sql.graph_daily_costs SUMs credits over ALL attempts, so the same
Cost > Unit costs > Task-graph panel's "Pipeline spend" flipped with mart warmth and
disagreed with its own "every task run" caption (bug-hunt round 28b twin-divergence).

V126 re-derives SP_LOAD_MARTS_V27 from V125 with the task-graph arm restructured to
mirror the live twin exactly: keep every attempt, tag the terminal one, count scheduled
tasks via TERMINAL_RN = 1, and SUM credits over all attempts. Counts stay terminal-
collapsed; the V125 MFA-gap fix is preserved; everything else byte-identical to V125.
These locks pin the mart side, prove the MFA-gap fix survived, and prove byte-fidelity.
"""

from __future__ import annotations

import difflib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DIR = _ROOT / "snowflake" / "migrations"
_V126 = (_MIG_DIR / "V126__task_graph_wh_credits_all_attempts.sql").read_text(encoding="utf-8")
_V125 = (_MIG_DIR / "V125__mfa_gap_active_user_coalesce.sql").read_text(encoding="utf-8")

_CANON = "COALESCE(U.DISABLED, FALSE) = FALSE"
_BARE = "AND U.DISABLED = FALSE"


def _proc_block(sql: str) -> str:
    """The CREATE OR REPLACE PROCEDURE ... $$; body for SP_LOAD_MARTS_V27."""
    start = sql.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    end = sql.index("\n$$;", start) + len("\n$$;")
    return sql[start:end]


def _without_task_graph_arm(proc: str, cte_head: str) -> str:
    """proc body with the task-graph MERGE's USING(...) block sliced out, so the rest
    can be byte-compared across V125/V126."""
    start = proc.index("            USING (\n                WITH " + cte_head)
    end = proc.index("\n            ) s", start) + len("\n            ) s")
    return proc[:start] + proc[end:]


def test_v126_guarded_versioned_and_proc_only() -> None:
    assert "EXCEPTION (-20126" in _V126 and "RAISE not_ready;" in _V126
    assert "IF (v < 125) THEN" in _V126
    assert "SELECT 126 AS VERSION" in _V126
    assert "WHERE VERSION = 126" in _V126
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in _V126
    # forward-healing: no in-migration CALL, no new object / schema change (the nightly
    # SP_LOAD_MARTS_V27 task re-stamps the mart on its next run).
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" not in _V126
    assert "CREATE TABLE" not in _V126 and "ALTER TABLE" not in _V126


def test_v126_task_graph_arm_sums_all_attempts_and_mirrors_the_live_twin() -> None:
    body = _proc_block(_V126)
    arm_start = body.index("            USING (\n                WITH attempts AS (")
    arm = body[arm_start:body.index("\n            ) s", arm_start)]
    # credits now SUM over EVERY attempt (no terminal-collapse before the credit join)
    assert "WITH attempts AS (" in arm
    assert "AS TERMINAL_RN" in arm
    assert "SUM(CREDITS) AS CREDITS" in arm
    # counts stay terminal-collapsed via TERMINAL_RN = 1 (a retried-then-succeeded task
    # is not a graph-run failure) — exactly the live twin's form
    assert "COUNT_IF(TERMINAL_RN = 1) AS TASK_RUNS" in arm
    assert "COUNT_IF(TERMINAL_RN = 1 AND STATE = 'FAILED') AS FAILED_TASKS" in arm
    # the old V102 terminal-only credit path is GONE from this arm
    assert "QUALIFY ROW_NUMBER() OVER (" not in arm
    assert "contextual pipeline cost, not the authoritative ledger" not in _V126


def test_v126_preserves_the_v125_mfa_gap_coalesce_fix() -> None:
    # V126 is derived from V125, so the MFA-gap arm must still use the COALESCE canonical
    # and the bare form must not reappear anywhere in the migration.
    body = _proc_block(_V126)
    assert f"WHERE U.DELETED_ON IS NULL AND {_CANON}" in body
    assert _BARE not in _V126


def test_v126_differs_from_v125_only_in_the_task_graph_arm() -> None:
    """Byte-fidelity: the V126 proc body differs from V125's ONLY inside the task-graph
    MERGE's USING(...) block. Everything else in the ~885-line loader is untouched, so
    no other arm (incl. the V125 MFA-gap fix) drifted."""
    rest_125 = _without_task_graph_arm(_proc_block(_V125), "runs AS (")
    rest_126 = _without_task_graph_arm(_proc_block(_V126), "attempts AS (")
    assert rest_125 == rest_126, "V126 changed something OUTSIDE the task-graph arm"
    # and the arm really did change (sanity: the diff is non-empty and is the credit fix)
    diff = [d for d in difflib.unified_diff(
        _proc_block(_V125).splitlines(), _proc_block(_V126).splitlines(), lineterm="", n=0)
        if d and d[0] in "+-" and not d.startswith(("+++", "---"))]
    assert any("TERMINAL_RN" in d and d.startswith("+") for d in diff)
    assert any("QUALIFY ROW_NUMBER()" in d and d.startswith("-") for d in diff)


def test_v126_is_the_latest_full_loader_definition() -> None:
    """V126 is the newest migration carrying a full SP_LOAD_MARTS_V27 body, so the next
    re-derivation starts from it (not the now-superseded V125/V113)."""
    defs = sorted(p for p in _MIG_DIR.glob("V[0-9]*.sql")
                  if "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27"
                  in p.read_text(encoding="utf-8"))
    assert defs[-1].name == "V126__task_graph_wh_credits_all_attempts.sql"


def test_v126_floor_tracks_the_tip() -> None:
    v = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V126 applied" in v
    assert "BETWEEN 1 AND 126) = 126" in v


def test_v126_is_tracked_in_deploy_and_admin_surfaces() -> None:
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V126__task_graph_wh_credits_all_attempts.sql" in (_ROOT / rel).read_text(encoding="utf-8")
    assert "126:" in (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
