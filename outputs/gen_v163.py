#!/usr/bin/env python3
"""Forward-generate V163: per-user AI runaway + Trust Center regression + the nightly failed-logins wording.

Next Fifty wave 4 (#37a COST_AI_USER_RUNAWAY, #44b SEC_TRUST_REGRESSION, #39's [07] SEC_FAILED_LOGINS text).
Reads V160__sleep_polling_alert.sql ONLY (the CURRENT definer of SP_ALERT_SCAN_DAILY: tests/test_proc_lineage.py;
V161 and V162 do not touch it) and emits, in order:

  guard (-20163, v < 162) -> ALERT_CONFIG seeds (WHEN NOT MATCHED only) -> SETTINGS seeds (WHEN NOT MATCHED only)
  -> marker + SP_ALERT_SCAN_DAILY re-derived from V160 -> SCHEMA_VERSION 163.

SP_ALERT_SCAN_DAILY deltas, each asserted by count (everything else byte-identical to V160; the V163 test
normalizes it back):
  D1a/D1b  [07] SEC_FAILED_LOGINS TITLE + DETAIL reworded (predicate, severity and DEDUPE_KEY unchanged)
  D2       counting arms [28] COST_AI_USER_RUNAWAY + [29] SEC_TRUST_REGRESSION inserted before [17] PIPE_REF_GAP
  D3       self-alert ' of 12 daily alert rule block(s)' -> 14
  D4       (12 - :fails) x5 -> 14; '/12 rule blocks ok (daily)' x3 -> /14
  D5       the RETURN label names V163

The owner decisions (2026-09-29) are binding: BOTH signals (credits > THRESHOLD_NUM x COCO_DAILY_CAP_CREDITS AND a
robust z >= AI_RUNAWAY_ROBUST_Z against the user's own prior 90 active days), except that a user with fewer than
5 prior active days is judged on the cap alone ('No baseline yet'); HIGH; AI Functions only through
AI_RUNAWAY_INCLUDE_FUNCTIONS (default FALSE); COMPANY = COALESCE(NULLIF(COMPANY_FOR_USER(user), 'UNKNOWN'), 'ALL').

No CALL, DROP or task statement at apply time; no new table, view, task, proc or UDF. With PREFLIGHT_OUT set, also
writes the read-only PREFLIGHT section (P163.1-P163.3) built from the SAME arm text; with PART_B_OUT set, the
read-only RUN_NEXT PART B verify grid (V163.1-V163.3). The byte-identity test never sets either.

Run: python outputs/gen_v163.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V160__sleep_polling_alert.sql"
V160 = BASE.read_text(encoding="utf-8")
NAME = "V163__ai_runaway_trust_regression.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


AI_RULE = "COST_AI_USER_RUNAWAY"
TRUST_RULE = "SEC_TRUST_REGRESSION"
AI_DEFAULT_MULT = 2            # THRESHOLD_NUM seed: the cap multiple (tuning units = METRIC_VALUE)
AI_DEFAULT_Z = 3.5             # AI_RUNAWAY_ROBUST_Z seed
TRUST_DEFAULT_RISE = 1         # THRESHOLD_NUM seed: at-risk entities added since the previous snapshot day

# ---------------------------------------------------------------------------------------------------
# Shared arm text. The PREFLIGHT re-runs these CTEs verbatim (only the named read window moves), so what the
# owner previews is what the scan will raise.
# ---------------------------------------------------------------------------------------------------
CFG = """cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )"""

CLK = """clk AS (
            SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY
        )"""

# COCO_DAILY_CAP_CREDITS: a missing, non-numeric, zero or negative value reads as 15 -- the app's
# `_coco_cap if _coco_cap > 0 else 15.0` (ai_chargeback.py).
KNOB = """knob AS (
            SELECT COALESCE(NULLIF(GREATEST(COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'COCO_DAILY_CAP_CREDITS', VALUE, NULL))), 15), 0), 0), 15) AS CAP_CR,
                   COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_RUNAWAY_ROBUST_Z', VALUE, NULL))), 3.5) AS Z_MIN,
                   COALESCE(TRY_TO_BOOLEAN(MAX(IFF(KEY = 'AI_RUNAWAY_INCLUDE_FUNCTIONS', VALUE, NULL))), FALSE) AS INCL_FN
            FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
        )"""

UD_TEMPLATE = """ud AS (
            SELECT f.USER_NAME, f.DAY, SUM(f.CREDITS) AS CR,
                   LISTAGG(DISTINCT f.SOURCE, '+') WITHIN GROUP (ORDER BY f.SOURCE) AS SOURCES
            FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY f
            CROSS JOIN clk k
            CROSS JOIN knob n
            WHERE f.DAY >= DATEADD('day', -{READ_DAYS}, k.TODAY) AND f.DAY < k.TODAY
              AND f.USER_NAME NOT IN ('ACCOUNT', 'UNKNOWN')
              AND (f.SOURCE <> 'Functions' OR n.INCL_FN)
            GROUP BY f.USER_NAME, f.DAY
            HAVING SUM(f.CREDITS) > 0
        )"""

CAND_ARM = """cand AS (
            SELECT u.USER_NAME, u.DAY, u.CR, u.SOURCES
            FROM ud u
            CROSS JOIN clk k
            CROSS JOIN knob n
            JOIN cfg c ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
            WHERE u.DAY >= DATEADD('day', -3, k.TODAY)
              AND u.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2)
        )"""

# the robust-z engine of V150 (anomaly._MAD_K / _MEANAD_K), baseline = the user's ACTIVE days in the 90 days
# BEFORE the scored day (the scored day is never its own baseline); < 5 active days = no baseline (Z NULL)
Z_CHAIN = """hist AS (
            SELECT c.USER_NAME, c.DAY, h.CR AS HCR
            FROM cand c
            JOIN ud h ON h.USER_NAME = c.USER_NAME AND h.DAY < c.DAY AND h.DAY >= DATEADD('day', -90, c.DAY)
        ),
        med AS (
            SELECT USER_NAME, DAY, MEDIAN(HCR) AS MED, COUNT(*) AS N_HIST
            FROM hist GROUP BY USER_NAME, DAY
        ),
        disp AS (
            SELECT h.USER_NAME, h.DAY, m.MED, m.N_HIST,
                   MEDIAN(ABS(h.HCR - m.MED)) AS MAD, AVG(ABS(h.HCR - m.MED)) AS MEAN_AD
            FROM hist h
            JOIN med m ON m.USER_NAME = h.USER_NAME AND m.DAY = h.DAY
            GROUP BY h.USER_NAME, h.DAY, m.MED, m.N_HIST
        ),
        scored AS (
            SELECT c.USER_NAME, c.DAY, c.CR, c.SOURCES, d.MED, COALESCE(d.N_HIST, 0) AS N_HIST,
                   CASE WHEN COALESCE(d.N_HIST, 0) < 5 THEN NULL
                        WHEN d.MAD > 0 THEN 0.6745 * (c.CR - d.MED) / d.MAD
                        WHEN d.MEAN_AD > 0 THEN 0.7979 * (c.CR - d.MED) / d.MEAN_AD
                        ELSE IFF(c.CR > d.MED, 999, 0) END AS Z
            FROM cand c
            LEFT JOIN disp d ON d.USER_NAME = c.USER_NAME AND d.DAY = c.DAY
        )"""

SNAP_TEMPLATE = """snap AS (
            SELECT t.SCANNER_ID, t.SCANNER_NAME, UPPER(t.SEVERITY) AS SEV, t.DAY, t.SCANNED_AT,
                   t.TOTAL_AT_RISK_COUNT AS CUR_N,
                   LAG(t.TOTAL_AT_RISK_COUNT) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_N,
                   LAG(t.DAY) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_DAY
            FROM DBA_MAINT_DB.OVERWATCH.SECURITY_TRUST_SNAPSHOT t
            CROSS JOIN clk k
            WHERE t.DAY >= DATEADD('day', -{SNAP_DAYS}, k.TODAY)
        )"""

UD_ARM = UD_TEMPLATE.replace("{READ_DAYS}", "93")          # scored days TODAY-3..-1, each with its 90 days before
SNAP_ARM = SNAP_TEMPLATE.replace("{SNAP_DAYS}", "30")       # the previous snapshot day may be days back

# the raise predicate of arm [29], shared with the PREFLIGHT replay
TRUST_PRED = ("s.PRIOR_N IS NOT NULL", "s.CUR_N > 0", "s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)",
              "s.SEV IN ('CRITICAL', 'HIGH')")


def _handler(rule: str) -> str:
    """The house v7 handler of arms [24]/[25], rule id swapped."""
    return f"""    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule {rule} - other rules unaffected', CURRENT_ROLE();
    END;
"""


ARM_28 = f"""\
    -- [28] {AI_RULE} (V163, Next-Fifty #37a: a runaway AI user, raised the morning after. BOTH signals:
    --      the user's AI credits on one complete mart DAY exceed THRESHOLD_NUM x COCO_DAILY_CAP_CREDITS
    --      (2 x 15 by default) AND sit >= AI_RUNAWAY_ROBUST_Z (3.5) robust-z above the user's OWN active days
    --      in the 90 days before it (median/MAD 0.6745, mean-absolute-deviation 0.7979 fallback -- the V150
    --      engine, but the scored day is never its own baseline). Fewer than 5 prior active days = no baseline
    --      yet: the cap leg alone decides (owner 2026-09-29), so a brand-new heavy user is not silent.
    --      FACT_AI_USAGE_DAILY, Cortex Code (Snowsight + CLI); AI Functions rows count only when
    --      AI_RUNAWAY_INCLUDE_FUNCTIONS is TRUE AND the row names a user (the loader books them to ACCOUNT
    --      today, so the switch is inert until it attributes them). The last 3 complete days are re-scored
    --      with a per-user-day key: this scan is a sibling of TASK_LOAD_MARTS_V27_DAILY under
    --      TASK_NIGHTLY_RECONCILE (V071), so a day the mart had not loaded yet is raised on the next run, once.
    --      METRIC_VALUE = the cap multiple (THRESHOLD_NUM units); COMPANY = the user's company, ALL when it is
    --      UNKNOWN. HIGH (c.SEVERITY), never a literal CRITICAL.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH {CFG},
        {CLK},
        {KNOB},
        {UD_ARM},
        {CAND_ARM},
        {Z_CHAIN}
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(s.USER_NAME), 'UNKNOWN'), 'ALL'),
               c.SEVERITY,
               LEFT(s.USER_NAME || ' used ' || ROUND(s.CR, 1) || ' AI credits on ' || TO_VARCHAR(s.DAY)
                   || ' (~$' || ROUND(s.CR * :ai_credit_price)::INT || '): ' || ROUND(s.CR / n.CAP_CR, 1)
                   || 'x the ' || ROUND(n.CAP_CR, 1) || '-credit daily cap', 300),
               LEFT(IFF(s.Z IS NULL,
                        'No baseline yet: ' || s.N_HIST || ' active day(s) in the 90 days before, fewer than 5, '
                            || 'so the cap alone raised this (a new or newly active heavy user). ',
                        'Robust z ' || ROUND(s.Z, 1) || ' (bar ' || n.Z_MIN || ', AI_RUNAWAY_ROBUST_Z) against '
                            || 'this user''s own median of ' || ROUND(s.MED, 2) || ' credits/day over '
                            || s.N_HIST || ' active day(s) in the 90 days before. ')
                   || 'Cap: above ' || ROUND(n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2), 1) || ' credits (THRESHOLD_NUM '
                   || 'x COCO_DAILY_CAP_CREDITS ' || ROUND(n.CAP_CR, 1) || '). Sources: ' || s.SOURCES || '. About '
                   || ROUND(s.CR * :ai_credit_price)::INT || ' USD at ' || ROUND(:ai_credit_price, 2)
                   || ' USD/credit (AI_CREDIT_PRICE_USD). Per-user daily credits: Cost Intelligence > Chargeback '
                   || '& AI > AI users. A per-user AI quota (Snowsight, Cost Management) caps it; size one with the '
                   || 'suggested per-user quota table under Per-user AI quotas & blocks there.', 2000),
               ROUND(s.CR / n.CAP_CR, 4),
               c.RULE_ID || '|' || s.USER_NAME || '|' || TO_VARCHAR(s.DAY)
        FROM cfg c
        JOIN scored s
          ON c.RULE_ID = '{AI_RULE}'
        CROSS JOIN knob n
        WHERE s.Z IS NULL OR s.Z >= n.Z_MIN

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
{_handler(AI_RULE)}"""

_PRED_SQL = "\n".join(f"         AND {p}" for p in TRUST_PRED)

ARM_29 = f"""\
    -- [29] {TRUST_RULE} (V163, Next-Fifty #44b: a Trust Center scanner's at-risk count went UP. Reads
    --      SECURITY_TRUST_SNAPSHOT (hourly SP_LOAD_SECURITY_FACTS: one row per scanner per Central day, today's
    --      row rewritten each hour) and compares each of the last two snapshot days with the scanner's previous
    --      snapshot day -- the V_SECURITY_TRUST_DELTA REGRESSED test, but not that view: it compares only the
    --      newest day with the one before, so a once-a-day reader at ~07:00 would miss a rise that landed after
    --      07:00 (by the next morning both days carry the new count). CRITICAL/HIGH scanners only; a scanner's
    --      first-ever snapshot (no previous day) never raises, so enabling a package does not flood. The loader
    --      books a scanner missing from FINDINGS as 0, so its return reads as a rise (the DETAIL says so). One
    --      event per scanner per snapshot day, company ALL, HIGH (c.SEVERITY), no self-clear.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH {CFG},
        {CLK},
        {SNAP_ARM}
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               LEFT('Trust Center ' || s.SEV || ' scanner regressed: ' || COALESCE(s.SCANNER_NAME, s.SCANNER_ID)
                   || ' at-risk entities ' || s.PRIOR_N || ' -> ' || s.CUR_N || ' on ' || TO_VARCHAR(s.DAY), 300),
               LEFT('Scanner ' || s.SCANNER_ID || ': ' || s.CUR_N || ' entities at risk in the ' || TO_VARCHAR(s.DAY)
                   || ' snapshot (latest scan ' || COALESCE(TO_VARCHAR(s.SCANNED_AT, 'YYYY-MM-DD HH24:MI'), 'unknown')
                   || '), up from ' || s.PRIOR_N || ' on ' || TO_VARCHAR(s.PRIOR_DAY) || '. '
                   || IFF(s.PRIOR_N = 0,
                          'The previous day read 0: the loader books a scanner missing from FINDINGS as 0, so this '
                              || 'may be the scanner returning rather than new risk. ',
                          '')
                   || 'Security > Trust Center for the scanner delta; the entities and the suggested fix are in '
                   || 'Snowsight > Monitoring > Trust Center > Findings. Fix there, then re-scan.', 2000),
               s.CUR_N - s.PRIOR_N,
               c.RULE_ID || '|' || s.SCANNER_ID || '|' || TO_VARCHAR(s.DAY)
        FROM cfg c
        JOIN snap s
          ON c.RULE_ID = '{TRUST_RULE}'
{_PRED_SQL}
        CROSS JOIN clk k
        WHERE s.DAY >= DATEADD('day', -1, k.TODAY)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
{_handler(TRUST_RULE)}"""

# ---------------------------------------------------------------------------------------------------
# [07] SEC_FAILED_LOGINS wording (#39, owner 2026-09-29): TITLE + DETAIL only. FACT_LOGIN_DAILY has
# LOGINS = COUNT(*) and FAILED_LOGINS = IS_SUCCESS = 'NO' (V002; loader V101), so successes = the difference.
# ---------------------------------------------------------------------------------------------------
OLD_07_TITLE = "               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY,\n"
OLD_07_DETAIL = "               'Investigate credential stuffing / lockouts.',\n"
NEW_07_TITLE = (
    "               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY\n"
    "                   || IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,\n"
    "                          ', ' || (lg.LOGINS - lg.FAILED_LOGINS) || ' successful', ' and no successful login'),\n")
NEW_07_DETAIL = (
    "               IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,\n"
    "                   'The same day also had successful logins. A failed burst followed within 60 minutes by a '\n"
    "                   || 'success raises SEC_LOGIN_TAKEOVER from the hourly scan (CRITICAL off-hours or for an admin '\n"
    "                   || 'role); this nightly count covers the whole day. ',\n"
    "                   'No successful login that day: most likely a lockout or a job still sending an old secret '\n"
    "                   || '(a guessing attempt that never got in looks the same). ')\n"
    "                   || 'Review Security > Access > Authentication: failed-login reasons and client IPs.',\n")

ANCHOR_17 = "    -- [17] PIPE_REF_GAP  (optional external"
SELF_12 = "' of 12 daily alert rule block(s) failed this run'"
SELF_14 = "' of 14 daily alert rule block(s) failed this run'"
RET_160 = "'alert scan daily v4 (V160: + COST_SLEEP_POLLING weekly sleep-polling push): '"
RET_163 = ("'alert scan daily v5 (V163: + COST_AI_USER_RUNAWAY + SEC_TRUST_REGRESSION, [07] burst-vs-lockout "
           "wording): '")

daily = extract_proc(V160, "SP_ALERT_SCAN_DAILY()")
assert daily.count("fails := fails + 1") == 12
daily = _swap(daily, OLD_07_TITLE, NEW_07_TITLE, "D1a")
daily = _swap(daily, OLD_07_DETAIL, NEW_07_DETAIL, "D1b")
daily = _swap(daily, ANCHOR_17, ARM_28 + ARM_29 + ANCHOR_17, "D2")
daily = _swap(daily, SELF_12, SELF_14, "D3")
daily = _swap(daily, "(12 - :fails)", "(14 - :fails)", "D4a", n=5)
daily = _swap(daily, "'/12 rule blocks ok (daily)'", "'/14 rule blocks ok (daily)'", "D4b", n=3)
daily = _swap(daily, RET_160, RET_163, "D5")
assert daily.count("fails := fails + 1") == 14

AI_NAME = ("Per-user AI runaway: one user''s AI credits on a day above THRESHOLD_NUM x the daily cap "
           "(COCO_DAILY_CAP_CREDITS) and a robust-z outlier against their own prior 90 days")
TRUST_NAME = ("Trust Center regression: a CRITICAL or HIGH scanner''s at-risk entity count rose by at least "
              "THRESHOLD_NUM since its previous snapshot day")
for _n in (AI_NAME, TRUST_NAME):
    assert len(_n.replace("''", "'")) <= 200 and ";" not in _n and "$" not in _n and "FALSE" not in _n

HEADER = f"""-- {NAME}
--
-- Next Fifty wave 4 (#37a, #44b, #39): two new counting arms in the nightly SP_ALERT_SCAN_DAILY -- a per-user
-- AI runaway and a Trust Center regression -- and clearer text for the nightly failed-logins alert.
--
-- WHY: (#37a) one user burning AI credits was visible only to someone who opened Cost Intelligence > Chargeback
-- & AI; the account-level COST_AI_CREEP (week over week) cannot see a single user. (#44b) nothing pushed a Trust
-- Center regression: V_SECURITY_TRUST_DELTA is pull-only and compares only the two newest snapshot days. (#39)
-- the nightly SEC_FAILED_LOGINS text read the same whether or not the burst got in; since V162 the hourly
-- SEC_LOGIN_TAKEOVER pages a burst that ended in a success.
--
--   ~ SP_ALERT_SCAN_DAILY re-derived from V160 (its current definer), byte-identical except:
--     + counting arm [28] {AI_RULE} (FACT_AI_USAGE_DAILY, mart-only): a user's AI credits on one complete
--       day above THRESHOLD_NUM ({AI_DEFAULT_MULT}) x COCO_DAILY_CAP_CREDITS (15) AND a robust z >= AI_RUNAWAY_ROBUST_Z ({AI_DEFAULT_Z})
--       against the user's own active days in the 90 days before (median/MAD, the V150 engine; the scored day
--       is never its own baseline). Fewer than 5 prior active days = no baseline yet: the cap alone decides
--       (owner 2026-09-29). AI Functions rows count only with AI_RUNAWAY_INCLUDE_FUNCTIONS = TRUE and a named
--       user (inert today: the loader books them to ACCOUNT). The last 3 complete days are re-scored each
--       morning, one event per user-day. HIGH; METRIC_VALUE = the cap multiple; COMPANY = COMPANY_FOR_USER,
--       ALL when UNKNOWN.
--     + counting arm [29] {TRUST_RULE} (SECURITY_TRUST_SNAPSHOT via LAG, not V_SECURITY_TRUST_DELTA): a
--       CRITICAL or HIGH scanner's at-risk count rose by >= THRESHOLD_NUM ({TRUST_DEFAULT_RISE}) against its previous
--       snapshot day; today's and yesterday's rows are checked, so a rise after the morning scan lands the next
--       morning. A first-ever snapshot never raises. HIGH, company ALL, one event per scanner per snapshot day.
--     ~ [07] SEC_FAILED_LOGINS: TITLE and DETAIL say whether the day also had successful logins and point a
--       burst that got in to the hourly SEC_LOGIN_TAKEOVER. Predicate, severity and key unchanged (no re-fire).
--     ~ tally 12 -> 14 (self-alert, heartbeat, RETURN).
--   + ALERT_CONFIG {AI_RULE} (COST, HIGH, {AI_DEFAULT_MULT}, 24h) and {TRUST_RULE} (SECURITY, HIGH,
--     {TRUST_DEFAULT_RISE}, 24h), WHEN NOT MATCHED only; AUTO_CLEAR_ENABLED keeps its default.
--   + SETTINGS AI_RUNAWAY_ROBUST_Z = {AI_DEFAULT_Z} and AI_RUNAWAY_INCLUDE_FUNCTIONS = FALSE, WHEN NOT MATCHED only.
--
-- COST: two mart-only INSERTs per nightly run (a few compile-seconds a day); COMPANY_FOR_USER runs only on a
-- raised row. No new table, view, task, proc or UDF.
-- LATENCY: daily (~07:00 Central). A runaway day the mart had not loaded yet is raised on the next morning's run;
-- a Trust Center rise after the morning scan, the next morning.
-- FIRST RUN: the next daily scan raises runaways from the last 3 complete mart days and regressions dated today
-- or yesterday; preview both with the read-only PREFLIGHT (P163.1, P163.3). Nothing runs at apply time.
-- ROLLBACK: re-run V160's SP_ALERT_SCAN_DAILY (the tally goes back to 12 and the old [07] text returns);
-- optionally disable the two rules in Alerts > Rules. The settings can stay.
-- Apply AFTER V162 (the [07] text names the hourly SEC_LOGIN_TAKEOVER). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20163, 'V163 requires V162 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 162) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The two rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered. THRESHOLD_NUM is the cap
-- multiple for the runaway (the rule editor tunes it; the robust-z bar is the AI_RUNAWAY_ROBUST_Z setting) and
-- the at-risk entities added for the regression.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('{AI_RULE}', 'COST', '{AI_NAME}', TRUE, 'HIGH', {AI_DEFAULT_MULT}, 24),
        ('{TRUST_RULE}', 'SECURITY', '{TRUST_NAME}', TRUE, 'HIGH', {TRUST_DEFAULT_RISE}, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- The runaway's two knobs (Admin > Settings). WHEN NOT MATCHED only.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('AI_RUNAWAY_ROBUST_Z', '{AI_DEFAULT_Z}'),
        ('AI_RUNAWAY_INCLUDE_FUNCTIONS', 'FALSE')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

"""

MARKER = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V160; + [28] COST_AI_USER_RUNAWAY + [29] SEC_TRUST_REGRESSION "
          "counting arms, [07] burst-vs-lockout wording, tally 12 -> 14, V163)\n")

DESCRIPTION = (
    "Next Fifty wave 4 (#37a, #44b, #39). SP_ALERT_SCAN_DAILY re-derived from V160, byte-identical except: + "
    f"counting arm [28] {AI_RULE} (FACT_AI_USAGE_DAILY, mart-only): the AI credits of one user on a complete day "
    f"above THRESHOLD_NUM ({AI_DEFAULT_MULT}) x COCO_DAILY_CAP_CREDITS AND a robust z at least AI_RUNAWAY_ROBUST_Z "
    f"({AI_DEFAULT_Z}) against the active days of that user in the 90 days before (median/MAD; fewer than 5 prior "
    "active days = no baseline, the cap alone decides); Cortex Code only unless AI_RUNAWAY_INCLUDE_FUNCTIONS and a "
    "named user; the last 3 complete days re-scored, one event per user-day; HIGH; METRIC_VALUE = the cap multiple; "
    "COMPANY = COMPANY_FOR_USER, ALL when UNKNOWN. + counting arm [29] "
    f"{TRUST_RULE} (SECURITY_TRUST_SNAPSHOT via LAG, not V_SECURITY_TRUST_DELTA): the at-risk count of a CRITICAL "
    f"or HIGH scanner rose by at least THRESHOLD_NUM ({TRUST_DEFAULT_RISE}) against its previous snapshot day, today "
    "and yesterday checked; a first snapshot never raises; HIGH, company ALL. [07] SEC_FAILED_LOGINS TITLE and "
    "DETAIL now say whether the day had successful logins and point a burst that got in to the hourly "
    "SEC_LOGIN_TAKEOVER (predicate, severity and key unchanged). Tally 12 -> 14. Seeds the two rules and the "
    f"settings AI_RUNAWAY_ROBUST_Z ({AI_DEFAULT_Z}) and AI_RUNAWAY_INCLUDE_FUNCTIONS (FALSE), WHEN NOT MATCHED only. "
    "No task change, no new object, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 163 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 163);
"""

out = HEADER + MARKER + daily + "\n" + VERSION_ROW

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
# every line V163 adds is ASCII (V160's body keeps its three em dashes byte-identical)
assert all(x.isascii() for x in (HEADER, MARKER, ARM_28, ARM_29, NEW_07_TITLE, NEW_07_DETAIL, RET_163, VERSION_ROW))
assert "ct_hour" not in daily and "AS DAILY_BURN" in daily
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK|DROP\b)", out, re.M | re.I)
assert "'SEC_LOGIN_TAKEOVER'" not in out          # named mid-string only: never a raiser reference in this file
for arm in (ARM_28, ARM_29):
    body = arm[arm.index("    BEGIN\n"):]
    assert "ACCOUNT_USAGE" not in body and "CURRENT_DATE" not in body and "$$" not in body
    assert body.count("fails := fails + 1") == 1
assert "'CRITICAL'" not in ARM_28
for line in out.splitlines():
    assert not line.lstrip().upper().startswith("CALL ") or line.startswith("        "), line

target = Path(os.environ.get("V163_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------------------------------
# PREFLIGHT (read-only; P163.1-P163.3 of PREFLIGHT_WAVE4.sql). Built from the SAME CTE text as the arms:
# only the read windows move (a replay of every complete day of the last 90 needs 180 days of history; the
# trust replay reads 60 days so its first days have a previous snapshot) and the rule rows fall back to their
# seed values until V163 seeds them.
# ---------------------------------------------------------------------------------------------------
PREFLIGHT_CFG_AI = """cfg AS (     -- the rule row as seeded by V163, or its seed values before the apply
            SELECT 'COST_AI_USER_RUNAWAY' AS RULE_ID,
                   COALESCE(MAX(IFF(c.ENABLED, c.THRESHOLD_NUM, NULL)), 2) AS THRESHOLD_NUM
            FROM (SELECT 1 AS ONE) o
            LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
        )"""
PREFLIGHT_CAND = """cand AS (    -- every complete day of the last 90 (the arm scores the last 3, above the cap leg)
            SELECT u.USER_NAME, u.DAY, u.CR, u.SOURCES
            FROM ud u
            CROSS JOIN clk k
            WHERE u.DAY >= DATEADD('day', -90, k.TODAY)
        )"""
PREFLIGHT_PX = """px AS (      -- the scan's own AI price read (SP_ALERT_SCAN_DAILY, V160)
            SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20) AS AI_PRICE
            FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
        )"""
_RAISE = "s.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2) AND (s.Z IS NULL OR s.Z >= n.Z_MIN)"
PREFLIGHT_UD = UD_TEMPLATE.replace("{READ_DAYS}", "183")
PREFLIGHT_SNAP = SNAP_TEMPLATE.replace("{SNAP_DAYS}", "60")
PREFLIGHT_TRUST_PRED = " AND ".join(p.replace("s.", "r.").replace("c.THRESHOLD_NUM", "t.THRESHOLD_NUM")
                                    for p in TRUST_PRED)

PREFLIGHT = f"""-- PREFLIGHT V163 (P163.1-P163.3) -- READ-ONLY preview of V163's two new daily rules. Changes nothing.
-- Run BEFORE applying V163 (or any time). Built from the scan's own arm text: only the read windows move, and
-- the rule rows and settings fall back to their seed values (2x the cap, z 3.5, Functions off) until V163 seeds
-- them.

-- P163.1 {AI_RULE} replayed over EVERY complete day of the last 90 (the arm itself scores the last 3), one row
--        per user: days over 1x and 2x the COCO_DAILY_CAP_CREDITS cap, WOULD_RAISE (both signals, or the cap alone
--        with no baseline), FIRST_RUN_RAISES (what the first scan after the apply raises: the last 3 days), the
--        USD of the would-raise days, and EVENT_COMPANY (the company the event would carry).
WITH {PREFLIGHT_CFG_AI},
        {CLK},
        {KNOB},
        {PREFLIGHT_PX},
        {PREFLIGHT_UD},
        {PREFLIGHT_CAND},
        {Z_CHAIN},
        agg AS (
            SELECT s.USER_NAME,
                   COUNT(*) AS ACTIVE_DAYS_90,
                   COUNT_IF(s.CR > n.CAP_CR) AS OVER_CAP_1X,
                   COUNT_IF(s.CR > n.CAP_CR * 2) AS OVER_CAP_2X,
                   COUNT_IF({_RAISE}) AS WOULD_RAISE,
                   COUNT_IF({_RAISE} AND s.Z IS NULL) AS WOULD_RAISE_NO_BASELINE,
                   COUNT_IF({_RAISE} AND s.DAY >= DATEADD('day', -3, k.TODAY)) AS FIRST_RUN_RAISES,
                   ROUND(MAX(s.CR), 2) AS MAX_DAY_CREDITS,
                   ROUND(MEDIAN(s.CR), 2) AS MEDIAN_DAY_CREDITS,
                   ROUND(SUM(IFF({_RAISE}, s.CR, 0)) * MAX(p.AI_PRICE), 2) AS WOULD_RAISE_USD
            FROM scored s
            CROSS JOIN knob n
            CROSS JOIN clk k
            CROSS JOIN px p
            JOIN cfg c ON c.RULE_ID = 'COST_AI_USER_RUNAWAY'
            GROUP BY s.USER_NAME
        )
SELECT a.USER_NAME,
       COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(a.USER_NAME), 'UNKNOWN'), 'ALL') AS EVENT_COMPANY,
       a.ACTIVE_DAYS_90, a.OVER_CAP_1X, a.OVER_CAP_2X, a.WOULD_RAISE, a.WOULD_RAISE_NO_BASELINE,
       a.FIRST_RUN_RAISES, a.MAX_DAY_CREDITS, a.MEDIAN_DAY_CREDITS, a.WOULD_RAISE_USD
FROM agg a
ORDER BY a.WOULD_RAISE DESC, a.OVER_CAP_1X DESC, a.USER_NAME;

-- P163.2 FACT_AI_USAGE_DAILY depth and who each source is booked to. Functions rows booked to ACCOUNT prove
--        AI_RUNAWAY_INCLUDE_FUNCTIONS changes nothing today (the arm skips ACCOUNT and UNKNOWN).
SELECT f.SOURCE,
       IFF(f.USER_NAME IN ('ACCOUNT', 'UNKNOWN'), f.USER_NAME, 'a named user') AS BOOKED_TO,
       MIN(f.DAY) AS FIRST_DAY, MAX(f.DAY) AS LAST_DAY, COUNT(DISTINCT f.DAY) AS DAYS,
       COUNT(DISTINCT f.USER_NAME) AS USERS, ROUND(SUM(f.CREDITS), 2) AS CREDITS
FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY f
GROUP BY f.SOURCE, IFF(f.USER_NAME IN ('ACCOUNT', 'UNKNOWN'), f.USER_NAME, 'a named user')
ORDER BY f.SOURCE, BOOKED_TO;

-- P163.3 {TRUST_RULE} replayed over the last 30 days: every rise, plus the latest row per scanner (does
--        any scanner run at all?), its snapshot days and its zero days (the loader books a scanner missing
--        from FINDINGS as 0, so a return from 0 reads as a rise). IN_FIRST_RUN = dated today or yesterday.
WITH {CLK},
        {PREFLIGHT_SNAP},
        thr AS (     -- the rule row as seeded by V163, or its seed value before the apply
            SELECT COALESCE(MAX(IFF(c.ENABLED, c.THRESHOLD_NUM, NULL)), 1) AS THRESHOLD_NUM
            FROM (SELECT 1 AS ONE) o
            LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = 'SEC_TRUST_REGRESSION'
        ),
        r AS (
            SELECT s.SCANNER_ID, s.SCANNER_NAME, s.SEV, s.DAY, s.PRIOR_DAY, s.PRIOR_N, s.CUR_N,
                   ROW_NUMBER() OVER (PARTITION BY s.SCANNER_ID ORDER BY s.DAY DESC) AS RN,
                   COUNT(*) OVER (PARTITION BY s.SCANNER_ID) AS SNAPSHOT_DAYS,
                   SUM(IFF(s.CUR_N = 0, 1, 0)) OVER (PARTITION BY s.SCANNER_ID) AS ZERO_DAYS
            FROM snap s
        )
SELECT r.SCANNER_ID, r.SCANNER_NAME, r.SEV, r.DAY, r.PRIOR_DAY, r.PRIOR_N, r.CUR_N, r.SNAPSHOT_DAYS, r.ZERO_DAYS,
       IFF(r.RN = 1, 'LATEST', '') AS LATEST_ROW,
       ({PREFLIGHT_TRUST_PRED}) AS WOULD_RAISE,
       (r.DAY >= DATEADD('day', -1, k.TODAY)) AS IN_FIRST_RUN,
       IFF(r.PRIOR_N = 0 AND r.CUR_N > 0, 'rise from a 0 day', '') AS NOTE
FROM r
CROSS JOIN thr t
CROSS JOIN clk k
WHERE r.DAY >= DATEADD('day', -30, k.TODAY)
  AND (r.RN = 1 OR (r.PRIOR_N IS NOT NULL AND r.CUR_N > r.PRIOR_N))
ORDER BY r.SCANNER_ID, r.DAY;
"""

# ---------------------------------------------------------------------------------------------------
# RUN_NEXT PART B (read-only; after applying V163). GET_DDL fragments carry no quote, backslash or newline.
# ---------------------------------------------------------------------------------------------------
PART_B_PRESENT = ("/14 rule blocks ok (daily)", "COST_AI_USER_RUNAWAY", "SEC_TRUST_REGRESSION",
                  "and no successful login", "AI_RUNAWAY_INCLUDE_FUNCTIONS", "alert scan daily v5 (V163:",
                  "SP_SCAN_SLEEP_POLLING(FALSE)")
PART_B_ABSENT = ("/12 rule blocks ok (daily)", "credential stuffing")
_body = daily[daily.index("$$") + 2:daily.rindex("$$")]
_body160 = extract_proc(V160, "SP_ALERT_SCAN_DAILY()")
for _f in (*PART_B_PRESENT, *PART_B_ABSENT):
    assert not set(_f) & {"'", "\\", "\n", "\r"}, _f
assert all(f in _body for f in PART_B_PRESENT) and not any(f in _body for f in PART_B_ABSENT)
assert all(f in _body160 for f in PART_B_ABSENT)
_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()')"
_ddl_rows = "\nUNION ALL\n".join(
    [f"SELECT 'V163.2 daily scan DDL has: {f}', IFF(CONTAINS({_DDL}, '{f}'), 'OK', 'FAIL: not the V163 body')"
     for f in PART_B_PRESENT]
    + [f"SELECT 'V163.2 daily scan DDL lacks: {f}', IFF(NOT CONTAINS({_DDL}, '{f}'), 'OK', 'FAIL: still the V160 text')"
       for f in PART_B_ABSENT])

PART_B = f"""\
-- PART B -- V163 verify (READ-ONLY). V163.1 + V163.2 right after the apply; V163.3 the next morning, after the
-- ~07:00 Central daily scan. Every RESULT should read OK (or the count named); paste the grids back.
SELECT 'V163.1 SCHEMA_VERSION has 163' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 163) = 1,
           'OK', 'FAIL: V163 did not finish') AS RESULT
UNION ALL
SELECT 'V163.1 {AI_RULE} seeded (enabled, HIGH, 2)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
             WHERE RULE_ID = '{AI_RULE}' AND ENABLED AND SEVERITY = 'HIGH' AND THRESHOLD_NUM = {AI_DEFAULT_MULT}) = 1,
           'OK', 'CHECK: missing, or edited since the seed')
UNION ALL
SELECT 'V163.1 {TRUST_RULE} seeded (enabled, HIGH, 1)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
             WHERE RULE_ID = '{TRUST_RULE}' AND ENABLED AND SEVERITY = 'HIGH' AND THRESHOLD_NUM = {TRUST_DEFAULT_RISE}) = 1,
           'OK', 'CHECK: missing, or edited since the seed')
UNION ALL
SELECT 'V163.1 SETTINGS AI_RUNAWAY_ROBUST_Z = {AI_DEFAULT_Z}, AI_RUNAWAY_INCLUDE_FUNCTIONS = FALSE',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
             WHERE (KEY = 'AI_RUNAWAY_ROBUST_Z' AND VALUE = '{AI_DEFAULT_Z}')
                OR (KEY = 'AI_RUNAWAY_INCLUDE_FUNCTIONS' AND VALUE = 'FALSE')) = 2,
           'OK', 'CHECK: a row is missing or was set before the apply')
UNION ALL
{_ddl_rows};

-- V163.3 the next morning, after the ~07:00 Central daily scan. Compare the event counts with PREFLIGHT P163.1
--        (FIRST_RUN_RAISES) and P163.3 (WOULD_RAISE and IN_FIRST_RUN).
SELECT 'V163.3 daily scan heartbeat reads 14/14' AS CHECK_NAME,
       COALESCE((SELECT IFF(MAX(STATUS) = 'alert scan daily 14/14 rule blocks ok (daily)', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_DAILY'),
                'FAIL: no ALERT_SCAN_DAILY row') AS RESULT
UNION ALL
SELECT 'V163.3 no rule_block_failed for the two rules (24h)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE ERROR_TYPE = 'rule_block_failed'
               AND (CONTEXT LIKE 'rule {AI_RULE} %' OR CONTEXT LIKE 'rule {TRUST_RULE} %')
               AND LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE for the rule')
UNION ALL
SELECT 'V163.3 {AI_RULE} events raised',
       (SELECT COUNT(*)::VARCHAR || ' event(s)' FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS WHERE RULE_ID = '{AI_RULE}')
UNION ALL
SELECT 'V163.3 {TRUST_RULE} events raised',
       (SELECT COUNT(*)::VARCHAR || ' event(s)' FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS WHERE RULE_ID = '{TRUST_RULE}');
"""

_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")

print(f"V163 written: {target} ({len(out)} bytes)")
