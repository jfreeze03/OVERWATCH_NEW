#!/usr/bin/env python3
"""Forward-generate V061__ai_loader_alert_score_purge_fixes.sql.

Correctness-only V061 (owner phasing: perf loader restructures -> V062). Ships the
byte-complete HIGH-confidence cost-review/bug-round-2 fixes; the loader-robustness
cluster (B5/B10/B11/B12) and SP_NOTIFY_WEBHOOK B9 are HELD for a dedicated,
smoke-tested pass (B9 relies on ARRAY/ARRAY_CONTAINS runtime semantics that a
byte-compare cannot prove and this environment cannot execute).

Fixes:
  C5  SP_LOAD_MARTS_V27 - the three AI arms window on CURRENT_TIMESTAMP() (moving
      intraday window) while every other daily arm uses CURRENT_DATE(). The MERGE
      overwrite meant a complete day-X row got recomputed as a partial once X exited
      the window (data corruption of FACT_AI_USAGE_DAILY). Day-align all three.
  C2  SP_LOAD_MARTS_V27 arm-[6] (MART_TASK_GRAPH_DAILY ROOT_ID rollup) and both
      SP_CHANGE_IMPACT_SCAN attribution arms sum CREDITS_ATTRIBUTED_COMPUTE only;
      add the Query-Acceleration term to match the measured contract (compute + QAS),
      mirroring the app fix shipped v4.74.0.
  C1  SP_ALERT_SCAN COST_BUDGET_PACE / COST_FORECAST_BREACH price MTD spend at the
      single compute rate; blend AI/Cortex credits at AI_CREDIT_PRICE_USD. Plus the
      score fact: FACT_PLATFORM_SCORE_DAILY gains CREDITS_BILLED_AI and SP_LOAD_
      PLATFORM_SCORE populates it (app follow-up lands the reader/scoring blend).
  C6  Seed COST_AI_CREEP - week-over-week AI-bucket growth priced at the AI rate;
      SP_ALERT_SCAN gains the [13b] arm; the false "AI has its own rules" carve-out
      comment is corrected.
  B41 SP_ALERT_SCAN self-alert said "of 17 alert rule block(s)"; true post-C6 = 20.
  B33 SP_PURGE_FACTS never purged FACT_OBJECT_COST_DAILY, FACT_STORAGE_ACCOUNT_DAILY,
      MART_TASK_NODE_DAILY; add them to the daily-retention loop.

Derivation law (see gen_v060.py): each proc re-derived from its CURRENT definition
via extract_proc (last-match) + apply()/apply_n() with asserted needle counts;
tests/test_v061_ai_loader_alert.py byte-compares each generated proc.
Reconcile on apply (in the migration tail; also in DEPLOYMENT.md V061 run-list):
  C5 -> CALL SP_LOAD_MARTS_V27('DAILY', 365)     (full-retention heal, owner-chosen)
  C1 -> CALL SP_LOAD_PLATFORM_SCORE(120)          (backfill the new AI column)
  C2 (arm-[6]) is HOURLY-branch and forward-self-heals on the trailing window; older
     MART_TASK_GRAPH_DAILY rows keep compute-only credits (accepted, MEDIUM finding).
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

_AI = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' "
       "OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')")


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    matches = pat.findall(text)
    assert matches, (path, name)
    return matches[-1]


def apply(body: str, edits: list[tuple[str, str]], name: str) -> str:
    for old, new in edits:
        n = body.count(old)
        assert n == 1, f"{name}: needle x{n} (want 1): {old[:70]!r}"
        body = body.replace(old, new)
    return body


def apply_n(body: str, old: str, new: str, want: int, name: str) -> str:
    n = body.count(old)
    assert n == want, f"{name}: needle x{n} (want {want}): {old[:70]!r}"
    return body.replace(old, new)


# --- SP_LOAD_MARTS_V27 (from V060): C5 day-align + C2 arm-[6] QAS ------------
marts = extract_proc("V060__family_elapsed_queued_alert_guard.sql", "SP_LOAD_MARTS_V27")
# C5: all 3 AI-arm windows CURRENT_TIMESTAMP -> CURRENT_DATE (count-verified 3)
marts = apply_n(marts,
                "DATEADD('day', -:d, CURRENT_TIMESTAMP())",
                "DATEADD('day', -:d, CURRENT_DATE())", 3, "SP_LOAD_MARTS_V27:C5")
# C2: arm-[6] attribution SUM gains the QAS term (matches app graph_sql.py:54)
marts = apply(marts, [
    ("SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS",
     "SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS"),
], "SP_LOAD_MARTS_V27:C2")
# guardrails: prior-release fixes survive
assert marts.count("COUNT_IF(EXECUTION_STATUS = 'FAIL') AS FAILS,") == 4, "V057 FAIL fix"
assert "EXECUTION_STATUS = 'FAILED'" not in marts
assert marts.count("loaded := loaded || 'task_node ';") == 1, "V058 arm [6b]"
assert marts.count("a ON a.ROOT_ID = h.QUERY_ID") == 1, "V059 ROOT rollup"
assert marts.count("AS TOTAL_ELAPSED_SEC,") == 1, "V060 #5"
assert marts.count("QUEUED_PROVISIONING_TIME, 0)) / 1000, 1) AS QUEUED_SEC,") == 1, "V060 #11"
assert "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)" in marts
assert marts.count("DATEADD('day', -:d, CURRENT_TIMESTAMP())") == 0
assert marts.count("DATEADD('day', -:d, CURRENT_DATE())") == 10, "was 7, +3 AI arms"
assert marts.count("SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS") == 1


# --- SP_ALERT_SCAN (from V060): C1-mart + C6 + B41 --------------------------
alert = extract_proc("V060__family_elapsed_queued_alert_guard.sql", "SP_ALERT_SCAN")
# C1-mart edit 1: declare + read AI_CREDIT_PRICE_USD
alert = apply(alert, [(
    "DECLARE\n    budget_usd FLOAT;\n    credit_price FLOAT;\n    emsg VARCHAR;\n"
    "    fails INT DEFAULT 0;\nBEGIN\n"
    "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),\n"
    "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)\n"
    "      INTO :budget_usd, :credit_price\n"
    "    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;",
    "DECLARE\n    budget_usd FLOAT;\n    credit_price FLOAT;\n    ai_credit_price FLOAT;\n    emsg VARCHAR;\n"
    "    fails INT DEFAULT 0;\nBEGIN\n"
    "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),\n"
    "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),\n"
    "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)\n"
    "      INTO :budget_usd, :credit_price, :ai_credit_price\n"
    "    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;",
)], "SP_ALERT_SCAN:C1-declare")
# C1-mart edit 2: both mtd CTEs blend AI at :ai_credit_price (byte-identical x2)
_mtd_old = (
    "        mtd AS (\n        SELECT\n"
    "            SUM(CREDITS_BILLED) * :credit_price AS MTD_USD,\n"
    "            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,\n"
    "            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,\n"
    "            SUM(CREDITS_BILLED) * :credit_price\n"
    "                / NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD\n"
    "        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY\n"
    "        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())\n        )")
_mtd_new = (
    "        mtd AS (\n"
    "        -- C1: AI/Cortex credits bill at AI_CREDIT_PRICE_USD, not the compute\n"
    "        -- rate. Dollarize as a two-partition sum over the canonical AI predicate:\n"
    "        -- OTHER credits x :credit_price + AI credits x :ai_credit_price.\n"
    "        SELECT\n"
    f"            SUM(CASE WHEN {_AI} THEN 0 ELSE CREDITS_BILLED END) * :credit_price\n"
    f"              + SUM(CASE WHEN {_AI} THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price AS MTD_USD,\n"
    "            DAY(CURRENT_DATE()) AS DAY_OF_MONTH,\n"
    "            DAY(LAST_DAY(CURRENT_DATE())) AS DAYS_IN_MONTH,\n"
    f"            (SUM(CASE WHEN {_AI} THEN 0 ELSE CREDITS_BILLED END) * :credit_price\n"
    f"              + SUM(CASE WHEN {_AI} THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)\n"
    "                / NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD\n"
    "        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY\n"
    "        WHERE DAY >= DATE_TRUNC('month', CURRENT_DATE())\n        )")
alert = apply_n(alert, _mtd_old, _mtd_new, 2, "SP_ALERT_SCAN:C1-mtd")
# C6 edit 1: correct the carve-out comment
alert = apply(alert, [(
    "        -- SPCS, serverless tasks, pipes...). Warehouses and AI have their own\n"
    "        -- rules, so they are excluded here. Re-alerts weekly while creeping.",
    "        -- SPCS, serverless tasks, pipes...). Warehouses have their own daily-\n"
    "        -- credit rules and AI has COST_AI_CREEP, so both are excluded here.\n"
    "        -- Re-alerts weekly while creeping.",
)], "SP_ALERT_SCAN:C6-comment")
# C6 edit 2: insert the [13b] COST_AI_CREEP guarded arm after [13]'s EXCEPTION
_c6_block = (
    "                   'rule COST_SERVERLESS_CREEP - other rules unaffected', CURRENT_ROLE();\n"
    "    END;\n"
    "    -- [13b] COST_AI_CREEP\n"
    "    BEGIN\n"
    "        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n"
    "            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n"
    "        WITH cfg AS (\n"
    "            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n"
    "        )\n"
    "        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY\n"
    "        FROM (\n"
    "        -- COST_AI_CREEP: the canonical AI/Cortex bucket (SERVICE_TYPE ILIKE\n"
    "        -- '%CORTEX%'/'AI%'/'%INTELLIGENCE%') from FACT_METERING_DAILY growing\n"
    "        -- week-over-week, dollarized at the AI credit rate (AI_CREDIT_PRICE_USD,\n"
    "        -- NOT the compute rate). COST_SERVERLESS_CREEP carves AI out; this rule\n"
    "        -- owns it. Re-alerts weekly while creeping.\n"
    "        SELECT c.RULE_ID, 'ALL', c.SEVERITY,\n"
    "               'AI/Cortex spend up ' || ROUND(a.GROWTH_PCT, 0) || '% week-over-week ($' ||\n"
    "                   ROUND(a.THIS_WK_USD, 0) || ' vs $' || ROUND(a.PRIOR_WK_USD, 0) || ' prior 7d)',\n"
    "               'Last 7d ' || ROUND(a.THIS_WK_CR, 2) || ' AI credits ($' || ROUND(a.THIS_WK_USD, 2) ||\n"
    "                   ' @ $' || ROUND(:ai_credit_price, 2) || '/cr) vs ' || ROUND(a.PRIOR_WK_CR, 2) ||\n"
    "                   ' credits prior. Cortex/AI usage grows silently - confirm the workload is ' ||\n"
    "                   'intentional and priced in. Breakdown: Cost > Spend (by service).',\n"
    "               a.GROWTH_PCT,\n"
    "               c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))\n"
    "        FROM cfg c\n"
    "        JOIN (\n"
    "            SELECT SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS THIS_WK_CR,\n"
    "                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS PRIOR_WK_CR,\n"
    "                   SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS THIS_WK_USD,\n"
    "                   SUM(IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) * :ai_credit_price AS PRIOR_WK_USD,\n"
    "                   -- Onset (prior week 0) is an infinite ratio: emit a finite 999%\n"
    "                   -- sentinel so a brand-new AI workload FIRES (the case budget-pace\n"
    "                   -- misses) instead of GROWTH_PCT going NULL and dropping the row.\n"
    "                   CASE WHEN SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) = 0\n"
    "                        THEN IFF(SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) > 0, 999, 0)\n"
    "                        ELSE (SUM(IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))\n"
    "                              / SUM(IFF(DAY < DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0))\n"
    "                              - 1) * 100 END AS GROWTH_PCT\n"
    "            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY\n"
    "            WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())\n"
    f"              AND {_AI}\n"
    "        ) a ON c.RULE_ID = 'COST_AI_CREEP'\n"
    "           AND a.THIS_WK_CR >= 5 AND a.GROWTH_PCT > c.THRESHOLD_NUM\n"
    "\n"
    "        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n"
    "        WHERE NOT EXISTS (\n"
    "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "        );\n"
    "    EXCEPTION\n"
    "        WHEN OTHER THEN\n"
    "            emsg := SQLERRM;\n"
    "            fails := fails + 1;\n"
    "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG\n"
    "                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
    "            SELECT 'AlertScan', 'rule_block_failed', :emsg,\n"
    "                   'rule COST_AI_CREEP - other rules unaffected', CURRENT_ROLE();\n"
    "    END;\n"
    "    -- [14] PIPE_COPY_FAILURES")
alert = apply(alert, [(
    "                   'rule COST_SERVERLESS_CREEP - other rules unaffected', CURRENT_ROLE();\n"
    "    END;\n"
    "    -- [14] PIPE_COPY_FAILURES",
    _c6_block,
)], "SP_ALERT_SCAN:C6-insert")
# C6 edit 3: block count 19 -> 20 in the RETURN
alert = apply(alert, [(
    "    RETURN 'alert scan v10 (V045: task rule restored + r25 teeth kept): ' || (19 - :fails) || '/19 rule blocks ok';",
    "    RETURN 'alert scan v10 (V045: task rule restored + r25 teeth kept): ' || (20 - :fails) || '/20 rule blocks ok';",
)], "SP_ALERT_SCAN:C6-return")
# B41: self-alert block count literal 17 -> 20
alert = apply(alert, [(
    "               :fails || ' of 17 alert rule block(s) failed this run',",
    "               :fails || ' of 20 alert rule block(s) failed this run',",
)], "SP_ALERT_SCAN:B41")
# guardrails
assert alert.count("IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')") == 1
assert alert.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(w.WAREHOUSE_NAME)") == 1
assert alert.count("AND WAREHOUSE_ID > 0") == 1, "V060 CS-ratio guard"
assert alert.count("fails := fails + 1;") == 20, "19 + C6 block"
assert alert.count("* :ai_credit_price AS MTD_USD") == 2, "both mtd arms blended"
assert alert.count("SUM(CREDITS_BILLED) * :credit_price AS MTD_USD") == 0, "old compute-rate gone"
assert alert.count("c.RULE_ID = 'COST_AI_CREEP'") == 1
assert alert.count("of 20 alert rule block(s)") == 1 and "of 17" not in alert
assert alert.count("(20 - :fails) || '/20 rule blocks ok'") == 1 and "(19 - :fails)" not in alert
assert alert.count("ai_credit_price FLOAT;") == 1


# --- SP_CHANGE_IMPACT_SCAN (from V031): C2 x2 -------------------------------
chg = extract_proc("V031__scan_tuning_and_tagcov.sql", "SP_CHANGE_IMPACT_SCAN")
chg = apply(chg, [
    ("SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR\n"
     "                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY\n"
     "                  WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())",
     "SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR\n"
     "                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY\n"
     "                  WHERE START_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())"),
    ("SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR\n"
     "                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY\n"
     "                  WHERE START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)",
     "SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR\n"
     "                  FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY\n"
     "                  WHERE START_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)"),
], "SP_CHANGE_IMPACT_SCAN:C2")
assert chg.count("SUM(CREDITS_ATTRIBUTED_COMPUTE + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR") == 2
assert chg.count("SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CR") == 0
assert chg.count("CREDITS_USED_QUERY_ACCELERATION") == 2
assert "SP_CHANGE_IMPACT_SCAN()" in chg


# --- SP_PURGE_FACTS (from V055): B33 add 3 daily facts ----------------------
purge = extract_proc("V055__cloud_services_breakdown.sql", "SP_PURGE_FACTS")
purge = apply(purge, [(
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY\n"
    "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
    "    total := total + SQLROWCOUNT;\n\n"
    "    RETURN 'purged ' || :total || ' row(s)';",
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY\n"
    "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
    "    total := total + SQLROWCOUNT;\n\n"
    "    -- V061 (B33): three daily facts were never purged - same settings-driven\n"
    "    -- daily window (FACT_RETENTION_DAYS_DAILY), DAY grain, CURRENT_DATE() basis.\n"
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY\n"
    "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
    "    total := total + SQLROWCOUNT;\n"
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY\n"
    "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
    "    total := total + SQLROWCOUNT;\n"
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY\n"
    "     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());\n"
    "    total := total + SQLROWCOUNT;\n\n"
    "    RETURN 'purged ' || :total || ' row(s)';",
)], "SP_PURGE_FACTS:B33")
assert purge.count("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_PURGE_FACTS()") == 1
assert purge.count("DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY") == 1
assert purge.count("DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY") == 1
assert purge.count("DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY") == 1
assert purge.count("WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());") == 21
assert purge.count("RETURN 'purged ' || :total || ' row(s)';") == 1


# --- SP_LOAD_PLATFORM_SCORE (from V045): C1 score AI split ------------------
score = extract_proc("V045__task_monitoring_restored.sql", "SP_LOAD_PLATFORM_SCORE")
score = apply(score, [
    ("            SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED\n",
     "            SELECT DAY, SUM(CREDITS_BILLED) AS CREDITS_BILLED,\n"
     f"                   SUM(CASE WHEN {_AI} THEN CREDITS_BILLED ELSE 0 END) AS CREDITS_BILLED_AI\n"),
    ("        SELECT spend.DAY,\n               spend.CREDITS_BILLED,\n",
     "        SELECT spend.DAY,\n               spend.CREDITS_BILLED,\n               spend.CREDITS_BILLED_AI,\n"),
    ("        CREDITS_BILLED = s.CREDITS_BILLED, QUERY_COUNT = s.QUERY_COUNT,\n",
     "        CREDITS_BILLED = s.CREDITS_BILLED, CREDITS_BILLED_AI = s.CREDITS_BILLED_AI, QUERY_COUNT = s.QUERY_COUNT,\n"),
    ("        (DAY, CREDITS_BILLED, QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB,\n",
     "        (DAY, CREDITS_BILLED, CREDITS_BILLED_AI, QUERY_COUNT, FAILED_COUNT, QUEUED_SEC, SPILL_GB,\n"),
    ("    VALUES (s.DAY, s.CREDITS_BILLED, s.QUERY_COUNT, s.FAILED_COUNT, s.QUEUED_SEC,\n",
     "    VALUES (s.DAY, s.CREDITS_BILLED, s.CREDITS_BILLED_AI, s.QUERY_COUNT, s.FAILED_COUNT, s.QUEUED_SEC,\n"),
], "SP_LOAD_PLATFORM_SCORE:C1")
assert score.count("AS CREDITS_BILLED_AI") == 1
assert score.count("CREDITS_BILLED_AI") == 6
assert score.count("SP_LOAD_PLATFORM_SCORE(DAYS_BACK FLOAT)") == 1
assert score.count("CREDITS_BILLED = s.CREDITS_BILLED,") == 1


# --- assemble the migration -------------------------------------------------
out = f"""-- V061__ai_loader_alert_score_purge_fixes.sql
--
-- Correctness-only consolidation (perf loader restructures -> V062; SP_NOTIFY_WEBHOOK
-- B9 and the B5/B10/B11/B12 loader-robustness cluster HELD for a smoke-tested pass).
--
--   C5  SP_LOAD_MARTS_V27: the three AI arms (Cortex code Snowsight/CLI + Functions)
--       day-align to CURRENT_DATE() like every other daily arm, so the MERGE stops
--       overwriting a complete day with a post-window partial recount.
--   C2  Query-Acceleration credits added to the proc/pipeline attribution sums:
--       SP_LOAD_MARTS_V27 arm-[6] (MART_TASK_GRAPH_DAILY) and both SP_CHANGE_IMPACT_SCAN
--       arms now SUM(COMPUTE + COALESCE(QAS,0)), matching the measured contract + app.
--   C1  SP_ALERT_SCAN COST_BUDGET_PACE/COST_FORECAST_BREACH price MTD as OTHER x compute
--       + AI x AI rate (AI_CREDIT_PRICE_USD); FACT_PLATFORM_SCORE_DAILY gains
--       CREDITS_BILLED_AI and SP_LOAD_PLATFORM_SCORE populates it (app blend is a paired
--       follow-up).
--   C6  COST_AI_CREEP seeded + [13b] arm: WoW growth of the AI bucket at the AI rate.
--   B41 self-alert block-count literal 17 -> 20 (post-C6).
--   B33 SP_PURGE_FACTS now purges FACT_OBJECT_COST_DAILY, FACT_STORAGE_ACCOUNT_DAILY,
--       MART_TASK_NODE_DAILY.
--
--   Idempotent; apply AFTER V060. Reconcile on apply (also in DEPLOYMENT.md):
--     CALL SP_LOAD_MARTS_V27('DAILY', 365);   -- C5 full-retention heal (owner-chosen)
--     CALL SP_LOAD_PLATFORM_SCORE(120);        -- C1 backfill CREDITS_BILLED_AI
--   C2 arm-[6] (HOURLY branch) forward-self-heals; older MART_TASK_GRAPH_DAILY rows
--   keep compute-only credits (accepted).
--
-- Derivation law: SP_LOAD_MARTS_V27 + SP_ALERT_SCAN re-derived from V060,
-- SP_CHANGE_IMPACT_SCAN from V031, SP_PURGE_FACTS from V055, SP_LOAD_PLATFORM_SCORE
-- from V045, each + the enumerated edits; tests/test_v061_ai_loader_alert.py
-- byte-compares all five.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20061, 'V061 requires V060 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 60) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- C1: additive AI partition of billed credits on the score fact (history NULL;
-- the app reader/scoring COALESCE(...,0) so old days price all-compute, never NaN).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY
    ADD COLUMN IF NOT EXISTS CREDITS_BILLED_AI NUMBER(18,6);

-- C6: seed the AI-specific cost-creep rule (idempotent; WHEN NOT MATCHED only).
-- MUST precede the SP_ALERT_SCAN re-derivation and any CALL SP_ALERT_SCAN().
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'COST_AI_CREEP' AS RULE_ID, 'COST' AS FAMILY,
           'AI/Cortex credits up threshold % week-over-week' AS NAME,
           TRUE AS ENABLED, 'MEDIUM' AS SEVERITY, 50 AS THRESHOLD_NUM, 168 AS WINDOW_HOURS
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- >>> derived:SP_LOAD_MARTS_V27
{marts}
-- >>> derived:SP_ALERT_SCAN
{alert}
-- >>> derived:SP_CHANGE_IMPACT_SCAN
{chg}
-- >>> derived:SP_PURGE_FACTS
{purge}
-- >>> derived:SP_LOAD_PLATFORM_SCORE
{score}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 61 AS VERSION,
       'AI loader/alert/score/purge correctness: AI arms day-aligned (C5), QAS added to proc/pipeline attribution (C2), MTD alerts + score priced at the AI rate (C1), COST_AI_CREEP seeded (C6), self-alert block count fixed (B41), SP_PURGE_FACTS covers 3 more daily facts (B33)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 61);

-- Reconcile / heal (owner-chosen full-retention for C5). Idempotent; safe to run
-- separately/off-hours. The C5 heal rewrites FACT_AI_USAGE_DAILY rows the old
-- moving-timestamp window corrupted; the C1 call backfills CREDITS_BILLED_AI.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(120);
"""
target = Path(os.environ.get("V061_OUT") or (MIG / "V061__ai_loader_alert_score_purge_fixes.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
