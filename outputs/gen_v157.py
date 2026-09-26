#!/usr/bin/env python3
"""Forward-generate V157: the single wave-2 re-derivation of both alert scans (Next-Fifty wave 2b).

Ranks 10a/b/d + 13 + 2 (CALL arm) + 12c, merged onto the CURRENT definer of both procs, V141
(tests/test_proc_lineage.py). Reads V141__alert_cadence_daily_cost_rules.sql ONLY and applies
anchored deltas, each asserted count == 1, in this order:

  SP_ALERT_SCAN (hourly)
    H0  (rework D12) the Central hour read ONCE into ct_hour (DECLARE DEFAULT 5 = inside both slots, so a
              failed read fails OPEN to hourly) -- the only input of every cadence gate below
    H1  (10d) delete the dead arm [15] (its rule was deleted at V034; the scan's only QUERY_HISTORY read)
    H1b (rework D3) delete arm [11] COST_CLOUD_SVC_RATIO -- the rule is RETIRED at file level (V150's
              COST_CLOUD_SVC_ANOMALY supersedes it); the scan's only WAREHOUSE_METERING_HISTORY read
    H2  (12c) arm [10] recurrence fix (H2a/H2b/H2d/H2c): a row closed for an EARLIER expiry (the date at the
              head of its DETAIL -- any kind, incl. a human ACTIONED/NOISE/EXPECTED, however late) or
              machine-closed (CONDITION_ENDED / SUPERSEDED) no longer blocks a rotated credential's next expiry
              cycle -- the key is unchanged, the DETAIL date is pinned to Central; never mint EXPIRING while the
              EXPIRED event is live
    H7  (rework D1/D2) arms [10] SEC_CRED_EXPIRY and [20] SEC_NEW_EXPOSURE run only in the 4-hourly security
              slot, IF (MOD(ct_hour, 4) = 1) around each UNCHANGED arm (01,05,09,13,17,21 Central)
    H3  (10a + 2 + rework D8) insert [22] OPS_PIPELINE_DEGRADED inside IF (MOD(ct_hour, 3) = 2)
              (02,05,...,23 Central; the arm text is byte-identical to the daily copy) + the ungated [23]
              PIPE_ETL_CYCLE add-on (SP_SCAN_ETL_CYCLE gates itself, V156) before the self-alert
    H8  (rework tally) self-alert denominator 13 -> 12 (V141's 13 - [15] - [11] + [22])
    H4  (12c C1) scope the V091 auto-clear sweep to its 3 PERF rules (applied BEFORE H5)
    H5  (12c + rework D9) insert the condition-ended sweep after the V091 sweep; each rule's clear sits in
              its raise arm's security-slot gate, still behind its EXISTS-OPEN gate
    H6  (10b + rework D10) [hb] heartbeat as ONE point UPDATE (an INSERT only when no row matched) + the
              new RETURN (12)
  SP_ALERT_SCAN_DAILY (no cadence gate: it runs once a day)
    D1  insert [22] (byte-identical to the hourly copy) + [24] COST_IDLE_OPPORTUNITY before [17]
    D2  self-alert denominator 9 -> 11
    D3  [hb] heartbeat (the same point-UPDATE shape) + the new RETURN

File order: guard (-20157, v < 156) -> ALERT_CONFIG seed (WHEN NOT MATCHED only) -> hourly marker + proc
-> daily marker + proc -> AUTO_CLEAR opt-in UPDATE (AFTER both procs) -> COST_CLOUD_SVC_RATIO retirement (the
V034 house pattern: close its lingering events as EXPECTED, delete its rule row) -> SCHEMA_VERSION 157.
No CALL of any raiser. Everything else in both V141 bodies stays byte-identical
(tests/migrations/test_v157_alert_scan_self_watch_idle_push.py normalizes each back to V141).

When PREFLIGHT_OUT is set, also writes PREFLIGHT_WAVE2B.sql: the [24] CTE chain as a plain read-only
SELECT that lists exactly which warehouses get COST_IDLE_OPPORTUNITY on the first daily run. The
byte-identity test never sets it.

Run: python outputs/gen_v157.py
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V141__alert_cadence_daily_cost_rules.sql"
V141 = BASE.read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str) -> str:
    assert text.count(old) == 1, f"{label}: expected exactly 1 anchor, got {text.count(old)}"
    return text.replace(old, new, 1)


hourly = extract_proc(V141, "SP_ALERT_SCAN()")
daily = extract_proc(V141, "SP_ALERT_SCAN_DAILY()")
assert hourly.count("fails := fails + 1") == 13 and daily.count("fails := fails + 1") == 9

# ---------------------------------------------------------------------------------------------------
# Cadence gates (wave-2b rework, owner decisions D1/D2/D8/D9/D12). ONE Central-hour read per run of the
# hourly scan, then a cheap MOD test around each gated block: a skipped block compiles nothing.
# ---------------------------------------------------------------------------------------------------
HOUR_EXPR = "HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP()))"
SEC_GATE = "MOD(ct_hour, 4) = 1"        # [10], [20] and their condition-ended clears: 01,05,09,13,17,21
OPS_GATE = "MOD(ct_hour, 3) = 2"        # [22] in the hourly scan: 02,05,08,11,14,17,20,23
HOUR_DEFAULT = 5                        # inside BOTH slots: a failed hour read fails OPEN (every arm runs)
assert HOUR_DEFAULT % 4 == 1 and HOUR_DEFAULT % 3 == 2


def _gate(expr: str, label: str, block: str) -> str:
    """Wrap an UNCHANGED block (never re-indented, so it stays byte-identical to its source) in a gate."""
    assert block.endswith("    END;\n") and block.startswith("    "), label
    return (f"    IF ({expr}) THEN   -- V157 cadence gate: {label}\n" + block
            + f"    END IF;   -- /V157 cadence gate: {label}\n")


_SEC_SLOTS = "every 4h (01,05,09,13,17,21 Central)"
_OPS_SLOTS = "every 3h (02,05,08,11,14,17,20,23 Central)"

HOUR_BLOCK = f"""\
    -- [cadence] V157 compile diet (Next-Fifty wave 2b rework): the Central hour this run started in, read
    -- ONCE. A gated block skips its whole statement -- nothing compiles, no ACCOUNT_USAGE read -- and a
    -- gated-off arm counts as ok (it never touches :fails):
    --   {SEC_GATE}  (01,05,09,13,17,21 Central): arms [10] SEC_CRED_EXPIRY and [20] SEC_NEW_EXPOSURE,
    --                        and each rule's condition-ended clear (a clear rides its raise arm's slot);
    --   {OPS_GATE}  (02,05,08,11,14,17,20,23 Central): [22] OPS_PIPELINE_DEGRADED (the daily scan's
    --                        copy stays daily).
    -- TASK_LOAD_HOURLY fires at :07 Central (CRON, DST-aware), so each slot is one run a day (a DST night can
    -- repeat or skip one slot; the dedupe keys absorb a repeat). If this read ever fails, ct_hour keeps its
    -- DEFAULT {HOUR_DEFAULT} -- inside BOTH slots -- so every gated block runs (fail-open to hourly) and the
    -- failure is logged (cadence_gate_failed). Does NOT touch :fails.
    BEGIN
        SELECT {HOUR_EXPR} INTO :ct_hour;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'cadence_gate_failed', :emsg,
                   'V157 Central-hour read - every gated block runs this pass', CURRENT_ROLE();
    END;

"""
HOUR_DECL = (f"    ct_hour INT DEFAULT {HOUR_DEFAULT};   -- V157: the Central hour of this run ([cadence] below); "
             f"{HOUR_DEFAULT} sits in both slots\n")

# ---------------------------------------------------------------------------------------------------
# Inserted blocks
# ---------------------------------------------------------------------------------------------------

# [22] -- identical text in BOTH procs (shared dedupe keys: whichever graph is alive raises it once).
ARM_22 = """\
    -- [22] OPS_PIPELINE_DEGRADED (V157, Next-Fifty #10: OVERWATCH watches its own pipeline from inside BOTH
    --      task graphs. Byte-identical in SP_ALERT_SCAN and SP_ALERT_SCAN_DAILY with shared dedupe keys, so
    --      whichever graph is still alive raises each finding once. (a) STALE: a SOURCE_FRESHNESS_STATE row
    --      past the shared name-rule cadence (DAILY/METERING in the name 30h, else 3h -- the app health strip,
    --      Admin, Control Room and NATIVE_ALERT_STALE_FACTS judge it the same way), including the scans' own
    --      heartbeat rows ALERT_SCAN_HOURLY / ALERT_SCAN_DAILY -- at most one event per source per last-load
    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged and swallowed
    --      (its task still reads SUCCEEDED) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS -- one
    --      event per (type, source, Central day). The three OPTIONAL SP_LOAD_MARTS_V27 arm sources (tag
    --      coverage, task node, AI usage) are left to the STALE leg, so a persistently failing optional arm
    --      raises once per episode instead of every day. (c) NOTIFY: SP_NOTIFY_WEBHOOK has not acquired its
    --      sender lease for 3h while a delivery route is enabled (V064 stamps OW_SENDER_LEASE.ACQUIRED_AT at
    --      the start of every run that acquires the lease). Clocks are pinned to Central -- every NTZ stamp
    --      read here is Central wall-clock -- and each free-text value is LEFT()-bounded to its column.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        fresh AS (
            SELECT SOURCE_NAME, LAST_LOAD_TS, STATUS,
                   DATEDIFF('minute', LAST_LOAD_TS,
                            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) AS AGE_MIN,
                   IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIM_H
            FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
        ),
        errs AS (
            SELECT ERROR_TYPE, SPLIT_PART(COALESCE(CONTEXT, ''), ' ', 1) AS SRC,
                   TO_DATE(LOGGED_AT) AS ERR_DAY, COUNT(*) AS N,
                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG
            FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            WHERE ERROR_TYPE IN ('mart_load_failed', 'fact_load_failed', 'extract_load_failed',
                                 'cloud_svc_mart_failed', 'object_cost_load_failed')
              AND LOGGED_AT >= DATEADD('hour', -24,
                                       CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
              AND SPLIT_PART(COALESCE(CONTEXT, ''), ' ', 1) NOT IN ('MART_TAG_COVERAGE_DAILY', 'MART_TASK_NODE_DAILY', 'FACT_AI_USAGE_DAILY')
            GROUP BY 1, 2, 3
        ),
        lease AS (
            SELECT ACQUIRED_AT,
                   DATEDIFF('minute', ACQUIRED_AT,
                            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) AS AGE_MIN
            FROM DBA_MAINT_DB.OVERWATCH.OW_SENDER_LEASE
            WHERE LEASE_NAME = 'SP_NOTIFY_WEBHOOK'
              AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r WHERE r.ENABLED)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT(f.SOURCE_NAME || IFF(f.LAST_LOAD_TS IS NULL, ' has never loaded',
                   ' is stale: ' || FLOOR(f.AGE_MIN / 60) || 'h'
                   || IFF(MOD(f.AGE_MIN, 60) > 0, ' ' || MOD(f.AGE_MIN, 60) || 'm', '')
                   || ' since its last load (limit ' || ROUND(f.LIM_H) || 'h)'), 300),
               LEFT(IFF(f.SOURCE_NAME IN ('ALERT_SCAN_HOURLY', 'ALERT_SCAN_DAILY'),
                   'Heartbeat of ' || IFF(f.SOURCE_NAME = 'ALERT_SCAN_HOURLY',
                       'SP_ALERT_SCAN (hourly graph TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> TASK_ALERT_SCAN)',
                       'SP_ALERT_SCAN_DAILY (daily graph TASK_LOAD_DAILY -> TASK_NIGHTLY_RECONCILE -> TASK_ALERT_SCAN_DAILY)')
                   || ': the scan stopped, or its heartbeat stamp failed (APP_ERROR_LOG scan_heartbeat_failed). '
                   || 'SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH -- a root auto-suspends after 10 consecutive '
                   || 'failures (V071).',
                   'Loader-owned freshness row (SOURCE_FRESHNESS_STATE, status ' || COALESCE(f.STATUS, '—')
                   || '). A stalled loader, a suspended task or a failed arm leaves this row behind while '
                   || 'TASK_HISTORY still reads SUCCEEDED. Admin > Migrations & freshness > Diagnose stale sources; '
                   || 'snowflake/loader_chain_check.sql.'), 2000),
               ROUND(f.AGE_MIN / 60.0, 1),
               c.RULE_ID || '|STALE|' || f.SOURCE_NAME || '|' || COALESCE(TO_VARCHAR(TO_DATE(f.LAST_LOAD_TS)), 'NEVER')
        FROM cfg c
        JOIN fresh f
          ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
         AND (f.LAST_LOAD_TS IS NULL OR f.AGE_MIN / 60.0 > f.LIM_H)
        UNION ALL
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT(x.ERROR_TYPE || ': ' || x.SRC || ' failed ' || x.N || 'x on ' || TO_VARCHAR(x.ERR_DAY), 300),
               LEFT('The loader logged this and returned normally, so its task still reads SUCCEEDED and readers '
                   || 'keep the previous fill. Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '
                   || COALESCE(LEFT(x.LAST_MSG, 600), '—') || '. Admin > Errors & telemetry (persisted error log).', 2000),
               x.N,
               c.RULE_ID || '|ERR|' || x.ERROR_TYPE || '|' || x.SRC || '|' || TO_VARCHAR(x.ERR_DAY)
        FROM cfg c
        JOIN errs x ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
        UNION ALL
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT('Alert notifier idle: SP_NOTIFY_WEBHOOK ' || IFF(l.ACQUIRED_AT IS NULL, 'has never run',
                   'has not started a run in ' || FLOOR(l.AGE_MIN / 60) || 'h'
                   || IFF(MOD(l.AGE_MIN, 60) > 0, ' ' || MOD(l.AGE_MIN, 60) || 'm', ''))
                   || ' while a delivery route is enabled', 300),
               LEFT('TASK_ALERT_NOTIFY runs after TASK_ALERT_SCAN and stamps OW_SENDER_LEASE.ACQUIRED_AT at the start '
                   || 'of every run that acquires the lease (V064). Teams/webhook delivery has stopped: SHOW TASKS '
                   || 'LIKE ''TASK_ALERT_NOTIFY'' IN SCHEMA DBA_MAINT_DB.OVERWATCH; fix the cause, then ALTER TASK '
                   || '... RESUME.', 2000),
               ROUND(l.AGE_MIN / 60.0, 1),
               c.RULE_ID || '|NOTIFY|' || COALESCE(TO_VARCHAR(TO_DATE(l.ACQUIRED_AT)), 'NEVER')
        FROM cfg c
        JOIN lease l
          ON c.RULE_ID = 'OPS_PIPELINE_DEGRADED'
         AND (l.ACQUIRED_AT IS NULL OR l.AGE_MIN > 180)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule OPS_PIPELINE_DEGRADED - other rules unaffected', CURRENT_ROLE();
    END;
"""

# [23] -- the rank-2 add-on slot: CALLs SP_SCAN_ETL_CYCLE (created by V156). NOT counted.
ARM_23 = """\
    -- [23] PIPE_ETL_CYCLE  (V157, Next-Fifty #2; optional external-dependency add-on: NOT counted toward the
    -- core scan-health tally, because SP_SCAN_ETL_CYCLE (V156) reads the customer Informatica CONTROL_STATUS
    -- table SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert. The scan
    -- raises PIPE_ETL_CYCLE_LATE / PIPE_ETL_CYCLE_NOT_STARTED / PIPE_ETL_TASK_FAILED itself. It runs BEFORE the
    -- supersede + snooze carry-forward sweeps below, so a WARN->CRIT->EXH escalation and a snoozed re-raise
    -- are settled in the same pass.)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (same contract as the daily scan's PIPE_REF_GAP /
            -- DQ_RECON_ERROR add-ons, V129/V137): logged, never fatal, self-heals when the grant lands.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'etl_cycle_scan_failed', LEFT(:emsg, 2000),
                   'rules PIPE_ETL_CYCLE_LATE / PIPE_ETL_CYCLE_NOT_STARTED / PIPE_ETL_TASK_FAILED - optional external add-on; needs SELECT on CONTROL_STATUS', CURRENT_ROLE();
    END;
"""

# #12c condition-ended sweep (after the V091 sweep, before the V117 carry-forward). NOT counted. Rework D9:
# each rule's clear (its probe + UPDATE, unchanged) sits inside its raise arm's security-slot gate.
CE_HEAD = """\
    -- [condition-ended sweep] V157 (Next-Fifty #12c): resolve a still-OPEN SECURITY event whose
    -- underlying STATE has ended, re-verified against the SAME ACCOUNT_USAGE source its raise arm
    -- reads -- a state check, not the V091 metric hysteresis above (which V157 scopes to its 3 PERF
    -- rules). Opt-in per rule (ALERT_CONFIG.ENABLED AND AUTO_CLEAR_ENABLED; V157 seeds it for these
    -- two rules). OPEN only: an ACK is a human working it, an active SNOOZE and every RESOLVED row are
    -- never touched. >=1h dwell (anti-flap; also covers the ACCOUNT_USAGE lag the raise arm already
    -- tolerates). A clear needs POSITIVE evidence:
    --   SEC_CRED_EXPIRY  -- no CREDENTIALS row for the event's (USER_NAME, NAME) is still expiring
    --                       inside GREATEST(THRESHOLD_NUM, COALESCE(CLEAR_THRESHOLD_NUM,
    --                       THRESHOLD_NUM)) days (rotated or removed). The clear window is never
    --                       narrower than the raise window, so arm [10] cannot re-raise what this
    --                       clears; an empty CREDENTIALS read or a NULL threshold never clears.
    --   SEC_NEW_EXPOSURE -- the event's (PRIVILEGE, GRANTED_ON, CREATED_ON) PUBLIC grant batch still
    --                       has rows in GRANTS_TO_ROLES and EVERY one carries DELETED_ON (a key that
    --                       matches no row never clears; batches older than 400 days are not
    --                       re-read, so such an event stays for a human).
    -- Keys are rebuilt with arm [10] / [20]'s exact expressions, never parsed out of DEDUPE_KEY, so a
    -- pipe inside a user/credential/object name cannot mis-split. Each rule is its own isolated block
    -- and skips its ACCOUNT_USAGE read entirely unless it has an OPEN event and is opted in (one EXISTS
    -- with a join -- no nested scalar subquery). The machine close CONDITION_ENDED is excluded from
    -- precision/MTTR in the app read-path like SUPERSEDED/AUTO_CLEARED/SNOOZE_SUPPRESSED.
    -- Cadence (compile diet): each rule's clear runs only in the slot its raise arm runs (MOD(ct_hour, 4) = 1),
    -- still behind its EXISTS-OPEN gate. Off-slot hours skip the probe and the ACCOUNT_USAGE read alike, so a
    -- clear lands in the first security slot after the evidence shows (up to ~4h later). An event raised in a
    -- slot is re-checked no sooner than the next one, so the 1h dwell always holds.
    -- Does NOT touch :fails.
"""
CE_CRED = """\
    BEGIN
        IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                    WHERE e.RULE_ID = 'SEC_CRED_EXPIRY' AND e.STATUS = 'OPEN'
                      AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
               SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
             WHERE ev.STATUS = 'OPEN'
               AND ev.RULE_ID = 'SEC_CRED_EXPIRY'
               AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                                  WHERE ENABLED AND AUTO_CLEAR_ENABLED AND THRESHOLD_NUM IS NOT NULL)
               AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
               AND EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS)   -- an empty read never clears
               AND ev.DEDUPE_KEY NOT IN (
                   -- arm [10]'s key for every credential STILL expiring inside the clear window (both bands)
                   SELECT k.DEDUPE_KEY
                   FROM (
                       SELECT c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || bd.BAND AS DEDUPE_KEY
                       FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
                       JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr
                         ON c.RULE_ID = 'SEC_CRED_EXPIRY'
                        AND cr.EXPIRATION_DATE IS NOT NULL
                        AND cr.EXPIRATION_DATE <= DATEADD('day',
                                GREATEST(c.THRESHOLD_NUM, COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM)),
                                CURRENT_TIMESTAMP())
                       CROSS JOIN (SELECT 'EXPIRING' AS BAND UNION ALL SELECT 'EXPIRED') bd
                   ) k
                   WHERE k.DEDUPE_KEY IS NOT NULL
               );
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'condition_ended_sweep_failed', :emsg, 'V157 condition-ended sweep SEC_CRED_EXPIRY - other rules unaffected', CURRENT_ROLE();
    END;
"""
CE_EXPO = """\
    BEGIN
        IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                    WHERE e.RULE_ID = 'SEC_NEW_EXPOSURE' AND e.STATUS = 'OPEN'
                      AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
               SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
             WHERE ev.STATUS = 'OPEN'
               AND ev.RULE_ID = 'SEC_NEW_EXPOSURE'
               AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                                  WHERE ENABLED AND AUTO_CLEAR_ENABLED)
               AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
               AND ev.DEDUPE_KEY IN (
                   -- arm [20]'s key for every PUBLIC grant batch that still exists and is now FULLY revoked
                   SELECT 'SEC_NEW_EXPOSURE' || '|' || g.PRIVILEGE || '|' || g.GRANTED_ON || '|' || TO_VARCHAR(g.CREATED_ON)
                   FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
                   WHERE g.GRANTEE_NAME = 'PUBLIC'
                     AND g.CREATED_ON >= DATEADD('day', -400, CURRENT_TIMESTAMP())
                   GROUP BY g.PRIVILEGE, g.GRANTED_ON, g.CREATED_ON
                   HAVING COUNT_IF(g.DELETED_ON IS NULL) = 0
               );
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'condition_ended_sweep_failed', :emsg, 'V157 condition-ended sweep SEC_NEW_EXPOSURE - other rules unaffected', CURRENT_ROLE();
    END;
"""
CE_BLOCK = (CE_HEAD
            + _gate(SEC_GATE, "SEC_CRED_EXPIRY clear rides arm [10] " + _SEC_SLOTS, CE_CRED)
            + _gate(SEC_GATE, "SEC_NEW_EXPOSURE clear rides arm [20] " + _SEC_SLOTS, CE_EXPO))


def _heartbeat(source: str, total: int, status_head: str, status_tail: str, graph: str) -> str:
    status = f"'{status_head}' || ({total} - :fails) || '/{total} rule blocks ok{status_tail}'"
    return f"""\
    -- [hb] scan heartbeat (V157, Next-Fifty #10b): stamp this scan's own SOURCE_FRESHNESS_STATE row LAST --
    -- after every arm and sweep -- so a row older than its cadence means the scan stopped (or this stamp
    -- keeps failing: APP_ERROR_LOG scan_heartbeat_failed). The {graph} graph's [22] arm, the app freshness
    -- boards and NATIVE_ALERT_STALE_FACTS read it with the shared name rule (ALERT_SCAN_HOURLY -> 3h,
    -- ALERT_SCAN_DAILY -> 30h). LAST_LOAD_TS is Central wall-clock like every loader stamp. Cheapest shape:
    -- ONE point UPDATE of this scan's own row; the INSERT runs only when it matched no row (the first run,
    -- or after the row was deleted), so the stamp self-heals. Isolated; does NOT touch :fails.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
           SET LAST_LOAD_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
               ROW_COUNT = ({total} - :fails),
               SNAPSHOT_TS = CURRENT_TIMESTAMP(),
               GENERATION = COALESCE(GENERATION, 0) + 1,
               STATUS = {status}
         WHERE SOURCE_NAME = '{source}';
        IF (SQLROWCOUNT = 0) THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
            SELECT '{source}', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
                   ({total} - :fails), 1, {status};
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'scan_heartbeat_failed', :emsg,
                   '{source} heartbeat stamp - alerts unaffected', CURRENT_ROLE();
    END;

"""


HB_HOURLY = _heartbeat("ALERT_SCAN_HOURLY", 12, "alert scan ", "", "daily")
HB_DAILY = _heartbeat("ALERT_SCAN_DAILY", 11, "alert scan daily ", " (daily)", "hourly")

# [24] -- the rank-13 weekly idle-waste push (daily scan only).
ARM_24 = """\
    -- [24] COST_IDLE_OPPORTUNITY (V157, Next-Fifty #13: weekly idle-waste push closing the loop alert -> one
    --      ALTER -> change scan + SP_LEDGER_AUTOBOOK. DB-side twin of the Cost Intelligence > Optimization &
    --      Savings > Idle & sizing ACTIONABLE figure (insights.idle_advisor + remediation.tighten_suspend_plan):
    --      FLAGGED = >=20% idle AND >=1 idle credit; recoverable = idle minus one 60s resume tail per active
    --      metered hour; only a settings-VERIFIED timer (the newest WAREHOUSE_CONFIG_SNAPSHOT batch, <=36h
    --      old -- a warehouse missing from it, dropped or renamed, never raises) that is
    --      disabled (<=0) or above 60s. Trailing 14 COMPLETE Central days, run-rated over the days the mart
    --      covers (at least 7). One event per warehouse per ISO week (Monday, Central); the HIGH band (>= 5x
    --      threshold) re-fires mid-week and the V067 sweep supersedes the MED one.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        ),
        win AS (
            SELECT WAREHOUSE_NAME, DAY, BILLED_HOURS, ACTIVE_HOURS, CREDITS_TOTAL,
                   COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100) AS IDLE_CR
            FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
            CROSS JOIN clk
            WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY
              AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
        ),
        cov AS (
            SELECT COUNT(DISTINCT DAY) AS COVERED_DAYS FROM win
        ),
        idle AS (
            SELECT WAREHOUSE_NAME,
                   SUM(BILLED_HOURS) AS METERED_HOURS,
                   GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS,
                   SUM(CREDITS_TOTAL) AS TOTAL_CREDITS,
                   SUM(IDLE_CR) AS IDLE_CREDITS
            FROM win
            GROUP BY WAREHOUSE_NAME
            HAVING SUM(CREDITS_TOTAL) > 0
        ),
        scored AS (
            SELECT i.WAREHOUSE_NAME, i.TOTAL_CREDITS, i.IDLE_CREDITS, v.COVERED_DAYS,
                   ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,
                   GREATEST(i.IDLE_CREDITS
                            - GREATEST(COALESCE(i.METERED_HOURS, 0) - i.IDLE_HOURS, 0) * (60 / 3600.0)
                              * COALESCE(i.TOTAL_CREDITS / NULLIF(i.METERED_HOURS, 0), 0), 0) AS RECOVERABLE_CREDITS
            FROM idle i
            CROSS JOIN cov v
        ),
        newest AS (
            SELECT MAX(SNAPSHOT_AT) AS BATCH_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('hour', -36, CURRENT_TIMESTAMP())
        ),
        cur AS (
            SELECT s.WAREHOUSE_NAME, s.AUTO_SUSPEND, s.SNAPSHOT_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT s
            JOIN newest n ON s.SNAPSHOT_AT >= DATEADD('minute', -10, n.BATCH_AT)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(s.WAREHOUSE_NAME) ORDER BY s.SNAPSHOT_AT DESC) = 1
        ),
        opp AS (
            SELECT s.WAREHOUSE_NAME, s.TOTAL_CREDITS, s.IDLE_CREDITS, s.COVERED_DAYS, s.IDLE_PCT,
                   s.RECOVERABLE_CREDITS, w.AUTO_SUSPEND, w.SNAPSHOT_AT,
                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,
                   GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC
            FROM scored s
            JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(s.WAREHOUSE_NAME)
            WHERE s.COVERED_DAYS >= 7
              AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1
              AND w.AUTO_SUSPEND IS NOT NULL
              AND (w.AUTO_SUSPEND <= 0 OR w.AUTO_SUSPEND > 60)
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL'),
               IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', c.SEVERITY),
               LEFT(o.WAREHOUSE_NAME || ' idle waste ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo: AUTO_SUSPEND '
                   || IFF(o.AUTO_SUSPEND <= 0, 'disabled', o.AUTO_SUSPEND || 's') || ' -> ' || o.TARGET_SEC || 's', 300),
               LEFT('Trailing ' || o.COVERED_DAYS || ' complete day(s): ' || ROUND(o.IDLE_CREDITS, 1) || ' of '
                   || ROUND(o.TOTAL_CREDITS, 1) || ' credits burned in hours with zero queries (' || o.IDLE_PCT
                   || '% idle). After the ~60s resume tail per active hour ' || ROUND(o.RECOVERABLE_CREDITS, 1)
                   || ' credits are recoverable: ~$' || ROUND(o.MONTHLY_USD)::INT || '/mo at $' || ROUND(:credit_price, 2)
                   || '/credit (about the ACTIONABLE figure Optimize shows with a 14-day window; this alert uses 14 '
                   || 'complete days). Timer verified by the daily SHOW WAREHOUSES snapshot at '
                   || TO_VARCHAR(o.SNAPSHOT_AT, 'YYYY-MM-DD HH24:MI') || '. Fix: '
                   || IFF(REGEXP_LIKE(o.WAREHOUSE_NAME, '^[A-Za-z_][A-Za-z0-9_$]*$'),
                          'ALTER WAREHOUSE ' || UPPER(o.WAREHOUSE_NAME) || ' SET AUTO_SUSPEND = ' || o.TARGET_SEC || ';',
                          'the name needs quoting - generate the statement in Cost Intelligence > Optimization & Savings > Remediation & ledger.')
                   || IFF(o.AUTO_SUSPEND > 0,
                          ' The next daily change scan registers the lower timer and SP_LEDGER_AUTOBOOK books and settles the measured saving (a $0 closed-loop row booked from this alert is adopted as that booking, not duplicated).',
                          ' Enabling a timer on a never-suspend warehouse is not auto-booked (SP_LEDGER_AUTOBOOK books only a decrease from a positive timer) - book it in Cost Intelligence > Optimization & Savings > Remediation & ledger.'),
                   2000),
               o.MONTHLY_USD,
               c.RULE_ID || '|' || UPPER(o.WAREHOUSE_NAME) || '|'
                   || IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', 'MED') || '|'
                   || TO_VARCHAR(DATEADD('day', 1 - DAYOFWEEKISO(k.TODAY), k.TODAY))
        FROM cfg c
        JOIN opp o
          ON c.RULE_ID = 'COST_IDLE_OPPORTUNITY'
         AND o.MONTHLY_USD >= c.THRESHOLD_NUM
        CROSS JOIN clk k

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_IDLE_OPPORTUNITY - other rules unaffected', CURRENT_ROLE();
    END;
"""

for _blk in (HOUR_BLOCK, ARM_22, ARM_23, CE_BLOCK, HB_HOURLY, HB_DAILY, ARM_24):
    assert "$$" not in _blk, "a $$ inside a scripting body ends it"
    assert _blk.count("(") == _blk.count(")"), "unbalanced parentheses in an inserted block"
assert ARM_22.count("fails := fails + 1") == 1 and ARM_24.count("fails := fails + 1") == 1
for _blk in (HOUR_BLOCK, ARM_23, CE_BLOCK, HB_HOURLY, HB_DAILY):
    assert "fails := fails + 1" not in _blk, "add-ons, sweeps and heartbeats never count"
assert "SEC_BREAK_GLASS_USE" not in ARM_22 + ARM_23 + CE_BLOCK + HB_HOURLY
assert HB_HOURLY.endswith("    END;\n\n") and HB_DAILY.endswith("    END;\n\n")
assert CE_BLOCK.count(f"    IF ({SEC_GATE}) THEN   -- V157 cadence gate: ") == 2
assert CE_BLOCK.count("    END IF;   -- /V157 cadence gate: ") == 2
for _blk in (HB_HOURLY, HB_DAILY):
    assert "MERGE" not in _blk and _blk.count("IF (SQLROWCOUNT = 0) THEN") == 1   # D10: point UPDATE
assert "ct_hour" not in ARM_22 + ARM_23 + ARM_24 + HB_HOURLY + HB_DAILY           # gates sit AROUND arms

# ---------------------------------------------------------------------------------------------------
# SP_ALERT_SCAN (hourly) deltas
# ---------------------------------------------------------------------------------------------------
new_hourly = hourly

# H0 (rework D12): the Central hour, declared with its fail-open default and read ONCE, first thing after
# the SETTINGS read (both anchors unique in the hourly body).
_DECL_END = "    fails INT DEFAULT 0;\nBEGIN\n"
new_hourly = _swap(new_hourly, _DECL_END, _DECL_END.replace("BEGIN\n", HOUR_DECL + "BEGIN\n"), "H0 declare")
_WAKE = "    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n\n    -- [wake] V086:"
new_hourly = _swap(new_hourly, _WAKE, _WAKE.replace("\n\n    -- [wake]", "\n\n" + HOUR_BLOCK + "    -- [wake]"),
                   "H0 hour read")

# H1 (10d): the dead arm [15], exactly V141's slice.
_A15, _A17 = "    -- [15] SEC_BREAK_GLASS_USE\n", "    -- [17] COST_DEPT_BUDGET_PACE\n"
assert new_hourly.count(_A15) == 1 and new_hourly.count(_A17) == 1
ARM15 = new_hourly[new_hourly.index(_A15):new_hourly.index(_A17)]
assert ARM15.count("fails := fails + 1") == 1 and "ACCOUNT_USAGE.QUERY_HISTORY" in ARM15
new_hourly = _swap(new_hourly, ARM15, "", "H1 arm [15]")

# H1b (rework D3): arm [11] COST_CLOUD_SVC_RATIO, exactly V141's slice -- the rule is retired at file level
# (RETIRE below); V150's per-warehouse COST_CLOUD_SVC_ANOMALY supersedes the fixed ratio.
_A11, _A14 = "    -- [11] COST_CLOUD_SVC_RATIO\n", "    -- [14] PIPE_COPY_FAILURES\n"
assert new_hourly.count(_A11) == 1 and new_hourly.count(_A14) == 1
ARM11 = new_hourly[new_hourly.index(_A11):new_hourly.index(_A14)]
assert ARM11.count("fails := fails + 1") == 1 and "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY" in ARM11
new_hourly = _swap(new_hourly, ARM11, "", "H1b arm [11]")

# H2 (12c): arm [10]'s recurrence fix, four count==1 swaps. The DEDUPE_KEY is NOT changed (the V067/V096
# supersede token, the h-leg below and the condition-ended sweep rebuild it verbatim). Instead the arm
# carries the credential's EXPIRATION_DATE out of b, and a CLOSED row blocks only when it was raised for THIS
# expiry: its cycle id is the expiry date every arm [10] since V009 writes at the head of DETAIL ('Rotate
# before ' || TO_VARCHAR(<expiry>, 'YYYY-MM-DD') || ..., one projection for both bands -- verified for all 30
# definers V009..V157 by tests/migrations/test_v157_*). It never depends on WHEN a row was raised or closed,
# so a prior cycle resolved late (an ACK'd EXPIRED closed days after the rotation) or a credential whose
# lifetime is at most THRESHOLD_NUM days (a 7-day PAT) still re-alerts. A live row (RESOLVED_AT NULL)
# always blocks. H2a: the carried column after the key expression.
H2A_OLD = ("               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
           "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')\n"
           "        FROM cfg c\n"
           "        JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr\n")
H2A_NEW = ("               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
           "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING'),\n"
           "               cr.EXPIRATION_DATE    -- V157: EXP_TS (this cycle's expiry: the dedupe's cycle id, not "
           "inserted)\n"
           "        FROM cfg c\n"
           "        JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr\n")
new_hourly = _swap(new_hourly, H2A_OLD, H2A_NEW, "H2a arm [10] carried column")

# H2b: the derived table's alias list (unique through the raise-window line above it).
H2B_OLD = ("         AND cr.EXPIRATION_DATE <= DATEADD('day', c.THRESHOLD_NUM, CURRENT_TIMESTAMP())\n\n"
           "        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n")
H2B_NEW = ("         AND cr.EXPIRATION_DATE <= DATEADD('day', c.THRESHOLD_NUM, CURRENT_TIMESTAMP())\n\n"
           "        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY, EXP_TS)\n")
new_hourly = _swap(new_hourly, H2B_OLD, H2B_NEW, "H2b arm [10] alias list")

# H2d: the DETAIL writer's date -- the cycle id -- pinned to Central. Every scheduled scan already rendered it
# in the account TIMEZONE (America/Chicago); pinning it means a hand-run scan from a session in another
# timezone writes the SAME date, so H2c's match (pinned the same way) is exact for every row written from V157.
H2D_OLD = ("               'Rotate before ' || TO_VARCHAR(cr.EXPIRATION_DATE, 'YYYY-MM-DD') ||\n"
           "                   ' to avoid auth failures for jobs and integrations using this credential.',\n")
H2D_NEW = ("               -- V157: this date is the cycle id the dedupe below matches; pinned to Central so a hand-run "
           "scan in another\n"
           "               -- session timezone writes the same date the scheduled scans always have\n"
           "               'Rotate before ' || TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', cr.EXPIRATION_DATE)"
           "::TIMESTAMP_NTZ, 'YYYY-MM-DD') ||\n"
           "                   ' to avoid auth failures for jobs and integrations using this credential.',\n")
new_hourly = _swap(new_hourly, H2D_OLD, H2D_NEW, "H2d arm [10] DETAIL date pinned to Central")

# H2c: arm [10]'s dedupe tail (unique through its own error CONTEXT).
H2_TAIL = (
    "    EXCEPTION\n"
    "        WHEN OTHER THEN\n"
    "            emsg := SQLERRM;\n"
    "            fails := fails + 1;\n"
    "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG\n"
    "                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
    "            SELECT 'AlertScan', 'rule_block_failed', :emsg,\n"
    "                   'rule SEC_CRED_EXPIRY - other rules unaffected', CURRENT_ROLE();\n"
)
H2_OLD = "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n        );\n" + H2_TAIL
H2_NEW = (
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "              AND COALESCE(e.RESOLUTION_KIND, '') NOT IN ('CONDITION_ENDED', 'SUPERSEDED')   "
    "-- V157: a machine close never blocks\n"
    "              -- V157: the key has no date, so the cycle id is the expiry date every arm [10] since V009 writes "
    "at the head\n"
    "              -- of DETAIL (Rotate before YYYY-MM-DD, both bands). A CLOSED row (by anyone: ACTIONED, NOISE, "
    "EXPECTED, a bulk\n"
    "              -- clear, however late) blocks only when it was raised for THIS expiry, never by when it was "
    "raised or closed,\n"
    "              -- so a rotated credential's next expiry re-alerts. A live row (RESOLVED_AT NULL) always blocks.\n"
    "              AND (e.RESOLVED_AT IS NULL\n"
    "                   OR e.DETAIL LIKE ('Rotate before ' || TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', b.EXP_TS)"
    "::TIMESTAMP_NTZ, 'YYYY-MM-DD') || '%'))\n"
    "        )\n"
    "          -- V157: never mint EXPIRING while this credential's EXPIRED event is live (the supersede sweep "
    "would resolve it in the same run: hourly churn)\n"
    "          AND NOT EXISTS (\n"
    "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS h\n"
    "            WHERE b.DEDUPE_KEY LIKE '%|EXPIRING'\n"
    "              AND h.RULE_ID = b.RULE_ID\n"
    "              AND h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')\n"
    "              AND h.STATUS IN ('OPEN', 'ACK', 'SNOOZED')\n"
    "        );\n" + H2_TAIL
)
new_hourly = _swap(new_hourly, H2_OLD, H2_NEW, "H2c arm [10] recurrence")
# the cycle id the dedupe matches is exactly the date the writer puts in DETAIL (same expression, same pin)
_CYCLE_ID = "TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', cr.EXPIRATION_DATE)::TIMESTAMP_NTZ, 'YYYY-MM-DD')"
assert ("'Rotate before ' || " + _CYCLE_ID + " ||\n") in H2D_NEW
assert ("e.DETAIL LIKE ('Rotate before ' || " + _CYCLE_ID.replace("cr.EXPIRATION_DATE", "b.EXP_TS")
        + " || '%'))\n") in H2_NEW
assert "WIN_DAYS" not in new_hourly and "AND (e.RESOLVED_AT IS NULL\n" in H2_NEW

# H7 (rework D1/D2): arms [10] and [20] -- ACCOUNT_USAGE reads that need not run hourly ([20] is the scan's heaviest compile; CREDENTIALS; the
# 19 s GRANTS_TO_ROLES family) -- run in the 4-hourly security slot only. Each arm is wrapped WHOLE, never
# re-indented, so its text (incl. arm [10]'s H2 fix) is untouched inside the gate.
for _arm, _nxt, _lbl in (("    -- [10] SEC_CRED_EXPIRY\n", "    -- [14] PIPE_COPY_FAILURES\n", "[10] "),
                         ("    -- [20] SEC_NEW_EXPOSURE", "    -- [21] SEC_POSTURE_METRIC", "[20] ")):
    _blk = new_hourly[new_hourly.index(_arm):new_hourly.index(_nxt)]
    assert _blk.count("fails := fails + 1") == 1 and _blk.endswith("    END;\n"), _lbl
    new_hourly = _swap(new_hourly, _blk, _gate(SEC_GATE, _lbl + _SEC_SLOTS, _blk), "H7 gate " + _lbl)

# H3 (10a + 2): [22] + [23] directly above the OPS_SCAN_DEGRADED self-alert.
_SELF = "    IF (fails > 0) THEN\n"
# Rework D8: the hourly copy of [22] runs every 3rd Central hour -- the gate sits AROUND the unchanged arm, so
# [22] stays byte-identical to the daily copy (which runs every morning, ungated). [23] stays ungated: the
# CALL is one statement and SP_SCAN_ETL_CYCLE applies its own ETL-window gate (V156).
ARM_22_GATED = _gate(OPS_GATE, "[22] " + _OPS_SLOTS, ARM_22)
new_hourly = _swap(new_hourly, _SELF, ARM_22_GATED + ARM_23 + _SELF, "H3 self-alert anchor")

# H8 (rework tally): V141's 13 counting arms - [15] - [11] + [22] = 12.
_T13 = "               :fails || ' of 13 alert rule block(s) failed this run',\n"
new_hourly = _swap(new_hourly, _T13, _T13.replace(" of 13 ", " of 12 "), "H8 self-alert denominator")

# H4 (12c C1): scope the V091 sweep to the 3 PERF rules whose still-firing set it recomputes.
H4_OLD = ("           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
          "                              WHERE ENABLED AND AUTO_CLEAR_ENABLED)\n")
H4_LINE = ("           AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')   "
           "-- V157: only rules whose still-firing set this sweep recomputes; other opt-ins have their own "
           "clear sweep\n")
new_hourly = _swap(new_hourly, H4_OLD, H4_OLD + H4_LINE, "H4 V091 scope")

# H5 (12c): the condition-ended sweep between the V091 sweep and the V117 carry-forward.
H5_OLD = ("            SELECT 'AlertScan', 'autoclear_sweep_failed', :emsg, 'V091 auto-clear sweep - other "
          "rules unaffected', CURRENT_ROLE();\n    END;\n")
new_hourly = _swap(new_hourly, H5_OLD, H5_OLD + "\n" + CE_BLOCK, "H5 after the V091 sweep")
assert new_hourly.index(CE_BLOCK) < new_hourly.index("    -- [snooze carry-forward sweep] V117:")

# H6 (10b): heartbeat last, then the new RETURN.
H6_OLD = "    RETURN 'alert scan v11 (V091: + auto-clear sweep): ' || (13 - :fails) || '/13 rule blocks ok';\n"
H6_NEW = (HB_HOURLY
          + "    RETURN 'alert scan v12 (V157: + OPS_PIPELINE_DEGRADED self-watch + ETL-cycle add-on + "
            "condition-ended sweep + heartbeat, - dead break-glass arm, - retired cloud-services ratio arm, "
            "security arms every 4h, self-watch every 3h): ' || (12 - :fails) || '/12 rule blocks ok';\n")
new_hourly = _swap(new_hourly, H6_OLD, H6_NEW, "H6 hourly RETURN")

assert new_hourly.count("fails := fails + 1") == 12          # 13 - [15] - [11] + [22]
assert "' of 12 alert rule block(s) failed this run'" in new_hourly and " of 13 " not in new_hourly
assert "SEC_BREAK_GLASS_USE" not in new_hourly and "ACCOUNT_USAGE.QUERY_HISTORY" not in new_hourly
assert "COST_CLOUD_SVC_RATIO" not in new_hourly and "WAREHOUSE_METERING_HISTORY" not in new_hourly
# the gates: one hour read, 2 arm + 2 clear security gates, 1 [22] gate, all balanced, ct_hour never reassigned
assert new_hourly.count("INTO :ct_hour") == 1 and "ct_hour :=" not in new_hourly
assert new_hourly.count(f"IF ({SEC_GATE}) THEN") == 4 and new_hourly.count(f"IF ({OPS_GATE}) THEN") == 1
assert new_hourly.count("END IF;   -- /V157 cadence gate: ") == 5
# ct_hour is read ONLY by the 5 gate conditions (plus its declaration, the read and comments)
assert new_hourly.count("ct_hour") == (HOUR_DECL.count("ct_hour") + HOUR_BLOCK.count("ct_hour") + 5
                                       + CE_HEAD.count("ct_hour"))
assert new_hourly.index("INTO :ct_hour") < new_hourly.index("    -- [01] COST_DAILY_CREDITS")
assert ARM_22_GATED in new_hourly
assert new_hourly.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();") == 1

# ---------------------------------------------------------------------------------------------------
# SP_ALERT_SCAN_DAILY deltas
# ---------------------------------------------------------------------------------------------------
new_daily = daily
_REF = "    -- [17] PIPE_REF_GAP"
new_daily = _swap(new_daily, _REF, ARM_22 + ARM_24 + _REF, "D1 [17] anchor")
new_daily = _swap(new_daily, "' of 9 daily alert rule block(s) failed this run'",
                  "' of 11 daily alert rule block(s) failed this run'", "D2 daily denominator")
D3_OLD = ("    RETURN 'alert scan daily v2 (V141: storage-surge/serverless-creep/egress-spike moved off the hourly "
          "scan): ' || (9 - :fails) || '/9 rule blocks ok (daily)';\n")
D3_NEW = (HB_DAILY
          + "    RETURN 'alert scan daily v3 (V157: + OPS_PIPELINE_DEGRADED self-watch + COST_IDLE_OPPORTUNITY + "
            "heartbeat): ' || (11 - :fails) || '/11 rule blocks ok (daily)';\n")
new_daily = _swap(new_daily, D3_OLD, D3_NEW, "D3 daily RETURN")
assert new_daily.count("fails := fails + 1") == 11           # 9 + [22] + [24]
assert "AS DAILY_BURN" in new_daily                          # V064 burn carried (history lock test_rec10)
assert "ct_hour" not in new_daily and "cadence gate" not in new_daily   # the daily scan runs once a day: no gate
assert new_daily.count(ARM_22) == 1 and new_hourly.count(ARM_22) == 1  # [22] byte-identical in both scans

# ---------------------------------------------------------------------------------------------------
# File assembly
# ---------------------------------------------------------------------------------------------------
HEADER = """\
-- V157__alert_scan_self_watch_idle_push.sql
--
-- Next-Fifty wave 2b, ranks 10a/b/d + 13 + 2 (ETL-cycle add-on arm) + 12c: the SINGLE wave-2 re-derivation
-- of both alert scans, each from its CURRENT definition V141 (tests/test_proc_lineage.py).
--
--   SP_ALERT_SCAN (hourly):
--     - arm [15] removed: it keyed on a rule whose ALERT_CONFIG row was deleted at V034, so it joined no
--       row, yet it was the hourly scan's only ACCOUNT_USAGE.QUERY_HISTORY read;
--     - arm [11] removed and COST_CLOUD_SVC_RATIO RETIRED (owner decision, wave-2b rework): V150's
--       per-warehouse robust-z COST_CLOUD_SVC_ANOMALY (daily, SP_ANOMALY_SWEEP) supersedes the fixed
--       10/20% ratio; [11] was the hourly scan's only WAREHOUSE_METERING_HISTORY read. The file retires the
--       rule the V034 way (below the procs): its row goes, its OPEN/ACK/SNOOZED events close as EXPECTED;
--     ~ cadence gates (compile diet): the Central hour is read ONCE per run into ct_hour. Arms [10]
--       SEC_CRED_EXPIRY (3.9 s) and [20] SEC_NEW_EXPOSURE (19.4 s, the scan's heaviest compile; [14]
--       PIPE_COPY_FAILURES at 5.8 s stays hourly) -- run
--       only when MOD(ct_hour, 4) = 1 (01,05,09,13,17,21 Central); [22] only when MOD(ct_hour, 3) = 2
--       (02,05,08,11,14,17,20,23). Each gate wraps an UNCHANGED arm; a gated-off arm counts as ok. A failed
--       hour read keeps the DEFAULT 5 (inside both slots): every gated block runs, like before V157;
--     + [22] OPS_PIPELINE_DEGRADED (counting): pipeline self-watch -- a stale SOURCE_FRESHNESS_STATE row, a
--       loader failure that was logged and swallowed, or an idle alert notifier -- every 3rd hour here and
--       every morning in the daily scan;
--     + [23] PIPE_ETL_CYCLE add-on (NOT counting, ungated): runs SP_SCAN_ETL_CYCLE (V156), which applies its
--       own ETL-window gate; a CONTROL_STATUS grant gap logs etl_cycle_scan_failed and never trips
--       OPS_SCAN_DEGRADED;
--     + condition-ended sweep (#12c): an OPEN SEC_CRED_EXPIRY / SEC_NEW_EXPOSURE event resolves as
--       CONDITION_ENDED once ACCOUNT_USAGE shows the credential rotated/removed or the PUBLIC grant batch
--       fully revoked (OPEN only, 1h dwell, positive evidence only). Each rule's clear runs only in its raise
--       arm's 4-hourly slot;
--     ~ the V091 auto-clear sweep is scoped to its 3 PERF rules (the only rules whose still-firing set it
--       recomputes), so opting another rule into AUTO_CLEAR_ENABLED never blanket-clears it after 1h;
--     ~ arm [10]: a prior event closed for an EARLIER expiry (by anyone -- a human ACTIONED/NOISE/EXPECTED
--       resolve included, however late) or machine-closed (CONDITION_ENDED / SUPERSEDED) no longer blocks
--       the credential's key, so a rotated credential's next expiry cycle re-alerts. The key itself is
--       unchanged: the cycle id is the expiry date every arm [10] since V009 writes at the head of DETAIL
--       ('Rotate before YYYY-MM-DD', both bands), now pinned to Central on both the write and the match.
--       A live (OPEN/ACK/SNOOZED) event, or a close for this same expiry, still blocks; EXPIRING is never
--       minted while that credential's EXPIRED event is live;
--     + [hb] heartbeat: stamps SOURCE_FRESHNESS_STATE 'ALERT_SCAN_HOURLY' last in every run, as ONE point
--       UPDATE (an INSERT only when the row is missing: the first run, or after a delete).
--     Counting arms 13 -> 12 (13 - [15] - [11] + [22]); the self-alert and the RETURN say 12.
--   SP_ALERT_SCAN_DAILY:
--     + [22] OPS_PIPELINE_DEGRADED (byte-identical to the hourly copy; shared dedupe keys, so whichever
--       graph is alive raises each finding once);
--     + [24] COST_IDLE_OPPORTUNITY (counting): weekly idle-waste push, the DB-side twin of the Optimize
--       ACTIONABLE figure (net of the 60s resume tail, settings-verified timer from the newest SHOW
--       WAREHOUSES snapshot batch -- a dropped/renamed warehouse never raises -- 14 complete Central days);
--     + [hb] heartbeat 'ALERT_SCAN_DAILY' (the same point-UPDATE shape). Counting arms 9 -> 11. No cadence
--       gate: the daily scan runs once a day.
--   ALERT_CONFIG: OPS_PIPELINE_DEGRADED (PLATFORM, HIGH) and COST_IDLE_OPPORTUNITY (COST, MEDIUM, 100 USD/month,
--   HIGH band at 5x) are seeded WHEN NOT MATCHED only. SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE are opted into
--   auto-clear AFTER both procs are replaced (never before: V141's unscoped sweep would blanket-clear them).
--
-- Everything else in both V141 bodies is byte-identical (tests/migrations/test_v157_* normalizes each back
-- to V141): DECLARE (+ ct_hour), the SETTINGS read, [wake], the 11 surviving hourly arms ([10] carries its
-- recurrence fix; [10] and [20] sit unchanged inside their gates), both self-alerts (hourly literal now 12),
-- the V067/V115 supersede sweep, the V091 body except its one scope line, the V117 carry-forward, the daily
-- arms [06]-[19], the [17]/[18] add-ons and the V064 trailing-30-complete-day burn.
--
-- LATENCY TRADE (owner decisions D1/D2/D8/D9; the compile saving is the point): a new PUBLIC grant
-- (SEC_NEW_EXPOSURE) and a credential entering its window or expiring (SEC_CRED_EXPIRY -- the EXPIRED band is
-- CRITICAL and auto-declares an incident) surface up to ~4h later than an hourly check would raise them, on
-- top of ACCOUNT_USAGE's own lag; a CONDITION_ENDED clear lands up to ~4h after the evidence; the hourly [22]
-- self-watch reports a stale source or an idle notifier up to ~3h later (the daily scan's copy still runs
-- every morning). Every other hourly arm and sweep still runs every hour.
-- A condition that starts and ends between two checks is never raised at all: a PUBLIC grant revoked before
-- the next 4-hourly check (an exposure shorter than ~4h, after ACCOUNT_USAGE lag; V141 already missed ones
-- under ~1h) and a stale-source or idle-notifier episode that clears between two [22] slots (the ERR leg's
-- 24h lookback still catches every logged loader failure). GRANTS_TO_ROLES history and Security > Changes
-- still show such a grant.
--
-- FIRST RUN: at the first [22] slot (hourly scan) or daily run, every SOURCE_FRESHNESS_STATE row already past
-- its cadence raises one HIGH OPS_PIPELINE_DEGRADED event, and the first daily run raises this ISO week's
-- COST_IDLE_OPPORTUNITY events (preview with the
-- separate read-only PREFLIGHT_WAVE2B.sql). A credential already inside its expiry window whose only prior
-- SEC_CRED_EXPIRY event for that key was closed for an EARLIER expiry date (an earlier cycle, e.g.
-- human-resolved, however late) raises its previously suppressed event once (CRITICAL, and an auto-declared
-- incident, if already expired). A close for the SAME expiry date, or a still-live event, still suppresses
-- it. Known edge: a row written before V157 by a hand-run scan from a session in another timezone carries
-- that zone's date; if the expiry fell on a different calendar date there, the row reads as an earlier cycle
-- and the event re-raises once (a duplicate, not a missed alert). Deploy the app build that excludes
-- CONDITION_ENDED from the human-resolution metrics BEFORE applying (it shipped in wave 2a). No procedure runs
-- at apply time: the scans pick this up on their next scheduled run. Applying also closes every OPEN, ACK'd
-- or SNOOZED COST_CLOUD_SVC_RATIO event as EXPECTED and deletes that rule's ALERT_CONFIG row (history in
-- ALERT_EVENTS is kept).
-- ROLLBACK (order matters): FIRST, by hand (never inside a migration), switch AUTO_CLEAR_ENABLED off for
-- SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE; only THEN re-run V141's two procs (RUNBOOK section 12, "Rolling back
-- V157"). Reversed, an hourly scan landing between the two steps runs V141's unscoped V091 sweep, which
-- AUTO_CLEARs their OPEN events 1h after raise -- and V141's arms [10]/[20] never re-raise an auto-cleared key.
-- The retired COST_CLOUD_SVC_RATIO row stays deleted after a rollback (V141's arm [11] then joins no row, like
-- the old [15]); re-seed it by hand only if the fixed ratio is wanted back.
-- Apply AFTER V156 (SP_SCAN_ETL_CYCLE must exist for [23]; before it, the arm only logs
-- etl_cycle_scan_failed). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20157, 'V157 requires V156 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 156) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Next-Fifty #10 + #13: the two new rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered,
-- and AUTO_CLEAR_ENABLED keeps its default.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('OPS_PIPELINE_DEGRADED', 'PLATFORM', 'OVERWATCH pipeline self-watch: a telemetry source past its load cadence, a swallowed loader failure, or an idle alert notifier', TRUE, 'HIGH', 0, 24),
        ('COST_IDLE_OPPORTUNITY', 'COST', 'Weekly idle-waste opportunity: a settings-verified AUTO_SUSPEND tightening recovers at least the threshold in USD per month', TRUE, 'MEDIUM', 100, 336)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

"""

MARK_H = ("-- >>> derived:SP_ALERT_SCAN  (from V141; - dead break-glass arm [15], - retired COST_CLOUD_SVC_RATIO "
          "arm [11], [10]/[20] + their condition-ended clears every 4h, + [22] OPS_PIPELINE_DEGRADED every 3h, + [23] "
          "PIPE_ETL_CYCLE add-on, + condition-ended sweep, V091 sweep scoped to PERF, arm [10] recurrence fix, "
          "+ [hb], V157)\n")
MARK_D = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V141; + [22] OPS_PIPELINE_DEGRADED, + [24] "
          "COST_IDLE_OPPORTUNITY, + [hb], V157)\n")

OPT_IN = """\
-- Next-Fifty #12c: opt the two state-verified SECURITY rules into the condition-ended sweep. Placed AFTER
-- both procs on purpose: under V141's body the unscoped V091 sweep would resolve these rules' OPEN events
-- 1h after raise with no condition check. TRUE-only; this file never switches a flag off.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET AUTO_CLEAR_ENABLED = TRUE
 WHERE RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_EXPOSURE');
"""

RETIRE = """\
-- Owner decision (wave-2b rework): retire COST_CLOUD_SVC_RATIO. V150's per-warehouse robust-z
-- COST_CLOUD_SVC_ANOMALY (daily, SP_ANOMALY_SWEEP) supersedes the fixed 10/20% ratio, and arm [11] -- an hourly
-- WAREHOUSE_METERING_HISTORY read -- is gone from SP_ALERT_SCAN above. The house retire pattern (V034,
-- SEC_BREAK_GLASS_USE): the rule row goes and lingering events close as EXPECTED -- SNOOZED ones too, since the
-- hourly wake step would otherwise reopen an event no scan can ever close again. ALERT_EVENTS history is kept.
-- Row FIRST, then the events: once the row is gone no arm [11] joins a config row -- not even a scan that
-- started before this file and still runs V141's body -- so nothing can re-raise the rule after the close.
DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
 WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO';

UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',
       RESOLVED_AT = CURRENT_TIMESTAMP()
 WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO' AND STATUS IN ('OPEN', 'ACK', 'SNOOZED');
"""

DESCRIPTION = (
    "Next-Fifty wave 2b (ranks 10a/b/d, 13, 2, 12c) plus the wave-2b compile-diet rework: the single wave-2 "
    "re-derivation of SP_ALERT_SCAN and SP_ALERT_SCAN_DAILY from V141, byte-identical otherwise. Hourly: dead "
    "break-glass arm [15] removed (its rule was deleted at V034; it was the scan''s only "
    "ACCOUNT_USAGE.QUERY_HISTORY read); arm [11] removed and COST_CLOUD_SVC_RATIO retired (V150 "
    "COST_CLOUD_SVC_ANOMALY supersedes the fixed ratio; its OPEN, ACK and SNOOZED events close as EXPECTED and "
    "its ALERT_CONFIG row is deleted, the V034 pattern); cadence gates from one Central-hour read per run "
    "(fail-open default 5): arms [10] SEC_CRED_EXPIRY and [20] SEC_NEW_EXPOSURE and their condition-ended "
    "clears run when MOD(hour, 4) = 1 (01,05,09,13,17,21 Central), [22] when MOD(hour, 3) = 2, so those "
    "alerts and clears can land up to about 4h (3h for [22]) later; + [22] "
    "OPS_PIPELINE_DEGRADED self-watch (a SOURCE_FRESHNESS_STATE row past the shared DAILY/METERING 30h else 3h "
    "name rule, at most one event per source per last-load day; a swallowed loader failure of the five "
    "NATIVE_ALERT_STALE_FACTS error types, one per type, source and Central day, the three optional mart arms "
    "left to the stale leg; an idle alert notifier via OW_SENDER_LEASE while a route is enabled); + [23] add-on "
    "arm running SP_SCAN_ETL_CYCLE (V156, ungated here; not counted toward OPS_SCAN_DEGRADED, logs "
    "etl_cycle_scan_failed); "
    "+ #12c condition-ended sweep (an OPEN SEC_CRED_EXPIRY or SEC_NEW_EXPOSURE event resolves CONDITION_ENDED "
    "once CREDENTIALS or GRANTS_TO_ROLES show the condition ended; 1h dwell, positive evidence only, opt-in via "
    "AUTO_CLEAR_ENABLED); the V091 auto-clear sweep scoped to its 3 PERF rules; arm [10] (key unchanged) "
    "ignores CONDITION_ENDED and SUPERSEDED rows and any row closed for an earlier expiry, human resolves "
    "included however late (the cycle id is the expiry date every arm [10] since V009 writes at the head of "
    "DETAIL, now pinned to Central on write and match; a live row always blocks), so a rotated credential''s "
    "next expiry re-alerts, and never mints "
    "EXPIRING while the EXPIRED event is live; + [hb] ALERT_SCAN_HOURLY heartbeat (one point UPDATE, an INSERT "
    "only when the row is missing). Tally 13 -> 12. Daily: + "
    "[22] (byte-identical, shared keys, ungated) + [24] COST_IDLE_OPPORTUNITY (weekly per warehouse, net "
    "recoverable USD/month after the 60s resume tail, settings-verified timer (newest SHOW WAREHOUSES snapshot "
    "batch) disabled or above 60s, 14 complete Central days with at least 7 covered; MEDIUM at 100 USD/month, "
    "HIGH band at 5x) + [hb] ALERT_SCAN_DAILY "
    "heartbeat. Tally 9 -> 11. Seeds OPS_PIPELINE_DEGRADED (PLATFORM, HIGH) and COST_IDLE_OPPORTUNITY (COST, "
    "MEDIUM, 100, 336h) WHEN NOT MATCHED only; opts SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE into auto-clear after "
    "both procs are replaced. No task change, no procedure run at apply time."
)
assert len(DESCRIPTION.replace("''", "'")) <= 4000
assert "'" not in DESCRIPTION.replace("''", "")          # every apostrophe doubled

TAIL = (
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)\n"
    "SELECT 157 AS VERSION,\n"
    f"       '{DESCRIPTION}' AS DESCRIPTION\n"
    "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 157);\n"
)

out = (HEADER + MARK_H + new_hourly + "\n\n" + MARK_D + new_daily + "\n\n" + OPT_IN + "\n" + RETIRE + "\n"
       + TAIL)

assert out.count("CREATE OR REPLACE PROCEDURE") == 2
_top_level = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)
for banned in ("CREATE TASK", "ALTER TASK", "EXECUTE TASK", "CALL "):
    assert banned not in _top_level, banned            # no task change, no apply-time procedure run
for banned in ("AUTO_CLEAR_ENABLED = FALSE", "SET ENABLED"):
    assert banned not in out, banned
assert out.index("SET AUTO_CLEAR_ENABLED = TRUE") > out.index(
    "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()") == 1   # only the [23] add-on, in the hourly body
# the retirement: the V034 shape, after both procs and the opt-in, and the only DELETE in the file
assert out.index(RETIRE) > out.index(OPT_IN) > out.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
assert out.count("DELETE FROM ") == 1 and _top_level.count("COST_CLOUD_SVC_RATIO'") == 2
assert "DROP " not in _top_level

target = Path(os.environ.get("V157_OUT") or (MIG / "V157__alert_scan_self_watch_idle_push.sql"))
target.write_text(out, encoding="utf-8", newline="\n")
print(f"wrote {target} ({len(out):,} chars)")

# ---------------------------------------------------------------------------------------------------
# Optional: PREFLIGHT_WAVE2B.sql -- the [24] chain as a plain read-only SELECT (owner runbox file).
# ---------------------------------------------------------------------------------------------------
_pre = os.environ.get("PREFLIGHT_OUT")
if _pre:
    chain = ARM_24[ARM_24.index("        clk AS (\n"):ARM_24.index(
        "        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY\n")]
    assert chain.rstrip().endswith(")") and chain.count(":credit_price") == 1
    chain = chain.replace(":credit_price", "(SELECT CREDIT_PRICE FROM px)")
    assert all(not ln or ln.startswith("        ") for ln in chain.splitlines())
    chain = "".join(ln[8:] + "\n" for ln in chain.splitlines())      # the arm's CTEs sit 8 deep
    preflight = f"""\
-- PREFLIGHT_WAVE2B.sql -- read-only preview for V157 (Next-Fifty #13 COST_IDLE_OPPORTUNITY).
--
-- Generated by outputs/gen_v157.py from the SAME text as SP_ALERT_SCAN_DAILY's [24] arm (clk -> opp CTE
-- chain), so it cannot drift. Lists exactly the warehouses that get a COST_IDLE_OPPORTUNITY event on the first
-- daily run after V157 (this ISO week's key; a HIGH row pages through a HIGH-floor route). Plain SELECT: no
-- writes, no procedure runs. The clock is pinned to Central inside the query, so the worksheet timezone does
-- not matter. Before V157 the rule has no ALERT_CONFIG row, so the seeded threshold (100 USD/month) is the
-- fallback; after V157 it reads the live THRESHOLD_NUM. Run as the role that owns the OVERWATCH procs.
WITH px AS (
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS CREDIT_PRICE
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
),
thr AS (
    SELECT COALESCE(MAX(IFF(RULE_ID = 'COST_IDLE_OPPORTUNITY', THRESHOLD_NUM, NULL)), 100) AS THRESHOLD_NUM,
           COALESCE(MAX(IFF(RULE_ID = 'COST_IDLE_OPPORTUNITY', SEVERITY, NULL)), 'MEDIUM') AS BASE_SEVERITY,
           COALESCE(MAX(IFF(RULE_ID = 'COST_IDLE_OPPORTUNITY', IFF(ENABLED, 'yes', 'no'), NULL)),
                    'not seeded yet') AS RULE_ENABLED
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
),
{chain}SELECT o.WAREHOUSE_NAME,
       COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL') AS COMPANY,
       IFF(o.MONTHLY_USD >= t.THRESHOLD_NUM * 5, 'HIGH', t.BASE_SEVERITY) AS SEVERITY_IF_RAISED,
       o.MONTHLY_USD,
       o.IDLE_PCT,
       ROUND(o.IDLE_CREDITS, 2) AS IDLE_CREDITS,
       ROUND(o.RECOVERABLE_CREDITS, 2) AS RECOVERABLE_CREDITS,
       o.COVERED_DAYS,
       o.AUTO_SUSPEND AS CURRENT_AUTO_SUSPEND_SEC,
       o.TARGET_SEC AS TARGET_AUTO_SUSPEND_SEC,
       o.SNAPSHOT_AT AS TIMER_VERIFIED_AT,
       'COST_IDLE_OPPORTUNITY|' || UPPER(o.WAREHOUSE_NAME) || '|'
           || IFF(o.MONTHLY_USD >= t.THRESHOLD_NUM * 5, 'HIGH', 'MED') || '|'
           || TO_VARCHAR(DATEADD('day', 1 - DAYOFWEEKISO(k.TODAY), k.TODAY)) AS DEDUPE_KEY_PREVIEW,
       t.RULE_ENABLED
FROM opp o
CROSS JOIN thr t
CROSS JOIN clk k
WHERE o.MONTHLY_USD >= t.THRESHOLD_NUM
ORDER BY o.MONTHLY_USD DESC;
"""
    Path(_pre).write_text(preflight, encoding="utf-8", newline="\n")
    print(f"wrote {_pre} ({len(preflight):,} chars)")
