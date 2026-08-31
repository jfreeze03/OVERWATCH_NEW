#!/usr/bin/env python3
"""Forward-generate V108: COST_CONTRACT_BREACH fires once the contract is already exhausted.

[MED] The [16] COST_CONTRACT_BREACH arm of SP_ALERT_SCAN_DAILY (the def V079 last re-derived)
projects DAYS_LEFT = CEIL((CONTRACT_CREDITS - CONSUMED) / trailing-30-day burn) and only fires when
    p.DAYS_LEFT BETWEEN 0 AND c.THRESHOLD_NUM
As soon as CONSUMED exceeds CONTRACT_CREDITS, (TOTAL - CONSUMED) is negative, DAYS_LEFT is negative,
and it falls outside [0, THRESHOLD_NUM] -- so the alert does NOT fire. CONSUMED only grows, so
DAYS_LEFT stays negative for the rest of the term: the account gets NO contract-breach alert exactly
in the over-contract state, which is the most expensive one (on-demand overage billing at premium
rates). The rule fires as you approach exhaustion, then goes permanently silent the instant you cross
it. This is a pure logic guard, not the ACCOUNT_USAGE-lag class -- and no other scan proc carries an
over-contract alert to cover the gap.

Fix: drop the lower bound so the arm also fires when DAYS_LEFT <= 0 (over-contract):
    AND p.DAYS_LEFT <= c.THRESHOLD_NUM
and emit a distinct EXHAUSTED title/metric in that state ('Contract EXHAUSTED: N credits over ...'),
with an EXHAUSTED token in the dedupe band so the WARN -> CRIT -> EXHAUSTED crossings each re-fire.
An account healthily far from breach has DAYS_LEFT large-positive > THRESHOLD_NUM and still does not
fire; the p.TOTAL > 0 AND p.DAILY_BURN > 0 gates (unconfigured contract / no burn) are unchanged.

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V107; the next daily
SP_ALERT_SCAN_DAILY evaluates the corrected guard. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V079__ai_predicate_coco_historical_split.sql"

OLD_GUARD = "AND p.DAYS_LEFT BETWEEN 0 AND c.THRESHOLD_NUM"
NEW_GUARD = "AND p.DAYS_LEFT <= c.THRESHOLD_NUM"

OLD_TITLE = (
    "               'Contract projected to exhaust in ' || p.DAYS_LEFT || ' day(s) (' ||\n"
    "                   TO_VARCHAR(p.EXHAUST_DATE) || ')',"
)
NEW_TITLE = (
    "               IFF(p.DAYS_LEFT <= 0,\n"
    "                   'Contract EXHAUSTED: ' || ROUND(p.CONSUMED - p.TOTAL, 0) ||\n"
    "                       ' credits over (crossed ' || TO_VARCHAR(p.EXHAUST_DATE) || ', ' ||\n"
    "                       ABS(p.DAYS_LEFT) || ' day(s) ago)',\n"
    "                   'Contract projected to exhaust in ' || p.DAYS_LEFT || ' day(s) (' ||\n"
    "                       TO_VARCHAR(p.EXHAUST_DATE) || ')'),"
)

OLD_DEDUPE = "IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN')"
NEW_DEDUPE = "IFF(p.DAYS_LEFT <= 0, 'EXH', IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN'))"

OLD_COMMENT = "; CRITICAL inside 14 days."
NEW_COMMENT = ("; CRITICAL inside 14 days. Also fires once the contract is already EXHAUSTED "
               "(DAYS_LEFT <= 0, over-contract / on-demand overage) with a distinct EXHAUSTED band "
               "so the WARN -> CRIT -> EXHAUSTED crossings each re-fire (cost-hunt6).")


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN_DAILY\(")

# base sanity: the contract-breach arm is present in its pre-fix shape
assert "COST_CONTRACT_BREACH" in proc
assert proc.count(OLD_GUARD) == 1, f"contract guard: got {proc.count(OLD_GUARD)}"
assert proc.count(OLD_TITLE) == 1, f"contract title: got {proc.count(OLD_TITLE)}"
assert proc.count(OLD_DEDUPE) == 1, f"contract dedupe band: got {proc.count(OLD_DEDUPE)}"
assert proc.count(OLD_COMMENT) == 1, f"contract comment: got {proc.count(OLD_COMMENT)}"

proc = proc.replace(OLD_GUARD, NEW_GUARD)
proc = proc.replace(OLD_TITLE, NEW_TITLE)
proc = proc.replace(OLD_DEDUPE, NEW_DEDUPE)
proc = proc.replace(OLD_COMMENT, NEW_COMMENT)

# post-conditions: exactly the targeted edits
assert NEW_GUARD in proc and "BETWEEN 0 AND c.THRESHOLD_NUM" not in proc
assert "'Contract EXHAUSTED: '" in proc and "ROUND(p.CONSUMED - p.TOTAL, 0)" in proc
assert NEW_DEDUPE in proc
assert "EXHAUSTED (DAYS_LEFT <= 0" in proc
# the approaching-breach messaging survives (now the else-branch of the IFF)
assert "'Contract projected to exhaust in ' || p.DAYS_LEFT" in proc
# the p.TOTAL > 0 / p.DAILY_BURN > 0 gates are untouched
assert "p.TOTAL > 0 AND p.DAILY_BURN > 0" in proc

out = f"""-- V108__cost_contract_breach_fires_when_exhausted.sql
--
-- COST_CONTRACT_BREACH false all-clear. The [16] arm of SP_ALERT_SCAN_DAILY fired only when
-- DAYS_LEFT BETWEEN 0 AND THRESHOLD_NUM, so once CONSUMED crossed CONTRACT_CREDITS the projected
-- DAYS_LEFT went negative and the alert went permanently silent -- exactly in the over-contract
-- state that bills on-demand overage at premium rates. No other scan proc covered the gap.
--
-- Re-derives SP_ALERT_SCAN_DAILY from V079 with the guard relaxed to DAYS_LEFT <= THRESHOLD_NUM so
-- the over-contract state (DAYS_LEFT <= 0) also fires, with a distinct 'Contract EXHAUSTED: N credits
-- over' CRITICAL title/metric and an EXHAUSTED dedupe band so WARN -> CRIT -> EXHAUSTED crossings each
-- re-fire. The p.TOTAL > 0 AND p.DAILY_BURN > 0 gates (unconfigured contract / no burn) and every
-- other arm are byte-identical. No schema change; owner applies in Snowsight after V107 and the next
-- daily SP_ALERT_SCAN_DAILY evaluates the corrected guard. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20108, 'V108 requires V107 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 107) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 108 AS VERSION,
       'COST_CONTRACT_BREACH fires when exhausted: SP_ALERT_SCAN_DAILY re-derived from V079 so the [16] arm guard is DAYS_LEFT <= THRESHOLD_NUM instead of BETWEEN 0 AND THRESHOLD_NUM. Once CONSUMED crossed CONTRACT_CREDITS the projected DAYS_LEFT went negative and the alert went permanently silent in the over-contract (on-demand overage) state; it now fires there too with a distinct Contract EXHAUSTED: N credits over CRITICAL title/metric and an EXHAUSTED dedupe band so WARN -> CRIT -> EXHAUSTED crossings each re-fire. The p.TOTAL > 0 AND p.DAILY_BURN > 0 gates and every other arm are byte-identical. Proc only, no schema change; forward-healing on the next daily SP_ALERT_SCAN_DAILY.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 108);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "AND p.DAYS_LEFT <= c.THRESHOLD_NUM" in out and "BETWEEN 0 AND c.THRESHOLD_NUM" not in out
assert "EXCEPTION (-20108" in out and "IF (v < 107) THEN" in out
assert "SELECT 108 AS VERSION" in out and "WHERE VERSION = 108)" in out

target = Path(os.environ.get("V108_OUT") or (MIG / "V108__cost_contract_breach_fires_when_exhausted.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
