#!/usr/bin/env python3
"""Forward-generate V079: make the LOADER's AI classification match the app-side
canonical predicate shipped in v4.158.0 (app/data/common), so Cortex Code /
CoWork (SNOWFLAKE_COCO_SNOWSIGHT) is attributed to AI, not compute/serverless.

The app DISPLAY splits AI vs compute at read time from raw FACT_METERING_DAILY
rows, so it is correct for all history. But four LOADER procs still classify
CoCo the old way:

  * SP_LOAD_PLATFORM_SCORE   -> FACT_PLATFORM_SCORE_DAILY.CREDITS_BILLED_AI   (base V061)
  * SP_ALERT_SCAN_DAILY      -> COST_BUDGET_PACE / COST_FORECAST_BREACH MTD_USD
                               + COST_AI_CREEP filter                         (base V066)
  * SP_REFRESH_EXEC_BOARD    -> MART_EXEC_BOARD IS_AI flag + DRIVER_LABEL      (base V073)
  * SP_ALERT_SCAN            -> COST_SERVERLESS_CREEP AI carve-out            (base V067)

The first three BROADEN the AI positive predicate (+CoCo/CoWork). The fourth is
the COMPLEMENT: COST_SERVERLESS_CREEP (only in the hourly SP_ALERT_SCAN) carves
AI out of its serverless scan with a hardcoded NOT IN (...,'AI_SERVICES') list
that does NOT exclude SNOWFLAKE_COCO_SNOWSIGHT. Without this arm, once
COST_AI_CREEP counts CoCo as AI, CoCo would ALSO still fire COST_SERVERLESS_CREEP
-- the same credits double-alerting with contradictory attribution (adversarial
finding). The carve-out is rewritten to exclude ALL AI via the canonical
not_ai_service_predicate(), so AI and serverless stay mutually exclusive.

RATE/BUCKET-ATTRIBUTION correction only: total CREDITS_BILLED is unchanged; only
the AI/OTHER partition, blended USD, and which single alert a CoCo surge trips
move. Budget-pace alerts soften slightly; AI-creep sharpens (CoCo joins AI).

Derivation law: each proc is extracted from its LATEST base migration and only
the AI classification changes (positive predicate broadened, or the serverless
carve-out rewritten to the canonical not-AI form). No other logic changes. A
re-fill of the two MATERIALIZING loaders re-attributes recent history at apply
time; the two alert scanners are NOT called here (they fire alerts) and
self-correct on their next scheduled run.

Owner applies in Snowsight after V078. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_PLATFORM = MIG / "V061__ai_loader_alert_score_purge_fixes.sql"
BASE_ALERTDAILY = MIG / "V066__alert_escalation_serverless_window_timeline_atomicity.sql"
BASE_EXECBOARD = MIG / "V073__calendar_triage_windows.sql"
BASE_ALERTSCAN = MIG / "V067__alert_attribution_onset_supersede_objectcost.sql"

# Canonical AI predicates, shared with the app so the two layers cannot re-drift.
sys.path.insert(0, str(ROOT))
from app.data.common import ai_service_predicate, not_ai_service_predicate  # noqa: E402

# Narrow loader predicate (byte-identical across the three positive-form procs)
# and the broadened form (== ai_service_predicate()).
OLD = ("(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' "
       "OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')")
NEW = ai_service_predicate()

# COST_SERVERLESS_CREEP's hardcoded AI carve-out (V067) and its canonical
# replacement: exclude warehouse metering AND all AI (incl. CoCo/CoWork).
OLD_CARVE = ("              AND SERVICE_TYPE NOT IN "
             "('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER', 'AI_SERVICES')")
NEW_CARVE = ("              AND SERVICE_TYPE NOT IN "
             "('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')\n"
             "              AND " + not_ai_service_predicate())


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


def broaden(base: Path, name: str, expected: int) -> str:
    """Re-derive a positive-form proc: broaden its AI predicate."""
    proc = extract_proc(base.read_text(encoding="utf-8"), name)
    assert proc.count(OLD) == expected, f"{name}: expected {expected} narrow predicates, got {proc.count(OLD)}"
    proc = proc.replace(OLD, NEW)
    assert proc.count(OLD) == 0, f"{name}: narrow predicate survived the broaden"
    assert proc.count(NEW) == expected, f"{name}: expected {expected} broadened predicates"
    return proc


def broaden_carveout(base: Path, name: str) -> str:
    """Re-derive SP_ALERT_SCAN: rewrite COST_SERVERLESS_CREEP's AI carve-out to
    the canonical not-AI predicate so CoCo (and all AI) is excluded from the
    serverless scan -- keeping it complementary with COST_AI_CREEP."""
    proc = extract_proc(base.read_text(encoding="utf-8"), name)
    assert proc.count(OLD_CARVE) == 1, f"{name}: expected 1 serverless carve-out, got {proc.count(OLD_CARVE)}"
    proc = proc.replace(OLD_CARVE, NEW_CARVE)
    assert proc.count(OLD_CARVE) == 0
    assert "COST_SERVERLESS_CREEP" in proc and not_ai_service_predicate() in proc
    return proc


score = broaden(BASE_PLATFORM, "SP_LOAD_PLATFORM_SCORE", 1)
alert_daily = broaden(BASE_ALERTDAILY, "SP_ALERT_SCAN_DAILY", 9)
exec_board = broaden(BASE_EXECBOARD, "SP_REFRESH_EXEC_BOARD", 2)
alert_scan = broaden_carveout(BASE_ALERTSCAN, "SP_ALERT_SCAN")

# Sanity: each proc kept its identity and its untouched anchors.
assert "AS CREDITS_BILLED_AI" in score
assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY" in score
assert "COST_AI_CREEP" in alert_daily and "COST_BUDGET_PACE" in alert_daily
assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" in alert_daily
assert "SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE" in exec_board
assert "AS IS_AI" in exec_board and "'AI/Cortex: '" in exec_board
assert "'AI_SERVICES'" not in NEW_CARVE  # the exact-name carve-out is gone, replaced by the canonical AI% match

out = f"""-- V079__ai_predicate_coco_historical_split.sql
--
-- Align the LOADER AI classification with the app-side canonical predicate from
-- v4.158.0 (app/data/common), so Cortex Code / CoWork (SNOWFLAKE_COCO_SNOWSIGHT)
-- is attributed to AI (billed at the AI rate) not compute/serverless. Re-derives
-- four procs whose latest definitions still classify CoCo the old way:
--   * SP_LOAD_PLATFORM_SCORE (V061)  -> FACT_PLATFORM_SCORE_DAILY.CREDITS_BILLED_AI
--   * SP_ALERT_SCAN_DAILY    (V066)  -> COST_BUDGET_PACE / COST_FORECAST_BREACH
--                                       MTD_USD + COST_AI_CREEP filter
--   * SP_REFRESH_EXEC_BOARD  (V073)  -> MART_EXEC_BOARD IS_AI + DRIVER_LABEL
--   * SP_ALERT_SCAN          (V067)  -> COST_SERVERLESS_CREEP AI carve-out
--
-- The first three broaden the AI positive predicate (+CoCo/CoWork). The fourth
-- rewrites COST_SERVERLESS_CREEP's hardcoded NOT IN (...,'AI_SERVICES') carve-out
-- to the canonical not-AI predicate so CoCo is EXCLUDED from the serverless scan
-- -- without it, CoCo would double-alert (AI-creep AND serverless-creep) on the
-- same credits. AI and serverless stay mutually exclusive.
--
-- RATE/BUCKET-ATTRIBUTION ONLY: total CREDITS_BILLED unchanged; only the AI/OTHER
-- split, blended USD, and which single alert a CoCo surge trips move.
--
-- Ends with a re-fill of the two MATERIALIZING loaders so recent history is
-- re-attributed at apply time. The two alert SCANNERS (SP_ALERT_SCAN,
-- SP_ALERT_SCAN_DAILY) are NOT called here -- they would fire alerts -- and
-- self-correct on their next scheduled run.
--
-- Owner applies in Snowsight after V078. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20079, 'V079 requires V078 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 78) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_PLATFORM_SCORE  (AI predicate broadened +CoCo/CoWork, from V061)
{score}
-- >>> derived:SP_ALERT_SCAN_DAILY  (AI predicate broadened +CoCo/CoWork, from V066)
{alert_daily}
-- >>> derived:SP_REFRESH_EXEC_BOARD  (AI predicate broadened +CoCo/CoWork, from V073)
{exec_board}
-- >>> derived:SP_ALERT_SCAN  (COST_SERVERLESS_CREEP carve-out -> canonical not-AI, from V067)
{alert_scan}
-- Re-attribute recent history now that the split includes CoCo. Both re-fills
-- are idempotent (MERGE / stage+SWAP); the alert scanners are left to their schedule.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(120);
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 79 AS VERSION,
       'AI predicate historical split: SP_LOAD_PLATFORM_SCORE / SP_ALERT_SCAN_DAILY / SP_REFRESH_EXEC_BOARD broaden the AI service-type predicate to include Cortex Code / CoWork (SNOWFLAKE_COCO_SNOWSIGHT), and SP_ALERT_SCAN rewrites the COST_SERVERLESS_CREEP AI carve-out to the canonical not-AI predicate so CoCo is not double-alerted. Materialized AI/compute splits + alert/score thresholds now price CoCo at the AI rate not compute - matching the v4.158.0 app display. Total credits unchanged; AI/OTHER split + blended USD only. Re-fills score + exec board.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 79);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 4
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20079" in out and "IF (v < 78) THEN" in out
assert "SELECT 79 AS VERSION" in out and "WHERE VERSION = 79)" in out
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(120);") == 1
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") == 1
# Never fire either alert scanner at apply time.
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" not in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in out
assert OLD not in out          # no narrow positive predicate survives
assert OLD_CARVE not in out    # no narrow serverless carve-out survives

target = Path(os.environ.get("V079_OUT") or (MIG / "V079__ai_predicate_coco_historical_split.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
