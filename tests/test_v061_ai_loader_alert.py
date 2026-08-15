"""Locks for V061 — AI loader/alert/score/purge correctness (correctness-only;
the B5/B10/B11/B12 loader-robustness cluster and SP_NOTIFY_WEBHOOK B9 are HELD for
a smoke-tested pass, so they must NOT appear here).

  C5  SP_LOAD_MARTS_V27 AI arms day-align (CURRENT_TIMESTAMP -> CURRENT_DATE).
  C2  QAS added to proc/pipeline attribution: marts arm-[6] + both SP_CHANGE_IMPACT_SCAN arms.
  C1  SP_ALERT_SCAN MTD blends AI at AI_CREDIT_PRICE_USD; FACT_PLATFORM_SCORE_DAILY gains
      CREDITS_BILLED_AI (SP_LOAD_PLATFORM_SCORE populates it).
  C6  COST_AI_CREEP seeded + [13b] arm. B41 self-alert block count 17 -> 20.
  B33 SP_PURGE_FACTS purges 3 more daily facts.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V61 = (_MIG / "V061__ai_loader_alert_score_purge_fixes.sql").read_text(encoding="utf-8")

_AI = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' "
       "OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v061_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v061.py")],
                       env={**os.environ, "V061_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V61, (
        "V061 drifted from outputs/gen_v061.py — regenerate, never hand-edit.")


def test_v061_guard_and_shape():
    assert "EXCEPTION (-20061" in _V61 and "IF (v < 60) THEN" in _V61 and "SELECT 61 AS VERSION" in _V61
    assert "WHERE VERSION = 61)" in _V61
    assert _V61.count("CREATE OR REPLACE PROCEDURE") == 5
    for name in ("SP_LOAD_MARTS_V27", "SP_ALERT_SCAN", "SP_CHANGE_IMPACT_SCAN",
                 "SP_PURGE_FACTS", "SP_LOAD_PLATFORM_SCORE"):
        assert f"-- >>> derived:{name}" in _V61
    assert ("ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY\n"
            "    ADD COLUMN IF NOT EXISTS CREDITS_BILLED_AI NUMBER(18,6);") in _V61
    assert "CREATE TABLE" not in _V61 and "CREATE TASK" not in _V61          # additive only
    # heal CALLs present (owner-chosen full-retention C5; C1 backfill)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);" in _V61
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(120);" in _V61


def test_v061_held_items_absent():
    """B9 (SP_NOTIFY_WEBHOOK) and the loader-robustness cluster are held — this
    migration must not re-derive/alter them (naming them in the header is fine)."""
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK" not in _V61
    assert "-- >>> derived:SP_NOTIFY_WEBHOOK" not in _V61
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE" not in _V61
    assert "ARRAY_CONTAINS" not in _V61                # B9's runtime-risky construct
    assert "-:cap, CURRENT_DATE()" not in _V61         # B5 coverage-aware cap


def test_v061_c5_ai_arms_day_aligned():
    marts = _proc(_V61, "SP_LOAD_MARTS_V27")
    assert marts.count("DATEADD('day', -:d, CURRENT_TIMESTAMP())") == 0
    assert marts.count("DATEADD('day', -:d, CURRENT_DATE())") == 10             # 7 + 3 AI arms
    for tbl in ("CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "CORTEX_CODE_CLI_USAGE_HISTORY",
                "CORTEX_FUNCTIONS_USAGE_HISTORY"):
        i = marts.index(tbl)
        assert "CURRENT_DATE())" in marts[i:i + 220] and "CURRENT_TIMESTAMP())" not in marts[i:i + 220]


def test_v061_c2_qas_added_everywhere():
    marts = _proc(_V61, "SP_LOAD_MARTS_V27")
    chg = _proc(_V61, "SP_CHANGE_IMPACT_SCAN")
    assert marts.count("SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS") == 1
    assert marts.count("SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS") == 0
    assert chg.count("SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR") == 2
    assert chg.count("SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR") == 0


def test_v061_c1_mart_blends_ai_rate():
    alert = _proc(_V61, "SP_ALERT_SCAN")
    assert alert.count("ai_credit_price FLOAT;") == 1
    assert alert.count("INTO :budget_usd, :credit_price, :ai_credit_price") == 1
    assert alert.count("* :ai_credit_price AS MTD_USD") == 2
    assert "SUM(CREDITS_BILLED) * :credit_price AS MTD_USD" not in alert         # flat-rate MTD gone


def test_v061_c6_ai_creep_seeded_and_armed():
    alert = _proc(_V61, "SP_ALERT_SCAN")
    # config seed precedes the derived proc AND any CALL
    seed_i = _V61.index("'COST_AI_CREEP' AS RULE_ID")
    proc_i = _V61.index("-- >>> derived:SP_ALERT_SCAN")
    assert seed_i < proc_i
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in _V61              # no in-migration scan call
    assert alert.count("c.RULE_ID = 'COST_AI_CREEP'") == 1
    assert alert.count("fails := fails + 1;") == 20                             # 19 + C6 block
    assert "AI has COST_AI_CREEP" in alert and "Warehouses and AI have their own" not in alert
    # B41 + C6 block-count lockstep
    assert alert.count("(20 - :fails) || '/20 rule blocks ok'") == 1 and "(19 - :fails)" not in alert
    assert "of 20 alert rule block(s)" in alert and "of 17" not in alert
    # onset hardening (adversarial-verify finding): a brand-new AI workload (prior
    # week 0) must still fire — a finite 999 sentinel, not a NULL ratio that drops.
    assert "THEN IFF(SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) > 0, 999, 0)" in alert
    assert "NULLIF(SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE())" not in alert   # old NULL-ratio form gone


def test_v061_b33_purges_three_more_facts():
    purge = _proc(_V61, "SP_PURGE_FACTS")
    for tbl in ("FACT_OBJECT_COST_DAILY", "FACT_STORAGE_ACCOUNT_DAILY", "MART_TASK_NODE_DAILY"):
        assert purge.count(f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{tbl}\n") == 1
    assert purge.count("WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());") == 21
    assert purge.count("RETURN 'purged ' || :total || ' row(s)';") == 1         # still terminal, once


def test_v061_c1_score_column_populated():
    score = _proc(_V61, "SP_LOAD_PLATFORM_SCORE")
    assert score.count("AS CREDITS_BILLED_AI") == 1
    assert score.count("CREDITS_BILLED_AI") == 6                                # spend, outer, UPDATE x2, INSERT, VALUES
    assert score.count("CREDITS_BILLED = s.CREDITS_BILLED,") == 1               # total arm untouched


def test_v061_ai_predicate_identical_everywhere():
    """B5-crit (cross-proc lock): the AI predicate is byte-identical across the
    V061 loader arms (SP_ALERT_SCAN mtd/creep + score loader).

    v4.158.0: the APP predicate was deliberately BROADENED to include Cortex
    Code / CoWork (SNOWFLAKE_COCO_SNOWSIGHT) via the canonical
    common.ai_service_predicate(), fixing the display/rate-split so CoCo prices
    at the AI rate. The V061 LOADER still carries the narrow 3-token predicate;
    re-syncing it (so the historical mart split + alert/score thresholds also
    count CoCo as AI) is the tracked follow-up owner migration. This pins BOTH
    sides of that known, intentional divergence."""
    # Loader side: unchanged — still the narrow predicate, identical across arms.
    assert _V61.count(_AI) >= 5                                                 # mtd x2 + creep + score
    from app.data import mart_sql
    from app.data.common import ai_service_predicate
    assert hasattr(mart_sql, "_AI_SERVICE_PRED")
    # App side: now the canonical predicate, which INCLUDES CoCo/CoWork and is a
    # strict superset of the loader's narrow form (the deliberate divergence).
    canonical = ai_service_predicate()
    assert "%COCO%" in canonical and "%COWORK%" in canonical
    assert canonical == mart_sql._AI_SERVICE_PRED
    assert _AI not in canonical                                                # loader form is narrower, pending migration


def test_v061_prior_fixes_survive():
    marts = _proc(_V61, "SP_LOAD_MARTS_V27")
    assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4    # V057
    assert "EXECUTION_STATUS = 'FAILED'" not in marts
    assert marts.count("loaded := loaded || 'task_node ';") == 1               # V058
    assert marts.count("a ON a.ROOT_ID = h.QUERY_ID") == 1                     # V059
    assert marts.count("AS TOTAL_ELAPSED_SEC,") == 1                          # V060 #5
    assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts
    alert = _proc(_V61, "SP_ALERT_SCAN")
    assert alert.count("AND WAREHOUSE_ID > 0") == 1                           # V060 guard
    assert alert.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(w.WAREHOUSE_NAME)") == 1  # V056


def test_v061_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for stmt in _plain_statements(_V61):
        sqlglot.parse(stmt, dialect="snowflake")
