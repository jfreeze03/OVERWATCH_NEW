"""Locks for V067 — Codex-review behavioral proc fixes.

Two procs re-derived (SP_ALERT_SCAN from V066, SP_LOAD_OBJECT_COST from V062) via
count-asserted needle edits in outputs/gen_v067.py:

  #22 COST_STORAGE_SURGE + PIPE_COPY_FAILURES use COMPANY_FOR_DATABASE (not a TRXS%/ALFA guess)
  #20 COST_SERVERLESS_CREEP emits a 999 onset sentinel when the prior week is 0
  #40 a post-scan sweep supersedes the lower-band OPEN alert when its higher-band sibling is open
  #10 SP_LOAD_OBJECT_COST returns non-OK after a rolled-back load

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V67 = (_MIG / "V067__alert_attribution_onset_supersede_objectcost.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v067_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v067.py")],
                       env={**os.environ, "V067_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V67, (
        "V067 drifted from outputs/gen_v067.py — regenerate, never hand-edit.")


def test_v067_guard_and_version():
    assert "EXCEPTION (-20067" in _V67 and "IF (v < 66) THEN" in _V67
    assert "SELECT 67 AS VERSION" in _V67 and "WHERE VERSION = 67)" in _V67
    assert _V67.count("CREATE OR REPLACE PROCEDURE") == 2
    assert "CREATE TABLE" not in _V67 and "CREATE TASK" not in _V67


# #22 — canonical company classification
def test_company_for_database_replaces_the_raw_guess():
    scan = _proc(_V67, "SP_ALERT_SCAN")
    assert "COMPANY_FOR_DATABASE(g.DATABASE_NAME)" in scan   # COST_STORAGE_SURGE
    assert "COMPANY_FOR_DATABASE(p.DB)" in scan              # PIPE_COPY_FAILURES
    assert "LIKE 'TRXS%', 'Trexis', 'ALFA'" not in scan      # the raw guess is gone


# #20 — serverless-creep onset sentinel (mirrors COST_AI_CREEP)
def test_serverless_creep_fires_on_onset():
    scan = _proc(_V67, "SP_ALERT_SCAN")
    creep = scan.split("COST_SERVERLESS_CREEP", 1)[1].split("-- [", 1)[0]
    assert "> 0, 999, 0)" in creep                           # finite onset when prior week 0
    assert "/ NULLIF(SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)), 0)" not in creep


# #40 — escalation supersede sweep (runs before RETURN, band-swap match, precision-safe kind)
def test_escalation_supersede_sweep():
    scan = _proc(_V67, "SP_ALERT_SCAN")
    assert "RESOLUTION_KIND = 'SUPERSEDED'" in scan
    assert "REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')" in scan
    assert "REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')" in scan
    assert scan.index("RESOLUTION_KIND = 'SUPERSEDED'") < scan.index("RETURN 'alert scan v10")


def test_supersede_kind_excluded_from_precision_score():
    # the precision score counts only ACTIONED/NOISE, so SUPERSEDED closes don't distort it
    ms = (_ROOT / "app" / "data" / "mart_sql.py").read_text(encoding="utf-8")
    assert "RESOLUTION_KIND IN ('ACTIONED', 'NOISE')" in ms


def test_supersede_excluded_from_operator_mttr():
    # #40 companion (adversarial-review catch): a machine SUPERSEDED close sets RESOLVED_AT,
    # so the operator MTTR panel + rule-precision resolved count must exclude it or dedupe
    # timings pollute human MTTR.
    from app.data import mart_sql
    mttr = mart_sql.alert_mttr(90)
    assert mttr.count("COALESCE(RESOLUTION_KIND, '') <> 'SUPERSEDED'") == 2   # RESOLVED + MTTR
    ms = (_ROOT / "app" / "data" / "mart_sql.py").read_text(encoding="utf-8")
    assert "COUNT_IF(COALESCE(RESOLUTION_KIND, '') <> 'SUPERSEDED') AS RESOLVED_EVENTS" in ms


# #17 — residual compute resolves its company from the executing warehouse
def test_residual_company_resolved_from_warehouse():
    oc = _proc(_V67, "SP_LOAD_OBJECT_COST")
    # the executing warehouse is carried through the QA stage (QAH has no WAREHOUSE_NAME,
    # so a LEFT JOIN to QUERY_HISTORY on QUERY_ID supplies it — the V036 pattern)
    assert "MAX(q.WAREHOUSE_NAME) AS WAREHOUSE_NAME" in oc
    assert "LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q\n      ON q.QUERY_ID = a.QUERY_ID" in oc
    # the residual row resolves COMPANY via the house UDF; the hardcoded UNKNOWN is gone
    assert "'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)" not in oc
    assert "COMPANY_FOR_WAREHOUSE(qa.WAREHOUSE_NAME), SUM(qa.CREDITS)" in oc
    # resolved company joins the grouping so residuals split by company (UDF still returns
    # UNKNOWN as the fallback when the warehouse doesn't resolve)
    residual = oc.split("QUERY_COMPUTE_RESIDUAL", 1)[1]
    assert "GROUP BY 1, 5;" in residual


# #10 — object-cost returns non-OK after a rollback
def test_object_cost_returns_non_ok_on_failure():
    oc = _proc(_V67, "SP_LOAD_OBJECT_COST")
    assert "failed BOOLEAN DEFAULT FALSE" in oc and "failed := TRUE;" in oc
    assert "IF (failed) THEN\n        RETURN 'FAILED: object-cost load rolled back" in oc
    assert oc.count("ROLLBACK") >= 1                          # the B34 wrap survives


def test_v067_preserves_v066_fixes():
    scan = _proc(_V67, "SP_ALERT_SCAN")
    assert "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN')" in scan    # V066 #1 pipe-copy band
    assert "AND USAGE_DATE < CURRENT_DATE()" in scan             # V066 #6 serverless today-exclusion


def test_v067_in_migration_registry():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 67 in _EXPECTED_MIGRATIONS
