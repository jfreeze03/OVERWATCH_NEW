#!/usr/bin/env python3
"""Forward-generate V169: SP_ALERT_SCAN_DAILY windows and keys (round-2 review, alerts cluster).

Reads V163__ai_runaway_trust_regression.sql ONLY -- the CURRENT definer of SP_ALERT_SCAN_DAILY (tests/
test_proc_lineage.py; V164-V168 never touch it) -- and emits, in order:

  guard (-20169, v < 168) -> marker + SP_ALERT_SCAN_DAILY re-derived from V163 -> one guarded ALERT_CONFIG NAME
  refresh (COST_EGRESS_SPIKE) -> SCHEMA_VERSION 169.

SP_ALERT_SCAN_DAILY deltas, each asserted by count (scoped to its arm's slice where the text repeats); everything
else is byte-identical to V163 and the V169 test normalizes it back:
  M     R2-041   both mtd CTEs gain MTD_COMPLETE_USD (the same two-partition pricing over DAY < today):
                 [08] COST_BUDGET_PACE compares it (not the today-inclusive MTD_USD) with the completed-days
                 allowance -- no false HIGH on days 2-5; [09] COST_FORECAST_BREACH projects
                 MTD_COMPLETE_USD + rate x (remaining days INCLUDING today). MTD_USD stays as an unused column.
  K16   R2-042 + R2-103   [16] COST_CONTRACT_BREACH: TOTAL only once CONTRACT_START_DATE parses (the app twin's r33
                 gate); CONSUMED within [start, CONTRACT_END_DATE) (end EXCLUSIVE, the app contract_pace clock);
                 a TERM_END column; nothing raises once today >= the end or when the projected exhaustion falls on
                 or after it (a blank end keeps V163's behaviour). The DAILY_BURN text is byte-identical.
  S12   R2-044   [12] COST_STORAGE_SURGE: LAG / ROW_NUMBER partition by DATABASE_ID over live rows (DELETED IS
                 NULL), so a dropped or re-created predecessor never pairs with the live database same-day.
  R18   R2-020 = R2-043   [18] DQ_RECON_ERROR: the r CTE carries MAX(LATEST_LOAD) AS NEWEST_LOAD and the key is
                 RULE|<newest error-cycle date> (still RULE|YYYY-MM-DD: V072 reads it as ACCOUNT, V117 strips it), so
                 a break no longer re-pages the next morning while its rows age out of the 48h window.
  E19   R2-047   [19] COST_EGRESS_SPIKE: the previous COMPLETE Central day, TRUE egress only (the Security > Egress
                 drill's predicate), top destination = that day's largest per-region total. Key unchanged.
  I24   R1-071   [24] COST_IDLE_OPPORTUNITY reads a NULL snapshot timer as 0 = never suspends (insights parity).
  T29   R1-233   [29] SEC_TRUST_REGRESSION: a THRESHOLD_NUM below 1 reads as 1.
  O22   holistic #4/#9   [22] OPS_PIPELINE_DEGRADED ERR leg: errs carries RERAISED (a PAGE 'AppCost' /
                 'StorageTruth' row -- V166's SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back, log and re-raise)
                 and the DETAIL says that run FAILED; every other loader's row keeps 'returned normally, so its task
                 still reads SUCCEEDED'. Byte-identical to V168's hourly twin (the V157 shared-arm design).
  R     the RETURN label names V169 (the tally stays 14: no arm is added).

R2-045 (pace ratio as METRIC_VALUE) is NOT here: it waits on the owner (owner_questions). The COST_EGRESS_SPIKE NAME
refresh touches the row only while NAME still equals its V043 seed. No CALL, DROP, task or ALERT_EVENTS write at
apply time. With PREFLIGHT_OUT / PART_B_OUT / REPAIR_OUT set, also writes the read-only PREFLIGHT (P169.1-P169.7),
the RUN_NEXT PART B verify grids (V169.1-V169.4) and the comment-only owner repair notes (the OPTIONAL R169.1
block, every statement commented out), built from the SAME arm text. The byte-identity test never sets them.

Run: python outputs/gen_v169.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V163__ai_runaway_trust_regression.sql"
V163 = BASE.read_text(encoding="utf-8")
NAME = "V169__alert_scan_daily_windows_and_keys.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert text.count(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}") == 1, name
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


def _swap_in(text: str, start: str, end: str, old: str, new: str, label: str, n: int = 1) -> str:
    """_swap inside the slice [start, end) only (an arm's span), the slice start unique."""
    assert text.count(start) == 1, f"{label}: slice start {start!r} x{text.count(start)}"
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + _swap(text[i:j], old, new, label, n) + text[j:]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


# ---------------------------------------------------------------------------------------------------
# Arm slices (each start unique in V163's SP_ALERT_SCAN_DAILY)
# ---------------------------------------------------------------------------------------------------
S08 = ("    -- [08] COST_BUDGET_PACE\n", "    -- [09] COST_FORECAST_BREACH\n")
S09 = ("    -- [09] COST_FORECAST_BREACH\n", "    -- [13b] COST_AI_CREEP\n")
S16 = ("    -- [16] COST_CONTRACT_BREACH\n", "    -- [12] COST_STORAGE_SURGE\n")
S12 = ("    -- [12] COST_STORAGE_SURGE\n", "    -- [13] COST_SERVERLESS_CREEP\n")
S19 = ("    -- [19] COST_EGRESS_SPIKE", "    -- [22] OPS_PIPELINE_DEGRADED")
S24 = ("    -- [24] COST_IDLE_OPPORTUNITY", "    -- [25] COST_SLEEP_POLLING")
S29 = ("    -- [29] SEC_TRUST_REGRESSION", "    -- [17] PIPE_REF_GAP")
S18 = ("    -- [18] DQ_RECON_ERROR", "    IF (fails > 0) THEN")
S22 = ("    -- [22] OPS_PIPELINE_DEGRADED", "    -- [24] COST_IDLE_OPPORTUNITY")

# ---- M (R2-041) ------------------------------------------------------------------------------------
AI_PRED = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%' OR "
           "SERVICE_TYPE ILIKE '%COCO%' OR SERVICE_TYPE ILIKE '%COWORK%')")
M_ANCHOR = (f"              + SUM(CASE WHEN {AI_PRED} THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price "
            "AS MTD_USD,\n")
M_ADD = (
    "            -- V169 (R2-041): month-to-date over COMPLETE days only (DAY < today), the numerator of\n"
    "            -- DAILY_RATE_USD. MTD_USD also holds the partial UTC row of today (the ~06:45 load sees part of\n"
    "            -- it), which the completed-days pace allowance ((DAY_OF_MONTH - 1) / DAYS_IN_MONTH) never budgets.\n"
    f"            SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT {AI_PRED} THEN CREDITS_BILLED ELSE 0 END) * :credit_price\n"
    f"              + SUM(CASE WHEN DAY < CURRENT_DATE() AND {AI_PRED} THEN CREDITS_BILLED ELSE 0 END) * "
    ":ai_credit_price AS MTD_COMPLETE_USD,\n")
P08_TITLE_OLD = ("               'MTD spend $' || ROUND(m.MTD_USD, 0) || ' is ' ||\n"
                 "                   ROUND(m.MTD_USD / NULLIF(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 2) ||\n")
P08_TITLE_NEW = ("               'MTD spend through yesterday $' || ROUND(m.MTD_COMPLETE_USD, 0) || ' is ' ||\n"
                 "                   ROUND(m.MTD_COMPLETE_USD / NULLIF(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), "
                 "2) ||\n")
P08_DETAIL_OLD = ("                   ROUND(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0) || '.',\n"
                  "               m.MTD_USD,\n")
P08_DETAIL_NEW = ("                   ROUND(:budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0) || ' for ' || "
                  "(m.DAY_OF_MONTH - 1)\n"
                  "                   || ' complete day(s); the partial metering of today is not counted.',\n"
                  "               m.MTD_COMPLETE_USD,\n")
P08_PRED_OLD = "         AND m.MTD_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM\n"
P08_PRED_NEW = "         AND m.MTD_COMPLETE_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM\n"
P09_TITLE_OLD = "                   ROUND(m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH), 0) ||\n"
P09_TITLE_NEW = ("                   ROUND(m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1), "
                 "0) ||\n")
P09_DETAIL_OLD = ("               'MTD $' || ROUND(m.MTD_USD, 0) || ' + $' || ROUND(m.DAILY_RATE_USD, 0) ||\n"
                  "                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH) || ' remaining days.',\n"
                  "               m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH),\n")
P09_DETAIL_NEW = ("               'MTD through yesterday $' || ROUND(m.MTD_COMPLETE_USD, 0) || ' + $' || "
                  "ROUND(m.DAILY_RATE_USD, 0) ||\n"
                  "                   '/day x ' || (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1) || ' remaining days incl. "
                  "today.',\n"
                  "               m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1),\n")
P09_PRED_OLD = "         AND (m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH))\n"
P09_PRED_NEW = "         AND (m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1))\n"

# ---- K16 (R2-042 + R2-103) ---------------------------------------------------------------------------
K16_COMMENT_OLD = "re-fire (cost-hunt6).\n"
K16_COMMENT_NEW = (
    "re-fire (cost-hunt6).\n"
    "        -- V169 (R2-042, R2-103): TOTAL counts only once CONTRACT_START_DATE parses (the app twin\n"
    "        -- mart_sql.contract_exhaustion, r33); CONSUMED counts [start, CONTRACT_END_DATE) -- the end is EXCLUSIVE,\n"
    "        -- the app contract_pace clock -- and nothing raises once CURRENT_DATE() >= the end or when the projected\n"
    "        -- exhaustion falls on or after it. A blank end keeps the unbounded pre-V169 behaviour.\n")
K16_COLS_OLD = "            SELECT TOTAL, CONSUMED, DAILY_BURN,\n"
K16_COLS_NEW = "            SELECT TOTAL, CONSUMED, DAILY_BURN, TERM_END,\n"
K16_TOTAL_OLD = ("                    (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0)\n"
                 "                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TOTAL,\n")
K16_TOTAL_NEW = ("                    (SELECT IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,\n"
                 "                                COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CONTRACT_CREDITS', VALUE, NULL))), 0))\n"
                 "                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TOTAL,\n")
K16_CONSUMED_OLD = "                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), CURRENT_DATE())) AS CONSUMED,\n"
K16_CONSUMED_NEW = (
    "                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), CURRENT_DATE())\n"
    "                       AND DAY < COALESCE(\n"
    "                         (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))\n"
    "                          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), '9999-12-31'::DATE)) AS CONSUMED,\n"
    "                    (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL)))\n"
    "                     FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) AS TERM_END,\n")
K16_JOIN_OLD = "           AND p.DAYS_LEFT <= c.THRESHOLD_NUM\n"
K16_JOIN_NEW = ("           AND p.DAYS_LEFT <= c.THRESHOLD_NUM\n"
                "           AND (p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))\n")

# ---- S12 (R2-044) -----------------------------------------------------------------------------------
S12_PART_OLD = "PARTITION BY DATABASE_NAME ORDER BY USAGE_DATE"
S12_PART_NEW = "PARTITION BY DATABASE_ID ORDER BY USAGE_DATE"
S12_HEAD_OLD = "        JOIN (\n            SELECT DATABASE_NAME, USAGE_DATE,\n"
S12_HEAD_NEW = (
    "        JOIN (\n"
    "            -- V169 (R2-044): one series per LIVE database id. A dropped or re-created predecessor keeps\n"
    "            -- reporting rows (DELETED set) under the same DATABASE_NAME while its Time Travel and Fail-safe\n"
    "            -- bytes remain, and a by-name window paired the two ids on the same day arbitrarily. A re-created\n"
    "            -- database has no PREV on its first day and does not raise.\n"
    "            SELECT DATABASE_NAME, USAGE_DATE,\n")
S12_WHERE_OLD = "            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())\n            QUALIFY"
S12_WHERE_NEW = ("            WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())\n"
                 "              AND DELETED IS NULL\n            QUALIFY")

# ---- R18 (R2-020 = R2-043) ----------------------------------------------------------------------------
R18_R_OLD = "                   LISTAGG(MTRC, ', ') WITHIN GROUP (ORDER BY N DESC) AS TOP_METRICS\n"
R18_R_NEW = ("                   LISTAGG(MTRC, ', ') WITHIN GROUP (ORDER BY N DESC) AS TOP_METRICS,\n"
             "                   MAX(LATEST_LOAD) AS NEWEST_LOAD   -- V169 (R2-020/R2-043): the newest error cycle in "
             "the window\n")
R18_KEY_OLD = ("               r.ERRORS,\n"
               "               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())\n"
               "        FROM cfg c\n"
               "        JOIN r ON c.RULE_ID = 'DQ_RECON_ERROR' AND r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)\n")
R18_KEY_NEW = ("               r.ERRORS,\n"
               "               c.RULE_ID || '|' || TO_VARCHAR(COALESCE(TO_DATE(r.NEWEST_LOAD), CURRENT_DATE()))   -- "
               "V169: one event per newest error-cycle day\n"
               "        FROM cfg c\n"
               "        JOIN r ON c.RULE_ID = 'DQ_RECON_ERROR' AND r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)\n")

# ---- E19 (R2-047) -----------------------------------------------------------------------------------
E19_CTE_OLD = ("        WITH cfg AS (\n"
               "            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n"
               "        )\n")
EGRESS_PRED = "(TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)"
XFER = f"""xfer AS (
            -- V169 (R2-047): one row per Central day and destination. TRUE egress only -- the predicate of the
            -- Security > Egress drill (security_sql.egress_baseline): a same-region internal transfer moves no
            -- data out of the account.
            SELECT CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE AS DAY,
                   COALESCE(TARGET_REGION, '(same region)') AS DEST,
                   SUM(BYTES_TRANSFERRED) AS BYTES
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -{{XFER_DAYS}}, CURRENT_TIMESTAMP())
              AND {EGRESS_PRED}
            GROUP BY 1, 2
        )"""
CLK = """clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        )"""
XFER_ARM = XFER.replace("{XFER_DAYS}", "16")       # the Central midnight of TODAY-14, across DST
E19_CTE_NEW = ("        WITH cfg AS (\n"
               "            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n"
               f"        ),\n        {CLK},\n        {XFER_ARM}\n")
E19_TITLE_OLD = "               'Egress ' || eg.GB_24H || ' GB in 24h (14d avg ' || eg.GB_AVG_14D || ' GB/day)',\n"
E19_TITLE_NEW = ("               'Egress ' || eg.GB_DAY || ' GB on ' || TO_VARCHAR(eg.SPIKE_DAY) || ' (14d avg ' || "
                 "eg.GB_AVG_14D || ' GB/day)',\n")
E19_METRIC_OLD = "               eg.GB_24H,\n"
E19_METRIC_NEW = "               eg.GB_DAY,\n"
EG_DAY = """SELECT MAX(DATEADD('day', -1, k.TODAY)) AS SPIKE_DAY,
                   ROUND(SUM(IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, 0)) / POWER(1024, 3), 1) AS GB_DAY,
                   ROUND(SUM(IFF(x.DAY >= DATEADD('day', -14, k.TODAY), x.BYTES, 0)) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(x.DEST, IFF(x.DAY = DATEADD('day', -1, k.TODAY), x.BYTES, NULL)) AS TOP_REGION
            FROM xfer x
            CROSS JOIN clk k
            WHERE x.DAY < k.TODAY"""
E19_EG_OLD = """            SELECT ROUND(SUM(IFF(START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP()),
                                 BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 1) AS GB_24H,
                   ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(TARGET_REGION, BYTES_TRANSFERRED) AS TOP_REGION
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
        ) eg
          ON c.RULE_ID = 'COST_EGRESS_SPIKE'
         AND eg.GB_24H >= c.THRESHOLD_NUM
"""
E19_EG_NEW = f"""            -- V169 (R2-047): the previous COMPLETE Central day. The scan runs ~06:50-07:30 Central, past the ~2h
            -- view latency, so consecutive daily windows tile the timeline (no blind slice before each scan, no
            -- task-jitter gap or overlap); the top destination is the largest per-region total of that day.
            {EG_DAY}
        ) eg
          ON c.RULE_ID = 'COST_EGRESS_SPIKE'
         AND eg.GB_DAY >= c.THRESHOLD_NUM
"""

# ---- I24 (R1-071) -----------------------------------------------------------------------------------
I24_HEAD_OLD = "    --      threshold) re-fires mid-week and the V067 sweep supersedes the MED one.)\n"
I24_HEAD_NEW = (
    "    --      threshold) re-fires mid-week and the V067 sweep supersedes the MED one.)\n"
    "    --      V169 (R1-071): a NULL snapshot timer reads as 0 = never suspends (SHOW WAREHOUSES reports a\n"
    "    --      never-suspend warehouse as a NULL auto_suspend), like insights.show_auto_suspend and the mart loader.\n")
I24_COL_OLD = "                   s.RECOVERABLE_CREDITS, w.AUTO_SUSPEND, w.SNAPSHOT_AT,\n"
I24_COL_NEW = "                   s.RECOVERABLE_CREDITS, COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND, w.SNAPSHOT_AT,\n"
I24_PRED_OLD = ("              AND w.AUTO_SUSPEND IS NOT NULL\n"
                "              AND (w.AUTO_SUSPEND <= 0 OR w.AUTO_SUSPEND > 60)\n")
I24_PRED_NEW = "              AND (COALESCE(w.AUTO_SUSPEND, 0) <= 0 OR w.AUTO_SUSPEND > 60)\n"

# ---- T29 (R1-233) -----------------------------------------------------------------------------------
T29_HEAD_OLD = "    --      Center shows the live count). Company ALL, HIGH (c.SEVERITY).)\n"
T29_HEAD_NEW = ("    --      Center shows the live count). Company ALL, HIGH (c.SEVERITY).)\n"
                "    --      V169 (R1-233): a THRESHOLD_NUM below 1 reads as 1 -- a regression is a rise; 0 raised every "
                "unchanged count.\n")
T29_PRED_OLD = "         AND s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)\n"
T29_PRED_NEW = "         AND s.CUR_N - s.PRIOR_N >= GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)\n"

# ---- O22 (holistic #4/#9; identical constants in outputs/gen_v168.py -- the twin arm) --------------
# V166 made SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back, log fact_load_failed (PAGE 'AppCost' /
# 'StorageTruth') and RE-RAISE, so their task reads FAILED; every earlier ERR-leg source logs and returns normally.
O22_RERAISE_PAGES = ("AppCost", "StorageTruth")
O22_HEAD_OLD = (
    "    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged and swallowed\n"
    "    --      (its task still reads SUCCEEDED) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS -- one\n")
O22_HEAD_NEW = (
    "    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged -- most\n"
    "    --      loaders swallow it (their task still reads SUCCEEDED); V166's SP_LOAD_APP_COST and\n"
    "    --      SP_LOAD_STORAGE_TRUTH roll back to the previous fill and re-raise (their task reads FAILED), and\n"
    "    --      the DETAIL says which (RERAISED, V168 + V169) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS"
    " -- one\n")
O22_ERRS_OLD = "                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG\n"
O22_ERRS_NEW = (
    "                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG,\n"
    "                   MAX(IFF(PAGE IN ("
    + ", ".join(f"'{p}'" for p in O22_RERAISE_PAGES)
    + "), 1, 0)) AS RERAISED   -- the V166 loads that roll back and re-raise\n")
O22_DETAIL_OLD = (
    "               LEFT('The loader logged this and returned normally, so its task still reads SUCCEEDED and readers '\n"
    "                   || 'keep the previous fill. Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '\n")
O22_DETAIL_NEW = (
    "               LEFT(IFF(x.RERAISED = 1,\n"
    "                        'The loader rolled back to its previous fill, logged this and re-raised: the run FAILED '\n"
    "                        || '(a scheduled run shows FAILED in TASK_HISTORY; a hand CALL raised the error to its '\n"
    "                        || 'caller) and readers keep the previous fill.',\n"
    "                        'The loader logged this and returned normally, so its task still reads SUCCEEDED and '\n"
    "                        || 'readers keep the previous fill.')\n"
    "                   || ' Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '\n")

# ---- R ------------------------------------------------------------------------------------------------
RET_163 = ("'alert scan daily v5 (V163: + COST_AI_USER_RUNAWAY + SEC_TRUST_REGRESSION, [07] burst-vs-lockout "
           "wording): '")
RET_169 = ("'alert scan daily v6 (V169: [08]/[09] complete-day MTD, [16] contract term, [12] live database id, "
           "[18] error-cycle day, [19] previous-day true egress, [24] NULL timer, [29] floor): '")

daily = extract_proc(V163, "SP_ALERT_SCAN_DAILY()")
assert daily.count("fails := fails + 1") == 14
assert daily.count(M_ANCHOR) == 2 and daily.count("AS DAILY_BURN") == 1
assert daily.count(S12_PART_OLD) == 3 and daily.count("c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())") == 2
assert daily.count("COALESCE(c.THRESHOLD_NUM, 1)") == 3
assert daily.count("still reads SUCCEEDED") == 3 and "RERAISED" not in daily
_burn = _between(daily, "                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)",
                 "AS DAILY_BURN")

daily = _swap(daily, M_ANCHOR, M_ANCHOR + M_ADD, "M", n=2)
daily = _swap_in(daily, *S08, P08_TITLE_OLD, P08_TITLE_NEW, "P08a")
daily = _swap_in(daily, *S08, P08_DETAIL_OLD, P08_DETAIL_NEW, "P08b")
daily = _swap_in(daily, *S08, P08_PRED_OLD, P08_PRED_NEW, "P08c")
daily = _swap_in(daily, *S09, P09_TITLE_OLD, P09_TITLE_NEW, "P09a")
daily = _swap_in(daily, *S09, P09_DETAIL_OLD, P09_DETAIL_NEW, "P09b")
daily = _swap_in(daily, *S09, P09_PRED_OLD, P09_PRED_NEW, "P09c")
daily = _swap_in(daily, *S16, K16_COMMENT_OLD, K16_COMMENT_NEW, "K16a")
daily = _swap_in(daily, *S16, K16_COLS_OLD, K16_COLS_NEW, "K16b")
daily = _swap_in(daily, *S16, K16_TOTAL_OLD, K16_TOTAL_NEW, "K16c")
daily = _swap_in(daily, *S16, K16_CONSUMED_OLD, K16_CONSUMED_NEW, "K16d")
daily = _swap_in(daily, *S16, K16_JOIN_OLD, K16_JOIN_NEW, "K16e")
daily = _swap_in(daily, *S12, S12_PART_OLD, S12_PART_NEW, "S12a", n=3)
daily = _swap_in(daily, *S12, S12_HEAD_OLD, S12_HEAD_NEW, "S12b")
daily = _swap_in(daily, *S12, S12_WHERE_OLD, S12_WHERE_NEW, "S12c")
daily = _swap_in(daily, *S18, R18_R_OLD, R18_R_NEW, "R18a")
daily = _swap_in(daily, *S18, R18_KEY_OLD, R18_KEY_NEW, "R18b")
daily = _swap_in(daily, *S19, E19_CTE_OLD, E19_CTE_NEW, "E19a")
daily = _swap_in(daily, *S19, E19_TITLE_OLD, E19_TITLE_NEW, "E19b")
daily = _swap_in(daily, *S19, E19_METRIC_OLD, E19_METRIC_NEW, "E19c")
daily = _swap_in(daily, *S19, E19_EG_OLD, E19_EG_NEW, "E19d")
daily = _swap_in(daily, *S24, I24_HEAD_OLD, I24_HEAD_NEW, "I24a")
daily = _swap_in(daily, *S24, I24_COL_OLD, I24_COL_NEW, "I24b")
daily = _swap_in(daily, *S24, I24_PRED_OLD, I24_PRED_NEW, "I24c")
daily = _swap_in(daily, *S29, T29_HEAD_OLD, T29_HEAD_NEW, "T29a")
daily = _swap_in(daily, *S29, T29_PRED_OLD, T29_PRED_NEW, "T29b")
daily = _swap_in(daily, *S22, O22_HEAD_OLD, O22_HEAD_NEW, "O22a")
daily = _swap_in(daily, *S22, O22_ERRS_OLD, O22_ERRS_NEW, "O22b")
daily = _swap_in(daily, *S22, O22_DETAIL_OLD, O22_DETAIL_NEW, "O22c")
daily = _swap(daily, RET_163, RET_169, "R")

# ---- post-asserts on the derived body -----------------------------------------------------------------
assert daily.count("fails := fails + 1") == 14                                     # tally unchanged
assert daily.count("(14 - :fails)") == 5 and daily.count("'/14 rule blocks ok (daily)'") == 3
assert daily.count("AS MTD_COMPLETE_USD,") == 2
assert daily.count("m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1)") == 3
for span in (S08, S09):
    _code = "\n".join(ln.split("--")[0] for ln in _between(daily, *span).splitlines())
    assert "m.MTD_USD" not in _code, span
assert _between(daily, "                    (SELECT COALESCE(SUM(CREDITS_BILLED), 0) / NULLIF(COUNT(DISTINCT DAY), 0)",
                "AS DAILY_BURN") == _burn                                          # history_locks/test_rec10
assert _between(daily, *S12).count("PARTITION BY DATABASE_NAME") == 0
assert daily.count("c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())") == 1          # [19] keeps its scan-day key
assert daily.count("COALESCE(c.THRESHOLD_NUM, 1)") == 3                             # [17] [18] + the floored [29]
assert "AND w.AUTO_SUSPEND IS NOT NULL" not in daily
assert "MAX_BY(TARGET_REGION, BYTES_TRANSFERRED)" not in daily and "GB_24H" not in daily
_d_body = daily[daily.index("$$") + 2:daily.rindex("$$")]
assert "$$" not in _d_body and "\\" not in _d_body
assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _d_body)) == set(
    re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", extract_proc(V163, "SP_ALERT_SCAN_DAILY()")))
assert "ct_hour" not in daily                                                      # the daily scan stays ungated
_a22 = _between(daily, *S22)
assert _a22.count("AS RERAISED") == 1 and _a22.count("x.RERAISED = 1") == 1 and "RERAISED" not in daily.replace(_a22, "")
assert _a22.count("the run FAILED") == 1 and _a22.count("returned normally, so its task still reads SUCCEEDED") == 1

# ---------------------------------------------------------------------------------------------------
# The ALERT_CONFIG NAME refresh (guarded on the V043 seed text)
# ---------------------------------------------------------------------------------------------------
NAME_EGRESS_SEED = "Outbound transfer above threshold (GB / 24h)"
NAME_EGRESS_NEW = "True egress (cross-region or cross-cloud) above threshold GB on the previous complete Central day"
assert len(NAME_EGRESS_NEW) <= 200 and "'" not in NAME_EGRESS_NEW and ";" not in NAME_EGRESS_NEW

NAMES = f"""
-- Rule NAME text follows the new window. The refresh touches the row only while NAME still equals its V043 seed, so
-- an operator's own edit survives; a re-run is a no-op. ALERT_CONFIG has no DESCRIPTION column.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = '{NAME_EGRESS_NEW}'
 WHERE RULE_ID = 'COST_EGRESS_SPIKE'
   AND NAME = '{NAME_EGRESS_SEED}';
"""

HEADER = f"""-- {NAME}
--
-- Round-2 review, alerts cluster (R2-041, R2-042, R2-103, R2-044, R2-020 = R2-043, R2-047, R1-071, R1-233): the
-- nightly scan's windows and keys.
--
-- WHY: (R2-041) COST_BUDGET_PACE compared a month-to-date total that includes the partial current day with a
-- completed-days allowance, so an on-budget month raised a false HIGH on days 2-5; COST_FORECAST_BREACH never
-- projected the rest of today. (R2-042) COST_CONTRACT_BREACH ignored CONTRACT_END_DATE: credits after the term
-- counted, and an exhaustion projected past the end (or a term already over) paged CRITICAL while the Contract
-- tab said the term is fine or over. (R2-103) with credits set and no start date the arm fabricated a runway from
-- about 0 consumed. (R2-044) COST_STORAGE_SURGE paired a dropped or re-created database with the live one by name.
-- (R2-020 / R2-043) DQ_RECON_ERROR keyed on the scan date, so a break in the 48h window paged twice. (R2-047)
-- COST_EGRESS_SPIKE named the destination of the largest single row in 14 days, counted same-region internal
-- moves, and its rolling window never counted the ~2h before each scan. (R1-071) COST_IDLE_OPPORTUNITY skipped a
-- never-suspend warehouse (SHOW reports a NULL timer). (R1-233) SEC_TRUST_REGRESSION fired on unchanged counts at
-- a threshold of 0. (Holistic #4/#9) the OPS_PIPELINE_DEGRADED ERR DETAIL told every logged loader failure 'its task
-- still reads SUCCEEDED', but V166's SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back and re-raise, so
-- TASK_HISTORY shows those runs FAILED.
--
--   ~ SP_ALERT_SCAN_DAILY re-derived from V163 (its current definer), byte-identical except:
--     ~ both mtd CTEs + MTD_COMPLETE_USD (DAY < today, the two-partition pricing): [08] pace and [09] forecast use
--       it; [09] projects the remaining days INCLUDING today. TITLE / DETAIL say through yesterday.
--     ~ [16]: TOTAL only with a parsable CONTRACT_START_DATE; CONSUMED within [start, CONTRACT_END_DATE) (end
--       exclusive); TERM_END; no event once today >= the end or when the exhaustion falls on or after it. The
--       burn window, bands, key, severity and METRIC_VALUE (DAYS_LEFT) are unchanged.
--     ~ [12]: LAG / ROW_NUMBER per DATABASE_ID over rows with DELETED IS NULL (title, key, predicate unchanged).
--     ~ [18]: the r CTE carries MAX(LATEST_LOAD) AS NEWEST_LOAD; key = RULE|<newest error-cycle date>.
--     ~ [19]: the previous complete Central day of TRUE egress (TARGET_REGION or TARGET_CLOUD set), top
--       destination = the largest per-region total of that day; TITLE 'Egress N GB on <day> (14d avg ...)'.
--     ~ [24]: a NULL snapshot timer reads as 0 (never suspends); [29]: GREATEST(COALESCE(THRESHOLD_NUM, 1), 1).
--     ~ [22] OPS_PIPELINE_DEGRADED ERR leg: errs carries RERAISED (a PAGE 'AppCost' / 'StorageTruth' row, the V166
--       loaders that roll back, log and re-raise); the DETAIL says that run FAILED (a scheduled run shows FAILED
--       in TASK_HISTORY; a hand CALL raised the error to its caller), and keeps 'returned normally, so its task
--       still reads SUCCEEDED' for every other loader. Keys, sources and windows unchanged; byte-identical to
--       V168's hourly twin.
--     ~ the RETURN label names V169; the 14-block tally is unchanged.
--   ~ ALERT_CONFIG NAME of COST_EGRESS_SPIKE, only while it still equals the V043 seed text.
--
-- COST: unchanged in kind: two extra conditional SUMs over the month of FACT_METERING_DAILY, two SETTINGS
-- sub-selects, a 16-day DATA_TRANSFER_HISTORY read grouped by day (was 14 days ungrouped).
-- LATENCY: daily (~07:00 Central). An egress spike now pages the next morning (the previous complete day).
-- FIRST RUN: the next daily scan. Expect: no COST_BUDGET_PACE on days 2-5 of an on-budget month; COST_FORECAST_BREACH
-- slightly more sensitive (it now projects today); COST_IDLE_OPPORTUNITY may raise for never-suspend warehouses for
-- the first time (PREFLIGHT P169.6 lists them); a contract past its end or outlasting it goes quiet; one egress
-- spike confined to 00:00-07:00 of yesterday can raise once more. Nothing runs at apply time.
-- DQ_RECON_ERROR keeps two residuals of its cycle-day key. (a) Once, at the transition: a pre-V169 scan keyed the
-- SCAN date, so a failing cycle that loads later on a date the old scan already keyed (A 21:00 after the A ~07:00
-- scan wrote DQ_RECON_ERROR|A) folds into that older event and is not paged; V163 would have paged it as |A+1.
-- (b) Standing: a failing re-run that loads on a date whose event already exists (after that morning's scan)
-- folds into that date's event and does not page the next morning -- one page per failing cycle date, the
-- Reconciliation errors panel's cycle definition. PREFLIGHT P169.4 shows the newest load and its hour; PART B
-- V169.4 (the morning after the first scan) flags a newest cycle covered only by an event raised before it loaded.
-- ROLLBACK: re-run V163's SP_ALERT_SCAN_DAILY (V163__ai_runaway_trust_regression.sql, the CREATE PROCEDURE); the
-- NAME text can stay.
-- Apply AFTER V168. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20169, 'V169 requires V168 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 168) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARKER = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V163; [08]/[09] complete-day MTD, [16] contract start gate + "
          "end bound, [12] live DATABASE_ID, [18] error-cycle-day key, [19] previous-day true egress, [24] NULL timer "
          "as 0, [29] threshold floor, [22] ERR re-raise wording, V169)\n")

DESCRIPTION = (
    "Round-2 review, alerts cluster (R2-041, R2-042, R2-103, R2-044, R2-020, R2-043, R2-047, R1-071, R1-233). "
    "SP_ALERT_SCAN_DAILY re-derived from V163, byte-identical except: both mtd CTEs gain MTD_COMPLETE_USD (complete "
    "days only), which COST_BUDGET_PACE compares with the completed-days allowance and COST_FORECAST_BREACH projects "
    "with the remaining days including today; COST_CONTRACT_BREACH counts TOTAL only with a parsable "
    "CONTRACT_START_DATE, bounds CONSUMED by CONTRACT_END_DATE (exclusive) and raises nothing once the term is over "
    "or when the exhaustion falls on or after its end; COST_STORAGE_SURGE compares per live DATABASE_ID; "
    "DQ_RECON_ERROR keys on the newest error-cycle date; COST_EGRESS_SPIKE reads the previous complete Central day "
    "of true egress with the top destination by per-region total; COST_IDLE_OPPORTUNITY reads a NULL snapshot timer "
    "as never suspends; SEC_TRUST_REGRESSION floors its threshold at 1; the OPS_PIPELINE_DEGRADED ERR detail says a "
    "run of the V166 app-cost or storage-truth loader rolled back and FAILED instead of claiming its task still reads "
    "SUCCEEDED; RETURN names V169, tally 14 unchanged. "
    "ALERT_CONFIG NAME of COST_EGRESS_SPIKE refreshed only while it equals the seed text. No task change, no new "
    "object, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 169 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169);
"""

out = HEADER + MARKER + daily + "\n" + NAMES + VERSION_ROW

# ---- post-asserts on the file -------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.count("-- >>> derived:") == 1 and "LINEAGE-WAIVER" not in out
_top = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK|CALL |DROP |DELETE )", _top,
                     re.M | re.I)
assert "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" not in _top
assert re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.(\w+)", _top) == ["ALERT_CONFIG"]
assert "V169 requires V168 first" in out and "SELECT 169 AS VERSION" in out
assert "COALESCE(SUM(CREDITS_BILLED), 0) / 30" not in out                         # history_locks/test_rec10
for _new in (HEADER, MARKER, M_ADD, P08_TITLE_NEW, P08_DETAIL_NEW, P08_PRED_NEW, P09_TITLE_NEW, P09_DETAIL_NEW,
             P09_PRED_NEW, K16_COMMENT_NEW, K16_COLS_NEW, K16_TOTAL_NEW, K16_CONSUMED_NEW, K16_JOIN_NEW, S12_HEAD_NEW,
             S12_WHERE_NEW, R18_R_NEW, R18_KEY_NEW, E19_CTE_NEW, E19_TITLE_NEW, E19_EG_NEW, I24_HEAD_NEW, I24_COL_NEW,
             I24_PRED_NEW, T29_HEAD_NEW, T29_PRED_NEW, O22_HEAD_NEW, O22_ERRS_NEW, O22_DETAIL_NEW, RET_169, NAMES,
             VERSION_ROW):
    assert _new.isascii(), _new[:60]                     # (V163's carried body keeps its own em dashes)
for line in out.splitlines():
    assert not line.lstrip().upper().startswith("CALL ") or line.startswith("        "), line

target = Path(os.environ.get("V169_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------------------------------
# Read-only PREFLIGHT (P169.1-P169.7), built from the DERIVED arm text; the scan's :binds become the scan's own
# SETTINGS reads (scalar sub-selects with the same fallbacks).
# ---------------------------------------------------------------------------------------------------
_BINDS = {
    ":budget_usd": "(SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0) "
                   "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)",
    ":ai_credit_price": "(SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) "
                        "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)",
    ":credit_price": "(SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) "
                     "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)",
}
assert re.search(r"COALESCE\(TRY_TO_DOUBLE\(MAX\(IFF\(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL\)\)\), 0\),\n", daily)


def _bind(sql: str) -> str:
    for k in (":budget_usd", ":ai_credit_price", ":credit_price"):     # longest first (no prefix clash)
        sql = sql.replace(k, _BINDS[k])
    assert not re.search(r"(?<![:\w]):[a-z_]+\b", sql.replace("HH24:MI", "")), sql
    return sql


_a08 = _between(daily, *S08)
MTD_CTE = _between(_a08, "        mtd AS (\n", "        SELECT b.RULE_ID")
assert MTD_CTE.rstrip().endswith(")")
_a16 = _between(daily, *S16)
P16 = _a16[_a16.index("        JOIN (\n            SELECT TOTAL") + len("        JOIN "):_a16.index(" p ON c.RULE_ID")]
_a12 = _between(daily, *S12)
G12 = _a12[_a12.index("        JOIN (\n") + len("        JOIN "):_a12.index(" g ON c.RULE_ID")]
G12_OLD = _between(extract_proc(V163, "SP_ALERT_SCAN_DAILY()"), *S12)
G12_OLD = G12_OLD[G12_OLD.index("        JOIN (\n") + len("        JOIN "):G12_OLD.index(" g ON c.RULE_ID")]
_a24 = _between(daily, *S24)
CHAIN24 = _a24[_a24.index("        WITH cfg AS (\n"):_a24.index("        SELECT b.RULE_ID")]

PREFLIGHT = f"""-- ====================================================================================================
--  V169 PREFLIGHT (read-only; run BEFORE applying V169, in a Central session). SP_ALERT_SCAN_DAILY windows and keys.
--  Each grid runs the arm's OWN text (outputs/gen_v169.py); the scan's binds are its own SETTINGS reads.
--  Changes nothing.
-- ====================================================================================================

-- P169.1 R2-041: this month's pace and forecast, old (V163) vs new (V169). OLD_PACE_RATIO counts the partial day
--        in the numerator; NEW_PACE_RATIO is MTD through yesterday over the completed-days allowance.
WITH {_bind(MTD_CTE.strip())}
SELECT m.DAY_OF_MONTH, m.DAYS_IN_MONTH, ROUND(m.MTD_USD, 0) AS MTD_USD, ROUND(m.MTD_COMPLETE_USD, 0) AS MTD_COMPLETE_USD,
       ROUND(m.DAILY_RATE_USD, 0) AS DAILY_RATE_USD,
       ROUND(m.MTD_USD / NULLIF({_BINDS[':budget_usd']} * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 3) AS OLD_PACE_RATIO,
       ROUND(m.MTD_COMPLETE_USD / NULLIF({_BINDS[':budget_usd']} * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH, 0), 3)
           AS NEW_PACE_RATIO,
       ROUND(m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH), 0) AS OLD_PROJECTION,
       ROUND(m.MTD_COMPLETE_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - m.DAY_OF_MONTH + 1), 0) AS NEW_PROJECTION,
       {_BINDS[':budget_usd']} AS MONTHLY_BUDGET_USD
FROM mtd m;

-- P169.2 R2-042 / R2-103: the contract settings and the [16] p subquery verbatim (CONSUMED, TERM_END, DAYS_LEFT,
--        EXHAUST_DATE), with WOULD_RAISE_NEW, plus every OPEN / ACK COST_CONTRACT_BREACH event and whether the new
--        arm would still raise it. Resolve the ones it would not in Alerts (and close a matching auto-declared incident).
SELECT KEY, VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY LIKE 'CONTRACT%' ORDER BY KEY;
WITH p AS {P16},
thr AS (
    SELECT COALESCE(MAX(IFF(ENABLED, THRESHOLD_NUM, NULL)), 30) AS THRESHOLD_NUM
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_CONTRACT_BREACH'
)
SELECT p.TOTAL, p.CONSUMED, p.DAILY_BURN, p.TERM_END, p.DAYS_LEFT, p.EXHAUST_DATE,
       (p.TOTAL > 0 AND p.DAILY_BURN > 0 AND p.DAYS_LEFT <= t.THRESHOLD_NUM
        AND (p.TERM_END IS NULL OR (CURRENT_DATE() < p.TERM_END AND p.EXHAUST_DATE < p.TERM_END))) AS WOULD_RAISE_NEW,
       e.EVENT_ID, e.STATUS, e.SEVERITY, e.RAISED_AT, e.TITLE
FROM p
CROSS JOIN thr t
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.RULE_ID = 'COST_CONTRACT_BREACH' AND e.STATUS IN ('OPEN', 'ACK');

-- P169.3 R2-044: databases with more than one DATABASE_ID on a day of the last 3 (a drop / re-create), and the
--        growth the old (by name) and new (live id) series compute for them.
WITH g_old AS {G12_OLD},
g_new AS {G12},
multi AS (
    SELECT DATABASE_NAME, USAGE_DATE, COUNT(DISTINCT DATABASE_ID) AS IDS,
           COUNT(DISTINCT IFF(DELETED IS NULL, DATABASE_ID, NULL)) AS LIVE_IDS
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -3, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(DISTINCT DATABASE_ID) > 1
)
SELECT m.DATABASE_NAME, m.USAGE_DATE, m.IDS, m.LIVE_IDS,
       ROUND(o.GROWTH_GB, 1) AS OLD_GROWTH_GB, ROUND(n.GROWTH_GB, 1) AS NEW_GROWTH_GB
FROM multi m
LEFT JOIN g_old o ON o.DATABASE_NAME = m.DATABASE_NAME
LEFT JOIN g_new n ON n.DATABASE_NAME = m.DATABASE_NAME
ORDER BY m.USAGE_DATE DESC, m.DATABASE_NAME;

-- P169.4 R2-020 / R2-043: the key the next scan writes (newest error-cycle date), the newest load's HOUR and whether
--        that key exists. FOLDS_INTO_OLDER_EVENT = the key was raised BEFORE the newest cycle loaded: after V169
--        that cycle is not paged (V169 FIRST RUN (a), once at the transition; a NEWEST_LOAD_HOUR after the ~07:00
--        scan makes it likely around the apply). Then the DQ_RECON_ERROR events of the last 90 days in raise order
--        (a next-day twin of the same TITLE is a duplicate page; close a still-OPEN / ACK older twin as SUPERSEDED
--        with the OPTIONAL owner repair R169.1 -- the Alerts RESOLVE radios cannot set that machine-close kind).
WITH n AS (
    SELECT MAX(LATEST_LOAD) AS NEWEST_LOAD FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
)
SELECT n.NEWEST_LOAD, HOUR(n.NEWEST_LOAD) AS NEWEST_LOAD_HOUR,
       'DQ_RECON_ERROR|' || TO_VARCHAR(COALESCE(TO_DATE(n.NEWEST_LOAD), CURRENT_DATE())) AS NEXT_KEY,
       e.STATUS AS EXISTING_STATUS, e.RAISED_AT AS EXISTING_RAISED_AT,
       e.RAISED_AT < n.NEWEST_LOAD AS FOLDS_INTO_OLDER_EVENT
FROM n
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = 'DQ_RECON_ERROR|' || TO_VARCHAR(COALESCE(TO_DATE(n.NEWEST_LOAD), CURRENT_DATE()));
SELECT EVENT_ID, DEDUPE_KEY, RAISED_AT, STATUS, RESOLUTION_KIND, TITLE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID = 'DQ_RECON_ERROR' AND RAISED_AT >= DATEADD('day', -90, CURRENT_TIMESTAMP())
ORDER BY RAISED_AT;

-- P169.5 R2-047: the last 14 complete Central days of TRUE egress (the arm's own xfer CTE, 30-day read) against the
--        rule threshold, the largest destination per day, and whether the next-morning event exists.
WITH {CLK},
        {XFER.replace("{XFER_DAYS}", "30")},
thr AS (
    SELECT COALESCE(MAX(IFF(ENABLED, THRESHOLD_NUM, NULL)), 100) AS THRESHOLD_NUM
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_EGRESS_SPIKE'
)
SELECT x.DAY, ROUND(SUM(x.BYTES) / POWER(1024, 3), 1) AS GB, MAX_BY(x.DEST, x.BYTES) AS TOP_REGION,
       ROUND(SUM(x.BYTES) / POWER(1024, 3), 1) >= MAX(t.THRESHOLD_NUM) AS OVER_THRESHOLD,
       MAX(IFF(e.EVENT_ID IS NULL, 0, 1)) = 1 AS EVENT_ON_NEXT_DAY
FROM xfer x
CROSS JOIN clk k
CROSS JOIN thr t
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = 'COST_EGRESS_SPIKE|' || TO_VARCHAR(DATEADD('day', 1, x.DAY))
WHERE x.DAY < k.TODAY AND x.DAY >= DATEADD('day', -14, k.TODAY)
GROUP BY x.DAY
ORDER BY x.DAY DESC;

-- P169.6 R1-071: the warehouses [24] raises for the FIRST time (a NULL snapshot timer = never suspends), with the
--        monthly USD and the rule threshold: the arm's own CTE chain.
{_bind(CHAIN24.rstrip())}
SELECT o.WAREHOUSE_NAME, o.MONTHLY_USD, o.IDLE_PCT, o.COVERED_DAYS, o.TARGET_SEC, c.THRESHOLD_NUM,
       o.MONTHLY_USD >= c.THRESHOLD_NUM AS WOULD_RAISE
FROM opp o
JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(o.WAREHOUSE_NAME)
JOIN cfg c ON c.RULE_ID = 'COST_IDLE_OPPORTUNITY'
WHERE w.AUTO_SUSPEND IS NULL
ORDER BY o.MONTHLY_USD DESC;

-- P169.7 R1-233: SEC_TRUST_REGRESSION events raised on no rise (METRIC_VALUE < 1) and the rule threshold. Close any
--        as EXPECTED in Alerts ('threshold-0 artifact'); reset a THRESHOLD_NUM <= 0 to 1 (the floor makes it cosmetic).
SELECT (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
         WHERE RULE_ID = 'SEC_TRUST_REGRESSION' AND METRIC_VALUE < 1) AS NO_RISE_EVENTS,
       (SELECT MAX(THRESHOLD_NUM) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
         WHERE RULE_ID = 'SEC_TRUST_REGRESSION') AS THRESHOLD_NUM;
"""

# ---------------------------------------------------------------------------------------------------
# RUN_NEXT PART B (read-only; after the apply). GET_DDL fragments carry no quote, backslash or newline.
# ---------------------------------------------------------------------------------------------------
PART_B_PRESENT = ("alert scan daily v6 (V169:", "AS MTD_COMPLETE_USD", "AS TERM_END", "PARTITION BY DATABASE_ID",
                  "AND DELETED IS NULL", "AS NEWEST_LOAD", "TARGET_CLOUD IS NOT NULL", "AS GB_DAY",
                  "COALESCE(w.AUTO_SUSPEND, 0) AS AUTO_SUSPEND", "GREATEST(COALESCE(c.THRESHOLD_NUM, 1), 1)",
                  "/14 rule blocks ok (daily)", "AS DAILY_BURN", "AS RERAISED")
PART_B_ABSENT = ("GB_24H", "AND w.AUTO_SUSPEND IS NOT NULL", "PARTITION BY DATABASE_NAME", "alert scan daily v5 (V163:")
_body163 = extract_proc(V163, "SP_ALERT_SCAN_DAILY()")
_body163 = _body163[_body163.index("$$") + 2:_body163.rindex("$$")]
for _f in (*PART_B_PRESENT, *PART_B_ABSENT):
    assert not set(_f) & {"'", "\\", "\n", "\r"}, _f
assert all(f in _d_body for f in PART_B_PRESENT) and not any(f in _d_body for f in PART_B_ABSENT)
assert all(f in _body163 for f in PART_B_ABSENT) and not all(f in _body163 for f in PART_B_PRESENT)
_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()')"
_ddl_rows = "\nUNION ALL\n".join(
    [f"SELECT 'V169.2 daily scan DDL has: {f}', IFF(CONTAINS({_DDL}, '{f}'), 'OK', 'FAIL: not the V169 body')"
     for f in PART_B_PRESENT]
    + [f"SELECT 'V169.2 daily scan DDL lacks: {f}', IFF(NOT CONTAINS({_DDL}, '{f}'), 'OK', 'FAIL: still V163 text')"
       for f in PART_B_ABSENT])
_RULES = ("COST_BUDGET_PACE", "COST_FORECAST_BREACH", "COST_CONTRACT_BREACH", "COST_STORAGE_SURGE",
          "COST_EGRESS_SPIKE", "COST_IDLE_OPPORTUNITY", "SEC_TRUST_REGRESSION")
_rule_like = " OR ".join(f"CONTEXT LIKE 'rule {r} %'" for r in _RULES)

PART_B = f"""\
-- PART B -- V169 verify (READ-ONLY; run in a Central session). Run V169.1 + V169.2 right after the apply and
-- the grids V169.3 + V169.4 the next morning, after the ~07:00 Central daily scan. Every RESULT should read OK;
-- paste the grids back.
SELECT 'V169.1 SCHEMA_VERSION has 169' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 169) = 1,
           'OK', 'FAIL: V169 did not finish') AS RESULT
UNION ALL
SELECT 'V169.1 COST_EGRESS_SPIKE NAME (refreshed unless edited)',
       (SELECT IFF(MAX(NAME) = '{NAME_EGRESS_NEW}', 'OK', 'CHECK: kept operator text: ' || MAX(NAME))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'COST_EGRESS_SPIKE')
UNION ALL
{_ddl_rows};

-- V169.3 the next morning: the heartbeat reads 14/14 and none of the changed arms failed.
SELECT 'V169.3 daily scan heartbeat reads 14/14' AS CHECK_NAME,
       COALESCE((SELECT IFF(MAX(STATUS) = 'alert scan daily 14/14 rule blocks ok (daily)', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY'),
                'FAIL: no ALERT_SCAN_DAILY row') AS RESULT
UNION ALL
SELECT 'V169.3 no rule_block_failed for the changed arms (24h)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE ERROR_TYPE IN ('rule_block_failed', 'recon_scan_failed')
               AND ({_rule_like} OR CONTEXT LIKE 'rule DQ_RECON_ERROR %')
               AND LOGGED_AT >= DATEADD('hour', -24, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE for the rule');

-- V169.4 the morning after the first post-apply daily scan (and after any failing reconciliation cycle): the newest
--        error cycle in ETL_RECON_RESULTS was paged by an event raised AFTER it loaded. CHECK = it folded into an
--        older event (FIRST RUN (a) the transition into a pre-V169 scan-date key, or (b) a same-date re-run that
--        failed after that date's page) or has no event; review Operations > Pipeline SLA > Data checks >
--        Reconciliation errors. RAISED_AT and LATEST_LOAD are both TIMESTAMP_NTZ, compared as stored.
WITH n AS (
    SELECT MAX(LATEST_LOAD) AS NEWEST_LOAD,
           'DQ_RECON_ERROR|' || TO_VARCHAR(TO_DATE(MAX(LATEST_LOAD))) AS CYCLE_KEY
    FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
)
SELECT 'V169.4 newest reconciliation error cycle was paged' AS CHECK_NAME,
       CASE WHEN n.NEWEST_LOAD IS NULL THEN 'OK: no reconciliation error in the look-back'
            WHEN e.RAISED_AT IS NULL THEN 'CHECK: no event keyed ' || n.CYCLE_KEY
                 || ' (below the rule threshold, the rule disabled, or no daily scan since that load)'
            WHEN e.RAISED_AT < n.NEWEST_LOAD THEN 'CHECK: the cycle loaded ' || TO_VARCHAR(n.NEWEST_LOAD)
                 || ' folded into the event raised ' || TO_VARCHAR(e.RAISED_AT)
                 || ' and was not paged; review Reconciliation errors'
            ELSE 'OK' END AS RESULT
FROM n
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = n.CYCLE_KEY;
"""

# the V169.4 grid keys exactly as arm [18] does for a non-empty table (the COALESCE only covers no rows)
assert "TO_VARCHAR(COALESCE(TO_DATE(r.NEWEST_LOAD), CURRENT_DATE()))" in _between(daily, *S18)

# The Alerts RESOLVE radios offer ACTIONED / NOISE / EXPECTED only (app/ui/pages/alerts.py RESOLUTION_KINDS), so a
# next-day DQ_RECON_ERROR twin -- a machine duplicate, which belongs under SUPERSEDED (left out of the RESOLVED count,
# MTTR and per-rule precision, mart_sql.py) -- gets the one guarded SQL exception, R169.1, shaped like V168's R168.1:
# COMMENTED OUT, bounded by the EVENT_IDs the owner pastes from P169.4, the rule and OPEN / ACK. Its placeholder matches
# no EVENT_ID (a UUID string), so the block uncommented as written changes nothing.
R169_1_PLACEHOLDER = "'<older-twin EVENT_ID from P169.4>'"
REPAIR = f"""\
-- Run in a Central session (RAISED_AT is Central wall-clock; the RUN_NEXT file leads with the timezone pin).
-- V169 owner repairs: NONE run at apply. The facts are correct; only the arms' arithmetic changed. Resolve historic
-- events in the Alerts UI (bulk resolve; ALERT_AUDIT records who and why), from these read-only PREFLIGHT grids:
--   P169.1 COST_BUDGET_PACE events of days 2-5 the complete-day ratio would not have raised (NOISE);
--   P169.2 COST_CONTRACT_BREACH events with WOULD_RAISE_NEW = FALSE (NOISE), and their auto-declared incidents;
--   P169.3 COST_STORAGE_SURGE events on a re-created database (EXPECTED, note 're-created database pairing');
--   P169.4 DQ_RECON_ERROR next-day duplicates still OPEN / ACK: NOT in the Alerts UI -- use R169.1 below;
--   P169.7 SEC_TRUST_REGRESSION events with METRIC_VALUE < 1 (EXPECTED).
-- R169.1 OPTIONAL (owner decision), the one exception to resolving in the Alerts UI: close the OLDER twin of each
-- next-day DQ_RECON_ERROR duplicate that PREFLIGHT P169.4 lists (its second grid: the earlier of two events with the
-- same TITLE on consecutive days) as SUPERSEDED. SUPERSEDED is a machine-close kind the Alerts RESOLVE radios do not
-- offer: they set ACTIONED / NOISE / EXPECTED, which count in the RESOLVED total and MTTR (and ACTIONED / NOISE also
-- move the rule's precision); SUPERSEDED stays out of all three, like the scans' own escalation supersede. Replace
-- the placeholder with those EVENT_IDs, each quoted, comma-separated. Only a listed row of this rule still OPEN or
-- ACK can change; a SNOOZED twin is left to wake, and the placeholder as written matches no row. No ALERT_AUDIT row
-- is written (the scans' machine closes write none either). Uncomment to run.
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
--    SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
--  WHERE EVENT_ID IN ({R169_1_PLACEHOLDER})
--    AND RULE_ID = 'DQ_RECON_ERROR'
--    AND STATUS IN ('OPEN', 'ACK');
-- Never DELETE an ALERT_EVENTS row and never rewrite DEDUPE_KEY.
"""
assert all(ln.startswith("--") for ln in REPAIR.splitlines() if ln.strip())       # every statement commented out

for _name, _sql in (("PREFLIGHT", PREFLIGHT), ("PART B", PART_B), ("REPAIR", REPAIR)):
    assert "\r" not in _sql and _sql.isascii() and "$$" not in _sql, _name
_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")
_rp = os.environ.get("REPAIR_OUT")
if _rp:
    Path(_rp).write_text(REPAIR, encoding="utf-8", newline="\n")
    print(f"wrote REPAIR {_rp}")

print(f"V169 written: {target} ({len(out)} bytes)")
