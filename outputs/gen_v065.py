#!/usr/bin/env python3
r"""Forward-generate V065__alert_run_rate_windows.sql.

Bug round 5 (2026-07-31): two RUN-RATE WINDOW fixes in SP_ALERT_SCAN_DAILY. Both
are pre-existing (NOT V064 regressions) and both are proc-body edits, so under the
derivation law they ship as an owner migration -- never a hand-edit of V064.

  rank2 (MEDIUM)  COST_FORECAST_BREACH -- DAILY_RATE_USD was
        MTD$ / DAY(CURRENT_DATE()): month-to-date dollars (which INCLUDE today's
        PARTIAL day) divided by the full day-of-month count understates the daily
        run-rate -> understates the month-end projection -> can SUPPRESS the budget
        breach alert. Now a COMPLETE-days-only rate:
        SUM(billed WHERE DAY < today) / COUNT(DISTINCT complete DAY).
        MTD_USD stays the additive base (today's partial counts once, as itself);
        only the projected remainder uses the complete-day rate. On day 1 of the
        month there is no complete day -> rate NULL -> no forecast alert that day,
        which is safer than projecting off a single partial day.
  rank3 (LOW)     COST_AI_CREEP -- THIS_WK spanned 8 days (DAY >= today-7 INCLUDES
        today) while PRIOR_WK spanned 7 -> inflated week-over-week AI growth. The
        scan window now excludes today (AND DAY < CURRENT_DATE()), so THIS_WK
        (DAY >= today-7) and PRIOR_WK (DAY < today-7) are equal, today-excluded
        7-complete-day windows.

Derivation law (see gen_v061..64): SP_ALERT_SCAN_DAILY is re-derived from its LATEST
def (V064) via extract_proc + count-asserted needle edits. tests/test_v065_*.py
byte-compares (regenerate to a temp file via V065_OUT). Idempotent; apply AFTER V064.
No new objects; no app RUNTIME change (the on-screen forecast was already fixed in
task #29 -- this repairs the automated alert backstop).
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def _apply(body: str, old: str, new: str, count: int, name: str) -> str:
    n = body.count(old)
    assert n == count, f"{name}: needle x{n} (want {count}): {old[:70]!r}"
    return body.replace(old, new)


def derive(name: str) -> str:
    base, edits = EDITS[name]
    body = extract_proc(base, name)
    for old, new, cnt in edits:
        body = _apply(body, old, new, cnt, name)
    for g in GUARDS.get(name, []):
        assert g in body, f"{name}: guardrail vanished: {g[:70]!r}"
    return body


# The canonical AI predicate, spelled EXACTLY as V064 spells it (kept as one string so
# the needles below cannot drift from the proc's own wording).
_AI = "(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')"

EDITS = {
    'SP_ALERT_SCAN_DAILY': ('V064__webhook_drain_watermarks_alert_burn_telemetry.sql', [
        # ---- rank2: complete-days-only run-rate. The identical DAILY_RATE_USD sits in
        # BOTH the [08] COST_BUDGET_PACE and [09] COST_FORECAST_BREACH mtd CTEs (byte-for-
        # byte the same), so this needle matches x2 and both are fixed. [09] projects with
        # it (the real bug); [08] computes it but does not reference it (a dead column --
        # the pace alert compares MTD_USD to the elapsed-share allowance), so fixing [08]
        # has no runtime effect yet keeps the two blocks consistent. The leading '(' + the
        # '/ NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD' tail keep the needle off the
        # MTD_USD base copy above it (which ends '... AS MTD_USD,' and stays untouched).
        (f"""            (SUM(CASE WHEN {_AI} THEN 0 ELSE CREDITS_BILLED END) * :credit_price
              + SUM(CASE WHEN {_AI} THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD""",
         f"""            -- V065 rank2: run-rate over COMPLETE days only (DAY < today). MTD_USD above is
            -- the month-to-date base (today's partial included, once); dividing it by the
            -- full day-of-month understated the daily rate -> under-projected the month-end
            -- forecast (COST_FORECAST_BREACH) -> could suppress the breach. Day 1 has no
            -- complete day -> NULLIF -> NULL rate -> no forecast alert that day.
            (SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT {_AI} THEN CREDITS_BILLED ELSE 0 END) * :credit_price
              + SUM(CASE WHEN DAY < CURRENT_DATE() AND {_AI} THEN CREDITS_BILLED ELSE 0 END) * :ai_credit_price)
                / NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD""", 2),
        # ---- rank3: COST_AI_CREEP -- exclude today so both weeks are 7 complete days --
        # The -14d window over FACT_METERING_DAILY filtered to the AI predicate is unique
        # to the AI-creep block (forecast uses DATE_TRUNC('month'), contract-breach -30..-1).
        (f"""            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
            WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())
              AND {_AI}""",
         f"""            FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
            WHERE DAY >= DATEADD('day', -14, CURRENT_DATE())
              AND DAY < CURRENT_DATE()   -- V065 rank3: exclude today so THIS_WK and PRIOR_WK are equal 7 complete days
              AND {_AI}""", 1),
    ]),
}

GUARDS = {
    'SP_ALERT_SCAN_DAILY': [
        "COST_FORECAST_BREACH",
        "COST_AI_CREEP",
        "COST_CONTRACT_BREACH",
        # V064 rec20-alert contract-breach burn must survive untouched
        "NULLIF(COUNT(DISTINCT DAY), 0)",
        "AND DATEADD('day', -1, CURRENT_DATE())) AS DAILY_BURN",
    ],
}

alertscan = derive("SP_ALERT_SCAN_DAILY")

# ---- correctness assertions on the generated proc ----
# rank2: old partial-day divisor gone from BOTH blocks; new complete-day rate in both
assert "/ NULLIF(DAY(CURRENT_DATE()), 0) AS DAILY_RATE_USD" not in alertscan, "rank2 partial-day divisor gone"
assert alertscan.count("NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD") == 2, "rank2 complete-day divisor (both blocks)"
assert alertscan.count("SUM(CASE WHEN DAY < CURRENT_DATE() AND NOT (SERVICE_TYPE") == 2, "rank2 complete-day non-AI base (both blocks)"
assert alertscan.count("SUM(CASE WHEN DAY < CURRENT_DATE() AND (SERVICE_TYPE") == 2, "rank2 complete-day AI base (both blocks)"
# MTD_USD base is untouched in both blocks (still sums the whole month-to-date incl today)
assert alertscan.count("* :ai_credit_price AS MTD_USD,") == 2, "rank2 MTD_USD base preserved as the additive anchor"
# rank3: both weeks now today-excluded and equal length
assert alertscan.count("AND DAY < CURRENT_DATE()   -- V065 rank3") == 1, "rank3 today excluded from the AI-creep scan"
assert "IFF(DAY >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS THIS_WK_CR" in alertscan, "rank3 THIS_WK boundary kept (now 7 complete days under the today-excluded window)"
assert "IFF(DAY <  DATEADD('day', -7, CURRENT_DATE()), CREDITS_BILLED, 0)) AS PRIOR_WK_CR" in alertscan, "rank3 PRIOR_WK boundary kept"
# nothing else in the proc changed shape
assert alertscan.count("CREATE OR REPLACE PROCEDURE") == 1

out = f"""-- V065__alert_run_rate_windows.sql
--
-- Bug round 5 (2026-07-31): two alert RUN-RATE WINDOW fixes in SP_ALERT_SCAN_DAILY.
-- Both are pre-existing (NOT V064 regressions); re-derived from V064's
-- SP_ALERT_SCAN_DAILY (the latest def) via outputs/gen_v065.py + count-asserted
-- needle edits. Idempotent; apply AFTER V064.
--
--   rank2 (MEDIUM)  COST_FORECAST_BREACH: DAILY_RATE_USD was MTD$ / DAY(CURRENT_DATE())
--                   -- month-to-date dollars (incl. today's PARTIAL day) over the full
--                   day-of-month understated the run-rate -> under-projected month-end ->
--                   could SUPPRESS the budget-breach alert. Now a COMPLETE-days-only rate:
--                   SUM(billed WHERE DAY < today) / COUNT(DISTINCT complete DAY). MTD_USD
--                   stays the additive base; only the projected remainder uses the rate.
--                   Day 1 has no complete day -> NULL rate -> no forecast alert that day.
--   rank3 (LOW)     COST_AI_CREEP: THIS_WK spanned 8 days (DAY >= today-7 INCLUDES today)
--                   vs PRIOR_WK 7 -> inflated week-over-week AI growth. The scan window now
--                   excludes today, so both weeks are equal 7-complete-day windows.
--
-- No new objects; no app RUNTIME change (the on-screen forecast was already fixed in
-- task #29). Deterministic alert logic -- byte-verified by tests/test_v065_alert_windows.py,
-- no owner smoke test required.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20065, 'V065 requires V064 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 64) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN_DAILY  (rank2 complete-day forecast rate + rank3 today-excluded AI-creep windows)
{alertscan}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 65 AS VERSION,
       'Alert run-rate window fixes (bug round 5): COST_FORECAST_BREACH projects from a COMPLETE-days-only run-rate (was MTD$/day-of-month over a partial today -> under-projected -> suppressed the budget-breach alert; rank2) and COST_AI_CREEP compares equal today-excluded 7-complete-day windows (was THIS_WK 8d incl today vs PRIOR_WK 7d -> inflated WoW; rank3). Re-derived from V064 SP_ALERT_SCAN_DAILY; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 65);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1, "one proc re-defined"
assert "CREATE TABLE" not in out and "CREATE TASK" not in out, "no new objects"
assert "EXCEPTION (-20065" in out and "IF (v < 64) THEN" in out, "version guard"

target = Path(os.environ.get("V065_OUT") or (MIG / "V065__alert_run_rate_windows.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
