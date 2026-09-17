"""Generate V141 (A1: move the 3 daily-grain cost alerts off the hourly scan onto the daily scan).

The COST_STORAGE_SURGE / COST_SERVERLESS_CREEP / COST_EGRESS_SPIKE arms read daily-grain
ACCOUNT_USAGE data and dedupe per day/week, so running them on the HOURLY scan re-evaluates the
same day 24x for no benefit. This re-derives both procs:
  - SP_ALERT_SCAN     (hourly, from V119): REMOVE arms [12][13][19]; tally 16 -> 13.
  - SP_ALERT_SCAN_DAILY (daily, from V137): INSERT those 3 arms as core arms; tally 6 -> 9.
The moved arm SQL is byte-identical (extracted from V119, dropped into the daily proc); only the
two denominators in each proc's OPS_SCAN_DEGRADED text + RETURN string change. Behavior is
unchanged (same dedupe keys => same alerts), just once/day instead of hourly.

Run: python outputs/gen_v141.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V119 = (MIG / "V119__alert_autoclear_hysteresis_fix.sql").read_text(encoding="utf-8")
V137 = (MIG / "V137__dq_recon_error_alert.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


hourly = extract_proc(V119, "SP_ALERT_SCAN()")
daily = extract_proc(V137, "SP_ALERT_SCAN_DAILY()")

# --- Lift the 3 arm blocks out of the hourly proc (verbatim). ---------------
arm_1213 = hourly[hourly.index("    -- [12] COST_STORAGE_SURGE"):hourly.index("    -- [14] PIPE_COPY_FAILURES")]
arm_19 = hourly[hourly.index("    -- [19] COST_EGRESS_SPIKE"):hourly.index("    -- [20] SEC_NEW_EXPOSURE")]
assert "COST_STORAGE_SURGE" in arm_1213 and "COST_SERVERLESS_CREEP" in arm_1213
assert "COST_EGRESS_SPIKE" in arm_19
assert arm_1213.count("fails := fails + 1") == 2 and arm_19.count("fails := fails + 1") == 1

# --- Hourly: remove the 3 arms, drop the tally 16 -> 13. ---------------------
new_hourly = hourly.replace(arm_1213, "").replace(arm_19, "")
assert new_hourly.count("fails := fails + 1") == hourly.count("fails := fails + 1") - 3
new_hourly = new_hourly.replace(
    "' of 16 alert rule block(s) failed this run'",
    "' of 13 alert rule block(s) failed this run'")
new_hourly = new_hourly.replace(
    "(16 - :fails) || '/16 rule blocks ok'",
    "(13 - :fails) || '/13 rule blocks ok'")
assert "COST_STORAGE_SURGE" not in new_hourly and "COST_EGRESS_SPIKE" not in new_hourly
assert "of 13 alert rule" in new_hourly and "/13 rule blocks ok" in new_hourly

# --- Daily: insert the 3 core arms before the [17] add-on; bump 6 -> 9. ------
_anchor = "    -- [17] PIPE_REF_GAP"
assert daily.count(_anchor) == 1
new_daily = daily.replace(_anchor, arm_1213 + arm_19 + _anchor)
assert new_daily.count("fails := fails + 1") == daily.count("fails := fails + 1") + 3
new_daily = new_daily.replace(
    "' of 6 daily alert rule block(s) failed this run'",
    "' of 9 daily alert rule block(s) failed this run'")
new_daily = new_daily.replace(
    "'alert scan daily v1 (task/login/budget-pace/forecast/AI-creep/contract): ' || (6 - :fails) || '/6 rule blocks ok (daily)'",
    "'alert scan daily v2 (V141: storage-surge/serverless-creep/egress-spike moved off the hourly scan): ' || (9 - :fails) || '/9 rule blocks ok (daily)'")
assert "COST_STORAGE_SURGE" in new_daily and "COST_EGRESS_SPIKE" in new_daily
assert "of 9 daily alert rule" in new_daily and "/9 rule blocks ok (daily)" in new_daily

HEADER = """\
-- V141__alert_cadence_daily_cost_rules.sql
--
-- A1 (owner decision 2026-09-10): move the 3 daily-grain cost alerts off the HOURLY scan onto the
-- DAILY scan. COST_STORAGE_SURGE (day-over-day storage growth), COST_SERVERLESS_CREEP (week-over-
-- week serverless credits) and COST_EGRESS_SPIKE (24h egress vs 14d avg) all read daily-grain
-- ACCOUNT_USAGE and dedupe per day/week, so running them on the hourly scan re-evaluates the same
-- day up to 24x for no added coverage (the dedupe key already collapses them to one alert/day).
--
-- This re-derives both scan procs, moving the three arms verbatim:
--   * SP_ALERT_SCAN (hourly, from V119): the 3 arms REMOVED; core tally 16 -> 13.
--   * SP_ALERT_SCAN_DAILY (daily, from V137): the same 3 arms ADDED as core arms before the [17]
--     external-dependency add-ons; core tally 6 -> 9.
-- The arm SQL is byte-identical (lifted from V119); only the two OPS_SCAN_DEGRADED denominators and
-- each RETURN string change. Same dedupe keys => the same alerts fire, just once/day not hourly.
-- Behaviour note: COST_EGRESS_SPIKE uses a rolling trailing-24h window; on the daily scan an
-- intraday egress spike is detected at the next daily run rather than the next hour (accepted with
-- the "all three to daily" decision). Proc-only; no schema/rule/task change. Apply AFTER V140.
-- Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20141, 'V141 requires V140 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 140) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN (from V119, minus the 3 daily-grain cost arms, V141)
"""

MIDDLE = "\n\n-- >>> derived:SP_ALERT_SCAN_DAILY (from V137, plus the 3 daily-grain cost arms, V141)\n"

SCHEMA_INSERT = """\

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 141 AS VERSION,
       'A1 alert cadence: moved COST_STORAGE_SURGE / COST_SERVERLESS_CREEP / COST_EGRESS_SPIKE (all daily-grain, dedupe per day/week) off the HOURLY SP_ALERT_SCAN onto the DAILY SP_ALERT_SCAN_DAILY so they no longer re-evaluate the same day up to 24x. Arm SQL lifted verbatim from V119; hourly core tally 16->13, daily core tally 6->9; only the OPS_SCAN_DEGRADED denominators and RETURN strings change. Same dedupe keys, same alerts, once/day. Proc-only, no schema/rule/task change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 141);
"""

out = HEADER + new_hourly + MIDDLE + new_daily + SCHEMA_INSERT
(MIG / "V141__alert_cadence_daily_cost_rules.sql").write_text(out, encoding="utf-8")
print("wrote V141 | hourly arms:", new_hourly.count("fails := fails + 1"),
      "| daily arms:", new_daily.count("fails := fails + 1"))
