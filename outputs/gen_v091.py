"""Byte-derive V091 (auto-clear cleared alerts) from V087.

V091 is V087's SP_ALERT_SCAN plus exactly two additive things:
  (a) ALTER ALERT_CONFIG ADD AUTO_CLEAR_ENABLED + CLEAR_THRESHOLD_NUM, and a seed
      enabling auto-clear on the three FACT_QUERY_HOURLY live-window rules; and
  (b) one final [auto-clear sweep] arm at the tail of SP_ALERT_SCAN (after the
      V067 SUPERSEDED sweep) that closes TODAY's still-open events for opted-in
      rules whose metric has dropped back below a per-rule CLEAR threshold
      (hysteresis, default 0.9 x THRESHOLD_NUM), OPEN-only, with a >=1h dwell.

Removing (a) and (b) reproduces V087's SP body byte-for-byte — the repo's
derived-scan discipline. Owner applies V091 in Snowsight after V090.
"""

from pathlib import Path

ROOT = Path(r"C:\Users\jfree\Documents\GitHub\OVERWATCH_NEW")
SRC = ROOT / "snowflake" / "migrations" / "V087__security_posture_rule.sql"
DST = ROOT / "snowflake" / "migrations" / "V091__alert_auto_clear.sql"

s = SRC.read_text(encoding="utf-8")


def sub(old: str, new: str) -> None:
    global s
    n = s.count(old)
    assert n == 1, f"anchor found {n}x (want 1): {old[:70]!r}"
    s = s.replace(old, new, 1)


# ---- 1. header comment block (top of file, through the version-note line) ----
_V087_HEADER_END = (
    "-- The ONLY SP_ALERT_SCAN edit vs V086 is the arm + the rule-block count 15 -> 16;\n"
    "-- reversing both reproduces the V086 body byte-for-byte. The scanner is NOT fired\n"
    "-- at apply time. Owner applies in Snowsight after V086."
)
_V091_HEADER = (
    "-- V091__alert_auto_clear.sql\n"
    "--\n"
    "-- Auto-resolve alerts whose condition has CLEARED (Upgrade Board P0). Two additive\n"
    "-- changes, both reversible to V087:\n"
    "--   * ALTER ALERT_CONFIG ADD AUTO_CLEAR_ENABLED BOOLEAN DEFAULT FALSE and\n"
    "--     CLEAR_THRESHOLD_NUM NUMBER(18,4) (hysteresis floor; NULL => 0.9 x THRESHOLD_NUM),\n"
    "--     then seed AUTO_CLEAR_ENABLED=TRUE on the three FACT_QUERY_HOURLY live-window\n"
    "--     rules (PERF_QUERY_FAIL_PCT, PERF_QUEUED_MINUTES, PERF_SPILL_GB).\n"
    "--   * Re-derive SP_ALERT_SCAN (from V087) with ONE final [auto-clear sweep] arm,\n"
    "--     after the V067 SUPERSEDED sweep, that RESOLVEs TODAY's still-OPEN events for\n"
    "--     opted-in rules whose scope is no longer firing at the CLEAR threshold\n"
    "--     (RESOLUTION_KIND='AUTO_CLEARED'). OPEN-only (never ACK/RESOLVED/SNOOZED);\n"
    "--     a >=1h dwell + the below-CLEAR hysteresis prevent a raise-then-clear flap;\n"
    "--     today's-bucket only, so a historical day-stamped exceedance is never rewritten.\n"
    "--\n"
    "--   * Add 'AND RESOLUTION_KIND <> AUTO_CLEARED' to the dedupe guard of those three\n"
    "--     raise arms so a same-day RECURRENCE re-alerts after an auto-clear (a manual\n"
    "--     RESOLVE still suppresses re-raise). Every other arm is byte-identical to V087.\n"
    "--\n"
    "-- AUTO_CLEARED is excluded from per-rule precision/MTTR in the app read-path exactly\n"
    "-- like SUPERSEDED. The scanner is NOT fired at apply time. Owner applies after V090."
)
sub(_V087_HEADER_END, _V091_HEADER)

# strip the original V087 title line (now replaced by the block above)
sub("-- V087__security_posture_rule.sql\n--\n", "")

# ---- 2. version guard: require V090 ----
sub("not_ready EXCEPTION (-20087, 'V087 requires V086 first - apply migrations in order.');",
    "not_ready EXCEPTION (-20091, 'V091 requires V090 first - apply migrations in order.');")
sub("    IF (v < 86) THEN", "    IF (v < 90) THEN")

# ---- 3. the derived-from note above the CREATE PROCEDURE ----
sub("-- >>> derived:SP_ALERT_SCAN  (+ generic arm [21] SEC_POSTURE_METRIC, from V086)",
    "-- >>> derived:SP_ALERT_SCAN  (+ [auto-clear sweep] AUTO_CLEARED, from V087)")

# ---- 4. new columns + seed (additive), right after the V087 METRIC_NAME ALTER ----
_ALTER_ANCHOR = ("-- Additive: the metric a posture rule watches (NULL for the existing hardcoded rules).\n"
                 "ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG ADD COLUMN IF NOT EXISTS METRIC_NAME VARCHAR(60);")
_NEW_COLS_SEED = _ALTER_ANCHOR + "\n\n" + (
    "-- Additive: opt-in auto-clear + a per-rule CLEAR (hysteresis) threshold.\n"
    "ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG ADD COLUMN IF NOT EXISTS AUTO_CLEAR_ENABLED BOOLEAN DEFAULT FALSE;\n"
    "ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG ADD COLUMN IF NOT EXISTS CLEAR_THRESHOLD_NUM NUMBER(18,4);\n"
    "\n"
    "-- Seed: enable auto-clear ONLY on the three FACT_QUERY_HOURLY live-window rules,\n"
    "-- whose DEDUPE_KEY is RULE_ID|scope|CURRENT_DATE() (re-evaluated every scan) and\n"
    "-- whose condition genuinely un-happens. Day-stamped fact rules are NEVER opted in.\n"
    "-- CLEAR_THRESHOLD_NUM left NULL => 0.9 x THRESHOLD_NUM at sweep time.\n"
    "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
    "   SET AUTO_CLEAR_ENABLED = TRUE\n"
    " WHERE RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB');"
)
sub(_ALTER_ANCHOR, _NEW_COLS_SEED)

# ---- 5. the [auto-clear sweep] arm, inserted just before the RETURN ----
_RETURN_ANCHOR = ("\n    RETURN 'alert scan v10 (V045: task rule restored + r25 teeth kept): '"
                  " || (16 - :fails) || '/16 rule blocks ok';")
_SWEEP = "\n" + r"""
    -- [auto-clear sweep] V091: resolve TODAY's still-OPEN live-window events whose
    -- scope has dropped back below the rule's CLEAR threshold (hysteresis, default
    -- 0.9 x THRESHOLD_NUM). Runs AFTER the raise arms + the supersede sweep so an
    -- escalated/superseded event is never also auto-cleared this pass. OPEN-only
    -- (manual RESOLVE wins and is never reopened; an active SNOOZE is left alone; an
    -- ACK is a human actively working it, so v1 leaves it too). The >=1h dwell plus
    -- below-CLEAR hysteresis mean an event cannot open and auto-close in one cadence.
    -- Only today's bucket (LIKE '%|<today>') is touched, so historical day-stamped
    -- exceedances are never rewritten. RESOLUTION_KIND='AUTO_CLEARED' is excluded from
    -- per-rule precision/MTTR in the app read-path exactly like SUPERSEDED. Wrapped so
    -- a sweep failure never breaks alerting (does NOT touch :fails).
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'
         WHERE ev.STATUS = 'OPEN'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                              WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.DEDUPE_KEY LIKE '%|' || CURRENT_DATE()                    -- today's live bucket only
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
           AND ev.DEDUPE_KEY NOT IN (
               -- scopes STILL firing at the CLEAR threshold. Same candidate subqueries
               -- as raise arms [03]/[04]/[05], recomputed at COALESCE(CLEAR, 0.9 x RAISE).
               WITH cfg AS (
                   SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                   WHERE ENABLED AND AUTO_CLEAR_ENABLED
               )
               SELECT c.RULE_ID || '|' || q.COMPANY || '|' || CURRENT_DATE() AS DEDUPE_KEY
               FROM cfg c
               JOIN (
                   SELECT COMPANY,
                          IFF(SUM(QUERY_COUNT) = 0, 0, SUM(FAILED_COUNT) / SUM(QUERY_COUNT) * 100) AS FAIL_PCT
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   GROUP BY COMPANY
                   HAVING SUM(QUERY_COUNT) >= 20
               ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT'
                  AND q.FAIL_PCT >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(QUEUED_SEC_SUM) / 60 AS QUEUED_MIN
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES'
                  AND q.QUEUED_MIN >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
               UNION ALL
               SELECT c.RULE_ID || '|' || q.WAREHOUSE_NAME || '|' || CURRENT_DATE()
               FROM cfg c
               JOIN (
                   SELECT WAREHOUSE_NAME, SUM(SPILL_REMOTE_GB) AS SPILL_GB
                   FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY
                   WHERE HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                     AND WAREHOUSE_NAME IS NOT NULL
                   GROUP BY WAREHOUSE_NAME
               ) q ON c.RULE_ID = 'PERF_SPILL_GB'
                  AND q.SPILL_GB >= COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'autoclear_sweep_failed', :emsg, 'V091 auto-clear sweep - other rules unaffected', CURRENT_ROLE();
    END;
"""
sub(_RETURN_ANCHOR,
    _SWEEP + "\n    RETURN 'alert scan v11 (V091: + auto-clear sweep): '"
    " || (16 - :fails) || '/16 rule blocks ok';")

# ---- 5b. recurrence: the three auto-clear rules' dedupe guard must ignore an
#          AUTO_CLEARED row, so a same-day recurrence re-alerts (a manual RESOLVE
#          still suppresses re-raise). Every other arm's guard is left untouched. ----
_GUARD = ("\n\n        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n"
          "        WHERE NOT EXISTS (\n"
          "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
          "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
          "        );")
_GUARD_FIX = ("\n\n        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n"
              "        WHERE NOT EXISTS (\n"
              "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
              "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
              "              AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'   -- V091: recurrence re-alerts after an auto-clear\n"
              "        );")
for _on in (
    "        ) q ON c.RULE_ID = 'PERF_QUERY_FAIL_PCT' AND q.FAIL_PCT >= c.THRESHOLD_NUM",
    "        ) q ON c.RULE_ID = 'PERF_QUEUED_MINUTES' AND q.QUEUED_MIN >= c.THRESHOLD_NUM",
    "        ) q ON c.RULE_ID = 'PERF_SPILL_GB' AND q.SPILL_GB >= c.THRESHOLD_NUM",
):
    sub(_on + _GUARD, _on + _GUARD_FIX)

# ---- 6. SCHEMA_VERSION footer ----
sub("SELECT 87 AS VERSION,", "SELECT 91 AS VERSION,")
sub("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 87);",
    "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 91);")
# replace the V087 description with the V091 one (unique anchor = the whole string)
_V087_DESC = ("       'Security finding -> monitored rule (CoCo Sec35): ALTER ALERT_CONFIG ADD METRIC_NAME; "
              "SP_ALERT_SCAN re-derived from V086 with a generic arm [21] that raises every enabled rule "
              "carrying a METRIC_NAME when its newest MART_SECURITY_POSTURE_DAILY reading is at/over "
              "THRESHOLD_NUM (posture counts are higher=worse). Rules are operator-created from a Security "
              "generate-INSERT UI, so the arm is the shared raiser; stale posture excluded; daily-deduped. "
              "Arm count 15->16; the scanner is not fired at apply time.' AS DESCRIPTION")
_V091_DESC = ("       'Auto-resolve cleared alerts (Upgrade Board P0): ALTER ALERT_CONFIG ADD "
              "AUTO_CLEAR_ENABLED + CLEAR_THRESHOLD_NUM, seed the three FACT_QUERY_HOURLY live-window rules; "
              "SP_ALERT_SCAN re-derived from V087 with a final [auto-clear sweep] that RESOLVEs today''s "
              "still-OPEN events (RESOLUTION_KIND=AUTO_CLEARED) once the scope drops below the CLEAR "
              "hysteresis floor (0.9 x THRESHOLD_NUM), OPEN-only, >=1h dwell, today''s bucket only. "
              "AUTO_CLEARED is excluded from per-rule precision/MTTR like SUPERSEDED. Scanner not fired at "
              "apply time.' AS DESCRIPTION")
sub(_V087_DESC, _V091_DESC)

DST.write_text(s, encoding="utf-8", newline="\n")
print(f"wrote {DST} ({len(s)} bytes)")
