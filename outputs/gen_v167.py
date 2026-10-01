#!/usr/bin/env python3
"""Forward-generate V167: mart-loader window edges, the AI coverage watermark and the atomic pattern reload.

Reads ONLY the three current definers (tests/test_proc_lineage.py; nothing in V160-V166 re-defines them):
  * V159__loader_compile_diet.sql  -> SP_LOAD_MARTS_V27(VARCHAR, FLOAT)
  * V064__webhook_drain_watermarks_alert_burn_telemetry.sql -> SP_NIGHTLY_RECONCILE()  (the file defines 4 procs)
  * V120__pattern_cost_runs_fanout_fix.sql -> SP_LOAD_PATTERN_COST(FLOAT)

and emits, in order:

  guard (-20167, v < 166) -> ALTER TABLE SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS COVERAGE_FROM DATE ->
  marker + SP_LOAD_MARTS_V27 -> marker + SP_NIGHTLY_RECONCILE -> marker + SP_LOAD_PATTERN_COST ->
  the scan-free MART_PATTERN_COST_DAILY twin DELETE -> SCHEMA_VERSION 167.

SP_LOAD_MARTS_V27 deltas (everything else byte-identical to V159; the V167 test reverses them):
  M1  R2-015  arm [1] qh span source: START_TIME padded one day back + an END_TIME floor at D-d
  M2  R2-014  arm [6] attempts: one lead-in day; final SELECT keeps only runs whose DAY >= D-d
  M3  R2-052  arm [9] ai_code: DAY / FIRST_TS / LAST_TS converted to Central first (USAGE_TIME is TIMESTAMP_TZ)
  M4  R1-016  DAILY freshness MERGE: COVERAGE_FROM (the deepest whole day both AI arms ever loaded), inside the
              EXISTING MERGE -- no UPDATE of SOURCE_FRESHNESS_STATE (tests/test_freshness_coverage.py)
SP_NIGHTLY_RECONCILE deltas (R2-018):
  N1  the four up-front DELETEs whose reload edge is wider than the hourly task's d=2 are gone (one comment)
  N2  the 'transient gap' TODO sentence corrected
  N3  a token-gated mark-and-sweep of those four tables right after the ('HOURLY', 3) verdict line
SP_LOAD_PATTERN_COST deltas (R2-010 D1-D5 + PATTERN-RESTAMP D6):
  P1  DECLARE lo DATE + lo := DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())
  P2  BEGIN TRANSACTION; DELETE WHERE DAY >= :lo; INSERT (the V120 aggregate, unchanged) ...; COMMIT
  P3  both source filters read >= :lo
  P4  the V068 freshness MERGE carries :lo AS COVERAGE_FROM (NULL-safe LEAST) -- still after the COMMIT
  P5  EXCEPTION WHEN OTHER THEN ROLLBACK; RAISE

No task, rule, grant or view change, and no CALL at apply time. With PREFLIGHT_OUT set, also writes the read-only
PREFLIGHT section (P167.1-P167.4); with PART_B_OUT, the read-only PART B grids (V167.1-V167.4); with REPAIR_OUT,
the ordered owner-run repair blocks (Central session first; the R2-014 rebuild is built from the arm-[6] text).

Run: python outputs/gen_v167.py   (V167_OUT overrides the migration path; the byte-identity test sets it)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_MARTS = MIG / "V159__loader_compile_diet.sql"
BASE_RECON = MIG / "V064__webhook_drain_watermarks_alert_burn_telemetry.sql"
BASE_PATTERN = MIG / "V120__pattern_cost_runs_fanout_fix.sql"
NAME = "V167__mart_loader_edges_ai_coverage_pattern_reload.sql"


def extract_proc(text: str, name: str) -> str:
    """One CREATE OR REPLACE PROCEDURE <name>, from its CREATE to its closing $$; (exactly one definition)."""
    head = f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}"
    assert text.count(head) == 1, f"{name}: expected exactly one definition, got {text.count(head)}"
    start = text.index(head)
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


def _swap_in(text: str, start: str, end: str, old: str, new: str, label: str, n: int = 1) -> str:
    """_swap scoped to the slice [start, end] (both anchors unique), so a repeated text elsewhere never moves."""
    assert text.count(start) == 1, f"{label}: slice start must be unique, got {text.count(start)}"
    i = text.index(start)
    j = text.index(end, i) + len(end)
    assert text.count(end, i) >= 1
    return text[:i] + _swap(text[i:j], old, new, label, n) + text[j:]


# ===================================================================================================
# SP_LOAD_MARTS_V27(VARCHAR, FLOAT)  (base V159)
# ===================================================================================================
marts_base = extract_proc(BASE_MARTS.read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27(")
assert marts_base.startswith(
    "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)")
assert "COVERAGE_FROM" not in marts_base and "CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)" not in marts_base

ARM1 = ("        -- [1] warehouse efficiency ------------------------------------------\n",
        "            loaded := loaded || 'wh_eff ';\n")
ARM6 = ("        -- [6] task graphs -----------------------------------------------------\n",
        "            loaded := loaded || 'graphs ';\n")
ARM9 = ("        -- [9] AI usage (Cortex Code views bill this account; Functions guarded)\n",
        "            loaded := loaded || 'ai_code ';\n")
DAILY_SPLIT = "    IF (UPPER(:SCOPE) = 'DAILY') THEN\n"

# M1 (R2-015) -- arm [1]'s qh span source: the 3-line block below is unique in the arm (q's block ends in GROUP BY)
M1_OLD = (
    "                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n"
    "                        WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                          AND WAREHOUSE_NAME IS NOT NULL\n"
    "                    ) s\n")
M1_NEW = (
    "                        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n"
    "                        -- V167 (R2-015): pad the span source one day back, so a query that started before the\n"
    "                        -- window's first Central midnight still marks the hours it spans on day D-d active\n"
    "                        -- (the 25-row GENERATOR caps a span at H0 + 24h, so one day reaches every D-d hour but\n"
    "                        -- one hour on the spring-forward night); the END_TIME floor drops prior-day queries that\n"
    "                        -- never reach D-d. m / q / m_idle keep D-d, so no output row moves to an earlier day.\n"
    "                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())\n"
    "                          AND COALESCE(END_TIME, START_TIME) >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                          AND WAREHOUSE_NAME IS NOT NULL\n"
    "                    ) s\n")

# M2 (R2-014) -- arm [6]
M2A_OLD = "                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
M2A_NEW = (
    "                    -- V167 (R2-014): one lead-in day, so a run that began before the window is seen whole\n"
    "                    -- here and dropped by the DAY filter below, instead of being re-keyed to its first\n"
    "                    -- in-window child as a phantom pipeline row on the window's first day\n"
    "                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())\n")
M2B_OLD = "                FROM runs GROUP BY 1, 2, 3, 4\n"
M2B_NEW = (
    "                FROM runs\n"
    "                -- V167 (R2-014): only runs whose first attempt is inside the window; a run that started\n"
    "                -- earlier was written whole on its own root day by the load that covered that day\n"
    "                WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())\n"
    "                GROUP BY 1, 2, 3, 4\n")

# M3 (R2-052) -- arm [9] ai_code (the ai_functions arm keys an LTZ START_TIME and stays as it is)
M3A_OLD = "                SELECT c.USAGE_TIME::DATE AS DAY,\n"
M3A_NEW = (
    "                -- V167 (R2-052): USAGE_TIME is TIMESTAMP_TZ, and ::DATE / ::TIMESTAMP_NTZ on a TIMESTAMP_TZ\n"
    "                -- read the value's OWN stored offset, not the session zone. Convert to Central first, so DAY\n"
    "                -- is the account day the Central-midnight window bound below (and every sibling fact) uses,\n"
    "                -- and FIRST_TS / LAST_TS hold Central wall clock (a no-op if the views stamp Central).\n"
    "                SELECT CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY,\n")
M3B_OLD = ("                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,\n"
           "                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,\n")
M3B_NEW = ("                       CONVERT_TIMEZONE('America/Chicago', MIN(c.USAGE_TIME))::TIMESTAMP_NTZ AS FIRST_TS,\n"
           "                       CONVERT_TIMEZONE('America/Chicago', MAX(c.USAGE_TIME))::TIMESTAMP_NTZ AS LAST_TS,\n")

# M4 (R1-016) -- the DAILY freshness MERGE (the same 5 lines also close the HOURLY MERGE: scoped to DAILY)
STAMP_DAY = "DATEADD('day', -:d + 1, CURRENT_DATE())"
M4_OLD = (
    "            STATUS = s.STATUS\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);\n")
M4_NEW = (
    "            STATUS = s.STATUS,\n"
    "            -- V167 (R1-016): the AI fact's loaded-from watermark. FACT_AI_USAGE_DAILY holds only days that\n"
    "            -- had usage, so MIN(DAY) is the first AI use, not how far back this loader reached, and the\n"
    "            -- app gate read it as the latter (blank 180d / 365d / Current-year AI panels). The HAVING above\n"
    "            -- stamps the row only when BOTH AI arms loaded. Record the earliest whole day such a run covered\n"
    "            -- (today - d + 1), kept as the deepest reach ever (LEAST): the daily d=3 run never narrows it.\n"
    f"            COVERAGE_FROM = IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY',\n"
    f"                                LEAST(COALESCE(t.COVERAGE_FROM, {STAMP_DAY}),\n"
    f"                                      {STAMP_DAY}),\n"
    "                                t.COVERAGE_FROM)\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS,\n"
    f"                IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY', {STAMP_DAY}, NULL));\n")

marts = marts_base
marts = _swap_in(marts, *ARM1, M1_OLD, M1_NEW, "M1 arm [1] qh")
marts = _swap_in(marts, *ARM6, M2A_OLD, M2A_NEW, "M2a arm [6] attempts")
marts = _swap_in(marts, *ARM6, M2B_OLD, M2B_NEW, "M2b arm [6] final SELECT")
marts = _swap_in(marts, *ARM9, M3A_OLD, M3A_NEW, "M3a arm [9] DAY")
marts = _swap_in(marts, *ARM9, M3B_OLD, M3B_NEW, "M3b arm [9] FIRST_TS / LAST_TS")
_h, _d = marts.split(DAILY_SPLIT, 1)
marts = _h + DAILY_SPLIT + _swap(_d, M4_OLD, M4_NEW, "M4 DAILY freshness MERGE")

# --- M-asserts ---------------------------------------------------------------------------------------
_hourly, _daily = marts.split(DAILY_SPLIT, 1)
_arm1 = marts[marts.index(ARM1[0]):marts.index(ARM1[1])]
assert _arm1.count("WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 2           # m and q keep D-d
assert _arm1.count("WHERE mh.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 1        # m_idle keeps D-d
assert _arm1.count("GENERATOR(ROWCOUNT => 25)") == 1
_arm6 = marts[marts.index(ARM6[0]):marts.index(ARM6[1])]
assert _arm6.count("WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())") == 1       # QAH bound unchanged
assert _arm6.count("WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 1     # IN-list unchanged
assert _arm6.count("-:d - 1") == 2 and _arm6.count("WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())") == 1
_arm9 = marts[marts.index(ARM9[0]):marts.index(ARM9[1])]
assert "c.USAGE_TIME::DATE" not in _arm9 and "MIN(c.USAGE_TIME)::" not in _arm9 and "MAX(c.USAGE_TIME)::" not in _arm9
assert "f.START_TIME::DATE AS DAY" in _daily and _daily.count("GROUP BY 1, 2, 3\n") == 1   # ai_functions + positional key
assert "COVERAGE_FROM" not in _hourly and _daily.count("COVERAGE_FROM") == 4
assert marts.count("UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE") == 0
assert marts.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t") == 2
assert marts.count("HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))") == 1
assert marts.count("IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN") == 3                                # V159 gates intact
LOADED_TOKENS = tuple(re.findall(r"loaded := loaded \|\| '(\w+) ';", marts))

# ===================================================================================================
# SP_NIGHTLY_RECONCILE()  (base V064)
# ===================================================================================================
recon_base = extract_proc(BASE_RECON.read_text(encoding="utf-8"), "SP_NIGHTLY_RECONCILE(")
SWEPT = (  # (table, :loaded token of the arm that MERGEs it, grain)
    ("MART_WAREHOUSE_EFFICIENCY_DAILY", "wh_eff", "day"),
    ("MART_TASK_GRAPH_DAILY", "graphs", "day"),
    ("FACT_QUERY_ROLE_HOURLY", "role_hr", "hour"),
    ("FACT_QUERY_SCHEMA_HOURLY", "schema_hr", "hour"),
)
for _t, _tok, _g in SWEPT:
    assert _tok in LOADED_TOKENS, _tok

N1_DELETES = {
    "MART_WAREHOUSE_EFFICIENCY_DAILY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY\n"
                                        "     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());\n"),
    "FACT_QUERY_ROLE_HOURLY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY\n"
                               "     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());\n"),
    "FACT_QUERY_SCHEMA_HOURLY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY\n"
                                 "     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());\n"),
    "MART_TASK_GRAPH_DAILY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY\n"
                              "     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());\n"),
}
N1_NOTE = (
    "    -- V167 (R2-018): MART_WAREHOUSE_EFFICIENCY_DAILY, FACT_QUERY_ROLE_HOURLY, FACT_QUERY_SCHEMA_HOURLY and\n"
    "    -- MART_TASK_GRAPH_DAILY are no longer DELETEd here: they are mark-and-swept after the marts CALL below.\n")
N2_OLD = (
    "    -- failure between the DELETEs (above) and these CALLs leaves a transient gap until the\n"
    "    -- next nightly run re-covers. The robust fix (build-into-staging + atomic SWAP, or one\n")
N2_NEW = (
    "    -- failure between the DELETEs (above) and these CALLs leaves a gap that the next hourly run\n"
    "    -- (extract-fed D-2 marts) or the held per-source watermark (daily facts) re-covers. V167\n"
    "    -- (R2-018): that did NOT hold for the four tables whose reload edge is wider than the hourly\n"
    "    -- task's ('HOURLY', 2) -- MART_WAREHOUSE_EFFICIENCY_DAILY + MART_TASK_GRAPH_DAILY (D-3) and\n"
    "    -- FACT_QUERY_ROLE_HOURLY + FACT_QUERY_SCHEMA_HOURLY (~now-3d): only this proc re-loads that\n"
    "    -- edge, so one failed arm lost it for good. Those four are no longer DELETEd up front; they\n"
    "    -- are MARK-AND-SWEPT after the SP_LOAD_MARTS_V27 CALL below.\n"
    "    -- The robust fix (build-into-staging + atomic SWAP, or one\n")
N3_ANCHOR = (
    "    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);\n"
    "    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));\n"
    "    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;\n")
# V159's ext_lo_hour expression (arms [3] / [4]) WITHOUT its COALESCE fallback: an empty extract -> NULL -> no sweep
EXT_LO_HOUR = ("DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), "
               "DATE_TRUNC('hour', MIN(START_TIME)))")
assert marts.count(EXT_LO_HOUR) == 1
DAY_BOUND = "DATEADD('day', -3, CURRENT_DATE())"


def _sweep(table: str, token: str, grain: str) -> str:
    if grain == "day":
        bound = f"     WHERE DAY >= {DAY_BOUND}\n"
    else:
        bound = (f"     WHERE HOUR_TS >= (SELECT GREATEST({DAY_BOUND},\n"
                 f"                              {EXT_LO_HOUR})\n"
                 "                       FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)\n")
    return (f"    DELETE FROM DBA_MAINT_DB.OVERWATCH.{table}\n" + bound
            + "       AND LOAD_TS < :recon_start\n"
            + f"       AND ARRAY_CONTAINS('{token}'::VARIANT, SPLIT(:rv, ' '));\n")


N3_SWEEP = (
    "    -- V167 (R2-018) MARK-AND-SWEEP. Every SP_LOAD_MARTS_V27 MERGE arm stamps LOAD_TS = CURRENT_TIMESTAMP()\n"
    "    -- on UPDATE and the column DEFAULTs to it on INSERT, so a row this reload wrote has LOAD_TS >=\n"
    "    -- recon_start; an older row inside the reload window is a key the reload no longer produces (a\n"
    "    -- vanished warehouse / pipeline / role / schema) and is swept. Each sweep runs ONLY when that arm's\n"
    "    -- own :loaded token is in THIS call's verdict (rv, still the marts verdict here), so a failed arm keeps\n"
    "    -- its previous rows (stale but present) instead of leaving a D-3 hole no later run refills. The hour\n"
    "    -- bound is the [3] / [4] arms' real lower edge GREATEST(D-3, ext_lo_hour); an empty extract gives NULL\n"
    "    -- and sweeps nothing.\n"
    + "".join(_sweep(*row) for row in SWEPT))

recon = recon_base
recon = _swap(recon, N1_DELETES["MART_WAREHOUSE_EFFICIENCY_DAILY"], N1_NOTE, "N1 wh_eff DELETE")
for _t in ("FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY", "MART_TASK_GRAPH_DAILY"):
    recon = _swap(recon, N1_DELETES[_t], "", f"N1 {_t} DELETE")
recon = _swap(recon, N2_OLD, N2_NEW, "N2 transient-gap comment")
recon = _swap(recon, N3_ANCHOR, N3_ANCHOR + N3_SWEEP, "N3 sweep after the marts verdict")
# --- N-asserts -------------------------------------------------------------------------------------
_call = recon.index("    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);\n")
_diag = recon.index("    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);\n")
for _t, _tok, _g in SWEPT:
    _first = recon.index(f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{_t}\n")
    assert recon.count(f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{_t}\n") == 1 and _call < _first < _diag, _t
assert recon.count("DELETE FROM DBA_MAINT_DB.OVERWATCH.") == 11                  # 7 untouched up front + 4 sweeps
assert recon.index("recon_start := CURRENT_TIMESTAMP();") < recon.index("DELETE FROM")
assert "transient gap" not in recon and recon.count("CALL DBA_MAINT_DB.OVERWATCH.") == 5

# ===================================================================================================
# SP_LOAD_PATTERN_COST(FLOAT)  (base V120)
# ===================================================================================================
pattern_base = extract_proc(BASE_PATTERN.read_text(encoding="utf-8"), "SP_LOAD_PATTERN_COST(")
P1_OLD = "$$\nBEGIN\n    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\n    USING (\n"
P1_NEW = (
    "$$\n"
    "DECLARE\n"
    "    lo DATE;   -- V167 (R2-010 D1): ONE lower bound for the DELETE and both source filters\n"
    "BEGIN\n"
    "    lo := DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE());\n"
    "    -- V167 (R2-010 D2): COMPANY is COMPANY_FOR_WAREHOUSE() at load time and part of the grain, so the\n"
    "    -- V037-V120 MERGE could not match a row whose warehouse was re-mapped: it INSERTed a second row\n"
    "    -- under the new company and kept the old one, and the ALL scope summed both. Replace the window\n"
    "    -- atomically instead (the SP_LOAD_OBJECT_COST idiom, V139): a failed INSERT rolls the DELETE back\n"
    "    -- and readers keep the previous fill. COMPANY stays in the grain (the mart has no WAREHOUSE_NAME,\n"
    "    -- so one DAY / HASH / DB can legitimately span companies). No DDL inside the transaction.\n"
    "    BEGIN TRANSACTION;\n"
    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n"
    "     WHERE DAY >= :lo;\n"
    "    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n"
    "        (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)\n")
P2_OLD = (
    "        GROUP BY 1, 2, 3, 4\n"
    "    ) s\n"
    "    ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY\n"
    "       AND t.DATABASE_NAME = s.DATABASE_NAME\n"
    "    WHEN MATCHED THEN UPDATE SET\n"
    "        t.RUNS = s.RUNS, t.CREDITS_ATTRIBUTED = s.CREDITS_ATTRIBUTED,\n"
    "        t.USERS_HLL = s.USERS_HLL, t.LOAD_TS = CURRENT_TIMESTAMP()\n"
    "    WHEN NOT MATCHED THEN INSERT (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)\n"
    "    VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.DATABASE_NAME, s.RUNS, s.CREDITS_ATTRIBUTED, s.USERS_HLL);\n")
P2_NEW = (
    "        GROUP BY 1, 2, 3, 4;\n"
    "    COMMIT;\n")
P3A_OLD = "                    WHERE START_TIME >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())\n"
P3A_NEW = "                    WHERE START_TIME >= :lo\n"
P3B_OLD = "                 AND q.START_TIME >= DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())\n"
P3B_NEW = "                 AND q.START_TIME >= :lo\n"
P4A_OLD = ("               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT\n"
           "    ) s\n")
P4A_NEW = ("               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT,\n"
           "               :lo AS COVERAGE_FROM   -- V167 (PATTERN-RESTAMP D6): the first day this run replaced\n"
           "    ) s\n")
P4B_OLD = ("        STATUS = 'loader'\n"
           "    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
           "    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');\n")
P4B_NEW = (
    "        STATUS = 'loader',\n"
    "        -- V167 (PATTERN-RESTAMP D6): the deepest day an atomic reload ever replaced, NULL-safe both ways\n"
    "        -- (a NULL DAYS_BACK keeps the old stamp). The app lifts its 90-day pattern cap only for a read\n"
    "        -- window whose first day is on or after it. Reached only after the COMMIT (a failure re-raises).\n"
    "        COVERAGE_FROM = LEAST(COALESCE(t.COVERAGE_FROM, s.COVERAGE_FROM), COALESCE(s.COVERAGE_FROM, t.COVERAGE_FROM))\n"
    "    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)\n"
    "    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader', s.COVERAGE_FROM);\n")
P5_OLD = "    RETURN 'OK';\nEND;\n$$;"
P5_NEW = (
    "    RETURN 'OK';\n"
    "EXCEPTION   -- V167 (R2-010 D5): never leave the window deleted; re-raise so the task still fails visibly\n"
    "    WHEN OTHER THEN\n"
    "        ROLLBACK;\n"
    "        RAISE;\n"
    "END;\n"
    "$$;")

pattern = pattern_base
pattern = _swap(pattern, P1_OLD, P1_NEW, "P1/P2 DECLARE + txn head")
pattern = _swap(pattern, P2_OLD, P2_NEW, "P2 MERGE tail -> INSERT + COMMIT")
pattern = _swap(pattern, P3A_OLD, P3A_NEW, "P3a QAH filter")
pattern = _swap(pattern, P3B_OLD, P3B_NEW, "P3b QH filter")
pattern = _swap(pattern, P4A_OLD, P4A_NEW, "P4a freshness source")
pattern = _swap(pattern, P4B_OLD, P4B_NEW, "P4b freshness MERGE arms")
pattern = _swap(pattern, P5_OLD, P5_NEW, "P5 EXCEPTION")
# --- P-asserts -------------------------------------------------------------------------------------
assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY" not in pattern
assert pattern.count("DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())") == 1 and pattern.count(">= :lo") == 3
_order = [pattern.index(s) for s in (
    "    BEGIN TRANSACTION;\n", "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n",
    "    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n", "    COMMIT;\n",
    "    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t\n", "        ROLLBACK;\n        RAISE;\n")]
assert _order == sorted(_order)
_txn = pattern[pattern.index("BEGIN TRANSACTION;"):pattern.index("COMMIT;")]
assert not re.search(r"\b(CREATE|ALTER|DROP|TRUNCATE)\b", _txn)
assert pattern.count("GROUP BY 1, 2, 3, 4") == 2 and "UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" not in pattern

for _p in (marts, recon, pattern):
    _body = _p[_p.index("$$") + 2:_p.rindex("$$")]
    assert "\\" not in _body and "$$" not in _body, "backslash-free body, no inner $$"

# ===================================================================================================
# The one-time in-migration repair (R2-010): the scan-free stale-company twin DELETE
# ===================================================================================================
TWIN_GROUPS = (
    "    SELECT DAY, QUERY_HASH, DATABASE_NAME, MAX(LOAD_TS) AS NEWEST_TS\n"
    "    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n"
    "    GROUP BY DAY, QUERY_HASH, DATABASE_NAME\n"
    "    HAVING COUNT(*) > 1\n")
TWIN_MATCH = ("t.DAY = g.DAY AND t.QUERY_HASH = g.QUERY_HASH AND t.DATABASE_NAME = g.DATABASE_NAME\n"
              "  AND t.LOAD_TS < DATEADD('minute', -10, g.NEWEST_TS)")
TWIN_DELETE = (
    "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\n"
    "USING (\n" + TWIN_GROUPS + ") g\n"
    "WHERE " + TWIN_MATCH + ";\n")

# ===================================================================================================
# File
# ===================================================================================================
ALTER = "ALTER TABLE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS COVERAGE_FROM DATE;\n"
MARKER_MARTS = ("-- >>> derived:SP_LOAD_MARTS_V27  (from V159; + R2-015 arm [1] span source padded a day, R2-014 arm [6] "
                "lead-in day + DAY filter, R2-052 arm [9] Central day key, R1-016 AI COVERAGE_FROM in the DAILY "
                "freshness MERGE, V167)")
MARKER_RECON = ("-- >>> derived:SP_NIGHTLY_RECONCILE  (from V064; R2-018 token-gated mark-and-sweep of the four "
                "wide-edge tables after the marts reload instead of DELETE-first, V167)")
MARKER_PATTERN = ("-- >>> derived:SP_LOAD_PATTERN_COST  (from V120; R2-010 atomic DELETE + INSERT of the window "
                  "instead of the COMPANY-keyed MERGE, + the PATTERN-RESTAMP COVERAGE_FROM stamp, V167)")

DESCRIPTION = (
    "Mart-loader window edges, the AI coverage watermark and the atomic pattern reload (R2-015, R2-014, R2-052 "
    "(also R2-017), R1-016, R2-018, R2-010, PATTERN-RESTAMP). SOURCE_FRESHNESS_STATE gains a nullable "
    "COVERAGE_FROM DATE. SP_LOAD_MARTS_V27 (re-derived from V159): arm [1] pads its query-span source one day "
    "back with an END_TIME floor, so a query crossing the window edge marks its hours active (idle no longer "
    "overstated); arm [6] reads one lead-in day and keeps only runs whose first attempt is in the window "
    "(no phantom child-named pipeline rows); arm [9] keys Cortex Code DAY and FIRST_TS/LAST_TS in Central "
    "(USAGE_TIME is TIMESTAMP_TZ); the DAILY freshness MERGE stamps COVERAGE_FROM for FACT_AI_USAGE_DAILY when "
    "both AI arms loaded (deepest reach, -d+1). SP_NIGHTLY_RECONCILE (re-derived from V064) no longer DELETEs "
    "MART_WAREHOUSE_EFFICIENCY_DAILY, MART_TASK_GRAPH_DAILY, FACT_QUERY_ROLE_HOURLY and FACT_QUERY_SCHEMA_HOURLY "
    "up front; it sweeps rows the reload did not re-stamp (LOAD_TS before the run) only when that arm loaded, "
    "so a failed arm no longer leaves a permanent D-3 hole. SP_LOAD_PATTERN_COST (re-derived from V120) "
    "replaces its window atomically (DELETE + INSERT, ROLLBACK + RAISE) instead of a COMPANY-keyed MERGE that "
    "left a stale-company twin after a COMPANY_SCOPE remap, and stamps COVERAGE_FROM. One-time in-migration "
    "repair: a scan-free DELETE of MART_PATTERN_COST_DAILY rows older than their group newest LOAD_TS by more "
    "than 10 minutes. No task, rule, grant or view change; nothing is CALLed at apply time. The heavy reloads "
    "(AI DAILY 365 reload-then-prune, pattern 364, HOURLY N, the arm [6] 364-day rebuild) are owner-run."
)
assert "'" not in DESCRIPTION and "\n" not in DESCRIPTION and len(DESCRIPTION) <= 4000

HEADER = f"""-- {NAME}
--
-- Mart-loader window edges, the AI coverage watermark and the atomic pattern reload (v4.609 round-2 fixes).
--
-- WHY:
--   * R2-015: SP_LOAD_MARTS_V27 arm [1] expanded a query's spanned hours only for queries that STARTED inside
--     the window, so the hours a midnight-crossing query ran on day D-d read idle. The nightly reconcile last
--     writes a day as D-3, its left edge, so every finished day kept that error: IDLE_PCT / IDLE_CREDITS (the
--     Optimize idle and sizing panels, COST_IDLE_OPPORTUNITY) overstated idle on ELT warehouses.
--   * R2-014: arm [6] built task-graph runs only from attempts inside the window, so a run whose root started
--     before the window's first midnight was re-keyed to its first in-window child: a phantom pipeline row on
--     D-d, finalised by the reconcile, double-counting the children's credits.
--   * R2-052 (also satisfies R2-017): the Cortex Code views stamp USAGE_TIME as TIMESTAMP_TZ, and ::DATE /
--     ::TIMESTAMP_NTZ read its OWN stored offset, so arm [9] keyed days off the Central account day (and the
--     Central-midnight scan bound cut the oldest kept key short every run: a permanent under-count).
--   * R1-016: FACT_AI_USAGE_DAILY has no zero-row spine, so MIN(DAY) is the first AI use, not how far back the
--     loader reached; the app gate read it as the latter and blanked 180d / 365d / Current-year AI panels.
--   * R2-018: SP_NIGHTLY_RECONCILE DELETEd four tables up front whose reload edge (D-3 / ~now-3d) is wider
--     than the hourly task's d=2; one failed loader arm (each swallows its error) left that edge empty for good.
--     The V064 'transient gap' comment (and CHANGELOG:12389) was wrong for these four.
--   * R2-010: SP_LOAD_PATTERN_COST MERGEd on (DAY, QUERY_HASH, COMPANY, DATABASE_NAME) with COMPANY computed at
--     load time, so an Apply-mapping remap left the old-company row beside the new one (ALL scope summed both).
--   * PATTERN-RESTAMP: rows older than V120's 90-day re-stamp still carry inflated RUNS; a full-year reload is
--     only safe on the atomic loader, and the app may only read past 90 days where a reload provably reached.
--
-- WHAT (each re-derived proc is byte-identical to its base except the enumerated deltas):
--   + SOURCE_FRESHNESS_STATE.COVERAGE_FROM DATE (nullable; every writer names its columns).
--   ~ SP_LOAD_MARTS_V27 from V159: arm [1] span source START_TIME >= D-d-1 AND COALESCE(END_TIME, START_TIME)
--     >= D-d (m / q / m_idle keep D-d); arm [6] attempts from D-d-1 and WHERE DAY >= D-d before the final
--     GROUP BY (the QAH and IN-list bounds unchanged); arm [9] CONVERT_TIMEZONE('America/Chicago', ...) on DAY,
--     FIRST_TS and LAST_TS (the ai_functions arm keys an LTZ START_TIME, unchanged); the DAILY freshness MERGE
--     writes COVERAGE_FROM for FACT_AI_USAGE_DAILY = LEAST(old, today - d + 1) -- its HAVING already requires
--     BOTH AI arms. No UPDATE of SOURCE_FRESHNESS_STATE anywhere.
--   ~ SP_NIGHTLY_RECONCILE from V064: the four up-front DELETEs go; after the ('HOURLY', 3) verdict line each
--     table is swept of rows the reload did not re-stamp (LOAD_TS < recon_start) inside its reload window,
--     ONLY when that arm's :loaded token is in the marts verdict (rv). The hour bound is V159's ext_lo_hour
--     expression without its COALESCE fallback (an empty extract sweeps nothing). V159's 'SP_NIGHTLY_RECONCILE
--     DELETEs D-3..today' comment is superseded; its d > 2 gate escape is still required.
--   ~ SP_LOAD_PATTERN_COST from V120: BEGIN TRANSACTION; DELETE WHERE DAY >= :lo; INSERT (V120's aggregate,
--     unchanged); COMMIT; the V068 freshness MERGE (after the COMMIT) stamps COVERAGE_FROM = LEAST(old, :lo);
--     EXCEPTION WHEN OTHER THEN ROLLBACK; RAISE (the task still fails loudly).
--   - One-time repair: DELETE MART_PATTERN_COST_DAILY rows older than their (DAY, QUERY_HASH, DATABASE_NAME)
--     group's newest LOAD_TS by more than 10 minutes (groups of 2+ rows only). Every legitimate company row of
--     a day's last covering run shares that run's statement timestamp (V120's MERGE re-stamped LOAD_TS on
--     MATCHED), so an older row is a stale-company twin. Scan-free; recoverable by Time Travel or a reload.
--     Preview with PREFLIGHT P167.1 (rows, credits, day range) and P167.2 (same-stamp groups that are kept).
--
-- COST: arm [1]'s span leg scans one more day of QUERY_HISTORY (3 days instead of 2 on the six gated hourly
-- runs, 4 instead of 3 on the reconcile); arm [6] one more day of TASK_HISTORY; the same statement shapes, so
-- V159's compile diet stands. The reconcile adds four small DELETEs (two with a one-row extract subquery). The
-- pattern loader's statement count goes 2 -> 4 (BEGIN / DELETE / INSERT / COMMIT) on its daily 3-day window.
-- The twin DELETE reads only the mart. No new task, schedule or warehouse resume.
-- FIRST RUN: the next hourly TASK_LOAD_HOURLY graph, the 06:40-07:30 Central nightly chain and the 06:45
-- TASK_PATTERN_COST_DAILY pick up the new bodies. Nothing runs at apply time. COVERAGE_FROM starts as
-- today - 2 (AI) / today - 3 (pattern) and reaches back only after the owner-run reloads in the handoff
-- (OWNER_REPAIRS, a Central session): AI DAILY 365 reload-then-prune, then CALL SP_LOAD_PATTERN_COST(364),
-- then HOURLY N inside a TASK_LOAD_HOURLY suspend window, then the atomic arm [6] 364-day rebuild.
-- Known limits (disclosed): a query of 24h+ starting in hour 23 two days before a left edge on the
-- spring-forward night can still miss one hour; a task-graph run longer than ~1 day past the left edge can
-- still leave a phantom row; Cortex Code rows older than the views' retention stay offset-keyed.
-- ROLLBACK: re-run the base CREATEs (V159 SP_LOAD_MARTS_V27, V064 SP_NIGHTLY_RECONCILE, V120
-- SP_LOAD_PATTERN_COST); the column is inert to every older body (leave it). The twin DELETE is recoverable
-- by Time Travel (AT before the apply) or CALL SP_LOAD_PATTERN_COST(N).
-- Apply AFTER V166. Idempotent; safe to re-run. Owner applies in Snowsight; this file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20167, 'V167 requires V166 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 166) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- R1-016 / PATTERN-RESTAMP: the loaded-from watermark both re-derived loaders write inside their own freshness
-- MERGE (nullable; read only by the app, behind has_migration(167)).
"""

TAIL = f"""
-- R2-010 one-time repair (after the atomic loader is in place): drop the stale-company twins the COMPANY-keyed
-- MERGE left behind (the V037 -> V044 -> V047 era, plus any Apply-mapping remap since). Scan-free: reads only the
-- mart. A group's legitimate company rows share their last covering run's LOAD_TS; the 10-minute slack absorbs
-- one load statement. Groups of one row are never touched.
{TWIN_DELETE}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 167 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 167);
"""

out = (HEADER + ALTER + "\n" + MARKER_MARTS + "\n" + marts + "\n\n" + MARKER_RECON + "\n" + recon + "\n\n"
       + MARKER_PATTERN + "\n" + pattern + "\n" + TAIL)

# ---- post-asserts -------------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 3 and out.count("$$") == 8
for marker, create in ((MARKER_MARTS, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27("),
                       (MARKER_RECON, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_NIGHTLY_RECONCILE("),
                       (MARKER_PATTERN, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(")):
    assert out.count(marker) == 1 and out.count(create) == 1 and marker + "\n" + create in out
_outside = "".join(p for i, p in enumerate(out.split("$$")) if i % 2 == 0)
assert not re.search(r"^\s*CALL\b", _outside, re.M), "no apply-time CALL"
assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+TASK|ALTER\s+TASK|EXECUTE\s+TASK|\bGRANT\b|ALERT_CONFIG)",
                     out.replace(marts, "").replace(recon, "").replace(pattern, ""))
assert not re.search(r"UPDATE\s+DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE", out)
assert out.count(ALTER) == 1 and out.index(ALTER) < out.index("CREATE OR REPLACE PROCEDURE")
assert out.index("EXCEPTION (-20167") < out.index(ALTER)
assert out.rindex("$$;") < out.index(TWIN_DELETE) < out.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
assert "V167 requires V166 first" in out and "IF (v < 166) THEN" in out and "SELECT 167 AS VERSION" in out
assert "@" not in out, "no address of any kind"

target = Path(os.environ.get("V167_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")


# ===================================================================================================
# PREFLIGHT (read-only; run before applying V167). Built from the SAME text as the migration.
# ===================================================================================================
def _arm6_source() -> str:
    """Arm [6]'s USING (...) s source exactly as V167 ships it (the inner SELECT)."""
    a = marts[marts.index(ARM6[0]):marts.index(ARM6[1])]
    head = "            USING (\n"
    tail = "\n            ) s\n"
    return a[a.index(head) + len(head):a.index(tail, a.index(head))]


PREFLIGHT = f"""-- PREFLIGHT_V167 (READ-ONLY). Run before applying V167; changes nothing.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- P167.1 R2-010: the MART_PATTERN_COST_DAILY rows the in-migration twin DELETE would remove (its own USING / WHERE)
SELECT COUNT(*) AS TWIN_ROWS, ROUND(SUM(t.CREDITS_ATTRIBUTED), 2) AS TWIN_CREDITS,
       SUM(t.RUNS) AS TWIN_RUNS, MIN(t.DAY) AS FIRST_DAY, MAX(t.DAY) AS LAST_DAY,
       COUNT(DISTINCT t.COMPANY) AS COMPANIES
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
JOIN (
{TWIN_GROUPS}) g
  ON {TWIN_MATCH};

-- P167.2 R2-010: multi-company groups whose rows all share one LOAD_TS window -- legitimate, KEPT by the DELETE
SELECT COUNT(*) AS KEPT_MULTI_COMPANY_GROUPS, MIN(DAY) AS FIRST_DAY, MAX(DAY) AS LAST_DAY
FROM (
    SELECT DAY, QUERY_HASH, DATABASE_NAME
    FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY
    GROUP BY DAY, QUERY_HASH, DATABASE_NAME
    HAVING COUNT(*) > 1 AND MIN(LOAD_TS) >= DATEADD('minute', -10, MAX(LOAD_TS))
) k;

-- P167.3 R2-052: do the Cortex Code views stamp a non-Central offset? MISKEYED > 0 proves the day-key defect.
SELECT 'Snowsight' AS SOURCE, TO_CHAR(USAGE_TIME, 'TZH:TZM') AS STORED_OFFSET, COUNT(*) AS ROWS_7D,
       COUNT_IF(USAGE_TIME::DATE <> CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE) AS MISKEYED
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
WHERE USAGE_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1, 2
UNION ALL
SELECT 'CLI', TO_CHAR(USAGE_TIME, 'TZH:TZM'), COUNT(*),
       COUNT_IF(USAGE_TIME::DATE <> CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE)
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
WHERE USAGE_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1, 2;

-- P167.4 R2-014: task-graph runs that cross Central midnight in the last 30 days (each was a phantom row candidate)
SELECT COUNT(*) AS STRADDLING_RUNS, COUNT(DISTINCT ROOT_NAME) AS PIPELINES
FROM (
    SELECT COALESCE(GRAPH_RUN_GROUP_ID::VARCHAR, QUERY_ID) AS RUN_KEY,
           MIN_BY(NAME, QUERY_START_TIME) AS ROOT_NAME,
           MIN(QUERY_START_TIME) AS FIRST_START,
           MAX(QUERY_START_TIME) AS LAST_START
    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
    WHERE QUERY_START_TIME >= DATEADD('day', -30, CURRENT_DATE())
      AND STATE IN ('SUCCEEDED', 'FAILED')
    GROUP BY 1
) r
WHERE DATE(FIRST_START) < DATE(LAST_START);
"""

# PART B (read-only; after the apply). Fragments are GET_DDL-checked against the deployed bodies.
PART_B_FRAGMENTS = (
    ("SP_LOAD_MARTS_V27(VARCHAR, FLOAT)", "COALESCE(END_TIME, START_TIME) >= DATEADD"),
    ("SP_LOAD_MARTS_V27(VARCHAR, FLOAT)", "CONVERT_TIMEZONE(" + "'America/Chicago', c.USAGE_TIME)::DATE AS DAY"),
    ("SP_LOAD_MARTS_V27(VARCHAR, FLOAT)", "WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, "
                                          "GENERATION, STATUS, COVERAGE_FROM)"),
    ("SP_NIGHTLY_RECONCILE()", "AND LOAD_TS < :recon_start"),
    ("SP_LOAD_PATTERN_COST(FLOAT)", "BEGIN TRANSACTION"),
    ("SP_LOAD_PATTERN_COST(FLOAT)", "AS COVERAGE_FROM"),
)
for _sig, _frag in PART_B_FRAGMENTS:
    _src = {"SP_LOAD_MARTS_V27(VARCHAR, FLOAT)": marts, "SP_NIGHTLY_RECONCILE()": recon,
            "SP_LOAD_PATTERN_COST(FLOAT)": pattern}[_sig]
    assert _frag in _src and "'" not in _frag.replace("'America/Chicago'", ""), _frag


def _ddl_check(sig: str, frag: str) -> str:
    lit = frag.replace("'", "''")
    return f"CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{sig}'), '{lit}')"


_ddl_cols = ",\n       ".join(f"{_ddl_check(s, f)} AS CHECK_{i + 1}" for i, (s, f) in enumerate(PART_B_FRAGMENTS))
PART_B = f"""-- PART B -- V167 (READ-ONLY). After the apply; changes nothing.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- V167.1 the watermark column exists and what each loader has stamped so far (NULL until its next run)
SELECT SOURCE_NAME, COVERAGE_FROM, STATUS, SNAPSHOT_TS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME IN ('FACT_AI_USAGE_DAILY', 'MART_PATTERN_COST_DAILY')
ORDER BY SOURCE_NAME;

-- V167.2 the deployed bodies carry the V167 deltas (expect every CHECK_n TRUE)
SELECT {_ddl_cols};

-- V167.3 R2-010: stale-company twins left in the pattern mart (expect 0 after the apply)
SELECT COUNT(*) AS TWIN_ROWS_LEFT
FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t
JOIN (
{TWIN_GROUPS}) g
  ON {TWIN_MATCH};

-- V167.4 R2-052: after OWNER_REPAIRS step 1, Cortex Code fact days vs the live views per Central day, D-31..D-4
SELECT COALESCE(f.DAY, l.DAY) AS DAY, f.CREDITS AS FACT_CREDITS, l.CREDITS AS LIVE_CREDITS,
       ROUND(COALESCE(f.CREDITS, 0) - COALESCE(l.CREDITS, 0), 4) AS DRIFT
FROM (
    SELECT DAY, SUM(CREDITS) AS CREDITS
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE IN ('Snowsight', 'CLI')
      AND DAY >= DATEADD('day', -31, CURRENT_DATE()) AND DAY < DATEADD('day', -3, CURRENT_DATE())
    GROUP BY 1
) f
FULL OUTER JOIN (
    SELECT CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE AS DAY, SUM(TOKEN_CREDITS) AS CREDITS
    FROM (
        SELECT USAGE_TIME, TOKEN_CREDITS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY
        WHERE USAGE_TIME >= DATEADD('day', -33, CURRENT_DATE())
        UNION ALL
        SELECT USAGE_TIME, TOKEN_CREDITS FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
        WHERE USAGE_TIME >= DATEADD('day', -33, CURRENT_DATE())
    ) c
    WHERE CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE >= DATEADD('day', -31, CURRENT_DATE())
      AND CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE < DATEADD('day', -3, CURRENT_DATE())
    GROUP BY 1
) l ON l.DAY = f.DAY
ORDER BY 1;
"""


# ===================================================================================================
# OWNER_REPAIRS (owner-run, ordered, after ALL of V166-V172 are applied). Central session first.
# ===================================================================================================
def _verdict_block(call: str, label: str, fail_like: str = "rv ILIKE 'MARTS WITH ERRORS%'") -> str:
    """The backfill_365 R1-231 idiom, in full: one guarded CALL per block. A failure verdict OR a raised error
    becomes a 'FAILED: ...' pane plus an APP_ERROR_LOG row (PAGE 'OwnerRepairV167'), so a Run All still reaches
    step 3's RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE (review: without the EXCEPTION handler a raised error aborted
    the worksheet and stranded TASK_LOAD_HOURLY suspended). Only a statement timeout or a Stop escapes."""
    return f"""EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
BEGIN
    CALL {call};
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR {fail_like}) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), '{label}', CURRENT_ROLE();
        RETURN 'FAILED: {label} -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: {label} -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepairV167', 'owner_repair_call_failed', LEFT(:emsg, 2000), '{label}', CURRENT_ROLE();
        RETURN 'FAILED: {label} - ' || emsg;
END;
$$;
"""


ARM6_SOURCE = _arm6_source()
REBUILD_DAYS = 364
_rebuild_src = ARM6_SOURCE.replace(":d", str(REBUILD_DAYS))
_rebuild_code = re.sub(r"--[^\n]*", "", re.sub(r"'[^']*'", "", _rebuild_src))
assert not re.search(r"(?<![:\w]):[A-Za-z_]", _rebuild_code), "a bind survived in the rebuild source"
assert _rebuild_src.count(f"-{REBUILD_DAYS} - 1") == 2 and ARM6_SOURCE.count(":d") == 4
GRAPH_COLS = ("DAY, PIPELINE, DATABASE_NAME, SCHEMA_NAME, GRAPH_RUNS, RUNS_WITH_FAILURES, "
              "TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS")
REBUILD_BLOCK = f"""EXECUTE IMMEDIATE $$
BEGIN
    BEGIN TRANSACTION;
    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -{REBUILD_DAYS}, CURRENT_DATE());
    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
        ({GRAPH_COLS})
    SELECT {GRAPH_COLS}
    FROM (
{_rebuild_src}
    ) s;
    COMMIT;
    RETURN 'ok: MART_TASK_GRAPH_DAILY rebuilt for {REBUILD_DAYS} days';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
"""

AI_RELOAD_BLOCK = """EXECUTE IMMEDIATE $$
DECLARE
    t0 TIMESTAMP_NTZ;
    rv VARCHAR;
    touched NUMBER DEFAULT 0;
    lo DATE;
    pruned NUMBER DEFAULT 0;
BEGIN
    t0 := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    -- prune only when THIS reload's ai_code arm loaded (its token is in the verdict); a failed required posture
    -- arm still reads 'MARTS WITH ERRORS' in the returned verdict -- re-run the block once it is fixed
    IF (rv IS NULL OR NOT ARRAY_CONTAINS('ai_code'::VARIANT, SPLIT(:rv, ' '))) THEN
        RETURN 'FAILED (nothing pruned): SP_LOAD_MARTS_V27(DAILY, 365) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    SELECT COUNT(*), MIN(DAY) INTO :touched, :lo
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE IN ('Snowsight', 'CLI') AND LOAD_TS >= :t0;
    IF (touched > 0) THEN
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
         WHERE SOURCE IN ('Snowsight', 'CLI') AND DAY > :lo AND LOAD_TS < :t0;
        pruned := SQLROWCOUNT;
    END IF;
    RETURN 'ok: ' || rv || ' | re-keyed Code rows ' || touched || ' from ' || COALESCE(TO_VARCHAR(lo), 'n/a')
           || ', pruned stale offset-keyed rows ' || pruned;
END;
$$;
"""

REPAIR = f"""-- OWNER_REPAIRS -- V167 section (owner-run, heavy, off-peak). Run AFTER V166-V172 are applied, outside the
-- 06:40-07:30 Central nightly chain and outside the :07 gated slots (00/04/08/12/16/20 Central). Each block is
-- its own statement, verdict-checked, and safe to re-run. STATEMENT_TIMEOUT: a timeout rolls each block back
-- (or, for a CALL, leaves the MERGE-only loaders idempotent) -- re-run it on a larger warehouse.
-- FIRST statement: pin the session to Central. The owner worksheet runs in UTC, and every loader below keys
-- days on DATE() / ::DATE / CURRENT_DATE() in the session zone.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- ---- step 0 (READ-ONLY) R2-018 probes: does the reconcile's DELETE-first window hold holes today? -------------
-- 0a mart_load_failed history for the four wide-edge tables (each implies a hole at DATE(LOGGED_AT) - 3)
SELECT LOGGED_AT, CONTEXT, LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'MartLoader' AND ERROR_TYPE = 'mart_load_failed'
  AND SPLIT_PART(CONTEXT, ' ', 1) IN ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'MART_TASK_GRAPH_DAILY',
                                      'FACT_QUERY_ROLE_HOURLY', 'FACT_QUERY_SCHEMA_HOURLY')
ORDER BY LOGGED_AT DESC;
-- 0b warehouse-efficiency day holes vs metering (last 90 complete days)
SELECT DATE(m.START_TIME) AS DAY, COUNT(DISTINCT m.WAREHOUSE_NAME) AS METERED,
       COUNT(DISTINCT e.WAREHOUSE_NAME) AS IN_MART
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY m
LEFT JOIN DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY e
  ON e.DAY = DATE(m.START_TIME) AND e.WAREHOUSE_NAME = m.WAREHOUSE_NAME
WHERE m.START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND m.START_TIME < CURRENT_DATE()
  AND m.WAREHOUSE_ID > 0 AND m.CREDITS_USED > 0
GROUP BY 1
HAVING COUNT(DISTINCT e.WAREHOUSE_NAME) < COUNT(DISTINCT m.WAREHOUSE_NAME)
ORDER BY 1;
-- 0c task-graph day holes: days with finished TASK_HISTORY runs but no mart row
SELECT DATE(h.QUERY_START_TIME) AS DAY, COUNT(*) AS TASK_RUNS
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY h
WHERE h.QUERY_START_TIME >= DATEADD('day', -90, CURRENT_DATE()) AND h.QUERY_START_TIME < CURRENT_DATE()
  AND h.STATE IN ('SUCCEEDED', 'FAILED')
  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY g WHERE g.DAY = DATE(h.QUERY_START_TIME))
GROUP BY 1
ORDER BY 1;
-- 0d role-hour / schema-hour holes: hours in FACT_QUERY_HOURLY with no role-hour or schema-hour row
SELECT q.HOUR_TS,
       IFF(r.HOUR_TS IS NULL, 'missing', 'ok') AS ROLE_HOURLY,
       IFF(s.HOUR_TS IS NULL, 'missing', 'ok') AS SCHEMA_HOURLY
FROM (SELECT DISTINCT HOUR_TS FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
      WHERE HOUR_TS >= DATEADD('day', -90, CURRENT_DATE())) q
LEFT JOIN (SELECT DISTINCT HOUR_TS FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY) r ON r.HOUR_TS = q.HOUR_TS
LEFT JOIN (SELECT DISTINCT HOUR_TS FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY) s ON s.HOUR_TS = q.HOUR_TS
WHERE r.HOUR_TS IS NULL OR s.HOUR_TS IS NULL
ORDER BY 1;

-- ---- step 1 R2-052 + R1-016: re-key Cortex Code days to Central and stamp the AI COVERAGE_FROM ---------------
-- RELOAD-THEN-PRUNE, never DELETE first (the ai_code arm swallows its own error; a failed reload after a DELETE
-- would empty up to 365 days). Rows the reload re-keyed carry LOAD_TS >= t0; an older Code row inside the
-- re-keyed range is a stale offset-keyed twin. Expect 'ok: MARTS OK (DAILY, 365d): posture ai_code
-- ai_functions | ...'; the freshness MERGE then stamps COVERAGE_FROM = today - 364. If the oldest day reads
-- truncated, re-run with 360.
{AI_RELOAD_BLOCK}
-- ---- step 2 PATTERN-RESTAMP: one atomic full-retention re-stamp of MART_PATTERN_COST_DAILY ------------------
-- Only after V167 (the atomic loader): a bare CALL on the V120 MERGE loader would add a year of twins. 364
-- avoids the partly aged-out D-365. A timeout rolls the whole window back. Stamps COVERAGE_FROM = today - 364,
-- after which the app reads pattern windows up to 365 days.
{_verdict_block("DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(364)", "SP_LOAD_PATTERN_COST(364)", "rv <> 'OK'")}
-- ---- step 3 R2-015 (+ any R2-018 hole step 0 found): re-stamp idle / active hours ----------------------------
-- Inside a TASK_LOAD_HOURLY suspend window (backfill_365.sql B12), so the minute-7 watermark trim cannot shrink
-- the widened extract mid-run. N = max(90, the deepest hole step 0 found): edit both 90s together. Idle days
-- older than N keep their pre-V167 (overstated) idle.
-- Each CALL block catches its own error (logged to APP_ERROR_LOG, PAGE 'OwnerRepairV167', pane 'FAILED: ...'),
-- so a Run All still reaches the RESUME; a statement timeout or a Stop cannot be caught that way:
-- !! IF THIS STOPS BEFORE THE RESUME, run the RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE lines by hand !!
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;
{_verdict_block("DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(90)", "SP_LOAD_QH_EXTRACT(90)",
                "rv ILIKE '%extract committed: false%'")}{_verdict_block(
    "DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 90)", "SP_LOAD_MARTS_V27(HOURLY, 90)")}ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;
SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');

-- ---- step 4 R2-014: rebuild MART_TASK_GRAPH_DAILY for {REBUILD_DAYS} days with the V167 arm [6] -------------------
-- After step 3 (its HOURLY MERGE cannot delete phantom rows). One anonymous block: BEGIN TRANSACTION; DELETE;
-- INSERT (V167's arm [6] source with d = {REBUILD_DAYS}); COMMIT -- any error or timeout ROLLBACKs and re-raises, so
-- the mart never sits half-empty. Run outside the :07 Central slots of arm [6] (00/04/08/12/16/20).
{REBUILD_BLOCK}"""

for _env, _text, _label in (("PREFLIGHT_OUT", PREFLIGHT, "PREFLIGHT"), ("PART_B_OUT", PART_B, "PART B"),
                            ("REPAIR_OUT", REPAIR, "OWNER_REPAIRS")):
    _path = os.environ.get(_env)
    if _path:
        Path(_path).write_text(_text, encoding="utf-8", newline="\n")
        print(f"wrote {_label} {_path}")
print(f"V167 written: {target} ({len(out)} bytes)")
