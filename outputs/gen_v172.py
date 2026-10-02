#!/usr/bin/env python3
"""Forward-generate V172: the detection scans classify company by the V044 rule and measure what they claim.

Five scan procs re-derived, each from its CURRENT definer (tests/test_proc_lineage.py; nothing in V141-V171
re-defines any of them), plus bounded one-time repairs:

  SP_CHANGE_IMPACT_SCAN      from V140  R2-023 COMPANY_FOR_DATABASE in both registration arms (arm 1a through a
                                        derived table g, the V030 shape); R2-021 anchored CALL match at the four
                                        QUERY_HISTORY sites ('CALL<name>(' or '.<name>(' over a whitespace-class
                                        strip, the object_run_history drill's rule); R2-025 the two TASK count legs
                                        collapse auto-retry attempts to the terminal attempt (derived table, -21d);
                                        R2-022 + R2-025 the step-5 AFTER credits/call counts settled (>8h) runs only,
                                        LEFT JOIN + HAVING, divided by COUNT(DISTINCT RUN_KEY) = scheduled runs;
                                        R1-124 VERDICT_DETAIL p95 in Hr/Min/Sec (the HD template below).
  SP_WAREHOUSE_CHANGE_SCAN   from V109  R1-124 VERDICT_DETAIL p95 + queue in Hr/Min/Sec.
  SP_SCAN_SCHEMA_DRIFT       from V133  R2-024 COMPANY_FOR_DATABASE(SPLIT_PART(a.FQN, '.', 1)) in a b (...) wrapper.
  SP_SCAN_CLOUD_SVC_ANOMALY  from V150  R1-227 the disabled-rule guard counts ENABLED rows (V150 tested a
                                        COALESCEd threshold for NULL, which never fired).
  SP_ANOMALY_SWEEP           from V150  R2-024 PIPE_DT_FAILURES / PIPE_VOLUME_DROP / DQ_BREACH COMPANY via
                                        COMPANY_FOR_DATABASE in b (...) wrappers; R2-095 the COST_ORG_ACCOUNT_CREEP
                                        pointer; R1-227 rider: COST_ANOMALY_SWEEP books only while ENABLED (no early
                                        RETURN); CORTEX-NULLIF the pre-explain CORTEX_MODEL read normalized like
                                        app.core.ai.normalize_model (the V171 digest form). RETURN stays 'v3'.

Repairs (after the CREATEs, before the version row; each idempotent and bounded):
  R1  R2-023 OBJECT_CHANGE_REGISTRY.COMPANY re-stamped from a DISTINCT DATABASE_NAME map; live (OPEN/ACK/SNOOZED,
      not incident-linked) PERF_CHANGE_REGRESSION events follow -- from the re-stamped registry row, else
      COMPANY_FOR_DATABASE of the database split out of the object FQN in DEDUPE_KEY part 2.
  R2  R2-024 live DQ_SCHEMA_DRIFT / PIPE_DT_FAILURES / PIPE_VOLUME_DROP / DQ_BREACH events re-stamped the same way.
  R4  R2-025 tracking TASK baselines re-frozen on the collapsed (terminal-attempt) basis over their own
      [CHANGE_SEEN_AT - 14d, CHANGE_SEEN_AT) window; credits/call rescaled by OLD_CALLS / NEW_CALLS (x1 on re-run).
  R3  R2-021 the six BASELINE_* columns nulled for tracking PROCEDURE rows whose short name is a strict suffix of
      another ACCOUNT_USAGE.PROCEDURES name (first apply only); the next scan re-freezes them. R3 runs LAST,
      directly before the version row its gate reads (holistic #15): nothing that can stop the file sits between.

Reads V109, V133, V140 and V150 ONLY; never imports app/ (the literals the tests lock against the app are copies).
No CALL, task or DROP statement at apply time. With PREFLIGHT_OUT set, also writes the read-only PREFLIGHT
section (P172.1-P172.6); with PART_B_OUT set, the RUN_NEXT PART B verify grids (V172.1-V172.4); with REPAIR_OUT
set, the owner-run OWNER_REPAIRS block (R172.0-R172.4). PART B and the repairs open with ALTER SESSION SET
TIMEZONE = 'America/Chicago'. The byte-identity test never sets any of them.

Run: python outputs/gen_v172.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V109 = (MIG / "V109__warehouse_change_scan_fail_token.sql").read_text(encoding="utf-8")
V133 = (MIG / "V133__dq_schema_drift.sql").read_text(encoding="utf-8")
V140 = (MIG / "V140__change_impact_exclude_self_procs.sql").read_text(encoding="utf-8")
V150 = (MIG / "V150__cloud_svc_anomaly_baseline.sql").read_text(encoding="utf-8")
NAME = "V172__detection_scans_company_and_accuracy.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


def _swap_in(text: str, start: str, end: str, edits: list[tuple[str, str, str]]) -> str:
    """Apply count-1 swaps inside the slice [start .. end) only (start unique, end the first after it)."""
    assert text.count(start) == 1, f"slice start {start[:40]!r} not unique"
    i = text.index(start)
    j = text.index(end, i)
    piece = text[i:j]
    for old, new, label in edits:
        piece = _swap(piece, old, new, label)
    return text[:i] + piece + text[j:]


# ===================================================================================================
# R1-124: HD(S, NUL) -- a seconds expression rendered like app.logic.formulas.humanize_duration(S, 's').
# ROUND(.., 'HALF_TO_EVEN') is Python's round(); the rounding mode needs a fixed-point operand, and every S
# below is a NUMBER column (or NUMBER arithmetic), never a FLOAT. NUL is the text of a NULL S.
# ===================================================================================================
def hd(s: str, nul: str, ind: str) -> str:
    r = f"ROUND({s}, 0, 'HALF_TO_EVEN')"
    w = ind + "     "
    return (f"CASE WHEN {s} IS NULL THEN '{nul}'\n"
            f"{w}WHEN ROUND({s} * 1000, 0, 'HALF_TO_EVEN') = 0 THEN '0s'\n"
            f"{w}WHEN {s} < 1 THEN ROUND({s} * 1000, 0, 'HALF_TO_EVEN')::INT || 'ms'\n"
            f"{w}WHEN {s} < 10 THEN TO_VARCHAR(ROUND({s}, 1, 'HALF_TO_EVEN'), 'FM90.0') || 's'\n"
            f"{w}ELSE TRIM(IFF({r} >= 3600, FLOOR({r} / 3600)::INT || 'h ', '')\n"
            f"{w}          || IFF(MOD(FLOOR({r} / 60), 60) > 0, MOD(FLOOR({r} / 60), 60)::INT || 'm ', '')\n"
            f"{w}          || IFF({r} < 3600 AND MOD({r}, 60) > 0, MOD({r}, 60)::INT || 's', ''))\n"
            f"{ind}END")


_P = "               "                          # the VERDICT_DETAIL continuation indent of both scans
_C = _P + "   "                                  # where each CASE starts ('|| ' + CASE)
HD_NOTE = (f"{_P}-- V172 (R1-124): durations in Hr/Min/Sec, the formulas.humanize_duration twin (HALF_TO_EVEN like\n"
           f"{_P}-- Python round, on fixed-point operands). With the spaced ASCII ' -> ' arrow the app shim\n"
           f"{_P}-- wh_change.humanize_verdict_detail finds nothing to rewrite in the new text.\n")

# ===================================================================================================
# SP_CHANGE_IMPACT_SCAN (from V140)
# ===================================================================================================
# CI1-CI3 (R2-023) arm 1a: the guess out of the GROUP BY; the UDF over the grouped derived table g.
OLD_CI1 = "    USING (\n        SELECT 'PROCEDURE' AS OBJECT_TYPE,\n"
NEW_CI1 = ("    USING (\n"
           "        SELECT g.OBJECT_TYPE, g.DATABASE_NAME, g.SCHEMA_NAME, g.OBJECT_NAME,\n"
           "               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,  -- V172 (R2-023): "
           "the V044 classification (COMPANY_SCOPE row, then TRXS_, then ALFA%/ADMIN, else UNKNOWN), not a raw "
           "TRXS%/ALFA guess; the UDF on a plain column outside the GROUP BY (V030 shape)\n"
           "               g.CHANGE_SEEN_AT\n"
           "        FROM (\n"
           "        SELECT 'PROCEDURE' AS OBJECT_TYPE,\n")
OLD_CI2 = "               IFF(PROCEDURE_CATALOG LIKE 'TRXS%', 'Trexis', 'ALFA') AS COMPANY,\n"
NEW_CI2 = ""
OLD_CI3 = "        GROUP BY 1, 2, 3, 4, 5\n    ) s\n"
NEW_CI3 = "        GROUP BY 1, 2, 3, 4\n        ) g\n    ) s\n"
# CI4 (R2-023) arm 1b: a plain column, no aggregate in this SELECT.
OLD_CI4 = "                   IFF(DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA') AS COMPANY,\n"
NEW_CI4 = ("                   DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) AS COMPANY,  -- V172 "
           "(R2-023): the V044 classification, not a raw TRXS%/ALFA guess; a plain column, no aggregate\n")
# CI5 (R2-021) the step-3 comment.
OLD_CI5 = "    --    Procedure calls are matched by 'NAME(' in normalized CALL text; a\n"
NEW_CI5 = ("    --    Procedure calls are matched by 'CALLNAME(' or '.NAME(' in whitespace-stripped CALL text\n"
           "    --    (V172: was a bare 'NAME(' suffix match, so RUN_NAME co-matched NAME); a\n")


def old_pred(pad: str) -> str:
    return (f"{pad}AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN\n"
            f"{pad}             REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')) > 0\n")


def new_pred(pad: str) -> str:
    return (f"{pad}AND (POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN\n"
            f"{pad}              REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0\n"
            f"{pad}     OR POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '(' IN\n"
            f"{pad}                 REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')) > 0)"
            "   -- V172 (R2-021): anchored like the object_run_history drill (RUN_SP_LOAD no longer counts as SP_LOAD)\n")


PAD_A, PAD_B = " " * 11, " " * 19                  # steps 3/4 and the step-5 legs: two sites each

# CI8 / CI9 (R2-025) the two TASK COUNT legs join the terminal attempt per scheduled run.
TASK_TERMINAL = """\
          JOIN (
              -- V172 (R2-025): one row per SCHEDULED run. Auto-retry attempts share a SCHEDULED_TIME and collapse
              -- to the terminal attempt BEFORE the join (the object_run_history drill, V101, V126), so runs, fails
              -- and p95 count scheduled runs and a retried-then-succeeded run is no failure. The step-5 credit legs
              -- still read every attempt (retry compute is real spend). The ON predicates below are unchanged.
              SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME,
                     QUERY_START_TIME, COMPLETED_TIME, STATE
              FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
              WHERE SCHEDULED_TIME >= DATEADD('day', -21, CURRENT_TIMESTAMP())
                AND STATE IN ('SUCCEEDED', 'FAILED')
              QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                         ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1
          ) h
"""
_REG = "          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r\n"
_RAW_TH = "          JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h\n"
ON_BASE = "            ON h.SCHEDULED_TIME >= DATEADD('day', -20, CURRENT_TIMESTAMP())\n"
ON_AFTER = "            ON h.SCHEDULED_TIME >= GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)\n"
OLD_CI8, NEW_CI8 = _REG + _RAW_TH + ON_BASE, _REG + TASK_TERMINAL + ON_BASE
OLD_CI9, NEW_CI9 = _REG + _RAW_TH + ON_AFTER, _REG + TASK_TERMINAL + ON_AFTER

# CI10-CI17 (R2-022 composed with R2-025) the step-5 AFTER credits/call UPDATE only (the baseline one is untouched).
AFTER_START = ("        UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t\n"
               "           SET AFTER_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.AFTER_CALLS, 0)\n")
AFTER_END = "    EXCEPTION\n"
OLD_CI10 = "           SET AFTER_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.AFTER_CALLS, 0)\n"
NEW_CI10 = "           SET AFTER_CREDITS_PER_CALL = s.CR_PER_CALL\n"
OLD_CI11 = "              SELECT x.CHANGE_ID, SUM(a.CR) AS TOTAL_CR\n"
NEW_CI11 = (
    "              -- V172 (R2-022 + R2-025): credits per SETTLED scheduled run. Only runs that started more than 8h\n"
    "              -- ago count (QUERY_ATTRIBUTION_HISTORY lags up to ~8h, the wait the baseline already makes), and\n"
    "              -- numerator and denominator cover the SAME runs: an unattributed settled run adds 0 credits, and\n"
    "              -- a task run's retry attempts add their credits to ONE run (RUN_KEY), so the divisor counts\n"
    "              -- scheduled runs like BASELINE_CALLS. A value is written only once at least one settled run is\n"
    "              -- attributed (HAVING), as for the baseline -- a QAH gap never reads as a false IMPROVED.\n"
    "              SELECT x.CHANGE_ID, SUM(COALESCE(a.CR, 0)) / NULLIF(COUNT(DISTINCT x.RUN_KEY), 0) AS CR_PER_CALL\n")
OLD_CI12 = "                  SELECT r.CHANGE_ID, q.QUERY_ID\n"
NEW_CI12 = "                  SELECT r.CHANGE_ID, q.QUERY_ID, q.QUERY_ID AS RUN_KEY\n"
OLD_CI13 = "                  SELECT r.CHANGE_ID, h.QUERY_ID\n"
NEW_CI13 = ("                  SELECT r.CHANGE_ID, h.QUERY_ID,\n"
            "                         r.OBJECT_NAME || '|' || TO_VARCHAR(h.SCHEDULED_TIME) AS RUN_KEY\n")
SETTLED = "DATEADD('hour', -8, CURRENT_TIMESTAMP())"
OLD_CI14 = "                   AND q.START_TIME > r.CHANGE_SEEN_AT\n"
NEW_CI14 = OLD_CI14 + f"                   AND q.START_TIME < {SETTLED}\n"
OLD_CI15 = "                   AND h.QUERY_START_TIME > r.CHANGE_SEEN_AT\n"
NEW_CI15 = OLD_CI15 + f"                   AND h.QUERY_START_TIME < {SETTLED}\n"
OLD_CI16 = "              ) x\n              JOIN (\n"
NEW_CI16 = "              ) x\n              LEFT JOIN (\n"
OLD_CI17 = "              GROUP BY x.CHANGE_ID\n          ) s\n"
NEW_CI17 = "              GROUP BY x.CHANGE_ID\n              HAVING COUNT(a.RID) > 0\n          ) s\n"

# CI18 (R1-124) the object scan's p95 tokens.
OLD_CI18 = ("               || ' | p95 ' || COALESCE(ROUND(BASELINE_P95_MS / 1000, 1)::VARCHAR, '?') || 's->'\n"
            "               || COALESCE(ROUND(AFTER_P95_MS / 1000, 1)::VARCHAR, '?') || 's'\n")
NEW_CI18 = (HD_NOTE
            + f"{_P}|| ' | p95 '\n"
            + f"{_P}|| {hd('(BASELINE_P95_MS / 1000)', '?', _C)}\n"
            + f"{_P}|| ' -> '\n"
            + f"{_P}|| {hd('(AFTER_P95_MS / 1000)', '?', _C)}\n")

ci = extract_proc(V140, "SP_CHANGE_IMPACT_SCAN()")
assert ci.count("REPLACE(REPLACE(UPPER(q.QUERY_TEXT), ' ', ''), CHR(10), '')") == 4
ci = _swap(ci, OLD_CI1, NEW_CI1, "CI1 arm 1a wrapper head")
ci = _swap(ci, OLD_CI2, NEW_CI2, "CI2 arm 1a guess out")
ci = _swap(ci, OLD_CI3, NEW_CI3, "CI3 arm 1a GROUP BY + close g")
ci = _swap(ci, OLD_CI4, NEW_CI4, "CI4 arm 1b UDF")
ci = _swap(ci, OLD_CI5, NEW_CI5, "CI5 step-3 comment")
ci = _swap(ci, old_pred(PAD_A), new_pred(PAD_A), "CI6 anchored match, steps 3/4", n=2)
ci = _swap(ci, old_pred(PAD_B), new_pred(PAD_B), "CI7 anchored match, step-5 legs", n=2)
ci = _swap(ci, OLD_CI8, NEW_CI8, "CI8 TASK baseline count leg")
ci = _swap(ci, OLD_CI9, NEW_CI9, "CI9 TASK after count leg")
ci = _swap_in(ci, AFTER_START, AFTER_END, [
    (OLD_CI10, NEW_CI10, "CI10 SET"),
    (OLD_CI11, NEW_CI11, "CI11 per-run quotient"),
    (OLD_CI12, NEW_CI12, "CI12 procedure RUN_KEY"),
    (OLD_CI13, NEW_CI13, "CI13 task RUN_KEY"),
    (OLD_CI14, NEW_CI14, "CI14 procedure settled"),
    (OLD_CI15, NEW_CI15, "CI15 task settled"),
    (OLD_CI16, NEW_CI16, "CI16 LEFT JOIN QAH"),
    (OLD_CI17, NEW_CI17, "CI17 HAVING"),
])
ci = _swap(ci, OLD_CI18, NEW_CI18, "CI18 VERDICT_DETAIL p95")
assert "REPLACE(REPLACE(UPPER(q.QUERY_TEXT)" not in ci and "LIKE 'TRXS%'" not in ci
assert ci.count("REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')") == 8
assert ci.count("QUALIFY ROW_NUMBER()") == 2 and ci.count("JOIN SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h") == 2
assert ci.count(SETTLED) == 4 and "NULLIF(t.AFTER_CALLS, 0)" not in ci
assert "SET BASELINE_CREDITS_PER_CALL = s.TOTAL_CR / NULLIF(t.BASELINE_CALLS, 0)" in ci   # baseline untouched
assert "RETURN 'change impact scan complete';" in ci

# ===================================================================================================
# SP_WAREHOUSE_CHANGE_SCAN (from V109)
# ===================================================================================================
OLD_WH1 = ("               || ' | p95 ' || COALESCE(BASELINE_P95_S::VARCHAR, '?') || 's->'\n"
           "               || COALESCE(AFTER_P95_S::VARCHAR, '?') || 's'\n"
           "               || ' | queue ' || COALESCE(BASELINE_QUEUED_MIN_PER_DAY::VARCHAR, '0') || '->'\n"
           "               || COALESCE(AFTER_QUEUED_MIN_PER_DAY::VARCHAR, '0') || ' min/d'\n")
NEW_WH1 = (HD_NOTE
           + f"{_P}|| ' | p95 '\n"
           + f"{_P}|| {hd('(BASELINE_P95_S)', '?', _C)}\n"
           + f"{_P}|| ' -> '\n"
           + f"{_P}|| {hd('(AFTER_P95_S)', '?', _C)}\n"
           + f"{_P}|| ' | queue '\n"
           + f"{_P}|| {hd('(COALESCE(BASELINE_QUEUED_MIN_PER_DAY, 0) * 60)', '0s', _C)}\n"
           + f"{_P}|| ' -> '\n"
           + f"{_P}|| {hd('(COALESCE(AFTER_QUEUED_MIN_PER_DAY, 0) * 60)', '0s', _C)}\n"
           + f"{_P}|| '/day'\n")

wh = extract_proc(V109, "SP_WAREHOUSE_CHANGE_SCAN()")
wh = _swap(wh, OLD_WH1, NEW_WH1, "WH1 VERDICT_DETAIL p95 + queue")
assert "'s->'" not in wh and "' min/d'" not in wh
assert "WHERE CURRENT_DATE() > TRACKING_UNTIL AND VERDICT = 'PENDING'" in wh       # V153's settle partner
assert "CURRENT_TIMESTAMP(), DATEADD('day', 14, CURRENT_DATE())" in wh

# ===================================================================================================
# SP_SCAN_SCHEMA_DRIFT (from V133) -- R2-024
# ===================================================================================================
B_COLS = "(RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)"
B_SELECT = "SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY"
COMPANY_NOTE = ("  -- V172 (R2-024): honor COMPANY_SCOPE overrides and UNKNOWN, not a raw TRXS%/ALFA guess (V067 #22); "
                "the b (...) wrapper as in SP_ALERT_SCAN")


def b_head(ind: str, sel: str, guess: str, udf_arg: str) -> tuple[str, str]:
    old = f"{ind}SELECT {sel},\n{ind}       IFF({guess} LIKE 'TRXS%', 'Trexis', 'ALFA'),\n"
    new = (f"{ind}{B_SELECT}\n{ind}FROM (\n{ind}SELECT {sel},\n"
           f"{ind}       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE({udf_arg}),{COMPANY_NOTE}\n")
    return old, new


def b_tail(ind: str, old_key: str) -> tuple[str, str]:
    old = (f"{ind}WHERE NOT EXISTS (\n{ind}    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
           f"{ind}    WHERE e.DEDUPE_KEY = {old_key}\n{ind});\n")
    new = (f"\n{ind}) b {B_COLS}\n{ind}WHERE NOT EXISTS (\n{ind}    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
           f"{ind}    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n{ind});\n")
    return old, new


OLD_DR1, NEW_DR1 = b_head("    ", "cfg.RULE_ID", "SPLIT_PART(a.FQN, '.', 1)", "SPLIT_PART(a.FQN, '.', 1)")
OLD_DR2, NEW_DR2 = b_tail("    ", "cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())")

dr = extract_proc(V133, "SP_SCAN_SCHEMA_DRIFT()")
dr = _swap(dr, OLD_DR1, NEW_DR1, "DR1 drift company + wrapper head")
dr = _swap(dr, OLD_DR2, NEW_DR2, "DR2 drift wrapper tail")
assert "LIKE 'TRXS%'" not in dr and dr.count("WHERE e.DEDUPE_KEY = b.DEDUPE_KEY") == 1

# ===================================================================================================
# SP_SCAN_CLOUD_SVC_ANOMALY (from V150) -- R1-227
# ===================================================================================================
OLD_CS1 = "    cs_floor FLOAT DEFAULT 1.0;   -- CS credits/day floor: below this, a spike is not worth paging\n"
NEW_CS1 = OLD_CS1 + "    enabled_cnt INT;\n"
OLD_CS2 = "    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr\n"
NEW_CS2 = ("    -- V172 (R1-227): gate on the ENABLED row count like SP_SCAN_SCHEMA_DRIFT / SP_SCAN_RECON_ERRORS. V150\n"
           "    -- tested :zthr IS NULL after COALESCE(.., 3.5), which never fired, so a disabled or deleted rule still\n"
           "    -- scanned (at 3.5, not the tuned threshold) and SP_NOTIFY_WEBHOOK delivered what it booked.\n"
           "    SELECT COUNT(*), COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :enabled_cnt, :zthr\n")
OLD_CS3 = "    IF (:zthr IS NULL) THEN\n"
NEW_CS3 = "    IF (:enabled_cnt = 0) THEN\n"

cs = extract_proc(V150, "SP_SCAN_CLOUD_SVC_ANOMALY()")
cs = _swap(cs, OLD_CS1, NEW_CS1, "CS1 DECLARE enabled_cnt")
cs = _swap(cs, OLD_CS2, NEW_CS2, "CS2 count + threshold read")
cs = _swap(cs, OLD_CS3, NEW_CS3, "CS3 count gate")
assert cs.index("IF (:enabled_cnt = 0) THEN") < cs.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")

# ===================================================================================================
# SP_ANOMALY_SWEEP (from V150)
# ===================================================================================================
OLD_SW1 = ("      AND NOT EXISTS (\n          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
           "          WHERE e.DEDUPE_KEY = 'COST_ANOMALY_SWEEP|'")
NEW_SW1 = ("      -- V172 (R1-227 rider): a disabled or deleted COST_ANOMALY_SWEEP rule books nothing. The threshold read\n"
           "      -- above COALESCEs to 3.5 and never gated, so turning the rule off changed nothing. Not an early\n"
           "      -- RETURN: the DT, drift, creep, volume, DQ and cloud-services arms below still run.\n"
           "      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
           "                  WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED)\n" + OLD_SW1)
OLD_SW2, NEW_SW2 = b_head("        ", "c.RULE_ID", "d.DATABASE_NAME", "d.DATABASE_NAME")
OLD_SW3, NEW_SW3 = b_tail("        ", "c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||\n"
                                       "                  '.' || d.NAME || '|' || TO_VARCHAR(CURRENT_DATE())")
OLD_SW4 = "                   '. Breakdown: Admin > Org spend.',\n"
NEW_SW4 = "                   '. Breakdown: Cost Intelligence > Contract & Forecast.',\n"
OLD_SW5, NEW_SW5 = b_head("        ", "c.RULE_ID", "v.DB", "v.DB")
OLD_SW6, NEW_SW6 = b_tail("        ", "c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL ||\n"
                                       "                  '|' || TO_VARCHAR(CURRENT_DATE())")
OLD_SW7, NEW_SW7 = b_head("        ", "c.RULE_ID", "s.DB", "s.DB")
OLD_SW8, NEW_SW8 = b_tail("        ", "c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)")
# CORTEX-NULLIF, the sweep half: the V171 digest form (C1_SELECT), INTO :ai_model, at the block's 8-space indent.
MODEL_RE_LITERAL = "[a-z0-9][a-z0-9.-]{1,60}"
DEFAULT_MODEL = "llama3.1-8b"
OLD_SW9 = ("        SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')\n"
           "          INTO :ai_model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n")
NEW_SW9 = ("        -- V172 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a\n"
           "        -- valid name else the default): a blank, padded, mixed-case or invalid stored value no longer\n"
           "        -- reaches COMPLETE (the V171 digest read, same literal)\n"
           f"        SELECT IFF(RLIKE(cm, '{MODEL_RE_LITERAL}'), cm, '{DEFAULT_MODEL}')\n"
           "          INTO :ai_model\n"
           "        FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm\n"
           "              FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);\n")

sw = extract_proc(V150, "SP_ANOMALY_SWEEP()")
sw = _swap(sw, OLD_SW1, NEW_SW1, "SW1 COST_ANOMALY_SWEEP enabled gate")
sw = _swap(sw, OLD_SW2, NEW_SW2, "SW2 PIPE_DT_FAILURES company + head")
sw = _swap(sw, OLD_SW3, NEW_SW3, "SW3 PIPE_DT_FAILURES tail")
sw = _swap(sw, OLD_SW4, NEW_SW4, "SW4 COST_ORG_ACCOUNT_CREEP pointer")
sw = _swap(sw, OLD_SW5, NEW_SW5, "SW5 PIPE_VOLUME_DROP company + head")
sw = _swap(sw, OLD_SW6, NEW_SW6, "SW6 PIPE_VOLUME_DROP tail")
sw = _swap(sw, OLD_SW7, NEW_SW7, "SW7 DQ_BREACH company + head")
sw = _swap(sw, OLD_SW8, NEW_SW8, "SW8 DQ_BREACH tail")
sw = _swap(sw, OLD_SW9, NEW_SW9, "SW9 CORTEX_MODEL read")
assert "LIKE 'TRXS%'" not in sw and sw.count("WHERE e.DEDUPE_KEY = b.DEDUPE_KEY") == 3
assert "RETURN 'anomaly sweep v3 complete';" in sw and "Admin > Org spend" not in sw
assert sw.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT();") == 1
assert sw.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();") == 1
_creep = "'Last 7d 99999999 vs prior 99999999 USD. Breakdown: Cost Intelligence > Contract & Forecast.'"
assert len(_creep) - 2 <= 100                       # the V164 SP_NOTIFY_WEBHOOK LEFT(DETAIL, 100) excerpt

# ===================================================================================================
# The repairs (plain statements; the UDF only ever on a plain column of a derived table, V030)
# ===================================================================================================
LIVE_STATUSES = "('OPEN', 'ACK', 'SNOOZED')"      # the re-stamps' live set; the R172.1 owner list reads the same
LIVE_UNLINKED = f"""\
                AND e.STATUS IN {LIVE_STATUSES}
                AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                                WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)"""
FQN_DB = "SPLIT_PART(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '.', 1)"
REGISTRY_MAP = """\
      SELECT d.DATABASE_NAME, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(d.DATABASE_NAME) AS CO
      FROM (SELECT DISTINCT DATABASE_NAME FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY) d"""
SCAN_RULES = "('DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP', 'DQ_BREACH')"
TASK_TERMINAL_30 = """\
      JOIN (SELECT DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME, STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
            WHERE SCHEDULED_TIME >= DATEADD('day', -30, CURRENT_TIMESTAMP())
              AND STATE IN ('SUCCEEDED', 'FAILED')
            QUALIFY ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME
                                       ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1) h
        ON h.QUERY_START_TIME >= DATEADD('day', -14, r.CHANGE_SEEN_AT)
       AND h.QUERY_START_TIME < r.CHANGE_SEEN_AT
       AND h.DATABASE_NAME || '.' || h.SCHEMA_NAME || '.' || h.NAME = r.OBJECT_NAME"""
SUFFIX_JOIN = """\
      JOIN SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES p
        ON ENDSWITH(UPPER(p.PROCEDURE_NAME), UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3)))
       AND UPPER(p.PROCEDURE_NAME) <> UPPER(SPLIT_PART(r.OBJECT_NAME, '.', 3))"""

REPAIR_R1A = f"""\
-- R1 (R2-023) OBJECT_CHANGE_REGISTRY.COMPANY by the V044 classification. The registry is append-only (MERGE WHEN
-- NOT MATCHED), so every row the old scan stamped with the raw guess kept it: an unmapped database read ALFA, a
-- COMPANY_SCOPE override was ignored. Only rows whose stamp changes are written.
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET COMPANY = m.CO
  FROM (
{REGISTRY_MAP}
  ) m
 WHERE t.DATABASE_NAME = m.DATABASE_NAME
   AND t.COMPANY IS DISTINCT FROM m.CO;
"""
REPAIR_R1B = f"""\
-- R1b live PERF_CHANGE_REGRESSION events (OPEN / ACK / SNOOZED, not linked to an incident -- an incident keeps
-- the company it was declared under) follow: from the re-stamped registry row of the same key, else the database
-- split out of the object FQN in DEDUPE_KEY part 2 ('PERF_CHANGE_REGRESSION|DB.SCHEMA.NAME|date'; part 2 is never
-- passed to the UDF whole). Step 7 dedupes on the key only, so nothing re-raises.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS t
   SET COMPANY = s.NEW_COMPANY
  FROM (
      SELECT k.EVENT_ID, COALESCE(g.COMPANY, k.FQN_COMPANY) AS NEW_COMPANY
      FROM (
          SELECT x.EVENT_ID, x.DEDUPE_KEY, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS FQN_COMPANY
          FROM (
              SELECT e.EVENT_ID, e.DEDUPE_KEY, {FQN_DB} AS DB
              FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.RULE_ID = 'PERF_CHANGE_REGRESSION'
{LIVE_UNLINKED}
          ) x
      ) k
      LEFT JOIN (
          SELECT 'PERF_CHANGE_REGRESSION|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE) AS DEDUPE_KEY,
                 MAX(r.COMPANY) AS COMPANY
          FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
          GROUP BY 1
      ) g ON g.DEDUPE_KEY = k.DEDUPE_KEY
  ) s
 WHERE t.EVENT_ID = s.EVENT_ID
   AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;
"""
REPAIR_R2 = f"""\
-- R2 (R2-024) live DQ_SCHEMA_DRIFT / PIPE_DT_FAILURES / PIPE_VOLUME_DROP / DQ_BREACH events re-stamped the same way:
-- all four keys are 'RULE|DB.SCHEMA.OBJECT|day', so the database is part 2 up to its first dot.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS t
   SET COMPANY = s.NEW_COMPANY
  FROM (
      SELECT x.EVENT_ID, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS NEW_COMPANY
      FROM (
              SELECT e.EVENT_ID, {FQN_DB} AS DB
              FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
              WHERE e.RULE_ID IN {SCAN_RULES}
{LIVE_UNLINKED}
      ) x
  ) s
 WHERE t.EVENT_ID = s.EVENT_ID
   AND t.COMPANY IS DISTINCT FROM s.NEW_COMPANY;
"""
REPAIR_R3 = f"""\
-- R3 (R2-021) the suffix match froze baselines that blended a RUN_<name> / X_<name> wrapper's calls into <name>.
-- Frozen baselines never recompute, so null them -- only for still-tracking PROCEDURE rows whose short name is a
-- strict suffix of another procedure's name (deleted procedures included: their old calls are still in the
-- window), and only once: a re-run must not undo a scan's re-freeze (each re-null re-freezes over a shorter
-- window). R3 runs LAST, directly before the version row, so a missing 172 row means R3 has not committed: every
-- statement that can stop the file (the five CREATEs, R1-R2, R4's 30-day TASK_HISTORY read) runs before it, and a
-- retry after any of them nulls these rows for the first time, even if a scan ran in between. The next
-- TASK_CHANGE_IMPACT_SCAN re-freezes them with the anchored match (over the scan's own 20-day reach: a change
-- older than 6 days gets a shorter baseline, at least 6 days).
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,
       BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL
  FROM (
      SELECT DISTINCT r.CHANGE_ID
      FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
{SUFFIX_JOIN}
      WHERE r.OBJECT_TYPE = 'PROCEDURE'
        AND CURRENT_DATE() <= r.TRACKING_UNTIL
        AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172)
  ) s
 WHERE t.CHANGE_ID = s.CHANGE_ID;
"""
REPAIR_R4 = f"""\
-- R4 (R2-025) still-tracking TASK baselines were frozen on raw attempts. Re-freeze them on the terminal attempt per
-- scheduled run over their own [CHANGE_SEEN_AT - 14d, CHANGE_SEEN_AT) window (30 days of TASK_HISTORY covers every
-- tracking row; no now-20d clip). The frozen credits numerator summed every attempt and still does, so credits/call
-- is rescaled by OLD_CALLS / NEW_CALLS (a factor of 1 on a re-run); a NULL credits/call stays NULL.
UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY t
   SET BASELINE_CALLS = s.CALLS, BASELINE_FAILS = s.FAILS,
       BASELINE_MEDIAN_MS = s.MED_MS, BASELINE_P95_MS = s.P95_MS,
       BASELINE_CREDITS_PER_CALL = s.OLD_CPC * s.OLD_CALLS / NULLIF(s.CALLS, 0)
  FROM (
      SELECT r.CHANGE_ID, COUNT(*) AS CALLS,
             COUNT_IF(h.STATE = 'FAILED') AS FAILS,
             MEDIAN(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME)) AS MED_MS,
             APPROX_PERCENTILE(DATEDIFF('millisecond', h.QUERY_START_TIME, h.COMPLETED_TIME), 0.95) AS P95_MS,
             MAX(r.BASELINE_CALLS) AS OLD_CALLS, MAX(r.BASELINE_CREDITS_PER_CALL) AS OLD_CPC
      FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
{TASK_TERMINAL_30}
      WHERE r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NOT NULL AND r.BASELINE_CALLS > 0
        AND CURRENT_DATE() <= r.TRACKING_UNTIL
      GROUP BY r.CHANGE_ID
  ) s
 WHERE t.CHANGE_ID = s.CHANGE_ID;
"""
REPAIRS = (REPAIR_R1A, REPAIR_R1B, REPAIR_R2, REPAIR_R4, REPAIR_R3)     # R3 LAST: its gate is the next statement
for _r in REPAIRS:
    _code = "\n".join(ln for ln in _r.splitlines() if not ln.lstrip().startswith("--"))
    assert "\\" not in _r and "$$" not in _r and _code.count(";") == 1 and _code.rstrip().endswith(";")
    assert not re.search(r"COMPANY_FOR_\w+\(\s*(?:MAX|MIN|SUM|ANY_VALUE|COUNT)\(", _r)          # V030
assert "SPLIT_PART(e.DEDUPE_KEY, '|', 2))" not in REPAIR_R1B + REPAIR_R2      # part 2 never passed whole

# ===================================================================================================
# The migration
# ===================================================================================================
HEADER = f"""-- {NAME}
--
-- The detection scans classify company by the V044 rule and measure what they claim (V166-V172 wave, detection
-- cluster). Five scan procs re-derived, each from its current definer, plus bounded one-time repairs.
--
-- WHY:
--   (R2-023) SP_CHANGE_IMPACT_SCAN stamped COMPANY with a raw TRXS%/ALFA guess: an unmapped database read ALFA,
--     a COMPANY_SCOPE override was ignored, and the Trexis / UNKNOWN scopes never saw those changes or their
--     PERF_CHANGE_REGRESSION alerts (and an auto-declared incident copied the wrong company).
--   (R2-024) SP_ANOMALY_SWEEP (PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH) and SP_SCAN_SCHEMA_DRIFT made the
--     same guess; SP_ALERT_SCAN dropped it in V067 #22. They now call COMPANY_FOR_DATABASE inside the SP_ALERT_SCAN
--     b (...) wrapper.
--   (R2-021) the scan matched procedure calls by a bare 'NAME(' suffix, so RUN_SP_LOAD counted as SP_LOAD in the
--     frozen baseline, the after window and credits/call; the Operations drill was anchored in v4.271 and the scan
--     was not. Now 'CALL<name>(' or '.<name>(' over a whitespace-class strip, the drill's rule.
--   (R2-025) TASK runs counted every auto-retry attempt: 7 of 14 runs retried once read as 21 runs, 7 failures,
--     REGRESSED (CRITICAL at 50%) while every scheduled run succeeded. Runs, fails and p95 now count the terminal
--     attempt per scheduled run (the drill, V101, V126); retry compute still counts toward credits/call.
--   (R2-022) the AFTER credits/call divided attributed credits by ALL runs, including runs QUERY_ATTRIBUTION_HISTORY
--     had not reached yet (up to ~8h): a nightly job's +55% read +44% (no alert) and a fresh hourly change read a
--     false IMPROVED. Now settled (>8h) runs only, numerator and denominator over the same runs, per scheduled run.
--   (R1-124) VERDICT_DETAIL (and so ALERT_EVENTS.DETAIL and the Teams / email lines) printed raw seconds and
--     minutes/day ('p95 1800.0s->2400.0s'); now Hr/Min/Sec ('p95 30m -> 40m', 'queue 2h 25m -> 3h 20m/day').
--   (R1-227) SP_SCAN_CLOUD_SVC_ANOMALY's disabled-rule guard tested a COALESCEd threshold for NULL and never fired;
--     a disabled COST_CLOUD_SVC_ANOMALY still booked (at 3.5) and was delivered. Rider: a disabled
--     COST_ANOMALY_SWEEP stops booking too (the other sweep arms still run).
--   (R2-095) the COST_ORG_ACCOUNT_CREEP DETAIL pointed at a retired 'Admin > Org spend'.
--   (CORTEX-NULLIF) the sweep's Cortex pre-explain read CORTEX_MODEL raw: a blank, padded, mixed-case or invalid
--     value reached COMPLETE and the AI explanation failed. Now normalized like app.core.ai.normalize_model.
--
--   ~ SP_CHANGE_IMPACT_SCAN      from V140 (R2-023, R2-021, R2-025, R2-022, R1-124)
--   ~ SP_WAREHOUSE_CHANGE_SCAN   from V109 (R1-124)
--   ~ SP_SCAN_SCHEMA_DRIFT       from V133 (R2-024)
--   ~ SP_SCAN_CLOUD_SVC_ANOMALY  from V150 (R1-227)
--   ~ SP_ANOMALY_SWEEP           from V150 (R2-024, R2-095, R1-227 rider, CORTEX-NULLIF); RETURN stays 'v3'
--   + repairs R1-R4 below (OVERWATCH tables only; reads: one ACCOUNT_USAGE.PROCEDURES join, 30 days of
--     TASK_HISTORY).
--
-- COST: the daily change-impact scan's two TASK count legs window 21 days of TASK_HISTORY (QUALIFY) instead of a
-- raw join; the anchored match runs REGEXP_REPLACE only on rows that pass the unchanged ILIKE pre-filter. The
-- repairs read ACCOUNT_USAGE.PROCEDURES once and 30 days of TASK_HISTORY once; seconds.
-- FIRST RUN: nothing runs at apply time. The next TASK_WAREHOUSE_CHANGE_SCAN (06:40 Central) and
-- TASK_CHANGE_IMPACT_SCAN (06:50) rewrite tracking rows' VERDICT_DETAIL and re-freeze the nulled PROCEDURE
-- baselines; the next TASK_ANOMALY_SWEEP books with the new company. Verdicts on collision- or retry-affected
-- objects change on that run (intended). Already-raised alerts are not re-raised (dedupe keys unchanged).
-- DELIVERY: re-raising is not delivery. SP_NOTIFY_WEBHOOK (V164) sends an OPEN event to a route only when the
-- route's COMPANY_FILTER is ALL or the event's COMPANY, once per (EVENT_ID, ROUTE_ID) in ALERT_DELIVERIES, and
-- V034 set every existing route to 'ALFA'. (a) On a database with no COMPANY_SCOPE row that is not TRXS_ / ALFA% /
-- ADMIN, PERF_CHANGE_REGRESSION, PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH and DQ_SCHEMA_DRIFT alerts are
-- now UNKNOWN and stop posting to an ALFA-only route (the first three seed HIGH, PIPE_DT_FAILURES is CRITICAL at
-- 5+ failures), with no undelivered_expired row either (the watchdog reads the same filter). To keep them
-- posting, map the database (Cost Intelligence > Spend & Attribution > Unmapped entities) or add an ALL or
-- UNKNOWN route. (b) An OPEN event the R1b / R2 re-stamps move to a company another enabled route carries becomes
-- eligible there: the next TASK_ALERT_NOTIFY run sends it once if it is still inside the send window (24h; 7d
-- for CRITICAL); an older one raised within 7 days gets one undelivered_expired row for that route instead.
-- ROLLBACK (order matters): 1. Re-run the base CREATEs (V140 SP_CHANGE_IMPACT_SCAN, V109
-- SP_WAREHOUSE_CHANGE_SCAN, V133 SP_SCAN_SCHEMA_DRIFT, V150 SP_SCAN_CLOUD_SVC_ANOMALY + SP_ANOMALY_SWEEP). The
-- re-stamped COMPANY values stay (they are the corrected values). 2. Right after V140's CREATE, before the next
-- change-impact scan (06:50 Central, or Operations' Run change-impact scan now), null the still-tracking
-- TASK and PROCEDURE baselines:
--     UPDATE DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
--        SET BASELINE_FROM = NULL, BASELINE_CALLS = NULL, BASELINE_FAILS = NULL,
--            BASELINE_MEDIAN_MS = NULL, BASELINE_P95_MS = NULL, BASELINE_CREDITS_PER_CALL = NULL
--      WHERE OBJECT_TYPE IN ('TASK', 'PROCEDURE') AND CURRENT_DATE() <= TRACKING_UNTIL AND NOT ALERTED;
-- Why: R4 and the V172 scan froze them per scheduled run (terminal attempt) and by the anchored CALL match, but
-- V140's AFTER legs count every attempt and the bare suffix match, and V140 re-freezes only a NULL baseline.
-- Kept, a task with 7 of 14 runs retried once on both sides reads 14 runs / 0 failed before vs 21 / 7 after:
-- REGRESSED, a false PERF_CHANGE_REGRESSION page; a rescaled credits/call reads a false IMPROVED. Nulled, V140's
-- next scan re-freezes them on its own basis (over its 20-day reach: a change older than 6 days gets a shorter
-- baseline). An ALERTED row keeps the baseline its alert was raised on (V140 alerts a row once). Check any
-- PERF_CHANGE_REGRESSION raised between steps 1 and 2 before acting on it.
-- Apply AFTER V171. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20172, 'V172 requires V171 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 171) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARK_CI = ("-- >>> derived:SP_CHANGE_IMPACT_SCAN  (from V140; COMPANY via COMPANY_FOR_DATABASE (R2-023), anchored "
           "CALL match (R2-021), TASK runs collapse retries (R2-025), settled per-run AFTER credits/call (R2-022), "
           "p95 in Hr/Min/Sec (R1-124), V172)\n")
MARK_WH = ("-- >>> derived:SP_WAREHOUSE_CHANGE_SCAN  (from V109; VERDICT_DETAIL p95 + queue in Hr/Min/Sec (R1-124), "
           "V172)\n")
MARK_DR = ("-- >>> derived:SP_SCAN_SCHEMA_DRIFT  (from V133; COMPANY via COMPANY_FOR_DATABASE in a b (...) wrapper "
           "(R2-024), V172)\n")
MARK_CS = ("-- >>> derived:SP_SCAN_CLOUD_SVC_ANOMALY  (from V150; the disabled-rule guard counts ENABLED rows "
           "(R1-227), V172)\n")
MARK_SW = ("-- >>> derived:SP_ANOMALY_SWEEP  (from V150; DT / volume / DQ_BREACH COMPANY via COMPANY_FOR_DATABASE "
           "(R2-024), COST_ORG_ACCOUNT_CREEP pointer (R2-095), COST_ANOMALY_SWEEP enabled gate (R1-227 rider), "
           "normalized CORTEX_MODEL read (CORTEX-NULLIF), V172)\n")

REPAIR_BANNER = """\
-- ---------------------------------------------------------------------------------------------------------------
-- One-time repairs (after the CREATEs, before the version row). Idempotent and bounded; OVERWATCH tables only.
-- ---------------------------------------------------------------------------------------------------------------
"""

DESCRIPTION = (
    "Detection scans (V166-V172 wave, detection cluster). SP_CHANGE_IMPACT_SCAN re-derived from V140: COMPANY via "
    "COMPANY_FOR_DATABASE in both registration arms (R2-023; arm 1a through a grouped derived table, the V030 "
    "shape); procedure calls matched by CALL<name>( or .<name>( over a whitespace-class strip, the drill rule, so "
    "RUN_<name> no longer blends into <name> (R2-021); TASK runs, fails and p95 count the terminal attempt per "
    "scheduled run (R2-025); the AFTER credits/call counts settled runs (started more than 8h ago) only, LEFT JOIN "
    "plus HAVING, divided by distinct scheduled runs (R2-022); VERDICT_DETAIL p95 in Hr/Min/Sec (R1-124). "
    "SP_WAREHOUSE_CHANGE_SCAN re-derived from V109: VERDICT_DETAIL p95 and queue in Hr/Min/Sec (R1-124). "
    "SP_SCAN_SCHEMA_DRIFT re-derived from V133 and SP_ANOMALY_SWEEP from V150: PIPE_DT_FAILURES, PIPE_VOLUME_DROP, "
    "DQ_BREACH and DQ_SCHEMA_DRIFT COMPANY via COMPANY_FOR_DATABASE in a b wrapper (R2-024); the sweep also points "
    "COST_ORG_ACCOUNT_CREEP at Cost Intelligence > Contract & Forecast (R2-095), books COST_ANOMALY_SWEEP only while "
    "the rule is enabled (R1-227 rider, no early return) and reads CORTEX_MODEL normalized like the app "
    "(CORTEX-NULLIF); RETURN stays v3. SP_SCAN_CLOUD_SVC_ANOMALY re-derived from V150: the disabled-rule guard "
    "counts ENABLED rows (R1-227). One-time repairs: OBJECT_CHANGE_REGISTRY.COMPANY re-stamped, and live "
    "(OPEN, ACK, SNOOZED, not incident-linked) PERF_CHANGE_REGRESSION, DQ_SCHEMA_DRIFT, PIPE_DT_FAILURES, "
    "PIPE_VOLUME_DROP and DQ_BREACH events re-stamped from the database in their object FQN; tracking TASK "
    "baselines re-frozen on the terminal-attempt basis with credits/call rescaled; last, right before this row, a "
    "first-apply null of suffix-collided tracking PROCEDURE baselines (re-frozen by the next scan). No new object, "
    "no task change, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION and "$" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 172 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172);
"""

out = (HEADER
       + MARK_CI + ci + "\n\n"
       + MARK_WH + wh + "\n\n"
       + MARK_DR + dr + "\n\n"
       + MARK_CS + cs + "\n\n"
       + MARK_SW + sw + "\n\n"
       + REPAIR_BANNER + "\n" + "\n".join(REPAIRS)
       + VERSION_ROW)

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out and "\\" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 5 and out.count("$$") == 12      # guard + five bodies
assert out.count("-- >>> derived:") == 5
for _body in (ci, wh, dr, cs, sw):
    _inner = _body[_body.index("$$") + 2:_body.rindex("$$")]
    assert "$$" not in _inner and "\\" not in _inner
_outside = "".join(p for i, p in enumerate(out.split("$$")) if i % 2 == 0)
assert not re.search(r"^\s*(?:CALL|CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK|DROP)\b", _outside, re.M | re.I)
assert "UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" not in out           # test_freshness_coverage
assert not re.search(r"LIKE\s+'TRXS%'\s*,\s*'Trexis'\s*,\s*'ALFA'", out)
# every line V172 adds is ASCII (the bases keep their em dashes byte-identical)
assert all(x.isascii() for x in (HEADER, MARK_CI, MARK_WH, MARK_DR, MARK_CS, MARK_SW, REPAIR_BANNER, VERSION_ROW,
                                 NEW_CI1, NEW_CI4, NEW_CI5, new_pred(PAD_A), TASK_TERMINAL, NEW_CI11, NEW_CI13,
                                 NEW_CI18, NEW_WH1, NEW_DR1, NEW_DR2, NEW_CS2, NEW_SW1, NEW_SW2, NEW_SW4, NEW_SW9,
                                 *REPAIRS))

target = Path(os.environ.get("V172_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ===================================================================================================
# PREFLIGHT (read-only; P172.1-P172.6). Built from the SAME text: the HD template, the repairs' derived tables.
# ===================================================================================================
HD_PROBES = (("1800", "1800", "30m"), ("8700", "8700", "2h 25m"), ("95.5", "95.5", "1m 36s"),
             ("94.5", "94.5", "1m 34s"), ("0.5", "0.5", "500ms"), ("5.04", "5.04", "5.0s"),
             ("9.96", "9.96", "10.0s"), ("45", "45", "45s"), ("3630", "3630", "1h"), ("3690", "3690", "1h 1m"),
             ("86400", "86400", "24h"), ("0", "0", "0s"), ("NULL", "NULL", "?"))
_probe_rows = ",\n         ".join(f"('{lbl}', {val}, '{want}')" for lbl, val, want in HD_PROBES)
PREFLIGHT = f"""-- PREFLIGHT V172 (P172.1-P172.6) -- READ-ONLY preview of V172. Changes nothing. Run any time before the apply.

-- P172.1 the R1-124 HD template (the exact CASE text both scans use) on fixed-point literals. Every RESULT OK
--        proves the template compiles and renders like the app's humanize_duration before the 06:40 scan runs it.
SELECT 'P172.1 HD(' || t.LBL || ')' AS CHECK_NAME, t.WANT,
       {hd('(t.S)', '?', '       ')} AS GOT,
       IFF(GOT = t.WANT, 'OK', 'FAIL: the template renders differently') AS RESULT
FROM (SELECT column1 AS LBL, column2::NUMBER(18, 2) AS S, column3 AS WANT
      FROM VALUES
         {_probe_rows}) t
ORDER BY 1;

-- P172.2 (R1) OBJECT_CHANGE_REGISTRY rows whose COMPANY the apply re-stamps (old -> new; UNKNOWN = an unmapped
--        database: map it in Spend & Attribution > Unmapped entities to keep it in a company scope).
SELECT r.COMPANY AS OLD_COMPANY, m.CO AS NEW_COMPANY, COUNT(*) AS REGISTRY_ROWS,
       COUNT_IF(CURRENT_DATE() <= r.TRACKING_UNTIL) AS STILL_TRACKING,
       LISTAGG(DISTINCT r.DATABASE_NAME, ', ') WITHIN GROUP (ORDER BY r.DATABASE_NAME) AS DATABASES
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
JOIN (
{REGISTRY_MAP}
) m ON m.DATABASE_NAME = r.DATABASE_NAME
WHERE r.COMPANY IS DISTINCT FROM m.CO
GROUP BY 1, 2
ORDER BY 3 DESC;

-- P172.3 (R1b + R2) live events of the five rules and the company the apply gives them. INCIDENT_LINKED rows are
--        NOT re-stamped (they keep their incident's company); see R172.2 after the apply.
SELECT y.RULE_ID, y.STATUS, y.OLD_COMPANY, y.NEW_COMPANY, y.INCIDENT_LINKED, COUNT(*) AS EVENTS
FROM (
    SELECT x.RULE_ID, x.STATUS, x.COMPANY AS OLD_COMPANY,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS NEW_COMPANY, x.INCIDENT_LINKED
    FROM (
        SELECT e.RULE_ID, e.STATUS, e.COMPANY, {FQN_DB} AS DB,
               IFF(EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                           WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID), 'YES', 'NO') AS INCIDENT_LINKED
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.RULE_ID IN ('PERF_CHANGE_REGRESSION', 'DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP',
                            'DQ_BREACH')
          AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
    ) x
) y
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3;

-- P172.4 (R3) still-tracking PROCEDURE rows whose frozen baseline the apply nulls (the suffix-collision set), with
--        the procedure names that blended in. The next 06:50 scan re-freezes them; CHANGE_AGE_DAYS > 6 = a shorter
--        baseline (the scan reaches 20 days back).
SELECT r.OBJECT_NAME, r.CHANGE_SEEN_AT, DATEDIFF('day', r.CHANGE_SEEN_AT, CURRENT_TIMESTAMP()) AS CHANGE_AGE_DAYS,
       r.BASELINE_CALLS, r.VERDICT,
       LISTAGG(DISTINCT p.PROCEDURE_CATALOG || '.' || p.PROCEDURE_SCHEMA || '.' || p.PROCEDURE_NAME, ', ')
           WITHIN GROUP (ORDER BY p.PROCEDURE_CATALOG || '.' || p.PROCEDURE_SCHEMA || '.' || p.PROCEDURE_NAME)
           AS COLLIDING_PROCEDURES
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
{SUFFIX_JOIN}
WHERE r.OBJECT_TYPE = 'PROCEDURE'
  AND CURRENT_DATE() <= r.TRACKING_UNTIL
GROUP BY 1, 2, 3, 4, 5
ORDER BY 2;

-- P172.5 (R4) still-tracking TASK rows: the frozen attempt-based baseline vs the terminal-attempt re-freeze the apply
--        writes (credits/call rescaled by OLD_CALLS / NEW_CALLS).
SELECT r.OBJECT_NAME, r.CHANGE_SEEN_AT, r.VERDICT,
       MAX(r.BASELINE_CALLS) AS OLD_CALLS, COUNT(*) AS NEW_CALLS,
       MAX(r.BASELINE_FAILS) AS OLD_FAILS, COUNT_IF(h.STATE = 'FAILED') AS NEW_FAILS,
       MAX(r.BASELINE_CREDITS_PER_CALL) AS OLD_CR_PER_RUN,
       MAX(r.BASELINE_CREDITS_PER_CALL) * MAX(r.BASELINE_CALLS) / NULLIF(COUNT(*), 0) AS NEW_CR_PER_RUN
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
{TASK_TERMINAL_30}
WHERE r.OBJECT_TYPE = 'TASK' AND r.BASELINE_FROM IS NOT NULL AND r.BASELINE_CALLS > 0
  AND CURRENT_DATE() <= r.TRACKING_UNTIL
GROUP BY 1, 2, 3
ORDER BY 2;

-- P172.6 (R1-227 + CORTEX-NULLIF) the two anomaly rules' state, and the model the sweep's pre-explain will run.
SELECT 'P172.6 ' || c.RULE_ID AS CHECK_NAME, IFF(c.ENABLED, 'ENABLED', 'DISABLED: V172 stops it booking') AS RESULT,
       c.THRESHOLD_NUM, c.UPDATED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
WHERE c.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP')
UNION ALL
SELECT 'P172.6 CORTEX_MODEL runs as', IFF(RLIKE(cm, '{MODEL_RE_LITERAL}'), cm, '{DEFAULT_MODEL}'), NULL, NULL
FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
      FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);
"""

# ===================================================================================================
# RUN_NEXT PART B (after applying V172). GET_DDL fragments carry no quote, backslash or newline.
# ===================================================================================================
TZ_PIN = "ALTER SESSION SET TIMEZONE = 'America/Chicago';"
DDL_PRESENT = {
    "SP_CHANGE_IMPACT_SCAN": ("COMPANY_FOR_DATABASE(g.DATABASE_NAME)", "[[:space:]]", "HAVING COUNT(a.RID) > 0",
                              "AS RUN_KEY", "HALF_TO_EVEN", "ORDER BY COMPLETED_TIME DESC NULLS LAST"),
    "SP_WAREHOUSE_CHANGE_SCAN": ("HALF_TO_EVEN", "FM90.0"),
    "SP_SCAN_SCHEMA_DRIFT": ("COMPANY_FOR_DATABASE(SPLIT_PART(a.FQN", "b.DEDUPE_KEY"),
    "SP_SCAN_CLOUD_SVC_ANOMALY": ("INTO :enabled_cnt, :zthr", "IF (:enabled_cnt = 0) THEN"),
    "SP_ANOMALY_SWEEP": ("COMPANY_FOR_DATABASE(d.DATABASE_NAME)", "COMPANY_FOR_DATABASE(v.DB)",
                         "COMPANY_FOR_DATABASE(s.DB)", "Cost Intelligence > Contract & Forecast", "RLIKE(cm,"),
}
DDL_ABSENT = {
    "SP_CHANGE_IMPACT_SCAN": ("CHR(10)", "TOTAL_CR / NULLIF(t.AFTER_CALLS"),
    "SP_WAREHOUSE_CHANGE_SCAN": ("s->",),
    "SP_SCAN_SCHEMA_DRIFT": ("IFF(SPLIT_PART(a.FQN",),
    "SP_SCAN_CLOUD_SVC_ANOMALY": ("IF (:zthr IS NULL)",),
    "SP_ANOMALY_SWEEP": ("IFF(d.DATABASE_NAME LIKE", "IFF(v.DB LIKE", "IFF(s.DB LIKE", "Admin > Org spend"),
}
_bodies = {"SP_CHANGE_IMPACT_SCAN": ci, "SP_WAREHOUSE_CHANGE_SCAN": wh, "SP_SCAN_SCHEMA_DRIFT": dr,
           "SP_SCAN_CLOUD_SVC_ANOMALY": cs, "SP_ANOMALY_SWEEP": sw}
_base = {"SP_CHANGE_IMPACT_SCAN": extract_proc(V140, "SP_CHANGE_IMPACT_SCAN()"),
         "SP_WAREHOUSE_CHANGE_SCAN": extract_proc(V109, "SP_WAREHOUSE_CHANGE_SCAN()"),
         "SP_SCAN_SCHEMA_DRIFT": extract_proc(V133, "SP_SCAN_SCHEMA_DRIFT()"),
         "SP_SCAN_CLOUD_SVC_ANOMALY": extract_proc(V150, "SP_SCAN_CLOUD_SVC_ANOMALY()"),
         "SP_ANOMALY_SWEEP": extract_proc(V150, "SP_ANOMALY_SWEEP()")}
_ddl_rows: list[str] = []
for _proc, _frags in DDL_PRESENT.items():
    _ddl = f"GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{_proc}()')"
    for _f in _frags:
        assert not set(_f) & {"'", "\\", "\n", "\r"} and _f in _bodies[_proc] and _f not in _base[_proc], (_proc, _f)
        _ddl_rows.append(f"SELECT 'V172.1 {_proc} DDL has: {_f}', "
                         f"IFF(CONTAINS({_ddl}, '{_f}'), 'OK', 'FAIL: not the V172 body')")
    for _f in DDL_ABSENT[_proc]:
        assert not set(_f) & {"'", "\\", "\n", "\r"} and _f not in _bodies[_proc] and _f in _base[_proc], (_proc, _f)
        _ddl_rows.append(f"SELECT 'V172.1 {_proc} DDL lacks: {_f}', "
                         f"IFF(NOT CONTAINS({_ddl}, '{_f}'), 'OK', 'FAIL: still the old body')")
_ddl_union = "\nUNION ALL\n".join(_ddl_rows)
# V172.4 / R172.4 (review r1). One guarded arm = PAGE + ERROR_TYPE + CONTEXT (each arm logs its own CONTEXT
# literal; the volume-drop and DQ_BREACH arms share 'dml_history_unavailable'). An arm is NEW when it logged since
# the apply and not in the 14 days before; PRE-EXISTING arms (by design on an account without TASK_VERSIONS,
# ORGANIZATION_USAGE or Cortex) are listed, never failed. The boundary is APPLIED_AT itself: the apply session
# (RUN_NEXT pins Central first, correction 4) and the tasks (account zone, Central) both stamp Central wall-clock,
# so a margin would only count the old bodies' last runs.
_log_arms = re.findall(r"SELECT '(\w+)', '(\w+)',[^;]*?'([^']*)', CURRENT_ROLE\(\)", ci + sw)
assert len(_log_arms) == 9 == len(set(_log_arms)) and {p for p, _, _ in _log_arms} == {"AnomalySweep",
                                                                                        "ChangeImpactScan"}
assert ci.count("APP_ERROR_LOG") == 2 and sw.count("APP_ERROR_LOG") == 7
assert all("APP_ERROR_LOG" not in b for b in (wh, dr, cs))
APPLY_AT = "(SELECT MAX(APPLIED_AT) AS T FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172)"
ERR_ARMS = f"""\
    SELECT l.PAGE, l.ERROR_TYPE, l.CONTEXT,
           COALESCE(l.ERROR_TYPE, '?') || ' (' || COALESCE(l.CONTEXT, '?') || ')' AS ARM, MAX(a.T) AS T,
           COUNT_IF(l.LOGGED_AT < a.T) AS ROWS_BEFORE, COUNT_IF(l.LOGGED_AT >= a.T) AS ROWS_SINCE
    FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
    JOIN {APPLY_AT} a
      ON l.LOGGED_AT >= DATEADD('day', -14, a.T)
    WHERE l.PAGE IN ('AnomalySweep', 'ChangeImpactScan')
    GROUP BY l.PAGE, l.ERROR_TYPE, l.CONTEXT
    HAVING COUNT_IF(l.LOGGED_AT >= a.T) > 0"""

PART_B = f"""\
-- PART B -- V172 verify (READ-ONLY after the session pin). V172.1 + V172.2 right after the apply; V172.3 after the
-- next 06:40 / 06:50 Central change scans; V172.4 after the next TASK_ANOMALY_SWEEP (07:00 Central). Every RESULT
-- should read OK (or the count named); paste the grids back.
{TZ_PIN}

SELECT 'V172.1 SCHEMA_VERSION has 172' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 172) = 1,
           'OK', 'FAIL: V172 did not finish') AS RESULT
UNION ALL
{_ddl_union};

-- V172.2 the re-stamps landed: no registry row and no live, unlinked event of the five rules disagrees with the
--        V044 classification of its database.
SELECT 'V172.2 registry COMPANY = COMPANY_FOR_DATABASE' AS CHECK_NAME,
       IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || COUNT(*) || ' row(s) still on the old stamp') AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
JOIN (
{REGISTRY_MAP}
) m ON m.DATABASE_NAME = r.DATABASE_NAME
WHERE r.COMPANY IS DISTINCT FROM m.CO
UNION ALL
SELECT 'V172.2 live unlinked events of the five rules on the V044 company',
       IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || COUNT(*) || ' event(s) still on the old stamp')
FROM (
    SELECT x.COMPANY, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS CO
    FROM (
        SELECT e.COMPANY, {FQN_DB} AS DB
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.RULE_ID IN ('PERF_CHANGE_REGRESSION', 'DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP', 'DQ_BREACH')
{LIVE_UNLINKED}
    ) x
) y
WHERE y.COMPANY IS DISTINCT FROM y.CO;

-- V172.3 after the next 06:40 / 06:50 scans: tracking rows read Hr/Min/Sec, and the R3-nulled PROCEDURE baselines
--        are re-frozen (BASELINE_FROM set again; NO_BASELINE is an honest outcome for a thin re-freeze window).
SELECT 'V172.3 object registry VERDICT_DETAIL in Hr/Min/Sec (tracking rows)' AS CHECK_NAME,
       IFF(COUNT_IF(VERDICT_DETAIL LIKE '%s->%') = 0,
           'OK (' || COUNT(*) || ' tracking row(s))', 'CHECK: ' || COUNT_IF(VERDICT_DETAIL LIKE '%s->%')
           || ' row(s) still raw -- has the 06:50 scan run since the apply?') AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
WHERE CURRENT_DATE() <= TRACKING_UNTIL AND VERDICT_DETAIL IS NOT NULL
UNION ALL
SELECT 'V172.3 warehouse registry VERDICT_DETAIL in Hr/Min/Sec (tracking rows)',
       IFF(COUNT_IF(VERDICT_DETAIL LIKE '%min/d%') = 0,
           'OK (' || COUNT(*) || ' tracking row(s))', 'CHECK: ' || COUNT_IF(VERDICT_DETAIL LIKE '%min/d%')
           || ' row(s) still raw -- has the 06:40 scan run since the apply?')
FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY
WHERE CURRENT_DATE() <= TRACKING_UNTIL AND VERDICT_DETAIL IS NOT NULL
UNION ALL
SELECT 'V172.3 tracking PROCEDURE rows with no frozen baseline',
       IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || COUNT(*) || ' row(s) -- has the 06:50 scan run since the apply?')
FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
WHERE OBJECT_TYPE = 'PROCEDURE' AND CURRENT_DATE() <= TRACKING_UNTIL AND BASELINE_FROM IS NULL;

-- V172.4 after the next TASK_CHANGE_IMPACT_SCAN (06:50) and TASK_ANOMALY_SWEEP (07:00 Central): no guarded arm
--        started failing at the apply. A compile surprise in a re-derived arm is swallowed by its EXCEPTION and
--        logged to APP_ERROR_LOG. An arm is PAGE + ERROR_TYPE + CONTEXT (the CONTEXT tells the volume-drop and
--        DQ_BREACH arms apart). FAIL names an arm that logged since the apply and was silent in the 14 days before.
--        An arm that logged before V172 too is pre-existing, listed after OK and never failed: on an account without
--        TASK_VERSIONS, ORGANIZATION_USAGE or Cortex it logs on every run by design. R172.4 shows both sides. The
--        boundary is the apply itself: APPLIED_AT (RUN_NEXT pins Central before the apply) and LOGGED_AT (the tasks
--        run in the account zone) are both Central wall-clock, and the old bodies' last runs fall before it.
SELECT 'V172.4 no new ' || p.PAGE || ' error since the apply' AS CHECK_NAME,
       CASE WHEN MAX(a.T) IS NULL THEN 'FAIL: SCHEMA_VERSION has no 172 row'
            WHEN COUNT_IF(x.ROWS_BEFORE = 0) > 0
                THEN 'FAIL: new since the apply (read R172.4): '
                     || LISTAGG(IFF(x.ROWS_BEFORE = 0, x.ARM, NULL), '; ')
            ELSE 'OK' END
       || IFF(COUNT_IF(x.ROWS_BEFORE > 0) > 0,
              ' -- pre-existing, logged in the 14 days before V172 too: '
              || LISTAGG(IFF(x.ROWS_BEFORE > 0, x.ARM, NULL), '; '), '') AS RESULT
FROM (SELECT column1 AS PAGE FROM VALUES ('AnomalySweep'), ('ChangeImpactScan')) p
CROSS JOIN {APPLY_AT} a
LEFT JOIN (
{ERR_ARMS}
) x ON x.PAGE = p.PAGE
GROUP BY p.PAGE
ORDER BY 1;
"""

# ===================================================================================================
# OWNER_REPAIRS block (owner-run, after ALL of V162-V172 are applied). Central session first.
# ===================================================================================================
REPAIR = f"""\
-- OWNER REPAIRS -- V172 (R172.0-R172.4). Owner-run, after the apply, in a Snowsight worksheet. The first statement
-- pins the session to Central (the scans key days by CURRENT_DATE / ::DATE in the account zone). Every block is
-- read-only except two commented statements: the R172.0 CALL (recommended right after the apply) and the
-- optional close under R172.3. Nothing here re-raises an alert.
{TZ_PIN}

-- R172.0 (RECOMMENDED, right after the apply, off-peak) refresh the change-impact registry now instead of at 06:50
-- Central. Until that scan runs, the tracking rows sit on mixed bases: R4 re-froze the TASK baselines per scheduled
-- run while AFTER_CALLS, AFTER_FAILS, VERDICT and VERDICT_DETAIL still hold the last V140 scan's attempt-based
-- values (a task with 7 of 14 runs retried once on both sides: BASELINE_CALLS 14 beside AFTER_CALLS 21, a +7
-- calls delta, under the old 'runs 21->21' text), and the PROCEDURE baselines R3 nulled still show their old
-- VERDICT. This CALL is the daily TASK_CHANGE_IMPACT_SCAN's own work (one scan's ACCOUNT_USAGE reads) and can
-- raise PERF_CHANGE_REGRESSION for rows that now cross the bar, as the 06:50 run would. Uncomment to run; if
-- skipped, the Operations change table reads mixed until 06:50.
-- CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN();

-- R172.1 (R2-021 / R2-025 / R2-022) live PERF_CHANGE_REGRESSION alerts -- OPEN, ACK or SNOOZED, the set the R1b
--        re-stamp covers -- whose re-computed verdict is no longer REGRESSED (a suffix-collision or retry
--        over-count raised them). Read after R172.0 or the next 06:50 scan. Resolve them in the app as EXPECTED
--        (Alerts drawer, type-to-confirm; wake a SNOOZED one first via Alerts > Snoozed > Wake selected now, or it
--        wakes back into triage still open) -- never by SQL: RESOLVE feeds per-rule precision.
SELECT e.EVENT_ID, e.STATUS, e.SEVERITY, e.TITLE, r.OBJECT_TYPE, r.VERDICT, r.VERDICT_DETAIL, r.LAST_EVALUATED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
JOIN DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY r
  ON e.DEDUPE_KEY = 'PERF_CHANGE_REGRESSION|' || r.OBJECT_NAME || '|' || TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)
WHERE e.RULE_ID = 'PERF_CHANGE_REGRESSION' AND e.STATUS IN {LIVE_STATUSES}
  AND r.VERDICT <> 'REGRESSED'
ORDER BY e.RAISED_AT;

-- R172.2 (R2-023 / R2-024) open incidents whose member alert of the five rules now carries a different company
--        (incident-linked alerts were deliberately NOT re-stamped). Re-scope by hand only if it matters.
SELECT y.INCIDENT_ID, y.INCIDENT_STATUS, y.INCIDENT_COMPANY, y.RULE_ID, y.EVENT_ID, y.ALERT_COMPANY, y.V044_COMPANY
FROM (
    SELECT x.INCIDENT_ID, x.INCIDENT_STATUS, x.INCIDENT_COMPANY, x.RULE_ID, x.EVENT_ID, x.ALERT_COMPANY,
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(x.DB) AS V044_COMPANY
    FROM (
        SELECT i.INCIDENT_ID, i.STATUS AS INCIDENT_STATUS, i.COMPANY AS INCIDENT_COMPANY, e.RULE_ID, e.EVENT_ID,
               e.COMPANY AS ALERT_COMPANY, {FQN_DB} AS DB
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID AND m.MEMBER_KIND = 'ALERT'
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e ON e.EVENT_ID = m.REF_ID
        WHERE e.RULE_ID IN ('PERF_CHANGE_REGRESSION', 'DQ_SCHEMA_DRIFT', 'PIPE_DT_FAILURES', 'PIPE_VOLUME_DROP',
                            'DQ_BREACH')
          AND i.STATUS IN ('OPEN', 'MITIGATED')
    ) x
) y
WHERE y.INCIDENT_COMPANY IS DISTINCT FROM y.V044_COMPANY
ORDER BY y.INCIDENT_ID;

-- R172.3 (R1-227) cloud-services or sweep anomaly events booked while their rule was disabled (V150
--        ignored the switch). Usually empty: both rules ship enabled.
SELECT e.RULE_ID, c.ENABLED, c.UPDATED_AT, e.STATUS, COUNT(*) AS EVENTS, MIN(e.RAISED_AT) AS FIRST_RAISED,
       MAX(e.RAISED_AT) AS LAST_RAISED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
WHERE e.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP')
  AND NOT c.ENABLED AND e.RAISED_AT > c.UPDATED_AT
GROUP BY 1, 2, 3, 4
ORDER BY 1, 4;
-- (OPTIONAL, only if R172.3 returned OPEN / ACK / SNOOZED rows) close them as EXPECTED -- uncomment to run:
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
--    SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED', RESOLVED_AT = CURRENT_TIMESTAMP()
--  WHERE e.RULE_ID IN ('COST_CLOUD_SVC_ANOMALY', 'COST_ANOMALY_SWEEP') AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
--    AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
--                WHERE c.RULE_ID = e.RULE_ID AND NOT c.ENABLED AND e.RAISED_AT > c.UPDATED_AT);

-- R172.4 (every re-derived guarded arm) after the next TASK_ANOMALY_SWEEP (07:00 Central): the APP_ERROR_LOG rows
--        of each arm that logged since the apply, beside the same arm's rows in the 14 days before (the V172.4 arm
--        table). NEW since V172 = the arm was silent before: a compile surprise in a re-derived arm, swallowed by
--        its EXCEPTION -- fix it before the next run. PRE-EXISTING = the arm already logged before V172 (by design
--        on an account without TASK_VERSIONS, ORGANIZATION_USAGE or Cortex): a V172 problem only if its since-V172
--        ERROR_MESSAGE differs from the before one. No rows = no arm logged since the apply.
SELECT IFF(x.ROWS_BEFORE = 0, 'NEW since V172', 'PRE-EXISTING') AS ARM_STATUS, x.PAGE, x.ARM,
       IFF(l.LOGGED_AT >= x.T, 'since V172', 'before V172') AS SIDE, l.LOGGED_AT, l.ERROR_MESSAGE
FROM (
{ERR_ARMS}
) x
JOIN DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
  ON l.PAGE = x.PAGE AND EQUAL_NULL(l.ERROR_TYPE, x.ERROR_TYPE) AND EQUAL_NULL(l.CONTEXT, x.CONTEXT)
 AND l.LOGGED_AT >= DATEADD('day', -14, x.T)
ORDER BY 1, 2, 3, l.LOGGED_AT DESC;
"""
for _x in (PART_B, REPAIR):                       # Central first (correction 4): the first statement
    assert next(ln for ln in _x.splitlines() if ln and not ln.startswith("--")) == TZ_PIN
assert f"AND e.STATUS IN {LIVE_STATUSES}" in REPAIR and f"e.STATUS IN {LIVE_STATUSES}" in LIVE_UNLINKED
assert PART_B.count(ERR_ARMS) == 1 and REPAIR.count(ERR_ARMS) == 1 and "'hour', -6" not in PART_B + REPAIR
for _sql in (PREFLIGHT, PART_B, REPAIR):
    assert "\\" not in _sql and "$$" not in _sql and "\r" not in _sql

for _env, _text, _label in (("PREFLIGHT_OUT", PREFLIGHT, "PREFLIGHT"), ("PART_B_OUT", PART_B, "PART B"),
                            ("REPAIR_OUT", REPAIR, "OWNER REPAIRS")):
    _p = os.environ.get(_env)
    if _p:
        Path(_p).write_text(_text, encoding="utf-8", newline="\n")
        print(f"wrote {_label} {_p}")

print(f"V172 written: {target} ({len(out)} bytes)")
