#!/usr/bin/env python3
"""Forward-generate V164: actionable Teams lines + a one-time CRITICAL escalation (Next-Fifty #40, v4.602.0).

Owner decisions (2026-09-29): every Teams line carries severity, title, company, detail and event id; a CRITICAL
nobody acknowledged within ESCALATE_AFTER_MIN (120) minutes is re-posted to the route(s) that delivered it and
emailed through the OVERWATCH_EMAIL integration's DEFAULT_RECIPIENTS (no address is ever written to a file); a
snooze or an incident ACK counts as acknowledged; the monthly alert drill escalates too.

Reads V064__webhook_drain_watermarks_alert_burn_telemetry.sql ONLY -- the CURRENT definer of SP_NOTIFY_WEBHOOK
(tests/test_proc_lineage.py; V070 / V112 / V157 / V160 only mention it) -- and emits, in order:

  guard (-20164, v < 163) -> ALERT_EVENTS.ESCALATED_AT (ADD COLUMN IF NOT EXISTS) -> SETTINGS seed
  (ESCALATE_AFTER_MIN '120', ESCALATE_EMAIL_INTEGRATION 'OVERWATCH_EMAIL'; WHEN NOT MATCHED only)
  -> marker + SP_NOTIFY_WEBHOOK re-derived from V064 -> SCHEMA_VERSION 164.

SP_NOTIFY_WEBHOOK deltas, each asserted by count (everything else byte-identical to V064; the V164 test
normalizes it back):
  N1a 12 escalation DECLAREs after the V063 fits_ids line
  N1b cursor c2 (enabled routes) after cursor c1
  N2  the line expression at BOTH V064 sites (the 3000-char fit and the LISTAGG) -> NEW_LINE, identical at both,
      so the fit still equals what is sent
  N3  the escalation pass between the drain's END FOR and the expired tail (inside the sender lease)
  N4  the RETURN tail gains the escalation count + note

No task change, no CALL, nothing runs at apply time. When PREFLIGHT_OUT is set, also writes the READ-ONLY V164
section of PREFLIGHT_WAVE4.sql (P164.1-P164.4) built from the SAME line and capture text; when PART_B_OUT is set,
the READ-ONLY RUN_NEXT PART B grids (V164.1-V164.4). The byte-identity test sets neither. This file never imports
app/ and never runs from the app.

Run: python outputs/gen_v164.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V064__webhook_drain_watermarks_alert_burn_telemetry.sql"
V064 = BASE.read_text(encoding="utf-8")
NAME = "V164__notify_actionable_lines_escalation.sql"

ESCALATE_AFTER_MIN = 120                 # owner decision 2026-09-29
ESCALATE_EMAIL_INTEGRATION = "OVERWATCH_EMAIL"
MAX_BATCHES = 6                          # V064:89, unchanged


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


PROC_BASE = extract_proc(V064, "SP_NOTIFY_WEBHOOK()")

# intervening changes that MUST survive the re-derivation (V063 capture-once ARRAY, V034 company filter, V026 JSON
# escape, V022 per-route ledger, V064 drain + lease + outer handler)
assert PROC_BASE.count("INTO :fits_ids") == 1
assert "AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')" in PROC_BASE
assert PROC_BASE.count("message := REPLACE(:message, ") == 5
assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)" in PROC_BASE
assert f"max_batches INT DEFAULT {MAX_BATCHES};" in PROC_BASE
assert PROC_BASE.count("DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE") == 3 and PROC_BASE.count("RAISE;") == 1

# ---------------------------------------------------------------------------------------------------
# N2 -- the line. ASCII only. DETAIL is flattened (LF / CR / TAB -> space) BEFORE the trim, so a detail that
# starts or ends with a line break, or is only whitespace, never leaves an empty ' | ' segment. TITLE, COMPANY,
# SEVERITY and EVENT_ID are NOT NULL (V004:38-52), so the line is never NULL: LISTAGG never drops an event the
# fit counted.
# ---------------------------------------------------------------------------------------------------
OLD_LINE = "'[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140)"
_FLAT = "TRIM(TRANSLATE(e.DETAIL, CHR(10) || CHR(13) || CHR(9), '   '))"
NEW_LINE = (OLD_LINE + " || ' | ' || e.COMPANY || IFF(COALESCE(" + _FLAT + ", '') = '', '', ' | ' || LEFT("
            + _FLAT + ", 100)) || ' | event ' || e.EVENT_ID")
ESC_LINE = ("'ESCALATED (unacked ' || DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), :esc_now)"
            " || ' min) ' || " + NEW_LINE)
assert NEW_LINE.isascii() and ESC_LINE.isascii()


def esc_chain(expr: str, ind: str) -> str:
    """The V064:150-156 JSON-escape length chain around ``expr`` (same REPLACE order and layout)."""
    return ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(\n"
            f"{ind}{expr},\n"
            f"{ind}CHR(92), CHR(92) || CHR(92)),\n"
            f"{ind}CHR(34), CHR(92) || CHR(34)),\n"
            f"{ind}CHR(10), CHR(92) || 'n'),\n"
            f"{ind}CHR(13), ''),\n"
            f"{ind}CHR(9),  CHR(92) || 't')")


# ---------------------------------------------------------------------------------------------------
# N3 -- the escalation capture. ONE text, used by the proc and (binds swapped) by PREFLIGHT P164.2.
# ---------------------------------------------------------------------------------------------------
SETTINGS_AFTER = "COALESCE(MAX(IFF(KEY = 'ESCALATE_AFTER_MIN', VALUE, NULL)), '" + str(ESCALATE_AFTER_MIN) + "')"
SETTINGS_EMAIL = ("COALESCE(MAX(IFF(KEY = 'ESCALATE_EMAIL_INTEGRATION', VALUE, NULL)), '"
                  + ESCALATE_EMAIL_INTEGRATION + "')")
AFTER_PARSE = "COALESCE(TRY_TO_NUMBER(TRIM({v})), 0)"      # minutes are whole numbers: '120.7' -> 121

# The eligibility, one clause per line (the V164 test pins each exactly once). ESCALATED_AT is its own line so
# the PREFLIGHT (run before the column exists) can drop exactly that line.
ESC_ESCALATED_LINE = "                  AND e.ESCALATED_AT IS NULL\n"
ESC_FROM_WHERE = (
    "                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID\n"
    "                WHERE e.SEVERITY = 'CRITICAL'\n"
    "                  AND e.STATUS = 'OPEN'\n"
    "                  AND e.ACK_AT IS NULL\n"
    + ESC_ESCALATED_LINE +
    "                  AND e.RAISED_AT >= DATEADD('day', -7, :esc_now)\n"
    "                  AND COALESCE(e.NOTIFIED_AT, e.RAISED_AT) <= DATEADD('minute', -1 * :esc_after, :esc_now)\n"
    "                  AND NOT EXISTS (SELECT 1\n"
    "                                  FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m\n"
    "                                  JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID\n"
    "                                  WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID\n"
    "                                    AND (i.ACK_AT IS NOT NULL OR i.STATUS <> 'OPEN'))\n"
    "                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a\n"
    "                                  WHERE a.EVENT_ID = e.EVENT_ID AND a.ACTION = 'SNOOZE')\n"
    "                  AND (COALESCE(TRIM(:esc_email), '') <> ''\n"
    "                       OR EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n"
    "                                  JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r\n"
    "                                    ON r.ROUTE_ID = d.ROUTE_ID AND r.ENABLED\n"
    "                                  WHERE d.EVENT_ID = e.EVENT_ID))\n"
)
ESC_CUM = ("                SELECT e.EVENT_ID, e.RAISED_AT,\n"
           "                       SUM(LEN(" + esc_chain(ESC_LINE, " " * 27) + ") + 2)\n"
           "                         OVER (ORDER BY e.RAISED_AT ASC, e.EVENT_ID\n"
           "                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN\n")
CAPTURE = ("            SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.RAISED_AT ASC, f.EVENT_ID)\n"
           "              INTO :esc_ids\n"
           "            FROM (\n"
           + ESC_CUM + ESC_FROM_WHERE +
           "            ) f\n"
           "            WHERE f.CUM_LEN <= 3000;\n")

# ---------------------------------------------------------------------------------------------------
# The deltas.
# ---------------------------------------------------------------------------------------------------
N1A_OLD = ("    fits_ids ARRAY;         -- V063: frozen fitting EVENT_IDs (VARCHAR EVENT_ID) shared by message + "
           "ledger + NOTIFIED_AT\n")
N1A_ADD = (
    "    esc_after_s VARCHAR;    -- V164 #40: SETTINGS.ESCALATE_AFTER_MIN (absent = '120'; 0 / not a number = off)\n"
    "    esc_after NUMBER DEFAULT 0;\n"
    "    esc_email VARCHAR;      -- V164 #40: SETTINGS.ESCALATE_EMAIL_INTEGRATION (absent = 'OVERWATCH_EMAIL'; '' = "
    "no email)\n"
    "    esc_now TIMESTAMP_NTZ;  -- V164 #40: one frozen clock for the whole escalation pass\n"
    "    esc_ids ARRAY;          -- V164 #40: the frozen escalation set (capture-once, the B9 invariant)\n"
    "    r_esc_ids ARRAY;        -- V164 #40: the part of esc_ids THIS route already delivered\n"
    "    esc_ok ARRAY;           -- V164 #40: ids at least one channel accepted (audited + stamped)\n"
    "    esc_msg VARCHAR;\n"
    "    esc_routes INT DEFAULT 0;\n"
    "    esc_emailed BOOLEAN DEFAULT FALSE;\n"
    "    escalated INT DEFAULT 0;\n"
    "    esc_note VARCHAR DEFAULT '';\n"
)
N1B_OLD = "        ORDER BY r.ROUTE_ID;\nBEGIN\n"
N1B_NEW = ("        ORDER BY r.ROUTE_ID;\n"
           "    c2 CURSOR FOR           -- V164 #40: enabled routes, for the escalation re-post\n"
           "        SELECT r.ROUTE_ID, r.INTEGRATION_NAME\n"
           "        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r\n"
           "        WHERE r.ENABLED\n"
           "        ORDER BY r.ROUTE_ID;\n"
           "BEGIN\n")
N3_OLD = "    END FOR;\n\n    -- Loud, not silent:"
N3_BLOCK = (
    "    END FOR;\n"
    "\n"
    "    -- V164 #40: CRITICAL ESCALATION PASS (Next-Fifty #40, owner decision 2026-09-29). A CRITICAL still OPEN\n"
    "    -- and never acknowledged (no ACK_AT; not in an incident someone acknowledged, mitigated or closed; never\n"
    "    -- snoozed -- an ALERT_AUDIT SNOOZE row) whose first notification (NOTIFIED_AT, or RAISED_AT when no route\n"
    "    -- ever took it) is ESCALATE_AFTER_MIN+ minutes old escalates ONCE: re-posted to every enabled route that\n"
    "    -- already delivered it (the same Teams route), and emailed through ESCALATE_EMAIL_INTEGRATION, i.e. to\n"
    "    -- that integration's DEFAULT_RECIPIENTS -- no address is written here. With the email leg off ('') only an\n"
    "    -- event some enabled route delivered is eligible, so an undeliverable one never holds the batch.\n"
    "    -- Capture-once: the ids are frozen oldest-first into esc_ids within 3000 escaped chars (the V063 B9\n"
    "    -- invariant), and every message, the audit and the stamp derive from that one set. Send, then audit, then\n"
    "    -- stamp: ESCALATED_AT is set only for ids a channel accepted, so an all-channel failure retries next run\n"
    "    -- inside the 7-day CRITICAL window (at-least-once, like the drain). Inside the sender lease, so two runs\n"
    "    -- never double-escalate. Isolated: an error here is logged (escalation_failed) and never re-raised -- the\n"
    "    -- deliveries above, the expired tail, the lease release and the RETURN below still run.\n"
    "    BEGIN\n"
    "        SELECT " + SETTINGS_AFTER + ",\n"
    "               " + SETTINGS_EMAIL + "\n"
    "          INTO :esc_after_s, :esc_email\n"
    "          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS\n"
    "         WHERE KEY IN ('ESCALATE_AFTER_MIN', 'ESCALATE_EMAIL_INTEGRATION');\n"
    "        esc_after := " + AFTER_PARSE.format(v=":esc_after_s") + ";\n"
    "        IF (esc_after <= 0) THEN\n"
    "            esc_note := '; escalation off (ESCALATE_AFTER_MIN)';\n"
    "        ELSE\n"
    "            esc_now := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;\n"
    "            esc_ok := ARRAY_CONSTRUCT();\n"
    + CAPTURE +
    "\n"
    "            IF (esc_ids IS NOT NULL AND ARRAY_SIZE(:esc_ids) > 0) THEN\n"
    "                -- Teams leg: re-post only what THIS route already delivered (the same route that paged).\n"
    "                FOR erec IN c2 DO\n"
    "                    r_route_id := erec.ROUTE_ID;\n"
    "                    r_integration := erec.INTEGRATION_NAME;\n"
    "                    SELECT ARRAY_AGG(DISTINCT d.EVENT_ID)\n"
    "                      INTO :r_esc_ids\n"
    "                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n"
    "                    WHERE d.ROUTE_ID = :r_route_id\n"
    "                      AND ARRAY_CONTAINS(d.EVENT_ID::VARIANT, :esc_ids);\n"
    "                    IF (r_esc_ids IS NOT NULL AND ARRAY_SIZE(:r_esc_ids) > 0) THEN\n"
    "                        SELECT LISTAGG(" + ESC_LINE + ", '\\n')\n"
    "                               WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)\n"
    "                          INTO :esc_msg\n"
    "                        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "                        WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :r_esc_ids);\n"
    "                        -- the V064 JSON escape (backslash first, then quote, newline, CR, tab)\n"
    "                        esc_msg := REPLACE(:esc_msg, CHR(92), CHR(92) || CHR(92));\n"
    "                        esc_msg := REPLACE(:esc_msg, CHR(34), CHR(92) || CHR(34));\n"
    "                        esc_msg := REPLACE(:esc_msg, CHR(10), CHR(92) || 'n');\n"
    "                        esc_msg := REPLACE(:esc_msg, CHR(13), '');\n"
    "                        esc_msg := REPLACE(:esc_msg, CHR(9),  CHR(92) || 't');\n"
    "                        BEGIN\n"
    "                            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(\n"
    "                                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(\n"
    "                                    'OVERWATCH ESCALATION - CRITICAL unacknowledged ' || :esc_after || '+ min:'\n"
    "                                    || CHR(92) || 'n' || LEFT(:esc_msg, 3000)),\n"
    "                                SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));\n"
    "                            esc_ok := ARRAY_CAT(:esc_ok, :r_esc_ids);\n"
    "                            esc_routes := esc_routes + 1;\n"
    "                        EXCEPTION\n"
    "                            WHEN OTHER THEN\n"
    "                                emsg := SQLERRM;\n"
    "                                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG\n"
    "                                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
    "                                SELECT 'NotifyWebhook', 'route_send_failed', :emsg,\n"
    "                                       'route ' || :r_route_id || ' integration ' || :r_integration ||\n"
    "                                       ' - escalation re-post; the email leg is unaffected',\n"
    "                                       CURRENT_ROLE();\n"
    "                        END;\n"
    "                    END IF;\n"
    "                END FOR;\n"
    "\n"
    "                -- Email leg: every escalated id, to the integration's DEFAULT_RECIPIENTS (never an address here).\n"
    "                IF (COALESCE(TRIM(:esc_email), '') <> '') THEN\n"
    "                    SELECT LISTAGG(" + ESC_LINE + ", CHR(10))\n"
    "                           WITHIN GROUP (ORDER BY e.RAISED_AT ASC, e.EVENT_ID)\n"
    "                      INTO :esc_msg\n"
    "                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "                    WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ids);\n"
    "                    BEGIN\n"
    "                        CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(\n"
    "                            SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(\n"
    "                                'OVERWATCH ESCALATION - CRITICAL alert(s) nobody acknowledged within '\n"
    "                                || :esc_after || ' minutes:' || CHR(10) || CHR(10) || :esc_msg || CHR(10) || CHR(10)\n"
    "                                || 'Acknowledge in OVERWATCH > Alerts > Open events. Each alert escalates once.'),\n"
    "                            SNOWFLAKE.NOTIFICATION.INTEGRATION(TRIM(:esc_email)));\n"
    "                        esc_ok := ARRAY_CAT(:esc_ok, :esc_ids);\n"
    "                        esc_emailed := TRUE;\n"
    "                    EXCEPTION\n"
    "                        WHEN OTHER THEN\n"
    "                            emsg := SQLERRM;\n"
    "                            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG\n"
    "                                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
    "                            SELECT 'NotifyWebhook', 'escalation_email_failed', :emsg,\n"
    "                                   'integration ' || :esc_email || ' - ' || ARRAY_SIZE(:esc_ids) ||\n"
    "                                   ' escalation(s) not emailed; check DEFAULT_RECIPIENTS and USAGE on the "
    "integration',\n"
    "                                   CURRENT_ROLE();\n"
    "                            esc_note := :esc_note || '; escalation email failed (APP_ERROR_LOG "
    "escalation_email_failed)';\n"
    "                    END;\n"
    "                END IF;\n"
    "\n"
    "                -- Send, then audit, then stamp: only ids a channel accepted, each once.\n"
    "                IF (ARRAY_SIZE(:esc_ok) > 0) THEN\n"
    "                    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (EVENT_ID, ACTION, NOTE, ACTED_BY)\n"
    "                    SELECT e.EVENT_ID, 'ESCALATE',\n"
    "                           'unacknowledged ' || :esc_after || '+ min; this run re-posted to ' || :esc_routes ||\n"
    "                           ' route(s), email ' || IFF(:esc_emailed, 'sent', 'not sent'),\n"
    "                           'SP_NOTIFY_WEBHOOK'\n"
    "                    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "                    WHERE e.ESCALATED_AT IS NULL\n"
    "                      AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ok);\n"
    "                    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "                       SET ESCALATED_AT = CURRENT_TIMESTAMP()\n"
    "                     WHERE e.ESCALATED_AT IS NULL\n"
    "                       AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :esc_ok);\n"
    "                    escalated := SQLROWCOUNT;\n"
    "                END IF;\n"
    "            END IF;\n"
    "        END IF;\n"
    "    EXCEPTION\n"
    "        WHEN OTHER THEN\n"
    "            emsg := SQLERRM;\n"
    "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG\n"
    "                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
    "            SELECT 'NotifyWebhook', 'escalation_failed', :emsg,\n"
    "                   'escalation pass - the deliveries above are unaffected; retries next run',\n"
    "                   CURRENT_ROLE();\n"
    "            esc_note := '; escalation pass FAILED (APP_ERROR_LOG escalation_failed)';\n"
    "    END;\n"
    "\n"
    "    -- Loud, not silent:"
)
N4_OLD = "           ' newly expired-undelivered (event,route) pair(s) flagged';\n"
N4_NEW = ("           ' newly expired-undelivered (event,route) pair(s) flagged; ' || :escalated ||\n"
          "           ' CRITICAL(s) escalated' || :esc_note;\n")

proc = PROC_BASE
proc = _swap(proc, OLD_LINE, NEW_LINE, "N2 line (fit + LISTAGG)", n=2)
proc = _swap(proc, N1A_OLD, N1A_OLD + N1A_ADD, "N1a DECLARE")
proc = _swap(proc, N1B_OLD, N1B_NEW, "N1b cursor c2")
proc = _swap(proc, N3_OLD, N3_BLOCK, "N3 escalation pass")
proc = _swap(proc, N4_OLD, N4_NEW, "N4 RETURN")

# post-conditions: the deltas landed, nothing else moved
assert proc.count(NEW_LINE) == 5 and OLD_LINE + "," not in proc and OLD_LINE + "\n" not in proc
assert proc.count("DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE") == 3 and proc.count("RAISE;") == 1
assert proc.count(f"max_batches INT DEFAULT {MAX_BATCHES};") == 1
assert proc.count("CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(") == 3
assert "SYSTEM$SEND_EMAIL" not in proc and "@" not in proc
assert proc.index("-- V164 #40: CRITICAL ESCALATION PASS") < proc.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG"
                                                                      "\n        (PAGE, ERROR_TYPE, ERROR_MESSAGE, "
                                                                      "CONTEXT, ROLE_NAME)\n    SELECT 'NotifyWebhook', "
                                                                      "'undelivered_expired'")
assert proc.index("-- V164 #40: CRITICAL ESCALATION PASS") < proc.index("       SET HELD = FALSE, HOLDER = NULL")
_blk = proc[proc.index("-- V164 #40: CRITICAL ESCALATION PASS"):proc.index("    -- Loud, not silent:")]
assert "RAISE" not in _blk.replace("RAISED_AT", "")
assert _blk.index("INTEGRATION(TRIM(:esc_email))") < _blk.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT") \
    < _blk.index("SET ESCALATED_AT = CURRENT_TIMESTAMP()")
assert proc.isascii()

# ---------------------------------------------------------------------------------------------------
# The file.
# ---------------------------------------------------------------------------------------------------
HEADER = f"""-- {NAME}
--
-- Next-Fifty #40 (owner decisions 2026-09-29): Teams lines you can act on, and a one-time escalation of a
-- CRITICAL nobody acknowledged.
--
-- WHY: a Teams line said only '[SEV] <title>' -- no company, no detail, no event id to find it by -- and a
-- CRITICAL was posted once and then sat unanswered: nothing re-paged it and nothing reached anyone off Teams.
--
--   + ALERT_EVENTS.ESCALATED_AT TIMESTAMP_NTZ (ADD COLUMN IF NOT EXISTS): the once-per-event escalation marker.
--   + SETTINGS ESCALATE_AFTER_MIN '{ESCALATE_AFTER_MIN}' (0 or not a number = escalation off) and ESCALATE_EMAIL_INTEGRATION
--     '{ESCALATE_EMAIL_INTEGRATION}' ('' = no email leg), WHEN NOT MATCHED only: a value set before the apply is kept.
--   ~ SP_NOTIFY_WEBHOOK re-derived from V064 (its current definer; V070, V112, V157 and V160 only mention it),
--     byte-identical except:
--       N1 12 escalation variables and cursor c2 (the enabled routes) in the DECLARE;
--       N2 every line reads '[SEV] <title, 140> | <company> | <detail, one line, 100> | event <EVENT_ID>' (ASCII),
--          identical in the 3000-char fit and in the LISTAGG, so the fit still equals what is sent (a worst-case
--          escaped line is under 900 chars, so a batch always holds at least one event);
--       N3 the escalation pass, inside the sender lease, after the drain and before the expired tail: a CRITICAL
--          still OPEN with no ACK_AT, not in an incident someone acknowledged, mitigated or closed, never snoozed
--          (an ALERT_AUDIT SNOOZE row), raised inside the 7-day CRITICAL send window, whose rule still exists, and
--          first notified (NOTIFIED_AT, else RAISED_AT) ESCALATE_AFTER_MIN+ minutes ago escalates ONCE: re-posted
--          to every enabled route that already delivered it, and emailed through ESCALATE_EMAIL_INTEGRATION with
--          the notification-integration send, i.e. to that integration's DEFAULT_RECIPIENTS (no address is stored
--          in OVERWATCH). With the email leg off, only an event some enabled route delivered is eligible.
--          Capture-once (oldest first, 3000 escaped chars); send, then an ALERT_AUDIT 'ESCALATE' row, then the
--          ESCALATED_AT stamp, only for ids a channel accepted (every channel failing = retried next run inside
--          the 7 days). Its own handler logs escalation_failed and never re-raises, so the expired tail, the lease
--          release and the RETURN still run. A re-post failure logs route_send_failed with the V064 CONTEXT
--          prefix (the Native delivery card attributes it to its route); an email failure logs
--          escalation_email_failed;
--       N4 the RETURN adds '<n> CRITICAL(s) escalated' and a note (off / email failed / pass failed).
--
-- !! OWNER SMOKE TEST (like V064): the notification send, the ARRAY handling and the nested cursor loop are
--    runtime-only; RUN_NEXT PART B V164.3 / V164.4 prove them. The email leg needs DEFAULT_RECIPIENTS on the
--    integration and USAGE on it for the proc owner (PREFLIGHT P164.1).
-- COST: one SETTINGS read and one small capture SELECT on ALERT_EVENTS per hourly TASK_ALERT_NOTIFY run, plus sends
--    only when something is eligible: a few compile-seconds an hour. No new table, task, view or warehouse.
-- LATENCY: hourly (TASK_ALERT_NOTIFY runs in the chain off TASK_LOAD_HOURLY), so with 120 minutes an escalation
--    goes out about 120-185 minutes after the first notification.
-- THROUGHPUT: lines are about 3x longer, so a 3000-char batch holds about 8-13 events instead of about 40;
--    max_batches stays {MAX_BATCHES} (PREFLIGHT P164.3 counts the runs that would have spilled to the next hour).
-- FIRST RUN: every OPEN, never-acknowledged CRITICAL of the last 7 days first notified 120+ minutes ago escalates on
--    the next hourly run (one 3000-char batch per run; the rest follow). PREFLIGHT P164.2 lists them: acknowledge or
--    resolve stale ones first, or seed SETTINGS ('ESCALATE_AFTER_MIN', '0') before the apply (the MERGE keeps it).
-- ROLLBACK: soft -- set SETTINGS ESCALATE_AFTER_MIN to '0'; hard -- re-run ONLY V064's SP_NOTIFY_WEBHOOK CREATE
--    (V064 lines 74-351; never the whole V064 file, which would also roll back three other procs), which also
--    restores the old line format. The column and the settings can stay.
-- Nothing runs at apply time (no CALL: a hand CALL of the notifier can page and email).
-- Apply AFTER V163. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20164, 'V164 requires V163 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 163) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The once-per-event escalation marker (NULL = never escalated). Additive; existing rows read NULL.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ADD COLUMN IF NOT EXISTS ESCALATED_AT TIMESTAMP_NTZ;

-- Escalation knobs (Admin > Settings). WHEN NOT MATCHED only: a value set before the apply (for example
-- ESCALATE_AFTER_MIN '0' to start with escalation off) is never overwritten. The integration is a NAME, not an
-- address: the email goes to that integration's DEFAULT_RECIPIENTS.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('ESCALATE_AFTER_MIN', '{ESCALATE_AFTER_MIN}'),
        ('ESCALATE_EMAIL_INTEGRATION', '{ESCALATE_EMAIL_INTEGRATION}')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

"""

MARKER = ("-- >>> derived:SP_NOTIFY_WEBHOOK  (from V064; actionable lines + one-time CRITICAL escalation pass, "
          "Next-Fifty #40, V164)\n")

DESCRIPTION = (
    "Next-Fifty #40: actionable Teams lines and a one-time CRITICAL escalation. SP_NOTIFY_WEBHOOK re-derived from "
    "V064, byte-identical except: every line reads [SEV] title (140) | company | detail (one line, 100) | event id, "
    "identical in the 3000-char fit and the LISTAGG (max_batches stays 6); and an escalation pass inside the sender "
    "lease, after the drain and before the expired tail. A CRITICAL still OPEN with no ACK_AT, not in an incident "
    "someone acknowledged, mitigated or closed, never snoozed, raised in the 7-day CRITICAL window, whose rule "
    "exists, and first notified (NOTIFIED_AT, else RAISED_AT) ESCALATE_AFTER_MIN (120) minutes ago escalates once: "
    "re-posted to every enabled route that already delivered it, and emailed through ESCALATE_EMAIL_INTEGRATION "
    "(OVERWATCH_EMAIL; that integration DEFAULT_RECIPIENTS, no address stored). Capture-once, oldest first; send, "
    "then an ALERT_AUDIT ESCALATE row, then the new ALERT_EVENTS.ESCALATED_AT stamp, only for ids a channel accepted; "
    "every channel failing retries next run. Isolated: escalation_failed is logged and never re-raised; a re-post "
    "failure logs route_send_failed, an email failure escalation_email_failed. The RETURN adds the escalated count. "
    "Seeds SETTINGS ESCALATE_AFTER_MIN 120 (0 = off) and ESCALATE_EMAIL_INTEGRATION OVERWATCH_EMAIL (blank = no "
    "email) WHEN NOT MATCHED only. No task change, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 164 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 164);
"""

out = HEADER + MARKER + proc + "\n" + VERSION_ROW

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and out.isascii() and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.count("EXECUTE IMMEDIATE") == 1
assert "V164 requires V163 first" in out and "IF (v < 163) THEN" in out
assert "SELECT 164 AS VERSION" in out and "WHERE VERSION = 164);" in out
assert not re.search(r"^\s*CALL\b", out[:out.index(MARKER)] + out[out.index("$$;", out.index(MARKER)) + 3:], re.M)
assert not re.search(r"(?:CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK|\bDROP\b)", out)
assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", out), "an email address must never be written to the migration"
assert "SYSTEM$SEND_EMAIL" not in out
assert out.index("ADD COLUMN IF NOT EXISTS ESCALATED_AT") < out.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS") \
    < out.index(MARKER) < out.index("CREATE OR REPLACE PROCEDURE") < out.index("INSERT INTO DBA_MAINT_DB.OVERWATCH"
                                                                              ".SCHEMA_VERSION")

target = Path(os.environ.get("V164_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ===================================================================================================
# Optional READ-ONLY PREFLIGHT section (P164.1-P164.4), from the SAME line + capture text.
# ===================================================================================================
_O = "DBA_MAINT_DB.OVERWATCH."


def _esc1(expr: str) -> str:
    return ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(" + expr + ", CHR(92), CHR(92) || CHR(92)), CHR(34), "
            "CHR(92) || CHR(34)), CHR(10), CHR(92) || 'n'), CHR(13), ''), CHR(9), CHR(92) || 't')")


PF_BINDS = ((":esc_now", "k.NOW_TS"), (":esc_after", "k.AFTER_MIN"), (":esc_email", "k.EMAIL_INTEGRATION"))


def _pf(text: str) -> str:
    for bind, col in PF_BINDS:
        text = text.replace(bind, col)
    return text


PF_WHERE = _pf(_swap(ESC_FROM_WHERE, ESC_ESCALATED_LINE, "", "P164.2 drop the ESCALATED_AT line"))
PF_WHERE = _swap(PF_WHERE, "                WHERE e.SEVERITY = 'CRITICAL'\n",
                 "                CROSS JOIN k\n                WHERE e.SEVERITY = 'CRITICAL'\n", "P164.2 CROSS JOIN k")
PF_ESC_LINE = _pf(ESC_LINE)
PREFLIGHT = f"""-- ======================================================================================================
-- V164 (Next-Fifty #40) PREFLIGHT -- READ-ONLY. Run BEFORE applying V164; changes nothing, sends nothing.
-- ======================================================================================================
-- P164.1 The email leg. OVERWATCH_EMAIL must be ENABLED and carry DEFAULT_RECIPIENTS: the escalation email is sent
--        with the notification-integration send, which goes to the integration's DEFAULT_RECIPIENTS (no address is
--        in git or in SETTINGS). BOOLEANS ONLY -- this grid never shows an address.
--        expect: ENABLED TRUE, DEFAULT_RECIPIENTS_SET TRUE (DEFAULT_SUBJECT_SET TRUE is nicer: the subject line).
--        If DEFAULT_RECIPIENTS_SET is FALSE: set it in Snowsight (docs/EMAIL_RECIPIENT_RUNBOOK.md, requirement 4),
--        or seed ('ESCALATE_EMAIL_INTEGRATION', '') before the apply for a Teams-only escalation.
DESC INTEGRATION OVERWATCH_EMAIL;
SELECT BOOLOR_AGG("property" = 'ENABLED' AND UPPER(TRIM("property_value")) = 'TRUE') AS ENABLED,
       BOOLOR_AGG("property" = 'DEFAULT_RECIPIENTS'
                  AND LOWER(COALESCE(TRIM("property_value"), '')) NOT IN ('', '[]', 'null')) AS DEFAULT_RECIPIENTS_SET,
       BOOLOR_AGG("property" = 'DEFAULT_SUBJECT'
                  AND LOWER(COALESCE(TRIM("property_value"), '')) NOT IN ('', '[]', 'null')) AS DEFAULT_SUBJECT_SET,
       BOOLOR_AGG("property" = 'ALLOWED_RECIPIENTS'
                  AND LOWER(COALESCE(TRIM("property_value"), '')) NOT IN ('', '[]', 'null')) AS ALLOWED_RECIPIENTS_SET
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
--        The proc runs as its owner (SNOW_ACCOUNTADMINS), which needs USAGE on the integration.
--        expect: SNOW_ACCOUNTADMINS_CAN_USE TRUE. FALSE is not final when SNOW_ACCOUNTADMINS inherits a role that
--        holds USAGE (the native email alerts it owns already send through this integration).
SHOW GRANTS ON INTEGRATION OVERWATCH_EMAIL;
SELECT BOOLOR_AGG("grantee_name" = 'SNOW_ACCOUNTADMINS' AND "privilege" IN ('USAGE', 'OWNERSHIP'))
           AS SNOW_ACCOUNTADMINS_CAN_USE,
       MAX(IFF("privilege" = 'OWNERSHIP', "grantee_name", NULL)) AS OWNER_ROLE
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

-- P164.2 First-run escalation census: V164's OWN capture text (binds swapped for the k columns; the ESCALATED_AT
--        line dropped -- the column does not exist before the apply). Every row escalates on the first hourly run
--        after the apply; WOULD_SEND_FIRST_RUN = inside that run's 3000-char batch (the rest follow on later runs).
--        REPOST_TO = the enabled routes that delivered it (the Teams re-post); EMAIL_LEG = the email goes too.
--        expect: only events you WANT re-posted and emailed. Acknowledge or resolve stale ones first, or seed
--        ('ESCALATE_AFTER_MIN', '0') before the apply and turn it on later in Admin > Settings.
WITH k AS (
    SELECT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS NOW_TS,
           {AFTER_PARSE.format(v=SETTINGS_AFTER)} AS AFTER_MIN,
           {SETTINGS_EMAIL} AS EMAIL_INTEGRATION
    FROM {_O}SETTINGS
    WHERE KEY IN ('ESCALATE_AFTER_MIN', 'ESCALATE_EMAIL_INTEGRATION')
),
rp AS (
    SELECT d.EVENT_ID, LISTAGG(DISTINCT r.INTEGRATION_NAME, ', ') AS REPOST_TO
    FROM {_O}ALERT_DELIVERIES d
    JOIN {_O}ALERT_ROUTES r ON r.ROUTE_ID = d.ROUTE_ID AND r.ENABLED
    GROUP BY d.EVENT_ID
),
cand AS (
    SELECT e.EVENT_ID, e.RULE_ID, e.COMPANY, e.RAISED_AT, e.NOTIFIED_AT, LEFT(e.TITLE, 120) AS TITLE,
           DATEDIFF('minute', COALESCE(e.NOTIFIED_AT, e.RAISED_AT), k.NOW_TS) AS UNACKED_MIN,
           COALESCE(TRIM(k.EMAIL_INTEGRATION), '') <> '' AS EMAIL_LEG,
           SUM(LEN({_esc1(PF_ESC_LINE)}) + 2)
             OVER (ORDER BY e.RAISED_AT ASC, e.EVENT_ID ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN
{PF_WHERE}                  AND k.AFTER_MIN > 0
)
SELECT x.EVENT_ID, x.RULE_ID, x.COMPANY, x.RAISED_AT, x.NOTIFIED_AT, x.UNACKED_MIN,
       COALESCE(rp.REPOST_TO, '(no route delivered it: email only)') AS REPOST_TO, x.EMAIL_LEG,
       x.CUM_LEN <= 3000 AS WOULD_SEND_FIRST_RUN, x.TITLE
FROM cand x
LEFT JOIN rp ON rp.EVENT_ID = x.EVENT_ID
ORDER BY x.RAISED_AT, x.EVENT_ID;

-- P164.3 Line length and throughput over 30 days of real deliveries: today's line vs the V164 line, and how many
--        runs (route x hour) would have needed more than max_batches = {MAX_BATCHES} batches with the longer line.
--        expect: RUNS_THAT_WOULD_SPILL 0 (a spill only delays the rest to the next hourly run; lever: max_batches).
WITH ev AS (
    SELECT d.ROUTE_ID, DATE_TRUNC('hour', d.SENT_AT) AS RUN_HOUR, e.EVENT_ID,
           LEN({_esc1(OLD_LINE)}) AS OLD_LEN,
           LEN({_esc1(NEW_LINE)}) AS NEW_LEN
    FROM {_O}ALERT_DELIVERIES d
    JOIN {_O}ALERT_EVENTS e ON e.EVENT_ID = d.EVENT_ID
    WHERE d.SENT_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())
),
per_run AS (
    SELECT ROUTE_ID, RUN_HOUR, COUNT(*) AS N,
           CEIL((SUM(OLD_LEN) + 2 * COUNT(*)) / 3000) AS OLD_BATCHES,
           CEIL((SUM(NEW_LEN) + 2 * COUNT(*)) / 3000) AS NEW_BATCHES
    FROM ev
    GROUP BY ROUTE_ID, RUN_HOUR
),
per_route AS (
    SELECT ROUTE_ID, ROUND(AVG(OLD_LEN)) AS AVG_LINE_TODAY, ROUND(AVG(NEW_LEN)) AS AVG_LINE_NEW,
           MAX(NEW_LEN) AS MAX_LINE_NEW
    FROM ev
    GROUP BY ROUTE_ID
)
SELECT p.ROUTE_ID, r.INTEGRATION_NAME, COUNT(*) AS RUNS_WITH_SENDS, MAX(p.N) AS PEAK_EVENTS_ONE_RUN,
       MAX(p.OLD_BATCHES) AS PEAK_BATCHES_TODAY, MAX(p.NEW_BATCHES) AS PEAK_BATCHES_NEW,
       COUNT_IF(p.NEW_BATCHES > {MAX_BATCHES}) AS RUNS_THAT_WOULD_SPILL,
       MAX(a.AVG_LINE_TODAY) AS AVG_LINE_TODAY, MAX(a.AVG_LINE_NEW) AS AVG_LINE_NEW,
       MAX(a.MAX_LINE_NEW) AS MAX_LINE_NEW
FROM per_run p
JOIN per_route a ON a.ROUTE_ID = p.ROUTE_ID
LEFT JOIN {_O}ALERT_ROUTES r ON r.ROUTE_ID = p.ROUTE_ID
GROUP BY p.ROUTE_ID, r.INTEGRATION_NAME
ORDER BY RUNS_THAT_WOULD_SPILL DESC, PEAK_EVENTS_ONE_RUN DESC;

-- P164.4 Cadence and routes: TASK_ALERT_NOTIFY started (state = started, predecessor TASK_ALERT_SCAN), and the
--        enabled routes the re-post can reach (a route re-posts only events it delivered itself).
SHOW TASKS LIKE 'TASK_ALERT_NOTIFY' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
SELECT ROUTE_ID, FAMILY, MIN_SEVERITY, INTEGRATION_NAME, COALESCE(COMPANY_FILTER, 'ALL') AS COMPANY_FILTER,
       DELIVER_DIGEST
FROM {_O}ALERT_ROUTES
WHERE ENABLED
ORDER BY ROUTE_ID;
"""
assert PF_WHERE.count("AND e.ESCALATED_AT IS NULL") == 0 and ":" not in re.sub(r"'[^']*'", "", PF_WHERE)
assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", PREFLIGHT) and PREFLIGHT.isascii()

# ===================================================================================================
# Optional READ-ONLY RUN_NEXT PART B grids (V164.1-V164.4). GET_DDL fragments are quote-, backslash- and
# newline-free (tests/migrations/test_v164_*.py checks each is in the proc and tells V164 from V064).
# ===================================================================================================
PART_B_PRESENT = (" | event ", "ESCALATED_AT", "escalation_failed", "escalation_email_failed",
                  "CRITICAL(s) escalated", "INTEGRATION(TRIM(:esc_email))", "ALERT_AUDIT a",
                  "max_batches INT DEFAULT 6")
PART_B_ABSENT = ("LEFT(e.TITLE, 140),",)
_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_NOTIFY_WEBHOOK()')"
_present = "\n           AND ".join(f"CONTAINS({_DDL}, '{f}')" for f in PART_B_PRESENT)
_absent = "\n           AND ".join(f"NOT CONTAINS({_DDL}, '{f}')" for f in PART_B_ABSENT)
PART_B = f"""-- PART B -- V164 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.
-- V164.1 / V164.2 now; V164.3 after the next hourly chain (TASK_LOAD_HOURLY :07 Central, then scan, then notify);
-- V164.4 by eye on the next Teams card.
SELECT 'V164.1a ALERT_EVENTS.ESCALATED_AT exists' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = 'ALERT_EVENTS'
               AND COLUMN_NAME = 'ESCALATED_AT') = 1,
           'OK', 'FAIL: the column is missing -- V164 did not finish') AS RESULT
UNION ALL
SELECT 'V164.1b ESCALATE_AFTER_MIN seeded (value)',
       COALESCE((SELECT MAX(VALUE) FROM {_O}SETTINGS WHERE KEY = 'ESCALATE_AFTER_MIN'), 'FAIL: no row')
UNION ALL
SELECT 'V164.1c ESCALATE_EMAIL_INTEGRATION seeded (value; blank = no email leg)',
       COALESCE((SELECT '[' || MAX(VALUE) || ']' FROM {_O}SETTINGS WHERE KEY = 'ESCALATE_EMAIL_INTEGRATION'),
                'FAIL: no row')
UNION ALL
SELECT 'V164.2 SP_NOTIFY_WEBHOOK is the V164 definition',
       IFF({_present}
           AND {_absent},
           'OK', 'FAIL: the stored proc is not V164 -- re-run the V164 CREATE')
UNION ALL
SELECT 'V164.1d SCHEMA_VERSION has 164',
       IFF((SELECT COUNT(*) FROM {_O}SCHEMA_VERSION WHERE VERSION = 164) = 1, 'OK', 'FAIL: V164 did not finish');

-- V164.3 (after the next hourly chain) the notifier ran the pass. expect: STATE SUCCEEDED and RETURN_VALUE ending
--        '... CRITICAL(s) escalated' (plus '; escalation off (ESCALATE_AFTER_MIN)' when set to 0).
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, RETURN_VALUE, LEFT(ERROR_MESSAGE, 200) AS ERROR_TEXT
FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
       SCHEDULED_TIME_RANGE_START => DATEADD('hour', -3, CURRENT_TIMESTAMP()),
       TASK_NAME => 'TASK_ALERT_NOTIFY'))
ORDER BY SCHEDULED_TIME DESC;
--        expect: no escalation_failed and no escalation_email_failed row (route_send_failed = a route refusing).
SELECT ERROR_TYPE, COUNT(*) AS N, MAX(LOGGED_AT) AS LAST_AT, ANY_VALUE(LEFT(ERROR_MESSAGE, 200)) AS SAMPLE_MSG
FROM {_O}APP_ERROR_LOG
WHERE PAGE = 'NotifyWebhook' AND LOGGED_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP())
GROUP BY ERROR_TYPE
ORDER BY N DESC;
--        expect: ESCALATE_AUDIT_ROWS = ESCALATED_EVENTS, and the events are PREFLIGHT P164.2's WOULD_SEND_FIRST_RUN
--        rows (fewer if someone acknowledged in between).
SELECT (SELECT COUNT(*) FROM {_O}ALERT_AUDIT WHERE ACTION = 'ESCALATE') AS ESCALATE_AUDIT_ROWS,
       (SELECT COUNT(*) FROM {_O}ALERT_EVENTS WHERE ESCALATED_AT IS NOT NULL) AS ESCALATED_EVENTS,
       (SELECT MAX(ESCALATED_AT) FROM {_O}ALERT_EVENTS) AS LAST_ESCALATED_AT;

-- V164.4 (manual) the next Teams card shows lines as '[SEV] <title> | <company> | <detail> | event <id>'. A full
--        end-to-end check: leave the next monthly OPS_ALERT_DRILL CRITICAL unacknowledged for 2 hours (only when
--        P164.2 was empty) -- expect one 'OVERWATCH ESCALATION' Teams post and one email to DEFAULT_RECIPIENTS.
"""
assert not re.search(r"^\s*CALL\b", PART_B, re.M) and not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", PART_B)
for _frag in (*PART_B_PRESENT, *PART_B_ABSENT):
    assert not set(_frag) & {"'", "\\", "\n", "\r"}, _frag

_pf_out = os.environ.get("PREFLIGHT_OUT")
if _pf_out:
    Path(_pf_out).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
_pb_out = os.environ.get("PART_B_OUT")
if _pb_out:
    Path(_pb_out).write_text(PART_B, encoding="utf-8", newline="\n")

print(f"V164 written: {target} ({len(out)} bytes)")
