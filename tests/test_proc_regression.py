"""Stored-procedure regression advisor (F7): SQL shape, pure classifier, wiring.

Two live builders (ops_sql.proc_sla_rollup / proc_regression) over
ACCOUNT_USAGE.QUERY_HISTORY CALL rows, plus the pure verdict classifier
(app/logic/proc_regression.py). App-only — no migration.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import ops_sql
from app.logic import proc_regression as pr

_ROOT = Path(__file__).resolve().parents[1]
_REGEXP = "REGEXP_SUBSTR(UPPER(QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1)"


# ============================================ proc_sla_rollup SQL shape =======

def test_rollup_shape_and_success_only_latency():
    sql = ops_sql.proc_sla_rollup(30, "ALFA")
    assert chr(92) not in sql                                   # POSIX classes only, never \s
    assert _REGEXP in sql                                       # proc name parsed from CALL text
    assert "QUERY_TYPE = 'CALL'" in sql
    assert "COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%'" in sql   # app self-CALLs excluded
    # latency is success-only so a proc that fails fast can't read as 'faster'
    assert "IFF(EXECUTION_STATUS = 'SUCCESS', TOTAL_ELAPSED_TIME, NULL) AS OK_MS" in sql
    assert "APPROX_PERCENTILE(OK_MS, 0.95)" in sql              # p95, never PERCENTILE_CONT
    for col in ("CALLS", "FAIL_PCT", "AVG_S", "P95_S", "MAX_S", "TOTAL_MIN"):
        assert col in sql
    # ranked by SLA impact = frequency x duration, but a 100%-failing proc (P95_S NULL -> rank 0)
    # is surfaced FIRST so the LIMIT can't bury the fully-broken proc this panel exists to show
    # (bug-hunt 2026-08-30).
    assert "ORDER BY CASE WHEN FAIL_PCT = 100 THEN 1 ELSE 0 END DESC" in sql
    assert "CALLS * COALESCE(P95_S, 0) DESC" in sql


def test_rollup_scoping_clamp_and_injection():
    assert "COMPANY_FOR_WAREHOUSE" not in ops_sql.proc_sla_rollup(30, "ALL")
    assert "COMPANY_FOR_WAREHOUSE" in ops_sql.proc_sla_rollup(30, "Trexis")
    assert "-90," in ops_sql.proc_sla_rollup(9999, "ALFA")      # window clamp
    assert "LIMIT 200" in ops_sql.proc_sla_rollup(30, "ALFA", limit=9999)  # limit clamp
    assert "''" in ops_sql.proc_sla_rollup(30, "x'y")          # injection-safe


# ============================================ proc_regression SQL shape =======

def test_regression_two_windows_and_delta_guard():
    sql = ops_sql.proc_regression(30, "ALFA")
    assert chr(92) not in sql
    assert _REGEXP in sql
    assert "QUERY_TYPE = 'CALL'" in sql
    # one scope call over 2x days (-60), current window split at -30 — day-aligned
    assert "-60," in sql and "-30," in sql
    assert "IFF(START_TIME >= DATEADD('day', -30, CURRENT_DATE()), 'CUR', 'PRIOR')" in sql
    # both windows must clear the min-call floor on SUCCESSFUL calls before compare
    assert "HAVING COUNT_IF(WIN = 'CUR' AND EXECUTION_STATUS = 'SUCCESS') >= 5" in sql
    assert "COUNT_IF(WIN = 'PRIOR' AND EXECUTION_STATUS = 'SUCCESS') >= 5" in sql
    # delta from UNROUNDED ms (not the display-rounded *_S columns), zero-guarded, worst-first
    assert "AS CUR_P95_MS" in sql and "AS PRIOR_P95_MS" in sql
    assert "(CUR_P95_MS - PRIOR_P95_MS) / NULLIF(PRIOR_P95_MS, 0)" in sql
    assert "AS P95_DELTA_PCT" in sql and "AS AVG_DELTA_PCT" in sql
    # ranked by the STRONGER of slowdown and fail-jump so the LIMIT keeps the High-severity
    # 'faster but failing' class (negative p95 delta, big fail-jump) — signed p95-delta alone
    # sorted them into the truncated tail and dropped them (bug-hunt 2026-08-30).
    assert "ORDER BY GREATEST(COALESCE(P95_DELTA_PCT, 0)," in sql
    assert "COALESCE(CUR_FAIL_PCT, 0) - COALESCE(PRIOR_FAIL_PCT, 0)) DESC NULLS LAST" in sql
    assert "CUR_FAIL_PCT" in sql and "PRIOR_FAIL_PCT" in sql


def test_regression_min_calls_scope_clamp_injection():
    assert ("HAVING COUNT_IF(WIN = 'CUR' AND EXECUTION_STATUS = 'SUCCESS') >= 10"
            in ops_sql.proc_regression(30, "ALFA", min_calls=10))
    assert "COMPANY_FOR_WAREHOUSE" not in ops_sql.proc_regression(30, "ALL")
    assert "COMPANY_FOR_WAREHOUSE" in ops_sql.proc_regression(30, "Trexis")
    # 9999 -> days clamps to 90, so the 2x scope is -180 and the split is -90
    reg = ops_sql.proc_regression(9999, "ALFA")
    assert "-180," in reg and "-90," in reg
    assert "''" in ops_sql.proc_regression(30, "x'y")


# ============================================ classifier (pure logic) =========

def _row(**over: float) -> dict:
    base = {"P95_DELTA_PCT": 0.0, "CUR_FAIL_PCT": 0.0, "PRIOR_FAIL_PCT": 0.0}
    base.update(over)
    return base


def test_classify_regressed_slower_stable_improved():
    assert pr.classify(_row(P95_DELTA_PCT=80.0))[:2] == ("Regressed", "High")
    assert pr.classify(_row(P95_DELTA_PCT=30.0))[:2] == ("Slower", "Medium")
    assert pr.classify(_row(P95_DELTA_PCT=5.0))[:2] == ("Stable", "Low")
    assert pr.classify(_row(P95_DELTA_PCT=-40.0))[:2] == ("Improved", "Low")


def test_classify_faster_but_failing_beats_apparent_improvement():
    # p95 dropped 10% but the failure rate jumped 45 points -> not a real win
    verdict, severity, score = pr.classify(
        _row(P95_DELTA_PCT=-10.0, CUR_FAIL_PCT=50.0, PRIOR_FAIL_PCT=5.0))
    assert verdict == "Faster but failing" and severity == "High" and score > 0


def test_classify_slower_and_failing_escalates_to_high():
    # slower AND failing more is strictly worse than either alone -> High Regressed,
    # never demoted to Medium 'Slower' (which would drop the failure spike).
    verdict, severity, _ = pr.classify(
        _row(P95_DELTA_PCT=30.0, CUR_FAIL_PCT=60.0, PRIOR_FAIL_PCT=2.0))
    assert verdict == "Regressed" and severity == "High"


def test_classify_faster_label_only_when_p95_did_not_rise():
    # +15% p95 with a fail spike is NOT 'Faster' — p95 rose, so it's a Regression
    verdict, severity, _ = pr.classify(
        _row(P95_DELTA_PCT=15.0, CUR_FAIL_PCT=25.0, PRIOR_FAIL_PCT=5.0))
    assert verdict == "Regressed" and severity == "High"
    # exactly-flat p95 with a fail spike IS 'Faster but failing'
    assert pr.classify(
        _row(P95_DELTA_PCT=0.0, CUR_FAIL_PCT=25.0, PRIOR_FAIL_PCT=5.0))[0] == "Faster but failing"


def test_every_verdict_tints_via_the_shared_palette():
    from app.ui.status_colors import _VERDICTS
    for verdict in ("Regressed", "Slower", "Stable", "Improved", "Faster but failing"):
        assert verdict.upper() in _VERDICTS


def test_classify_missing_columns_never_crash():
    for r in ({}, {"P95_DELTA_PCT": None}, {"P95_DELTA_PCT": "x"}):
        _verdict, severity, score = pr.classify(r)
        assert severity in {"High", "Medium", "Low"} and isinstance(score, int)


def test_annotate_sorts_worst_first_and_handles_empty():
    assert pr.annotate(pd.DataFrame()).empty
    df = pd.DataFrame([
        _row(P95_DELTA_PCT=5.0),     # Stable / Low
        _row(P95_DELTA_PCT=90.0),    # Regressed / High
        _row(P95_DELTA_PCT=25.0),    # Slower / Medium
    ])
    out = pr.annotate(df)
    assert list(out["VERDICT"]) == ["Regressed", "Slower", "Stable"]
    assert list(out["SEVERITY"]) == ["High", "Medium", "Low"]


# ============================================ wiring + canary =================

def test_proc_regression_wired_into_operations():
    src = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "proc_regression(" in src and "Stored-procedure regression" in src
    assert "proc_sla_rollup(" in src            # both tables rendered (rollup + regression)
    assert "ops_proc_reg_toggle" in src
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "ops.proc_regression" in canary and "ops.proc_sla_rollup" in canary
