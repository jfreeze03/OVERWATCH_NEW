#!/usr/bin/env python3
"""Forward-generate V160: COST_SLEEP_POLLING, the chronic SYSTEM$WAIT sleep-polling alert (v4.596.0).

The DB-side push of v4.595's Cost > Spend panel "Which statement families bill the most cloud services"
(app/data/mart_sql.cloud_svc_billed_families). V150's COST_CLOUD_SVC_ANOMALY can never fire on a chronic
poller (it is its own 28-day baseline), so a steady SYSTEM$WAIT loop -- 57.3 of 218.55 CS credits a week in
the owner's 2026-09-26 DIAG, ~$211/week -- was visible only to someone who opened the panel.

Reads V157__alert_scan_self_watch_idle_push.sql ONLY (the CURRENT definer of SP_ALERT_SCAN_DAILY:
tests/test_proc_lineage.py; V158/V159 do not touch it) and emits, in order:

  guard (-20160, v < 159) -> SLEEP_POLLING_WEEKLY (TRANSIENT; the weekly census + receipt) -> ALERT_CONFIG
  seed (WHEN NOT MATCHED only; AUTO_CLEAR_ENABLED TRUE in the seed) -> NEW SP_SCAN_SLEEP_POLLING(BOOLEAN)
  -> marker + SP_ALERT_SCAN_DAILY re-derived from V157 -> SCHEMA_VERSION 160.

SP_ALERT_SCAN_DAILY deltas, each asserted by count (everything else byte-identical to V157; the V160 test
normalizes it back):
  D1  insert the counting CALL arm [25] before [17] PIPE_REF_GAP
  D2  self-alert denominator ' of 11 daily alert rule block(s)' -> 12
  D3  (11 - :fails) x5 -> (12 - :fails); '/11 rule blocks ok (daily)' x3 -> /12
  D4  the RETURN label names V160

The sleep signature is a LITERAL copy of app/logic/system_wait.SLEEP_SQL_PATTERN with each LF written as the
two-character escape \\n (Snowflake turns it back into LF inside the proc's string literal), plus the
SLEEP_EXCLUDED_TYPE_PREFIXES and the two cs_driver next-step texts. The generator never imports app/; the
permanent tests/test_sleep_polling_parity.py locks the parity against the LATEST proc body, so a change to the
app pattern forces a forward re-derivation.

No SETTINGS key, no task change, no procedure run at apply time. When PREFLIGHT_OUT is set, also writes
PREFLIGHT_SLEEP_POLLING.sql: the census as one read-only SELECT with a WOULD_RAISE column (the byte-identity
test never sets it).

Run: python outputs/gen_v160.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V157__alert_scan_self_watch_idle_push.sql"
V157 = BASE.read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


# ---------------------------------------------------------------------------------------------------
# Literal copies of the app's sleep definition (tests/test_sleep_polling_parity.py locks them).
# ---------------------------------------------------------------------------------------------------
SLEEP_SQL_PATTERN = (
    "^[[:space:]]*((/[*]([^*]|[*]+[^*/])*[*]+/|--[^\n]*\n|//[^\n]*\n)[[:space:]]*)*"
    "(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(]"
)
SLEEP_RE_LITERAL = SLEEP_SQL_PATTERN.replace("\n", "\\n")          # one physical line, LF as \n
assert "\n" not in SLEEP_RE_LITERAL and "'" not in SLEEP_RE_LITERAL
SLEEP_EXCLUDED_TYPE_PREFIXES = ("CREATE", "ALTER", "MULTI_STATEMENT")
NOT_EXCLUDED = " AND ".join(f"f.QUERY_TYPE NOT LIKE '{p}%'" for p in SLEEP_EXCLUDED_TYPE_PREFIXES)
TASK_ACTION = (
    "Stop sleeping inside the task: run the dependent step AFTER its predecessor in a task graph, or "
    "trigger it when a stream has data, instead of a CALL SYSTEM$WAIT loop. A resize won't help.")
CLIENT_ACTION = (
    "Move the wait out of Snowflake: let the scheduler own the interval (Control-M cyclic interval or "
    "file watcher, orchestrator sensor), or run one short readiness check per cycle instead of "
    "SYSTEM$WAIT; cloud-services credits accrue for the whole sleep. A resize won't help.")


def _q(text: str) -> str:
    return text.replace("'", "''")


RULE = "COST_SLEEP_POLLING"
DEFAULT_THR = 25            # USD per week (owner default 2026-09-27: MEDIUM at 25, HIGH at 5x)
CHRONIC_DAYS = 5            # active on at least 5 of the 7 complete days
MUTE_DAYS = 28              # a NOISE / EXPECTED resolve mutes its band this long
IDLE_TAIL_DAYS = 4          # idle on the window's last 4 days = stopped (clear)

# ---------------------------------------------------------------------------------------------------
# The census: shared by the proc's [snapshot] INSERT and the read-only PREFLIGHT. {NEWEST}, {WEEK},
# {PRICE} are the proc's bind variables or the PREFLIGHT's CTE reads.
# ---------------------------------------------------------------------------------------------------
CENSUS_CTES = f"""bill AS (      -- = panel bill: account-wide, all service types, complete metering days only
        SELECT x.DAY, SUM(COALESCE(x.CREDITS_CLOUD_SVCS, 0) + COALESCE(x.CREDITS_ADJUSTMENT, 0)) AS CS_BILLED_DAY
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY x
        WHERE x.DAY >= DATEADD('day', -7, {{NEWEST}}) AND x.DAY < {{NEWEST}}
        GROUP BY x.DAY
    ),
    win AS (
        SELECT MIN(b.DAY) AS WIN_START, MAX(b.DAY) AS WIN_END, COUNT_IF(b.CS_BILLED_DAY > 0) AS ABOVE_DAYS FROM bill b
    ),
    fd AS (
        SELECT m.DAY, m.QUERY_PARAMETERIZED_HASH, m.QUERY_TYPE, m.WAREHOUSE_NAME, m.USER_NAME, m.ROLE_NAME,
               m.SAMPLE_TEXT, m.RUNS, m.CS_CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m
        JOIN bill b ON b.DAY = m.DAY
    ),
    fam AS (            -- panel family grain (hash x type x warehouse x user); sleep decided on the window-MIN sample
        SELECT fd.QUERY_PARAMETERIZED_HASH, fd.QUERY_TYPE, fd.WAREHOUSE_NAME, fd.USER_NAME,
               MIN(fd.SAMPLE_TEXT) AS SAMPLE_TEXT
        FROM fd
        GROUP BY fd.QUERY_PARAMETERIZED_HASH, fd.QUERY_TYPE, fd.WAREHOUSE_NAME, fd.USER_NAME
    ),
    sleepfam AS (       -- = mart_sql.cloud_svc_billed_families tagged.SLEEP_FLAG (tests/test_sleep_polling_parity.py)
        SELECT f.QUERY_PARAMETERIZED_HASH, f.QUERY_TYPE, f.WAREHOUSE_NAME, f.USER_NAME,
               COALESCE(REGEXP_SUBSTR(UPPER(f.SAMPLE_TEXT),
                                      '(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(][^)]{{0,40}}[)]'),
                        'SYSTEM$WAIT(...)') AS CALL_TEXT
        FROM fam f
        WHERE f.QUERY_PARAMETERIZED_HASH <> 'n/a'
          AND {NOT_EXCLUDED}
          AND REGEXP_INSTR(UPPER(f.SAMPLE_TEXT), '{SLEEP_RE_LITERAL}') > 0
    ),
    sd AS (             -- sleep rows keyed by POLLER: warehouse x user, or x task owner role when USER_NAME = SYSTEM
        SELECT d.DAY, d.WAREHOUSE_NAME, d.USER_NAME, IFF(UPPER(d.USER_NAME) = 'SYSTEM', d.ROLE_NAME, '') AS TASK_ROLE,
               '{RULE}|' || LEFT(UPPER(REPLACE(d.WAREHOUSE_NAME, '|', '/')), 100) || '|'
                   || LEFT(UPPER(REPLACE(IFF(UPPER(d.USER_NAME) = 'SYSTEM', 'SYSTEM:' || d.ROLE_NAME, d.USER_NAME),
                                         '|', '/')), 120)
                   || '|' AS POLLER_KEY,
               d.QUERY_PARAMETERIZED_HASH || '|' || d.QUERY_TYPE AS FAMILY_ID, s.CALL_TEXT, d.RUNS, d.CS_CREDITS
        FROM fd d
        JOIN sleepfam s
          ON s.QUERY_PARAMETERIZED_HASH = d.QUERY_PARAMETERIZED_HASH AND s.QUERY_TYPE = d.QUERY_TYPE
         AND s.WAREHOUSE_NAME = d.WAREHOUSE_NAME AND s.USER_NAME = d.USER_NAME
    ),
    pd AS (             -- poller x day: the GROUP-cap unit (never a sum of per-family caps)
        SELECT sd.POLLER_KEY, sd.DAY, SUM(sd.RUNS) AS RUNS_DAY, SUM(sd.CS_CREDITS) AS CS_DAY
        FROM sd GROUP BY sd.POLLER_KEY, sd.DAY
    ),
    pb AS (
        SELECT p.POLLER_KEY, SUM(p.RUNS_DAY) AS RUNS, SUM(p.CS_DAY) AS CS_CREDITS, COUNT(*) AS ACTIVE_DAYS,
               MAX(p.DAY) AS LAST_ACTIVE_DAY, SUM(LEAST(p.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))) AS BILLED_CS_CREDITS
        FROM pd p JOIN bill b ON b.DAY = p.DAY
        GROUP BY p.POLLER_KEY
    ),
    pc AS (             -- identity + the waits it runs, largest first
        SELECT c.POLLER_KEY, MAX(c.WAREHOUSE_NAME) AS WAREHOUSE_NAME, MAX(c.USER_NAME) AS USER_NAME,
               MAX(c.TASK_ROLE) AS TASK_ROLE, SUM(c.FAMILIES) AS FAMILIES,
               LEFT(LISTAGG(c.CALL_TEXT || ' ' || TO_VARCHAR(ROUND(c.CS, 1)) || ' cr', '; ')
                    WITHIN GROUP (ORDER BY c.CS DESC, c.CALL_TEXT), 400) AS CALLS
        FROM (SELECT sd.POLLER_KEY, sd.CALL_TEXT, MAX(sd.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
                     MAX(sd.USER_NAME) AS USER_NAME, MAX(sd.TASK_ROLE) AS TASK_ROLE,
                     COUNT(DISTINCT sd.FAMILY_ID) AS FAMILIES, SUM(sd.CS_CREDITS) AS CS
              FROM sd GROUP BY sd.POLLER_KEY, sd.CALL_TEXT) c
        GROUP BY c.POLLER_KEY
    ),
    app AS (            -- = panel app CTE (cs_driver.owner_hint's USER_TOP_APP), same days
        SELECT u.USER_NAME, MAX_BY(u.APPLICATION, u.APP_QUERIES) AS USER_TOP_APP
        FROM (SELECT a.USER_NAME, a.APPLICATION, SUM(a.QUERIES) AS APP_QUERIES
              FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY a
              WHERE a.DAY >= DATEADD('day', -7, {{NEWEST}}) AND a.DAY < {{NEWEST}} AND a.APPLICATION <> '(unknown)'
              GROUP BY a.USER_NAME, a.APPLICATION) u
        GROUP BY u.USER_NAME
    ),
    acct AS (           -- = panel SLEEP_BILLED_CS_CREDITS_ALL: every sleep poller capped per day as ONE group
        SELECT SUM(d.RUNS_DAY) AS RUNS, SUM(d.CS_DAY) AS CS_CREDITS, COUNT(*) AS ACTIVE_DAYS, MAX(d.DAY) AS LAST_ACTIVE_DAY,
               SUM(LEAST(d.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))) AS BILLED_CS_CREDITS
        FROM (SELECT p.DAY, SUM(p.RUNS_DAY) AS RUNS_DAY, SUM(p.CS_DAY) AS CS_DAY FROM pd p GROUP BY p.DAY) d
        JOIN bill b ON b.DAY = d.DAY
    ),
    nf AS (SELECT COUNT(*) AS N FROM sleepfam),
    np AS (SELECT COUNT(*) AS N FROM pb)"""

CENSUS_SELECT = f"""SELECT {{WEEK}}, 'POLLER', a.POLLER_KEY, a.WAREHOUSE_NAME, a.USER_NAME, a.TASK_ROLE, ap.USER_TOP_APP,
           IFF(UPPER(a.USER_NAME) = 'SYSTEM',                                   -- SQL twin of cs_driver.owner_hint
               IFF(a.TASK_ROLE <> '', 'Task owner (role ' || a.TASK_ROLE || ')', 'Task owner'),
               IFF(COALESCE(ap.USER_TOP_APP, '') <> '', ap.USER_TOP_APP || ' · ' || a.USER_NAME, 'User ' || a.USER_NAME)),
           a.CALLS, a.FAMILIES, NULL, b.RUNS, b.ACTIVE_DAYS, b.LAST_ACTIVE_DAY, b.CS_CREDITS, b.BILLED_CS_CREDITS,
           ROUND(b.BILLED_CS_CREDITS * {{PRICE}}, 2), w.ABOVE_DAYS, w.WIN_START, w.WIN_END, {{PRICE}}
    FROM pc a
    JOIN pb b ON b.POLLER_KEY = a.POLLER_KEY
    CROSS JOIN win w
    LEFT JOIN app ap ON ap.USER_NAME = a.USER_NAME
    UNION ALL
    SELECT {{WEEK}}, 'ACCOUNT', '{RULE}|*|*|', NULL, NULL, NULL, NULL, NULL, NULL,
           nf.N, np.N, s.RUNS, s.ACTIVE_DAYS, s.LAST_ACTIVE_DAY, s.CS_CREDITS, s.BILLED_CS_CREDITS,
           ROUND(COALESCE(s.BILLED_CS_CREDITS, 0) * {{PRICE}}, 2), w.ABOVE_DAYS, w.WIN_START, w.WIN_END, {{PRICE}}
    FROM acct s CROSS JOIN win w CROSS JOIN nf CROSS JOIN np"""

CENSUS_COLS = ("WEEK_START, ROW_KIND, POLLER_KEY, WAREHOUSE_NAME, USER_NAME, TASK_ROLE, USER_TOP_APP, OWNER_HINT, CALLS,\n"
               "         FAMILIES, POLLERS, RUNS, ACTIVE_DAYS, LAST_ACTIVE_DAY, CS_CREDITS, BILLED_CS_CREDITS, USD_WEEK,\n"
               "         ABOVE_ALLOWANCE_DAYS, WIN_START, WIN_END, CREDIT_PRICE_USD")


def _census(newest: str, week: str, price: str) -> tuple[str, str]:
    ctes = CENSUS_CTES.replace("{NEWEST}", newest)
    sel = CENSUS_SELECT.replace("{WEEK}", week).replace("{PRICE}", price)
    return ctes, sel


_P_CTES, _P_SEL = _census(":newest", ":week_start", ":credit_price")
POLLER_TAIL = "LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - IFF(SUBSTR(e.DEDUPE_KEY, -16, 6) = '|HIGH|', 15, 14))"

PROC = f"""CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FORCE_RUN BOOLEAN)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- {RULE} (V160): chronic SYSTEM$WAIT sleep polling per POLLER (warehouse x user, or x task owner role when
-- USER_NAME = SYSTEM), priced exactly like Cost > Spend > Which statement families bill the most cloud services
-- (mart_sql.cloud_svc_billed_families, v4.595): the 7 newest COMPLETE metering days (the newest
-- FACT_METERING_DAILY row is the UTC day still in progress at the 06:45 load); per day LEAST(poller CS,
-- GREATEST(0, account CS + adjustment)) as ONE group; x CREDIT_PRICE_USD. Sleep = system_wait.SLEEP_SQL_PATTERN
-- (statement shape) on the family (hash x type x warehouse x user) window-MIN sample. CALLed daily by
-- SP_ALERT_SCAN_DAILY arm [25] (counting); works once per ISO week (Central) -- the first call with no
-- completed receipt and complete data -- else one small read. No EXCEPTION handler: a failure reaches arm [25]
-- (OPS_SCAN_DEGRADED) and, with no receipt, is redone by the next daily scan. Mart-only; no freshness stamp.
DECLARE
    today_ct DATE;
    week_start DATE;
    n_enabled INT DEFAULT 0;
    n_done INT DEFAULT 0;
    forced BOOLEAN DEFAULT FALSE;
    credit_price FLOAT DEFAULT 3.68;          -- FLOAT + TRY_TO_DOUBLE (V153: a NUMBER priced 3.68 as 4)
    newest DATE;
    n_days INT DEFAULT 0;
    n_pollers INT DEFAULT 0;
    n_raised INT DEFAULT 0;
    n_superseded INT DEFAULT 0;
    n_cleared INT DEFAULT 0;
BEGIN
    -- [gate] the ONLY statement on a not-due day
    SELECT MAX(k.TODAY), MAX(k.WEEK_START), COUNT(DISTINCT c.RULE_ID), COUNT(DISTINCT r.COMPLETED_AT)
      INTO :today_ct, :week_start, :n_enabled, :n_done
    FROM (SELECT d.TODAY, DATEADD('day', 1 - DAYOFWEEKISO(d.TODAY), d.TODAY) AS WEEK_START
          FROM (SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY) d) k
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
           ON c.RULE_ID = '{RULE}' AND c.ENABLED
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY r
           ON r.WEEK_START = k.WEEK_START AND r.ROW_KIND = 'ACCOUNT' AND r.COMPLETED_AT IS NOT NULL;
    IF (COALESCE(:FORCE_RUN, FALSE)) THEN
        forced := TRUE;
    END IF;
    IF (n_enabled = 0) THEN
        RETURN 'sleep polling scan skipped ({RULE} disabled or missing)';
    END IF;
    IF (n_done > 0 AND NOT forced) THEN
        RETURN 'sleep polling scan skipped (week of ' || TO_VARCHAR(:week_start) || ' already evaluated)';
    END IF;

    -- [ready] price + the 7 newest complete metering days, each carrying statement rows
    SELECT MAX(px.PRICE), MAX(mx.NEWEST), COUNT(md.DAY)
      INTO :credit_price, :newest, :n_days
    FROM (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) px
    CROSS JOIN (SELECT MAX(z.DAY) AS NEWEST FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY z) mx
    LEFT JOIN (SELECT x.DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY x
               WHERE x.DAY >= DATEADD('day', -10, :today_ct) GROUP BY x.DAY) xd
           ON xd.DAY >= DATEADD('day', -7, mx.NEWEST) AND xd.DAY < mx.NEWEST
    LEFT JOIN (SELECT m.DAY FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m
               WHERE m.DAY >= DATEADD('day', -10, :today_ct) GROUP BY m.DAY) md
           ON md.DAY = xd.DAY;
    IF (newest IS NULL OR newest < DATEADD('day', -2, today_ct) OR n_days < 7) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'AlertScan', 'sleep_polling_scan_deferred',
               'newest metering day ' || COALESCE(TO_VARCHAR(:newest), 'none') || '; ' || :n_days
                   || ' of the 7 window days carry both metering and statement rows',
               'rule {RULE} - week of ' || TO_VARCHAR(:week_start)
                   || ' not evaluated; the next daily scan retries', CURRENT_ROLE();
        RETURN 'sleep polling scan deferred (data not complete)';
    END IF;

    -- [snapshot] the week's census (the ONE heavy statement): a POLLER row per sleep poller + the ACCOUNT receipt row
    DELETE FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY WHERE WEEK_START = :week_start;
    INSERT INTO DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY
        ({CENSUS_COLS})
    WITH {_P_CTES}
    SELECT w2.* FROM (
    {_P_SEL}
    ) w2;
    n_pollers := SQLROWCOUNT - 1;

    -- [raise] one event per chronic poller per episode (band-aware live/mute hold; uncorrelated LEFT JOIN)
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT RULE_ID, SEVERITY, GREATEST(COALESCE(THRESHOLD_NUM, {DEFAULT_THR}), 1) AS THR
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
        WHERE RULE_ID = '{RULE}' AND ENABLED
    ),
    ev AS (             -- key minus its fixed '<MED|HIGH>|YYYY-MM-DD' tail = POLLER_KEY
        SELECT {POLLER_TAIL} AS POLLER_KEY,
               IFF(SUBSTR(e.DEDUPE_KEY, -16, 6) = '|HIGH|', 2, 1) AS BAND_RANK,
               e.STATUS, COALESCE(e.RESOLUTION_KIND, 'ACTIONED') AS KIND, e.RESOLVED_AT
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.RULE_ID = '{RULE}'
    ),
    hold AS (
        SELECT v.POLLER_KEY,
               MAX(IFF(v.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                       OR (v.STATUS = 'RESOLVED' AND v.KIND IN ('NOISE', 'EXPECTED')
                           AND v.RESOLVED_AT >= DATEADD('day', -{MUTE_DAYS}, CURRENT_TIMESTAMP())),
                       v.BAND_RANK, 0)) AS HOLD_RANK,
               MAX(IFF(v.STATUS = 'RESOLVED'
                       AND v.KIND NOT IN ('NOISE', 'EXPECTED', 'SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED',
                                          'CONDITION_ENDED'),
                       TO_DATE(v.RESOLVED_AT), NULL)) AS LAST_ACTIONED_DAY
        FROM ev v
        GROUP BY v.POLLER_KEY
    ),
    cand AS (
        SELECT c.RULE_ID, c.SEVERITY AS BASE_SEVERITY, IFF(s.USD_WEEK >= c.THR * 5, 2, 1) AS BAND_RANK,
               s.POLLER_KEY, s.WAREHOUSE_NAME, s.USER_NAME, s.OWNER_HINT, s.CALLS, s.FAMILIES, s.RUNS, s.ACTIVE_DAYS,
               s.LAST_ACTIVE_DAY, s.CS_CREDITS, s.BILLED_CS_CREDITS, s.USD_WEEK, s.ABOVE_ALLOWANCE_DAYS,
               s.WIN_START, s.WIN_END, s.CREDIT_PRICE_USD
        FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY s
        JOIN cfg c ON s.USD_WEEK >= c.THR
        WHERE s.WEEK_START = :week_start AND s.ROW_KIND = 'POLLER'
          AND s.ACTIVE_DAYS >= {CHRONIC_DAYS}                                   -- chronic: {CHRONIC_DAYS}+ of the 7 days
    )
    SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
    FROM (
    SELECT o.RULE_ID,
           COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL'),
           IFF(o.BAND_RANK = 2 AND o.BASE_SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', o.BASE_SEVERITY),
           LEFT(IFF(o.WAREHOUSE_NAME = 'NONE', 'No warehouse', o.WAREHOUSE_NAME) || ' sleep polling ~$'
                || ROUND(o.USD_WEEK)::INT || '/week: ' || o.OWNER_HINT, 300),
           LEFT('Sleep polling on ' || o.ACTIVE_DAYS || ' of the 7 complete days ' || TO_VARCHAR(o.WIN_START) || ' to '
                || TO_VARCHAR(o.WIN_END) || ' (last on ' || TO_VARCHAR(o.LAST_ACTIVE_DAY) || '), ~'
                || ROUND(o.USD_WEEK)::INT || ' USD/week billed. Owner: ' || o.OWNER_HINT || '. Next step: '
                || IFF(UPPER(o.USER_NAME) = 'SYSTEM',
                       '{_q(TASK_ACTION)}',
                       '{_q(CLIENT_ACTION)}')
                || ' Verify: Cost Intelligence > Spend & Attribution > Cloud-services health, 7-day window, Which '
                || 'statement families bill the most cloud services. Evidence: ' || o.RUNS || ' sleep run(s) in '
                || o.FAMILIES || ' statement family(ies): ' || COALESCE(o.CALLS, 'a wait call')
                || '. Cloud services ' || ROUND(o.CS_CREDITS, 2) || ' credits used, ' || ROUND(o.BILLED_CS_CREDITS, 2)
                || ' billed (the account was above its free 10% allowance on ' || o.ABOVE_ALLOWANCE_DAYS
                || ' of 7 days) = ~$' || ROUND(o.USD_WEEK)::INT || '/week, ~$' || ROUND(o.USD_WEEK * 52 / 12)::INT
                || '/month at $' || ROUND(o.CREDIT_PRICE_USD, 2) || '/credit. A sleep compiles in well under 0.1 s, so '
                || 'compile-ranked views never show it, and the per-warehouse cloud-services baseline rule never trips '
                || 'on a steady poller (it is its own baseline). Checked weekly; while OPEN, this event resolves itself '
                || 'once the poller bills under the clear level (half the threshold by default) or is idle on the last '
                || '{IDLE_TAIL_DAYS} complete days; an acknowledged event stays yours to close (resolve it as actioned '
                || 'once fixed).', 2000),
           o.USD_WEEK,
           o.POLLER_KEY || IFF(o.BAND_RANK = 2, 'HIGH', 'MED') || '|' || TO_VARCHAR(:week_start)
    FROM cand o
    LEFT JOIN hold h ON h.POLLER_KEY = o.POLLER_KEY
    WHERE COALESCE(h.HOLD_RANK, 0) < o.BAND_RANK
      AND (h.LAST_ACTIONED_DAY IS NULL OR o.LAST_ACTIVE_DAY > h.LAST_ACTIONED_DAY)
    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
    );
    n_raised := SQLROWCOUNT;

    -- [supersede] a live HIGH replaces the same poller's live MED from any week (V067 only pairs same-date keys);
    -- SNOOZED too, or the V086 wake would reopen the MED beside its HIGH.
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                WHERE RULE_ID = '{RULE}' AND STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                  AND SUBSTR(DEDUPE_KEY, -16, 6) = '|HIGH|')) THEN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
         WHERE lo.RULE_ID = '{RULE}' AND lo.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
           AND SUBSTR(lo.DEDUPE_KEY, -15, 5) = '|MED|'
           AND LEFT(lo.DEDUPE_KEY, LENGTH(lo.DEDUPE_KEY) - 14) IN (
               SELECT LEFT(hi.DEDUPE_KEY, LENGTH(hi.DEDUPE_KEY) - 15)
               FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS hi
               WHERE hi.RULE_ID = '{RULE}' AND hi.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
                 AND SUBSTR(hi.DEDUPE_KEY, -16, 6) = '|HIGH|');
        n_superseded := SQLROWCOUNT;
    END IF;

    -- [clear] CONDITION_ENDED: OPEN only, 1h dwell, opt-in flag. Positive evidence = the complete window [ready]
    -- proved: the poller is absent, bills under the clear level, or was idle on the window's last {IDLE_TAIL_DAYS} days.
    IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID
                WHERE e.RULE_ID = '{RULE}' AND e.STATUS = 'OPEN' AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
         WHERE ev.RULE_ID = '{RULE}' AND ev.STATUS = 'OPEN'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap
           AND {POLLER_TAIL.replace("e.DEDUPE_KEY", "ev.DEDUPE_KEY")} NOT IN (
               SELECT s.POLLER_KEY
               FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY s
               JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = '{RULE}'
               WHERE s.WEEK_START = :week_start AND s.ROW_KIND = 'POLLER'
                 AND s.USD_WEEK >= LEAST(COALESCE(c.CLEAR_THRESHOLD_NUM, GREATEST(COALESCE(c.THRESHOLD_NUM, {DEFAULT_THR}), 1) * 0.5),
                                         GREATEST(COALESCE(c.THRESHOLD_NUM, {DEFAULT_THR}), 1))
                 AND s.LAST_ACTIVE_DAY >= DATEADD('day', -{IDLE_TAIL_DAYS - 1}, s.WIN_END));
        n_cleared := SQLROWCOUNT;
    END IF;

    -- [receipt] LAST: only a fully evaluated week stops the rest of the week's calls
    UPDATE DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY
       SET RAISED = :n_raised, SUPERSEDED = :n_superseded, CLEARED = :n_cleared,
           COMPLETED_AT = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
     WHERE WEEK_START = :week_start AND ROW_KIND = 'ACCOUNT';

    RETURN 'sleep polling scan week of ' || TO_VARCHAR(:week_start) || ': ' || :n_pollers || ' poller(s) over '
           || TO_VARCHAR(DATEADD('day', -7, :newest)) || '..' || TO_VARCHAR(DATEADD('day', -1, :newest))
           || '; raised ' || :n_raised || ', superseded ' || :n_superseded || ', cleared ' || :n_cleared;
END;
$$;
"""

ARM_25 = f"""\
    -- [25] {RULE} (V160: chronic SYSTEM$WAIT sleep polling, the DB-side push of Cost > Spend > Which
    --      statement families bill the most cloud services. SP_SCAN_SLEEP_POLLING gates ITSELF: it works once per
    --      ISO week (Central) -- the first daily scan with no completed receipt and complete metering -- and
    --      otherwise returns after one small read; a not-due day counts as ok. COUNTING, like [24]: an internal
    --      mart-only rule, so a failure trips OPS_SCAN_DEGRADED and, with no receipt, the next daily scan redoes it.)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule {RULE} - other rules unaffected', CURRENT_ROLE();
    END;
"""

daily = extract_proc(V157, "SP_ALERT_SCAN_DAILY()")
assert daily.count("fails := fails + 1") == 11
daily = _swap(daily, "    -- [17] PIPE_REF_GAP  (optional external", ARM_25 + "    -- [17] PIPE_REF_GAP  (optional external", "D1")
daily = _swap(daily, "' of 11 daily alert rule block(s) failed this run'", "' of 12 daily alert rule block(s) failed this run'", "D2")
daily = _swap(daily, "(11 - :fails)", "(12 - :fails)", "D3a", n=5)
daily = _swap(daily, "'/11 rule blocks ok (daily)'", "'/12 rule blocks ok (daily)'", "D3b", n=3)
daily = _swap(daily,
              "'alert scan daily v3 (V157: + OPS_PIPELINE_DEGRADED self-watch + COST_IDLE_OPPORTUNITY + heartbeat): '",
              "'alert scan daily v4 (V160: + COST_SLEEP_POLLING weekly sleep-polling push): '", "D4")
assert daily.count("fails := fails + 1") == 12

HEADER = f"""-- V160__sleep_polling_alert.sql
--
-- {RULE}: the chronic SYSTEM$WAIT sleep-polling alert -- the DB-side push of v4.595's Cost > Spend panel
-- "Which statement families bill the most cloud services" (app/data/mart_sql.cloud_svc_billed_families).
--
-- WHY: the owner's DIAG (2026-09-26, 7 days) found the account's cloud services above the free 10%-of-compute
-- allowance EVERY day, so every marginal cloud-services credit is billed, and SYSTEM$WAIT polling at 57.3 of
-- 218.55 CS credits a week (~$211/week at 3.68 USD/credit): Control-M 'select system$wait(10)' on
-- WH_ALFA_TRANSFORM_PRD and SYSTEM tasks' CALL SYSTEM$WAIT(30/60/1200) on WH_TRXS_TRANSFORM. V150's
-- COST_CLOUD_SVC_ANOMALY can never fire on a chronic poller (it is its own 28-day baseline), so nothing pushed it.
--
--   + SLEEP_POLLING_WEEKLY (TRANSIENT): the weekly census -- one POLLER row per sleep poller, one ACCOUNT row that
--     is both the week's receipt and the panel-parity group total. Not a FACT_/MART_ table: no freshness stamp.
--   + SP_SCAN_SLEEP_POLLING(FORCE_RUN BOOLEAN) (new): a poller = warehouse x user, or x task owner role when the
--     statements run as SYSTEM (a task). Billed exactly like the panel: the 7 newest COMPLETE metering days (the
--     newest FACT_METERING_DAILY row is the UTC day in progress at the 06:45 load), per day
--     LEAST(poller CS, GREATEST(0, account CS + adjustment)) capped as ONE group, x CREDIT_PRICE_USD. A sleep is
--     the app's statement SHAPE (app/logic/system_wait.SLEEP_SQL_PATTERN, SELECT or CALL of the wait function
--     after optional leading comments; not DDL, not a multi-statement parent, not the unhashed bucket).
--     Raises MEDIUM when a poller slept on {CHRONIC_DAYS}+ of the 7 days and billed >= THRESHOLD_NUM USD/week
--     ({DEFAULT_THR} by default); HIGH at 5x. One event per poller per EPISODE: a live (OPEN/ACK/SNOOZED) event holds its
--     band; a NOISE/EXPECTED resolve mutes that band {MUTE_DAYS} days (a 5x HIGH still breaks through); an ACTIONED resolve
--     re-raises only on polling after the resolve day; a live HIGH supersedes the poller's MED from any week.
--     Resolves itself (CONDITION_ENDED, OPEN only, 1h dwell, AUTO_CLEAR_ENABLED) once the poller is absent, bills
--     under the clear level (CLEAR_THRESHOLD_NUM, else half the threshold) or was idle on the window's last
--     {IDLE_TAIL_DAYS} days.
--   ~ SP_ALERT_SCAN_DAILY re-derived from V157 (its current definer), byte-identical except: + counting arm [25]
--     CALLing the new proc (before [17]); tally 11 -> 12 (self-alert, heartbeat, RETURN). The proc gates ITSELF:
--     it works once per ISO week (Central) -- the first daily scan with no completed receipt and complete data --
--     and otherwise returns after one small read. A failure trips OPS_SCAN_DEGRADED and, with no receipt, the
--     next daily scan redoes the week.
--   + ALERT_CONFIG {RULE} (COST, MEDIUM, {DEFAULT_THR} USD/week, 168h, AUTO_CLEAR_ENABLED TRUE), WHEN NOT MATCHED only.
--
-- COST: ~2 small statements on 6 days a week, ~10 on the due day (one heavy census over 7 days of an OVERWATCH
-- mart) = a few compile-seconds a week, no ACCOUNT_USAGE view, no new task or warehouse resume.
-- LATENCY: weekly. A new poller raises at the next week's evaluation; a stopped one clears up to ~10 days later.
-- FIRST RUN: the first daily scan after apply evaluates the current ISO week (applied Sunday 2026-09-27, that is
-- Monday 2026-09-28 ~07:00 Central over 09-21..09-27); preview with the read-only PREFLIGHT_SLEEP_POLLING.sql.
-- Nothing runs at apply time.
-- ROLLBACK: re-run V157's SP_ALERT_SCAN_DAILY (tally back to 11; nothing calls the new proc), optionally disable
-- the rule and close its events as EXPECTED; teardown.sql drops the proc (the table line is commented).
-- Apply AFTER V159. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20160, 'V160 requires V159 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 159) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The weekly census + receipt (transient: rebuilt every week from the marts).
CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY (
    WEEK_START           DATE          NOT NULL,   -- ISO Monday (Central) of the evaluating run = the DEDUPE_KEY date
    ROW_KIND             VARCHAR(10)   NOT NULL,   -- POLLER | ACCOUNT (the week's receipt + panel-parity total)
    POLLER_KEY           VARCHAR(260)  NOT NULL,   -- {RULE}|<WH>|<USER or SYSTEM:ROLE>|  (the key prefix)
    WAREHOUSE_NAME       VARCHAR(300),
    USER_NAME            VARCHAR(300),
    TASK_ROLE            VARCHAR(300),
    USER_TOP_APP         VARCHAR(300),
    OWNER_HINT           VARCHAR(700),
    CALLS                VARCHAR(400),
    FAMILIES             NUMBER(9,0),
    POLLERS              NUMBER(9,0),
    RUNS                 NUMBER(18,0),
    ACTIVE_DAYS          NUMBER(3,0),
    LAST_ACTIVE_DAY      DATE,
    CS_CREDITS           FLOAT,
    BILLED_CS_CREDITS    FLOAT,
    USD_WEEK             NUMBER(18,2),
    ABOVE_ALLOWANCE_DAYS NUMBER(3,0),
    WIN_START            DATE,
    WIN_END              DATE,
    CREDIT_PRICE_USD     FLOAT,
    RAISED               NUMBER(9,0),
    SUPERSEDED           NUMBER(9,0),
    CLEARED              NUMBER(9,0),
    EVALUATED_AT         TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    COMPLETED_AT         TIMESTAMP_NTZ
);

-- The rule. WHEN NOT MATCHED only -- an operator's edits are never clobbered. AUTO_CLEAR_ENABLED rides the seed:
-- since V157 the V091 sweep is scoped to its 3 PERF rules, so the flag only gates this rule's own clear.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('{RULE}', 'COST', 'Chronic sleep polling: one poller (warehouse + user, or task owner role) slept on {CHRONIC_DAYS}+ of the 7 newest complete days and billed at least the threshold in USD per week of cloud services', TRUE, 'MEDIUM', {DEFAULT_THR}, 168, TRUE)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS, s.AUTO_CLEAR_ENABLED);

"""

MARKER = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V157; + [25] COST_SLEEP_POLLING counting CALL arm, "
          "tally 11 -> 12, V160)\n")

DESCRIPTION = (
    f"{RULE}: the chronic SYSTEM$WAIT sleep-polling alert, the DB-side push of the v4.595 Cost > Spend panel "
    "Which statement families bill the most cloud services (V150 COST_CLOUD_SVC_ANOMALY can never fire on a chronic "
    "poller, it is its own baseline). + SLEEP_POLLING_WEEKLY (transient weekly census: POLLER rows + an ACCOUNT "
    "receipt row = the panel group total). + SP_SCAN_SLEEP_POLLING(BOOLEAN): poller = warehouse x user, or x task "
    "owner role for SYSTEM; billed like the panel over the 7 newest complete metering days (per day the smaller of "
    "the poller credits and the account billed cloud services, capped as one group, x CREDIT_PRICE_USD); sleep = "
    "the app statement shape (system_wait.SLEEP_SQL_PATTERN, not DDL, not a multi-statement parent); MEDIUM at "
    f"{CHRONIC_DAYS}+ of 7 days and at least THRESHOLD_NUM USD/week ({DEFAULT_THR}), HIGH at 5x; one event per poller per "
    f"episode (live hold, NOISE/EXPECTED mute {MUTE_DAYS} days per band, ACTIONED re-raises only on later polling, a live "
    "HIGH supersedes the MED); CONDITION_ENDED self-clear (OPEN only, 1h dwell) once the poller stops, bills under "
    f"the clear level or is idle {IDLE_TAIL_DAYS} days. SP_ALERT_SCAN_DAILY re-derived from V157, byte-identical except the "
    "counting arm [25] (the proc works once per ISO week behind a receipt, retried daily) and tally 11 -> 12. Seeds "
    f"{RULE} (COST, MEDIUM, {DEFAULT_THR}, 168h, AUTO_CLEAR_ENABLED TRUE) WHEN NOT MATCHED only. No task change, no "
    "SETTINGS key, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 160 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 160);
"""

out = HEADER + PROC + "\n" + MARKER + daily + "\n" + VERSION_ROW

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);") == 1
assert "ct_hour" not in daily and "AS DAILY_BURN" in daily
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK)", out, re.M | re.I), "no task statement"
assert "SNOWFLAKE.ACCOUNT_USAGE" not in PROC and "TRY_TO_NUMBER" not in PROC and "SOURCE_FRESHNESS_STATE" not in PROC
assert PROC.count(SLEEP_RE_LITERAL) == 1
_body = PROC[PROC.index("$$") + 2:PROC.rindex("$$")]
assert "$$" not in _body
for stmt_head in ("-- [snapshot]", "-- [raise]", "-- [clear]"):
    seg = PROC[PROC.index(stmt_head):]
    seg = seg[:seg.index("-- [", 5)] if "-- [" in seg[5:] else seg
    assert "(SELECT MAX(" not in seg, stmt_head
assert "V160 requires V159 first" in out and "SELECT 160 AS VERSION" in out
assert out.startswith("-- V160__sleep_polling_alert.sql\n")
for line in out.splitlines():
    if line.lstrip().upper().startswith("CALL "):
        assert "SP_SCAN_SLEEP_POLLING(FALSE)" in line or line.startswith("        "), line

target = Path(os.environ.get("V160_OUT") or MIG / "V160__sleep_polling_alert.sql")
target.write_text(out, encoding="utf-8", newline="\n")

# ---- optional read-only PREFLIGHT ------------------------------------------------------------------
pf = os.environ.get("PREFLIGHT_OUT")
if pf:
    _f_ctes, _f_sel = _census("(SELECT NEWEST FROM mx)", "(SELECT WEEK_START FROM clk)", "(SELECT PRICE FROM px)")
    preflight = f"""-- PREFLIGHT_SLEEP_POLLING.sql -- READ-ONLY preview of V160's {RULE} first evaluation.
-- Run BEFORE applying V160 (or any time). Lists every sleep poller over the 7 newest COMPLETE metering days with
-- its billed USD/week and WOULD_RAISE (active on {CHRONIC_DAYS}+ of the 7 days and USD_WEEK >= threshold; HIGH at 5x),
-- plus the ACCOUNT row (= the Spend panel's sleep-polling billed credits for the same days). Changes nothing.
-- The threshold falls back to {DEFAULT_THR} USD/week until V160 seeds the rule. DEDUPE_KEY_PREVIEW carries THIS
-- ISO week's Monday; the scan's real key uses the Monday of the day the daily scan evaluates (run on a Sunday,
-- the preview shows the week just ending while Monday's evaluation keys the new week).
WITH clk AS (
        SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY,
               DATEADD('day', 1 - DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE),
                       CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE) AS WEEK_START
    ),
    mx AS (SELECT MAX(z.DAY) AS NEWEST FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY z),
    px AS (SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE
           FROM DBA_MAINT_DB.OVERWATCH.SETTINGS),
    thr AS (SELECT COALESCE(MAX(IFF(c.ENABLED, GREATEST(COALESCE(c.THRESHOLD_NUM, {DEFAULT_THR}), 1), NULL)), {DEFAULT_THR}) AS THR
            FROM (SELECT 1 AS ONE) o
            LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = '{RULE}'),
    {_f_ctes},
    census ({CENSUS_COLS.replace(chr(10) + '         ', ' ')}) AS (
    {_f_sel}
    )
SELECT c.ROW_KIND, c.WAREHOUSE_NAME, c.OWNER_HINT, c.CALLS, c.FAMILIES, c.RUNS, c.ACTIVE_DAYS, c.LAST_ACTIVE_DAY,
       ROUND(c.CS_CREDITS, 2) AS CS_CREDITS, ROUND(c.BILLED_CS_CREDITS, 2) AS BILLED_CS_CREDITS, c.USD_WEEK,
       c.ABOVE_ALLOWANCE_DAYS, c.WIN_START, c.WIN_END,
       IFF(c.ROW_KIND = 'POLLER', c.ACTIVE_DAYS >= {CHRONIC_DAYS} AND c.USD_WEEK >= t.THR, NULL) AS WOULD_RAISE,
       IFF(c.ROW_KIND = 'POLLER', IFF(c.USD_WEEK >= t.THR * 5, 'HIGH', 'MED'), NULL) AS BAND,
       IFF(c.ROW_KIND = 'POLLER', c.POLLER_KEY || IFF(c.USD_WEEK >= t.THR * 5, 'HIGH', 'MED') || '|'
                                  || TO_VARCHAR(c.WEEK_START), NULL) AS DEDUPE_KEY_PREVIEW
FROM census c
CROSS JOIN thr t
ORDER BY c.ROW_KIND DESC, c.USD_WEEK DESC NULLS LAST;
"""
    Path(pf).write_text(preflight, encoding="utf-8", newline="\n")

print(f"V160 written: {target} ({len(out)} bytes)")
