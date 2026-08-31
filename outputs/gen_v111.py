#!/usr/bin/env python3
"""Forward-generate V111: COST_BUDGET_PACE completed-days pace window (alerting hunt finding D).

[LOW] The [08] COST_BUDGET_PACE arm of SP_ALERT_SCAN_DAILY (latest def = V108) computes the
elapsed-share allowance with DAY_OF_MONTH = DAY(CURRENT_DATE()) -- today counted as a FULLY elapsed
day -- while MTD_USD covers only ~(D-1) completed days (today's row is partial or, given ACCOUNT_USAGE
latency at daily-scan time, absent). Numerator over ~(D-1) days vs a D-day allowance understates the
pace ratio by ~1/D, so a genuine early-month overspend can stay silent -- the account-level sibling of
the COST_DEPT_BUDGET_PACE partial-today bias V107 fixed for departments.

Fix: base the elapsed-share allowance on COMPLETED days -- (DAY(CURRENT_DATE()) - 1) / DAYS_IN_MONTH --
in the TITLE ratio, DETAIL allowance, and the fire test, and guard day-1 (DAY_OF_MONTH > 1) so the
allowance is never 0. MTD_USD is deliberately left month-to-date (today included once) because the
[09] COST_FORECAST_BREACH arm uses it as the projection base; only the pace DENOMINATOR changes, which
also makes the fix conservative (never suppresses a real breach).

Procedure re-derivation only, no schema change. Owner applies in Snowsight after V110; the next daily
SP_ALERT_SCAN_DAILY evaluates the corrected pace window. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V108__cost_contract_breach_fires_when_exhausted.sql"

OLD_SHARE = "m.DAY_OF_MONTH / m.DAYS_IN_MONTH"
NEW_SHARE = "(m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH"

OLD_FIRE = "AND m.MTD_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM"
NEW_FIRE = ("AND m.DAY_OF_MONTH > 1\n"
            "         AND m.MTD_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH * c.THRESHOLD_NUM")


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN_DAILY\(")

# the pace-share expression appears exactly 3x (TITLE ratio, DETAIL allowance, fire test), all in
# the [08] arm; the [09] forecast arm uses (DAYS_IN_MONTH - DAY_OF_MONTH) which is a different string.
assert proc.count(OLD_SHARE) == 3, f"pace-share expr: got {proc.count(OLD_SHARE)}"
proc = proc.replace(OLD_SHARE, NEW_SHARE)
# now add the day-1 guard to the (transformed) fire test
assert proc.count(OLD_FIRE) == 1, f"fire test: got {proc.count(OLD_FIRE)}"
proc = proc.replace(OLD_FIRE, NEW_FIRE)

# post-conditions
assert proc.count(NEW_SHARE) == 3
assert "m.DAY_OF_MONTH > 1\n         AND m.MTD_USD >" in proc
assert " m.DAY_OF_MONTH / m.DAYS_IN_MONTH" not in proc  # no stray full-day share left
# the forecast arm's remaining-days math is untouched
assert "(m.DAYS_IN_MONTH - m.DAY_OF_MONTH)" in proc
assert "COST_BUDGET_PACE" in proc and "COST_CONTRACT_BREACH" in proc  # both daily arms survive

out = f"""-- V111__cost_budget_pace_completed_days.sql
--
-- COST_BUDGET_PACE completed-days pace window (alerting hunt finding D). The [08] arm of
-- SP_ALERT_SCAN_DAILY compared MTD_USD (~D-1 completed days) to an elapsed-share allowance that
-- counted today as a fully elapsed day (DAY_OF_MONTH / DAYS_IN_MONTH), understating the pace ratio by
-- ~1/D and letting a genuine early-month overspend stay silent -- the account-level sibling of the
-- COST_DEPT_BUDGET_PACE partial-today bias V107 fixed for departments.
--
-- Re-derives SP_ALERT_SCAN_DAILY from V108 with the allowance on COMPLETED days
-- ((DAY_OF_MONTH - 1) / DAYS_IN_MONTH) in the TITLE ratio, DETAIL, and fire test, plus a DAY_OF_MONTH
-- > 1 day-1 guard. MTD_USD stays month-to-date (the [09] forecast arm uses it as the projection base);
-- only the pace denominator changes, which also makes the fix conservative. Everything else
-- byte-identical. No schema change; owner applies after V110 and the next daily SP_ALERT_SCAN_DAILY
-- evaluates the corrected window. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20111, 'V111 requires V110 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 110) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 111 AS VERSION,
       'COST_BUDGET_PACE completed-days pace window: SP_ALERT_SCAN_DAILY re-derived from V108 so the [08] arm elapsed-share allowance uses (DAY_OF_MONTH - 1) / DAYS_IN_MONTH (completed days) with a DAY_OF_MONTH > 1 day-1 guard, instead of counting today as a fully elapsed day while MTD_USD covers only completed days. The account budget-pace alert no longer under-fires early in the month (the account-level sibling of the V107 dept-pace fix). MTD_USD stays month-to-date for the forecast arm. Everything else byte-identical. Proc only, no schema change; forward-healing on the next daily SP_ALERT_SCAN_DAILY.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 111);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20111" in out and "IF (v < 110) THEN" in out
assert "SELECT 111 AS VERSION" in out and "WHERE VERSION = 111)" in out

target = Path(os.environ.get("V111_OUT") or (MIG / "V111__cost_budget_pace_completed_days.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
