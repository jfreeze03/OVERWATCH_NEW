#!/usr/bin/env python3
"""Forward-generate V153: settle autobooked savings on the FULL 14-day window (Next-Fifty #11 + #5 adopt).

SP_WAREHOUSE_CHANGE_SCAN (V109) keeps refreshing a change's AFTER_* stats and VERDICT daily while
CURRENT_DATE() <= TRACKING_UNTIL, but a row leaves PENDING as soon as AFTER_DAYS >= 3 AND
AFTER_QUERIES >= 20 -- and SP_LEDGER_AUTOBOOK (V145) settled any non-PENDING row. So VERIFIED_USD froze
on ~3 days of after-data; NO_BASELINE rows settled the day they were booked with a NULL after-rate
COALESCEd to 0 (the WHOLE baseline booked as VERIFIED); and the credit rate was read with TRY_TO_NUMBER
into a NUMBER (scale 0), so the seeded '3.68' priced as 4 (+8.7% on every autobook VERIFIED_USD).

DERIVATION BASES (the CURRENT definers -- round-13 class):
  * SP_LEDGER_AUTOBOOK = V145 (lineage V038 -> V118 -> V145; V145 carries V118's LBA-1 dedup and V038's
    book/settle core, so nothing intervening is lost -- the migration test normalizes back to V145).
  * SP_VERIFY_IDLE_SAVINGS = V053 (V007 original).

Deltas vs V145 (each anchor asserts count == 1; the test reverses every one back to V145 byte-for-byte):
  D1  rate FLOAT via TRY_TO_DOUBLE.
  D2  ADOPT UPDATE before the INSERT: a manual ESTIMATED row for the same change (the app twin rule,
      mart_sql._ledger_twin_select -- same ON/WHERE lines) is stamped with SOURCE_CHANGE_ID instead of the
      INSERT booking a second row. 1:1 only (double QUALIFY), unbooked saving-direction changes only.
  D3  (MIN_CLUSTERS arm) deliberately NOT here -- owner decision O-3 (wave 3; if ever taken, a separate
      top-level INSERT after the V145 INSERT, never an OR-nested correlated EXISTS).
  D4  settle gate: the 5 closed verdicts AND CURRENT_DATE() > r.TRACKING_UNTIL AND
      r.AFTER_CREDITS_PER_DAY IS NOT NULL (decision O-2: NO_BASELINE / INSUFFICIENT_AFTER settle on credits).
  D5  settle NOTE: full-window wording + per-day query-volume ratio (VOLUME_CONFOUNDED outside 0.7-1.3x),
      PERF_REGRESSED (decision O-4), PERF_UNJUDGED -- dollars / STATE / VERIFIED_* never move with volume.
  D6  close-out: closed-window NO_BASELINE / INSUFFICIENT_AFTER with NOTHING metered after the change
      (AFTER_CREDITS_PER_DAY IS NULL) closes REJECTED with no dollars.
  V053: +3 lines (comment x2 + AND SOURCE_CHANGE_ID IS NULL) in the verifier's items CTE.

Writes to $V153_OUT when set (the regen test), else the migration path. This file never runs from the app.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_AUTOBOOK = MIG / "V145__ledger_autobook_stamp_finding_type.sql"
BASE_VERIFIER = MIG / "V053__action_layer_remediation_verify.sql"


def extract_procedure(text: str, name: str) -> str:
    """CREATE OR REPLACE PROCEDURE ... through its closing '$$;' (no trailing newline)."""
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}(")
    body_open = text.index("$$", start)
    return text[start:text.index("$$;", body_open + 2) + 3]


def once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"expected exactly 1 occurrence of: {old[:80]!r} (got {text.count(old)})"
    return text.replace(old, new, 1)


p145 = extract_procedure(BASE_AUTOBOOK.read_text(encoding="utf-8"), "SP_LEDGER_AUTOBOOK")
p053 = extract_procedure(BASE_VERIFIER.read_text(encoding="utf-8"), "SP_VERIFY_IDLE_SAVINGS")

# ---- D1: FLOAT rate --------------------------------------------------------------------------
D1_OLD = ("    rate NUMBER;\nBEGIN\n"
          "    SELECT COALESCE(TRY_TO_NUMBER(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)")
D1_NEW = ("    rate FLOAT;\nBEGIN\n"
          "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)")

# ---- the V145 INSERT direction block, carried VERBATIM into ADOPT --------------------------------
DIR_START = "            (r.SETTING = 'AUTO_SUSPEND'"
DIR_END = "WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 0 END)\n"
_ds = p145.index(DIR_START)
DIR145 = p145[_ds:p145.index(DIR_END, _ds) + len(DIR_END)]
assert p145.count(DIR145) == 1 and "MIN_CLUSTERS" not in DIR145

# ---- D2: ADOPT, inserted before the V145 INSERT comment ------------------------------------------
INSERT_ANCHOR = "    -- Book detected cost-lever changes as ESTIMATED $0. V145: also stamp FINDING_TYPE from the\n"
ADOPT = (
    "    -- V153 ADOPT (Next-Fifty #5 follow-up): an app-booked manual ESTIMATED row for the SAME change is\n"
    "    -- stamped with the change's SOURCE_CHANGE_ID instead of the INSERT below booking a second row. Same\n"
    "    -- match as the app twin rule (mart_sql._ledger_twin_select): same warehouse, same lever (RESIZE ==\n"
    "    -- SIZE), the scan saw it from 1h before to 3 days after the row was booked. One-to-one only (first\n"
    "    -- change per row AND first row per change), only unbooked saving-direction changes; anything else\n"
    "    -- falls through to the INSERT + the app twin rule exactly as before. The adopted row keeps its app\n"
    "    -- ESTIMATED_USD and settles below like any autobook row.\n"
    "    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l\n"
    "       SET SOURCE_CHANGE_ID = a.CHANGE_ID,\n"
    "           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | adopted by the daily change scan (change '\n"
    "                        || a.CHANGE_ID || '): settles on its 14-day measured window.', 2000)\n"
    "      FROM (SELECT m.ITEM_ID, r.CHANGE_ID\n"
    "              FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER m\n"
    "              JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r\n"
    "                ON UPPER(r.WAREHOUSE_NAME) = UPPER(TRIM(m.TARGET_OBJECT))\n"
    "               AND r.SETTING = IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE', UPPER(TRIM(m.FINDING_TYPE)))\n"
    "               AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ >= DATEADD('hour', -1, m.CREATED_AT)\n"
    "               AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ < DATEADD('day', 3, m.CREATED_AT)\n"
    "             WHERE m.SOURCE_CHANGE_ID IS NULL\n"
    "               AND m.STATE = 'ESTIMATED'\n"
    "               AND UPPER(TRIM(m.FINDING_TYPE)) IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')\n"
    "               AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER b\n"
    "                               WHERE b.SOURCE_CHANGE_ID = r.CHANGE_ID)\n"
    "               AND (\n"
    + DIR145
    + "               )\n"
    "            QUALIFY ROW_NUMBER() OVER (PARTITION BY m.ITEM_ID ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1\n"
    "                AND ROW_NUMBER() OVER (PARTITION BY r.CHANGE_ID ORDER BY m.CREATED_AT, m.ITEM_ID) = 1) a\n"
    "     WHERE l.ITEM_ID = a.ITEM_ID\n"
    "       AND l.SOURCE_CHANGE_ID IS NULL;\n"
    "\n")

# ---- D4: settle gate (the scan's own closed-window predicate, 5 closed verdicts, metered after) ----
D4_OLD = "             WHERE r.VERDICT <> 'PENDING'\n               -- Rank ONLY changes"
D4_NEW = ("             WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')\n"
          "               AND CURRENT_DATE() > r.TRACKING_UNTIL\n"
          "               AND r.AFTER_CREDITS_PER_DAY IS NOT NULL\n"
          "               -- Rank ONLY changes")

# ---- D5a: settle comment ----------------------------------------------------------------------
D5A_OLD = ("    -- VERIFIED at $0 so the physical saving is booked exactly once.\n"
           "    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l\n"
           "       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),")
D5A_NEW = ("    -- VERIFIED at $0 so the physical saving is booked exactly once.\n"
           "    -- V153: settle ONLY once the change's 14-day after-window has CLOSED (CURRENT_DATE() >\n"
           "    -- TRACKING_UNTIL is the scan's own close-out predicate: it stops refreshing AFTER_* and VERDICT\n"
           "    -- then) and only when credits were metered after the change; the V145 gate settled on ~3 days\n"
           "    -- of after-data. NO_BASELINE / INSUFFICIENT_AFTER (fewer than 20 queries in a window) settle on\n"
           "    -- CREDITS too, marked PERF_UNJUDGED. The NOTE carries the per-day query-volume ratio (after vs the\n"
           "    -- 14-day baseline), VOLUME_CONFOUNDED outside 0.7-1.3x. Dollars are never adjusted for volume.\n"
           "    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l\n"
           "       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),")

# ---- D5b: settle NOTES (every nullable operand COALESCEd; the V118 LBA-1 tail follows untouched) ----
D5B_OLD = ("           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured '\n"
           "                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' -> '\n"
           "                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))\n"
           "                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))\n"
           "                        || 'd (' || s.VERDICT || '); floor $5/mo.'\n")
D5B_NEW = ("           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured on the full window: '\n"
           "                        || TO_VARCHAR(ROUND(COALESCE(s.BASE, 0), 2)) || ' credits/day (14d baseline) -> '\n"
           "                        || TO_VARCHAR(ROUND(COALESCE(s.AFT, 0), 2))\n"
           "                        || ' credits/day over ' || TO_VARCHAR(COALESCE(s.AFTER_DAYS, 0))\n"
           "                        || 'd after (' || s.VERDICT || '); floor $5/mo.'\n"
           "                        || ' | volume ' || COALESCE(TO_VARCHAR(ROUND(s.VOL_RATIO, 2)), '?')\n"
           "                        || 'x baseline queries/day'\n"
           "                        || IFF(s.VOL_RATIO < 0.7 OR s.VOL_RATIO > 1.3,\n"
           "                               ' - VOLUME_CONFOUNDED: the workload itself moved, so part of the credit delta may not be the lever (dollars not adjusted).',\n"
           "                               '.')\n"
           "                        || IFF(s.VERDICT = 'REGRESSED' AND s.WH_SAVED_MONTHLY_USD >= 5,\n"
           "                               ' | PERF_REGRESSED: credits fell but latency, queueing or failures got worse (WH_CHANGE_REGRESSION).', '')\n"
           "                        || IFF(s.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER'),\n"
           "                               ' | PERF_UNJUDGED: fewer than 20 queries in a window, so latency/queue/failure axes were not judged.', '')\n")

# ---- D5c: VOL_RATIO select column ----------------------------------------------------------------
D5C_OLD = ("                   r.AFTER_CREDITS_PER_DAY AS AFT,\n"
           "                   ROW_NUMBER() OVER (")
D5C_NEW = ("                   r.AFTER_CREDITS_PER_DAY AS AFT,\n"
           "                   -- V153: per-day query volume after the change vs the 14-day baseline\n"
           "                   -- (BASELINE_QUERIES is a 14-day total, AFTER_QUERIES a total over AFTER_DAYS).\n"
           "                   (r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))\n"
           "                       / NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO,\n"
           "                   ROW_NUMBER() OVER (")

# ---- D6: close-out -- REJECT only when nothing was metered after the change ----------------------
D6_OLD = "       AND l.STATE = 'ESTIMATED';\n\n    RETURN 'OK';"
CLOSEOUT = (
    "       AND l.STATE = 'ESTIMATED';\n\n"
    "    -- V153 close-out: a closed-window NO_BASELINE / INSUFFICIENT_AFTER change with NO metered credits\n"
    "    -- after it (AFTER_CREDITS_PER_DAY NULL: the warehouse never ran after the change) has nothing to\n"
    "    -- measure, so it closes REJECTED with no dollars. The V145 gate settled these through the credit\n"
    "    -- formula with the NULL after-rate COALESCEd to 0, i.e. booked the WHOLE baseline as VERIFIED.\n"
    "    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l\n"
    "       SET STATE = 'REJECTED',\n"
    "           VERIFIED_AT = CURRENT_TIMESTAMP(),\n"
    "           VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK',\n"
    "           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | not measurable (' || r.VERDICT\n"
    "                        || '): no metered credits after the change, so no saving is booked.', 2000)\n"
    "      FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY r\n"
    "     WHERE l.SOURCE_CHANGE_ID = r.CHANGE_ID\n"
    "       AND l.STATE = 'ESTIMATED'\n"
    "       AND r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')\n"
    "       AND CURRENT_DATE() > r.TRACKING_UNTIL\n"
    "       AND r.AFTER_CREDITS_PER_DAY IS NULL;\n\n"
    "    RETURN 'OK';")

p153 = p145
p153 = once(p153, D1_OLD, D1_NEW)
p153 = once(p153, INSERT_ANCHOR, ADOPT + INSERT_ANCHOR)
p153 = once(p153, D4_OLD, D4_NEW)
p153 = once(p153, D5A_OLD, D5A_NEW)
p153 = once(p153, D5B_OLD, D5B_NEW)
p153 = once(p153, D5C_OLD, D5C_NEW)
p153 = once(p153, D6_OLD, CLOSEOUT)

# ---- SP_VERIFY_IDLE_SAVINGS (V053): +3 lines ------------------------------------------------------
V_OLD = ("          AND (FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %')\n    ),")
V_NEW = ("          AND (FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %')\n"
         "          -- V153: a row tied to a detected change (autobook or adopted) settles on the change\n"
         "          -- scan's measured window -- an idle-spend proposal for it is noise, or a second verify.\n"
         "          AND SOURCE_CHANGE_ID IS NULL\n    ),")
pv153 = once(p053, V_OLD, V_NEW)

# ---- self-checks: every delta reverses to the exact base (the migration test does the same) --------
_norm = p153
for _new, _old in ((CLOSEOUT, D6_OLD), (D5C_NEW, D5C_OLD), (D5B_NEW, D5B_OLD), (D5A_NEW, D5A_OLD),
                   (D4_NEW, D4_OLD), (ADOPT, ""), (D1_NEW, D1_OLD)):
    assert _norm.count(_new) == 1, _new[:60]
    _norm = _norm.replace(_new, _old)
assert _norm == p145, "V153 SP_LEDGER_AUTOBOOK does not normalize back to V145"
assert pv153.replace(V_NEW, V_OLD) == p053, "V153 SP_VERIFY_IDLE_SAVINGS does not normalize back to V053"
assert p153.count(DIR145) == 2                  # the V145 INSERT's filter + its verbatim copy in ADOPT
assert "MIN_CLUSTERS" not in p153               # O-3: no MIN arm
assert "<> 'PENDING'" not in p153
assert "TRY_TO_NUMBER(MAX(IFF(KEY = 'CREDIT_PRICE_USD'" not in p153

HEADER = """\
-- V153__ledger_autobook_full_window_settle.sql -- settle autobooked savings on the FULL 14-day window.
--
--   Next-Fifty #11 (+ the #5 adopt follow-up). SP_WAREHOUSE_CHANGE_SCAN (V109) refreshes a change's
--   AFTER_* stats and VERDICT daily while CURRENT_DATE() <= TRACKING_UNTIL (detection day + 14), and a
--   row leaves PENDING as soon as AFTER_DAYS >= 3 AND AFTER_QUERIES >= 20. SP_LEDGER_AUTOBOOK (V145)
--   settled any non-PENDING row, so VERIFIED_USD froze on ~3 days of after-data and was never
--   re-measured; NO_BASELINE rows settled the SAME day they were booked (no after-rate yet, COALESCEd
--   to 0 = the whole baseline booked as saved). The credit rate was also read into a NUMBER (scale 0)
--   via TRY_TO_NUMBER, so the seeded '3.68' priced as 4 (+8.7% on every autobook VERIFIED_USD).
--
--   SP_LEDGER_AUTOBOOK is re-derived from V145, its current definition (V145 already carries V118's
--   LBA-1 dedup and V038's book/settle core -- preserved byte-for-byte). Deltas, each locked by the
--   normalize-and-compare test tests/migrations/test_v153_ledger_autobook_full_window_settle.py:
--     1. rate FLOAT via TRY_TO_DOUBLE (every other proc's idiom).
--     2. ADOPT: an app-booked manual ESTIMATED row for the same change (the Next-Fifty #5 twin rule,
--        same ON/WHERE lines as mart_sql._ledger_twin_select) gets SOURCE_CHANGE_ID stamped instead of
--        the scan booking a second row for the change. One-to-one; unbooked saving-direction only.
--     3. Settle gate: CURRENT_DATE() > TRACKING_UNTIL AND AFTER_CREDITS_PER_DAY IS NOT NULL AND VERDICT
--        IN (IMPROVED, NEUTRAL, REGRESSED, NO_BASELINE, INSUFFICIENT_AFTER). NO_BASELINE /
--        INSUFFICIENT_AFTER mean fewer than 20 queries in a window, not missing credits -- they settle
--        on the measured credits too (a low-query warehouse is the usual auto-suspend target).
--     4. Settle NOTE: full-window wording + per-day query-volume ratio, VOLUME_CONFOUNDED outside
--        0.7-1.3x (dollars NOT adjusted), PERF_REGRESSED on a REGRESSED verdict that still saved, and
--        PERF_UNJUDGED on NO_BASELINE / INSUFFICIENT_AFTER.
--     5. Close-out: a closed-window NO_BASELINE / INSUFFICIENT_AFTER change with nothing metered after
--        it (AFTER_CREDITS_PER_DAY NULL) closes REJECTED with no dollars.
--   No MIN_CLUSTERS arm: that lever stays registry-only (Next-Fifty #38 is wave 3).
--   Timing: a change now settles 15-16 days after it is made (the morning after its 14-day window
--   closes), not ~3; "verified this quarter" / the active run-rate move accordingly.
--   SP_VERIFY_IDLE_SAVINGS is re-derived from V053 to skip rows tied to a detected change.
--   Forward-only: already-settled rows are never rewritten here (re-settling them is a separate owner
--   opt-in, RESETTLE_AUTOBOOK_14D.sql on the runbox). No schema change, no task change, teardown
--   unchanged. Apply AFTER V152. Idempotent.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20153, 'V153 requires V152 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 152) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

TAIL = """\
-- Run once at apply (the same work TASK_LEDGER_AUTOBOOK does after the 06:40 America/Chicago change
-- scan). This is NOT a no-op: it can ADOPT a manual ESTIMATED row booked in the last 3 days whose change
-- the scan has already seen but not yet booked. Nothing is settle-eligible yet (the V145 gate already
-- settled every non-PENDING row, and a closed window is never PENDING). Run it under the
-- RUN_NEXT prelude ALTER SESSION SET TIMEZONE = 'America/Chicago' so CURRENT_DATE() and ADOPT's
-- LTZ -> NTZ cast use the task clock, not a UTC worksheet's. If it errors, SCHEMA_VERSION 153 is not
-- written; ADOPT stamps may already be committed (harmless under V145) -- roll back by re-running the
-- V145 and V053 CREATE OR REPLACE blocks.
CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 153 AS VERSION,
       'SP_LEDGER_AUTOBOOK settles on the FULL 14-day window (Next-Fifty #11): re-derived from V145 (V118 LBA-1 dedup + V038 core preserved byte-for-byte) with settle gate CURRENT_DATE() > TRACKING_UNTIL AND AFTER_CREDITS_PER_DAY IS NOT NULL over the 5 closed verdicts, so a change settles 15-16 days after it is made instead of ~3; NO_BASELINE / INSUFFICIENT_AFTER settle on metered credits with a PERF_UNJUDGED note and close REJECTED only when nothing was metered after the change (V145 booked the whole baseline for them); the settle note carries the per-day query-volume ratio with a VOLUME_CONFOUNDED marker outside 0.7-1.3x (dollars not adjusted) and PERF_REGRESSED; the credit rate is FLOAT via TRY_TO_DOUBLE (TRY_TO_NUMBER rounded 3.68 to 4, +8.7%); an app-booked manual row for the same change is ADOPTED (SOURCE_CHANGE_ID stamped, Next-Fifty #5 twin rule) instead of double-booked. No MIN_CLUSTERS arm. SP_VERIFY_IDLE_SAVINGS re-derived from V053 to skip change-tied rows. Forward-only (historical rows untouched; re-settle is an owner opt-in); no schema or task change; teardown unchanged.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 153);
"""

out = (HEADER
       + "-- >>> derived:SP_LEDGER_AUTOBOOK  (from V145; full-window settle gate, volume note, adopt, "
         "FLOAT rate, close-out; V153)\n"
       + p153 + "\n\n"
       + "-- >>> derived:SP_VERIFY_IDLE_SAVINGS  (from V053; skip rows tied to a detected change; V153)\n"
       + pv153 + "\n\n"
       + TAIL)

# ---- file-level self-assertions ---------------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2
assert "CREATE OR REPLACE VIEW" not in out and "CREATE TASK" not in out and "ALTER TASK" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "ALERT_CONFIG" not in out
assert "EXCEPTION (-20153" in out and "IF (v < 152) THEN" in out
assert "SELECT 153 AS VERSION" in out and "WHERE VERSION = 153)" in out
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();") == 1
assert "$_" not in out and "RAISE EXCEPTION (" not in out

target = Path(os.environ.get("V153_OUT") or (MIG / "V153__ledger_autobook_full_window_settle.sql"))
target.write_text(out, encoding="utf-8", newline="\n")
print(f"wrote {target} ({len(out)} chars)")
