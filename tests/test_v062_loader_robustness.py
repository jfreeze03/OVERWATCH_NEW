"""Locks for V062 — loader robustness + correctness + alert split + webhook.

Owner scope (2026-07-30): R3-4 (fail predicate <> 'SUCCESS'), B5 (backfill day-cap),
B10 (reconcile boundary-hour clamp), B11 (hourly-facts watermark catch-up), B12
(backfill/task suspend, app-side script), B34 (daily-facts/object-cost txn wraps),
C9 (daily alert blocks -> SP_ALERT_SCAN_DAILY). B9 (webhook) and the T3.1-T3.4
perf-loader restructures are DEFERRED to V063 and must NOT appear here (an adversarial
review found a send-vs-ledger race in the authored B9 fix that needs runtime testing).

The generator is the source of truth: outputs/gen_v062.py re-derives every proc from
its LATEST base via count-asserted needle edits. The first test regenerates and
byte-compares — never hand-edit V062.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V62 = (_MIG / "V062__loader_robustness_alert_split_webhook.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


# ---------------------------------------------------------------------------
# Derivation law: V062 must regenerate byte-identical from its generator.
# ---------------------------------------------------------------------------
def test_v062_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v062.py")],
                       env={**os.environ, "V062_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V62, (
        "V062 drifted from outputs/gen_v062.py — regenerate, never hand-edit.")


def test_v062_guard_and_version():
    assert "EXCEPTION (-20062" in _V62 and "IF (v < 61) THEN" in _V62
    assert "SELECT 62 AS VERSION" in _V62 and "WHERE VERSION = 62)" in _V62
    # 9 procs re-derived/authored; balanced $$ (9*2 + 1 guard block = 20)
    assert _V62.count("CREATE OR REPLACE PROCEDURE") == 9
    assert _V62.count("$$") == 20


# ---------------------------------------------------------------------------
# R3-4 — fail predicate standardized in mart AND app (parity)
# ---------------------------------------------------------------------------
def test_r34_mart_predicate_standardized():
    # QH_EXTRACT x2 + OPS_DIAG x1 + MARTS_V27 x4 = 7 query-fail predicates flipped;
    # zero remaining = 'FAIL' for the query column.
    assert _V62.count("EXECUTION_STATUS <> 'SUCCESS'") == 7
    assert "EXECUTION_STATUS = 'FAIL'" not in _V62
    # task-state predicate (TASK_HISTORY.STATE) is a different column — untouched.
    assert "STATE = 'FAILED'" in _V62


def test_r34_app_legs_ship_with_mart():
    # the 4 paired app reads (run_mart_first live fallbacks) MUST match the loader,
    # else the "Failed" tiles disagree mart-vs-live (the R3-4 defect re-introduced).
    for rel in ("app/data/ops_sql.py", "app/data/insights_sql.py", "app/data/mart_sql.py"):
        assert "EXECUTION_STATUS = 'FAIL'" not in _read(rel), rel
    assert _read("app/data/ops_sql.py").count("EXECUTION_STATUS <> 'SUCCESS'") >= 2
    # backfill_365 internal-consistency twin
    assert "EXECUTION_STATUS = 'FAIL'" not in _read("snowflake/backfill_365.sql")


# ---------------------------------------------------------------------------
# B5 / B10 — backfill day-cap + boundary-hour clamp (no more LEAST(:d, 2))
# ---------------------------------------------------------------------------
def test_b5_backfill_caps_lifted():
    marts = _proc(_V62, "SP_LOAD_MARTS_V27")
    assert "LEAST(:d, 2)" not in marts  # the silent 2-day cap is gone
    assert "GREATEST(DATEADD('day', -:d, CURRENT_DATE()), :ext_lo)" in marts
    assert "ext_lo DATE;" in marts


def test_b10_boundary_hour_clamp():
    ops = _proc(_V62, "SP_LOAD_OPS_DIAG")
    # all 3 hour lower-bounds clamped to the extract's first whole hour
    assert ops.count("GREATEST(DATE_TRUNC('hour', DATEADD('day', -:d, CURRENT_TIMESTAMP())), :ext_lo)") == 3
    assert "ext_lo TIMESTAMP_NTZ;" in ops


# ---------------------------------------------------------------------------
# B11 — hourly-facts watermark catch-up + reconcile reset
# ---------------------------------------------------------------------------
def test_b11_watermark_catchup():
    hourly = _proc(_V62, "SP_LOAD_HOURLY_FACTS")
    assert "HOURLY_FACTS" in hourly and "OW_LOAD_WATERMARKS" in hourly
    reconcile = _proc(_V62, "SP_NIGHTLY_RECONCILE")
    assert "'QH_EXTRACT', 'HOURLY_FACTS', 'DAILY_FACTS'" in reconcile


# ---------------------------------------------------------------------------
# B34 — transaction-wrapped DELETE+INSERT
# ---------------------------------------------------------------------------
def test_b34_transaction_wraps():
    daily = _proc(_V62, "SP_LOAD_DAILY_FACTS")
    obj = _proc(_V62, "SP_LOAD_OBJECT_COST")
    assert "BEGIN TRANSACTION" in daily and "ROLLBACK" in daily
    assert "BEGIN TRANSACTION" in obj and "ROLLBACK" in obj


# ---------------------------------------------------------------------------
# C9 — daily alert blocks split out; hourly proc reduced to 14; task DAG
# ---------------------------------------------------------------------------
def test_c9_hourly_reduced_and_daily_created():
    hourly = _proc(_V62, "SP_ALERT_SCAN")
    for tag in ("-- [06]", "-- [07]", "-- [08]", "-- [09]", "-- [13b]", "-- [16]"):
        assert tag not in hourly, f"daily block {tag} still in hourly scan"
    # surviving hourly blocks keep their ORIGINAL numbers (no renumber)
    for tag in ("-- [01]", "-- [05]", "-- [10]", "-- [15]", "-- [19]"):
        assert tag in hourly, tag
    assert "/14 rule blocks ok" in hourly and "/20 rule blocks ok" not in hourly
    daily = _proc(_V62, "SP_ALERT_SCAN_DAILY")
    for tag in ("-- [06]", "-- [07]", "-- [08]", "-- [09]", "-- [13b]", "-- [16]"):
        assert tag in daily, f"daily block {tag} missing from SP_ALERT_SCAN_DAILY"
    assert "/6 rule blocks ok (daily)" in daily
    # distinct dedupe key (T4: no OPS_SCAN_DEGRADED collision with the hourly scan)
    assert "'|DAILY|'" in daily


def test_c9_task_dag_chained_after_load_daily():
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN_DAILY" in _V62
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY" in _V62
    assert "SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_DAILY')" in _V62
    assert "WAREHOUSE = WH_ALFA_ADMIN" in _V62  # the renamed warehouse


# ---------------------------------------------------------------------------
# B9 — DEFERRED to V063 (adversarial review found a send-vs-ledger race; the
# correct capture-once-ARRAY fix needs runtime smoke-testing). V062 must NOT
# re-derive SP_NOTIFY_WEBHOOK.
# ---------------------------------------------------------------------------
def test_b9_webhook_deferred_to_v063():
    assert "-- >>> derived:SP_NOTIFY_WEBHOOK" not in _V62      # not re-derived here
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK" not in _V62
    assert "B9" in _V62 and "V063" in _V62                     # the deferral is documented


# ---------------------------------------------------------------------------
# T3 deferral — the perf restructures must NOT be in V062
# ---------------------------------------------------------------------------
def test_v062_excludes_deferred_t3():
    marts = _proc(_V62, "SP_LOAD_MARTS_V27")
    # T3.1 introduces an OW_QH_EXTRACT-vs-raw two-variant arm[1]; T3.2 a _OW_TASK_BASE
    # temp; T3.3 a _OW_WMH_STAGE. None may appear until V063.
    assert "_OW_TASK_BASE" not in marts and "_OW_WMH_STAGE" not in marts


# ---------------------------------------------------------------------------
# Guardrails — prior-release fixes survive the V062 re-derivation
# ---------------------------------------------------------------------------
def test_v062_preserves_prior_release_fixes():
    marts = _proc(_V62, "SP_LOAD_MARTS_V27")
    assert marts.count("COUNT_IF(EXECUTION_STATUS <> 'SUCCESS') AS FAILS") == 4  # was V057 =FAIL
    assert "loaded := loaded || 'task_node ';" in marts                         # V058 arm [6b]
    assert "a ON a.ROOT_ID = h.QUERY_ID" in marts                               # V059 ROOT rollup
    assert "AS TOTAL_ELAPSED_SEC," in marts                                     # V060 #5
    # TASK_HISTORY.STATE predicates (a different column) untouched — same counts as
    # the V061 base (V059/V060 added task-graph arms, so 3 each, not V057's 2).
    assert marts.count("STATE = 'FAILED'") == 3
    assert marts.count("STATE IN ('SUCCEEDED', 'FAILED')") == 3
