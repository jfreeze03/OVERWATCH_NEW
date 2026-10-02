#!/usr/bin/env python3
"""Forward-generate V168: SP_ALERT_SCAN keys and sweeps (round-2 review, alerts cluster).

Reads V162__security_takeover_admin_grant.sql ONLY -- the CURRENT definer of SP_ALERT_SCAN (tests/test_proc_lineage.py;
V163 re-derives only SP_ALERT_SCAN_DAILY, V164 only SP_NOTIFY_WEBHOOK, V165 only SP_DAILY_DIGEST, V166/V167 touch
neither scan) -- and emits, in order:

  guard (-20168, v < 167) -> marker + SP_ALERT_SCAN re-derived from V162 -> two guarded ALERT_CONFIG NAME refreshes
  -> SCHEMA_VERSION 168.

SP_ALERT_SCAN deltas, each asserted by count (and scoped to its arm's slice where the text repeats); everything else is
byte-identical to V162 and the V168 test normalizes it back:
  P1   R2-040   the dead prologue reads go: budget_usd / ai_credit_price DECLAREs, SETTINGS read narrowed to INTO
                :credit_price (arm [17] still binds it -- the hourly scan has no budget or AI-price arm since V141)
  A14  R2-035   arm [14] PIPE_COPY_FAILURES is keyed by the Central FAILURE day over whole Central days (yesterday +
                today; a -50h prune), not the scan day over a rolling 24h: the TITLE names the day, the key keeps
                its |CRIT|/|WARN| band and trailing |YYYY-MM-DD (the V067 sweep and V117 parse them)
  A18  R2-036 + R2-039   arm [18] SEC_NEW_ADMIN_NETWORK counts SUCCESSES, says 'logged in' only when one got in
                (else 'N failed login attempt(s) ... (0 successful)'), and keys on user|IP[|FAILED]|first-seen Central
                day (LEFT(user, 200): <= 286 chars). The NOT EXISTS adds a 48h same-episode guard on the EXACT
                date-stripped base (never a STARTSWITH: 'R|U|IP|FAILED|d|' starts with the success base 'R|U|IP|' and
                would swallow the success event R2-039 exists to raise); its legacy-key branch drops '|FAILED' so a
                failures-only episode raised under V162's undated key does not re-raise once at apply.
  A20  R2-091   arm [20] SEC_NEW_EXPOSURE DETAIL points at Security -> Changes (Recent grant changes), where
                navigate.investigation_target and the playbook already send it
  S67  R2-039 D4   the V067 supersede sweep resolves a failures-only SEC_NEW_ADMIN_NETWORK event once the same user +
                IP's success event opens within 48h of it
  S91  R2-034   the V091 auto-clear sweep drops V096's >= -48h RAISED_AT bound (a multi-day or hysteresis-held PERF
                condition stranded its older day-keyed events OPEN for good); its two stale comment lines are fixed
  S117 R2-036 D4   the V117 carry-forward comment names [18]'s first-seen day
  A22  holistic #4/#9   arm [22] OPS_PIPELINE_DEGRADED ERR leg: errs carries RERAISED (a PAGE 'AppCost' /
                'StorageTruth' row -- V166's SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back, log and re-raise),
                and the DETAIL says that run FAILED; every other loader's row keeps 'returned normally, so its task
                still reads SUCCEEDED'. Byte-identical to V169's daily twin (the V157 shared-arm design).
  R    the RETURN label names V168 (the tally stays 14: no arm is added)

The two NAME refreshes (PIPE_COPY_FAILURES, SEC_NEW_ADMIN_NETWORK) touch a row only while NAME still equals its seed
text (V011 / V043), so an operator's edit is never overwritten. ALERT_CONFIG has no DESCRIPTION column.

No CALL, DROP, task or ALERT_EVENTS write at apply time: the first hourly scan after the apply self-heals R2-034's
stranded OPEN PERF events. With PREFLIGHT_OUT / PART_B_OUT / REPAIR_OUT set, also writes the read-only PREFLIGHT
(P168.1-P168.4), the RUN_NEXT PART B verify grids (V168.1-V168.4) and the OPTIONAL owner repair block (every
statement commented out) built from the SAME arm text. The byte-identity test never sets them.

Run: python outputs/gen_v168.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V162__security_takeover_admin_grant.sql"
V162 = BASE.read_text(encoding="utf-8")
NAME = "V168__alert_scan_hourly_keys_and_sweeps.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert text.count(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}") == 1, name
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


def _swap_in(text: str, start: str, end: str, old: str, new: str, label: str, n: int = 1) -> str:
    """_swap inside the slice [start, end) only (an arm's span), the slice anchors each unique."""
    assert text.count(start) == 1, f"{label}: slice start {start!r} x{text.count(start)}"
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + _swap(text[i:j], old, new, label, n) + text[j:]


# ---------------------------------------------------------------------------------------------------
# Arm / sweep slices (each start unique in V162's SP_ALERT_SCAN)
# ---------------------------------------------------------------------------------------------------
A14_START, A14_END = "    -- [14] PIPE_COPY_FAILURES\n", "    -- [17] COST_DEPT_BUDGET_PACE\n"
A18_START = "    -- [18] SEC_NEW_ADMIN_NETWORK"
A18_END = "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]"
A20_START, A20_END = "    -- [20] SEC_NEW_EXPOSURE", "    END IF;   -- /V157 cadence gate: [20]"

# ---- P1 (R2-040) -----------------------------------------------------------------------------------
P1_DECL_BUDGET = "    budget_usd FLOAT;\n    credit_price FLOAT;\n"
P1_DECL_AI = "    credit_price FLOAT;\n    ai_credit_price FLOAT;\n"
P1_READ_OLD = (
    "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),\n"
    "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),\n"
    "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)\n"
    "      INTO :budget_usd, :credit_price, :ai_credit_price\n")
P1_READ_NEW = (
    "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)   -- V168: the rate arm [17] binds\n"
    "      INTO :credit_price\n")

# ---- A14 (R2-035) ----------------------------------------------------------------------------------
A14_COMMENT_OLD = (
    "        -- PIPE_COPY_FAILURES: failed or partial file loads in the last 24h.\n"
    "        -- Broken ingestion is the most preventable 'found out too late' class.\n")
A14_COMMENT_NEW = (
    "        -- PIPE_COPY_FAILURES: failed or partial file loads per Central failure day (yesterday + today).\n"
    "        -- Broken ingestion is the most preventable 'found out too late' class. V168: keyed by the day the\n"
    "        -- files FAILED over whole Central days (not the scan day over a rolling 24h), so the count of a day only\n"
    "        -- grows -- the same files never re-raise after midnight and a band never steps back down.\n")
A14_TITLE_OLD = "' failed file load(s) (24h)',\n"
A14_TITLE_NEW = "' failed file load(s) on ' || TO_VARCHAR(p.FAIL_DAY),\n"
A14_KEY_OLD = ("IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #1: band "
               "matches the CRITICAL severity so a HIGH->CRITICAL crossing re-fires\n")
A14_KEY_NEW = ("IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(p.FAIL_DAY)  -- V066 #1: band "
               "matches the CRITICAL severity so a HIGH->CRITICAL crossing re-fires; V168: the failure day\n")
A14_COL_OLD = ("            SELECT TABLE_CATALOG_NAME AS DB, TABLE_SCHEMA_NAME AS SCH, TABLE_NAME AS TBL,\n"
               "                   MAX(PIPE_NAME) AS PIPE,\n")
A14_COL_NEW = ("            SELECT TABLE_CATALOG_NAME AS DB, TABLE_SCHEMA_NAME AS SCH, TABLE_NAME AS TBL,\n"
               "                   TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY,\n"
               "                   MAX(PIPE_NAME) AS PIPE,\n")
A14_WHERE_OLD = "            WHERE LAST_LOAD_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n"
A14_WHERE_NEW = (
    "            WHERE LAST_LOAD_TIME >= DATEADD('hour', -50, CURRENT_TIMESTAMP())   -- V168: prune only; yesterday "
    "00:00 Central is at most 49h back (DST fall-back included)\n"
    "              AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME))\n"
    "                  >= DATEADD('day', -1, TO_DATE(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())))\n")
A14_GROUP_OLD = "            GROUP BY 1, 2, 3\n"
A14_GROUP_NEW = "            GROUP BY 1, 2, 3, 4\n"

# ---- A18 (R2-036 + R2-039, composed) -------------------------------------------------------------
A18_HEAD_OLD = "    -- [18] SEC_NEW_ADMIN_NETWORK (V043 — the r25 panel, with teeth)\n    BEGIN\n"
A18_HEAD_NEW = (
    "    -- [18] SEC_NEW_ADMIN_NETWORK (V043 — the r25 panel, with teeth)\n"
    "    --      V168 (R2-039): SUCCESSES counts the attempts that got in; the TITLE says 'logged in' only then, else\n"
    "    --      'N failed login attempt(s) from new network <IP> (0 successful)'. A failures-only pair keys\n"
    "    --      user|IP|FAILED|<day>, so a later success in the same 24h raises its own event (the V067 sweep then\n"
    "    --      supersedes the failed one). V168 (R2-036): the key ends in the pair's first-seen Central day, so a\n"
    "    --      network quiet 90+ days alerts again (the rule name, playbook and Security panel promise the\n"
    "    --      re-flag; the undated V043 key matched the pair's first event forever). The 48h guard compares the\n"
    "    --      EXACT date-stripped base, so a late earlier login across Central midnight, or a pre-V168 undated\n"
    "    --      key, never raises one episode twice, and a failures-only key never swallows the success.\n"
    "    BEGIN\n")
A18_SELECT_OLD = (
    "               nn.USER_NAME || ' logged in from new network ' || nn.CLIENT_IP,\n"
    "               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline. Auth: '\n"
    "                   || COALESCE(nn.AUTH_FACTOR, '?')\n"
    "                   || '. Expected after travel/VPN/host changes; anything else is the finding.',\n"
    "               nn.LOGINS,\n"
    "               c.RULE_ID || '|' || nn.USER_NAME || '|' || nn.CLIENT_IP\n")
A18_SELECT_NEW = (
    "               LEFT(nn.USER_NAME || IFF(nn.SUCCESSES > 0,\n"
    "                   ' logged in from new network ' || nn.CLIENT_IP,\n"
    "                   ': ' || nn.LOGINS || ' failed login attempt(s) from new network ' || nn.CLIENT_IP\n"
    "                       || ' (0 successful)'), 300),\n"
    "               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline; successful '\n"
    "                   || nn.SUCCESSES || ' of ' || nn.LOGINS || ' attempt(s). Auth: '\n"
    "                   || COALESCE(nn.AUTH_FACTOR, '?')\n"
    "                   || IFF(nn.SUCCESSES > 0,\n"
    "                          '. Expected after travel/VPN/host changes; anything else is the finding.',\n"
    "                          '. No attempt got in; a success from this IP inside its first 24h raises a separate "
    "event.'),\n"
    "               nn.LOGINS,\n"
    "               c.RULE_ID || '|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, '', "
    "'|FAILED')\n"
    "                   || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', nn.FIRST_SEEN)), 'YYYY-MM-DD')\n")
A18_COUNT_OLD = "                   COUNT(*) AS LOGINS,\n"
A18_COUNT_NEW = ("                   COUNT(*) AS LOGINS,\n"
                 "                   COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,\n")
A18_DEDUPE_OLD = (
    "        WHERE NOT EXISTS (\n"
    "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "        );\n")
A18_DEDUPE_NEW = (
    "        WHERE NOT EXISTS (\n"
    "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "               OR (e.RULE_ID = b.RULE_ID\n"
    "                   AND e.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
    "                   AND (e.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')\n"
    "                        OR (LENGTH(e.DEDUPE_KEY) = LENGTH(b.DEDUPE_KEY)\n"
    "                            AND LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - 10)\n"
    "                                = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10))))\n"
    "        );\n")

# ---- A20 (R2-091) ----------------------------------------------------------------------------------
A20_OLD = "GRANTS_TO_ROLES - review in Security -> Access.'"
A20_NEW = "GRANTS_TO_ROLES - review in Security -> Changes (Recent grant changes).'"

# ---- S67 (R2-039 D4) --------------------------------------------------------------------------------
S67_OLD = "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))\n"
S67_NEW = (
    "                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')\n"
    "                      -- V168 (R2-039): a failures-only SEC_NEW_ADMIN_NETWORK event (user|IP|FAILED|<day>) is\n"
    "                      -- superseded once the same user + IP success event (user|IP|<day>) opens within 48h of it.\n"
    "                      OR (lo.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'\n"
    "                          AND lo.DEDUPE_KEY LIKE '%|FAILED|____-__-__'\n"
    "                          AND LEFT(hi.DEDUPE_KEY, LENGTH(hi.DEDUPE_KEY) - 11)\n"
    "                              = REPLACE(LEFT(lo.DEDUPE_KEY, LENGTH(lo.DEDUPE_KEY) - 11), '|FAILED', '')\n"
    "                          AND hi.RAISED_AT >= lo.RAISED_AT\n"
    "                          AND hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)))\n")

# ---- S91 (R2-034) -----------------------------------------------------------------------------------
S91_HEAD_OLD = "    -- [auto-clear sweep] V091: resolve TODAY's still-OPEN live-window events whose\n"
S91_HEAD_NEW = "    -- [auto-clear sweep] V091: resolve still-OPEN live-window events (any raise day, V168) whose\n"
S91_TODAY_OLD = (
    "    -- Only today's bucket (LIKE '%|<today>') is touched, so historical day-stamped\n"
    "    -- exceedances are never rewritten. RESOLUTION_KIND='AUTO_CLEARED' is excluded from\n")
S91_TODAY_NEW = (
    "    -- V168: no age bound (the V096 >= -48h bound is gone). Every OPEN event of the 3 rules is re-checked each\n"
    "    -- scan on its date-stripped identity (V119), so a multi-day or hysteresis-held condition never\n"
    "    -- strands its older day-stamped events OPEN. RESOLUTION_KIND='AUTO_CLEARED' is excluded from\n")
S91_BOUND = ("           AND ev.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())                    -- V096: "
             "recent window (was date-in-key); catches next-day-cleared 24h conditions\n")

# ---- S117 (R2-036 D4) -------------------------------------------------------------------------------
S117_OLD = ("    -- GENUINE future re-raises, leaving a pre-existing untriaged OPEN sibling for a human. Entity-\n"
            "    -- only keys (IP, grant time) never end in a bare date so they are never stripped -- untouched.\n")
S117_NEW = ("    -- GENUINE future re-raises, leaving a pre-existing untriaged OPEN sibling for a human. Entity-\n"
            "    -- only keys (grant time) never end in a bare date so they are never stripped -- untouched. The\n"
            "    -- [18] first-seen day (V168) strips to user|IP or user|IP|FAILED; a re-raise needs 90 quiet days.\n")

# ---- A22 (holistic #4/#9; identical constants in outputs/gen_v169.py -- the twin arm) --------------
# V166 made SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back, log fact_load_failed (PAGE 'AppCost' /
# 'StorageTruth') and RE-RAISE, so their task reads FAILED; every earlier ERR-leg source logs and returns normally.
A22_START, A22_END = "    -- [22] OPS_PIPELINE_DEGRADED", "    END IF;   -- /V157 cadence gate: [22]"
A22_RERAISE_PAGES = ("AppCost", "StorageTruth")
A22_HEAD_OLD = (
    "    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged and swallowed\n"
    "    --      (its task still reads SUCCEEDED) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS -- one\n")
A22_HEAD_NEW = (
    "    --      day (key = the stale LAST_LOAD_TS date, or NEVER). (b) ERR: a failure a loader logged -- most\n"
    "    --      loaders swallow it (their task still reads SUCCEEDED); V166's SP_LOAD_APP_COST and\n"
    "    --      SP_LOAD_STORAGE_TRUTH roll back to the previous fill and re-raise (their task reads FAILED), and\n"
    "    --      the DETAIL says which (RERAISED, V168 + V169) -- the same five ERROR_TYPEs as NATIVE_ALERT_STALE_FACTS"
    " -- one\n")
A22_ERRS_OLD = "                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG\n"
A22_ERRS_NEW = (
    "                   MAX(LOGGED_AT) AS LAST_AT, MAX_BY(ERROR_MESSAGE, LOGGED_AT) AS LAST_MSG,\n"
    "                   MAX(IFF(PAGE IN ("
    + ", ".join(f"'{p}'" for p in A22_RERAISE_PAGES)
    + "), 1, 0)) AS RERAISED   -- the V166 loads that roll back and re-raise\n")
A22_DETAIL_OLD = (
    "               LEFT('The loader logged this and returned normally, so its task still reads SUCCEEDED and readers '\n"
    "                   || 'keep the previous fill. Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '\n")
A22_DETAIL_NEW = (
    "               LEFT(IFF(x.RERAISED = 1,\n"
    "                        'The loader rolled back to its previous fill, logged this and re-raised: the run FAILED '\n"
    "                        || '(a scheduled run shows FAILED in TASK_HISTORY; a hand CALL raised the error to its '\n"
    "                        || 'caller) and readers keep the previous fill.',\n"
    "                        'The loader logged this and returned normally, so its task still reads SUCCEEDED and '\n"
    "                        || 'readers keep the previous fill.')\n"
    "                   || ' Last at ' || TO_VARCHAR(x.LAST_AT, 'YYYY-MM-DD HH24:MI') || ': '\n")

# ---- R ------------------------------------------------------------------------------------------------
RET_162 ="'alert scan v13 (V162: + SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT hourly; V157 gates unchanged): '"
RET_168 = ("'alert scan v14 (V168: [14] failure-day key, [18] outcome + first-seen-day key, auto-clear any raise "
           "day; V162 arms and V157 gates unchanged): '")

hourly = extract_proc(V162, "SP_ALERT_SCAN()")
assert hourly.count("fails := fails + 1") == 14
assert hourly.count(":credit_price") == 2 and hourly.count(":budget_usd") == 1 and hourly.count(":ai_credit_price") == 1
assert hourly.count("GROUP BY 1, 2, 3\n") == 2 and hourly.count(A18_DEDUPE_OLD) == 9
assert hourly.count("LAST_LOAD_TIME") == 1 and hourly.count("review in Security -> Access") == 2
assert hourly.count("still reads SUCCEEDED") == 3 and "RERAISED" not in hourly

hourly = _swap(hourly, P1_DECL_BUDGET, "    credit_price FLOAT;\n", "P1a")
hourly = _swap(hourly, P1_DECL_AI, "    credit_price FLOAT;\n", "P1b")
hourly = _swap(hourly, P1_READ_OLD, P1_READ_NEW, "P1c")
hourly = _swap_in(hourly, A14_START, A14_END, A14_COMMENT_OLD, A14_COMMENT_NEW, "A14a")
hourly = _swap_in(hourly, A14_START, A14_END, A14_TITLE_OLD, A14_TITLE_NEW, "A14b")
hourly = _swap_in(hourly, A14_START, A14_END, A14_KEY_OLD, A14_KEY_NEW, "A14c")
hourly = _swap_in(hourly, A14_START, A14_END, A14_COL_OLD, A14_COL_NEW, "A14d")
hourly = _swap_in(hourly, A14_START, A14_END, A14_WHERE_OLD, A14_WHERE_NEW, "A14e")
hourly = _swap_in(hourly, A14_START, A14_END, A14_GROUP_OLD, A14_GROUP_NEW, "A14f")
hourly = _swap(hourly, A18_HEAD_OLD, A18_HEAD_NEW, "A18a")
hourly = _swap_in(hourly, A18_START, A18_END, A18_SELECT_OLD, A18_SELECT_NEW, "A18b")
hourly = _swap_in(hourly, A18_START, A18_END, A18_COUNT_OLD, A18_COUNT_NEW, "A18c")
hourly = _swap_in(hourly, A18_START, A18_END, A18_DEDUPE_OLD, A18_DEDUPE_NEW, "A18d")
hourly = _swap_in(hourly, A20_START, A20_END, A20_OLD, A20_NEW, "A20")
hourly = _swap(hourly, S67_OLD, S67_NEW, "S67")
hourly = _swap(hourly, S91_HEAD_OLD, S91_HEAD_NEW, "S91a")
hourly = _swap(hourly, S91_TODAY_OLD, S91_TODAY_NEW, "S91b")
hourly = _swap(hourly, S91_BOUND, "", "S91c")
hourly = _swap(hourly, S117_OLD, S117_NEW, "S117")
hourly = _swap_in(hourly, A22_START, A22_END, A22_HEAD_OLD, A22_HEAD_NEW, "A22a")
hourly = _swap_in(hourly, A22_START, A22_END, A22_ERRS_OLD, A22_ERRS_NEW, "A22b")
hourly = _swap_in(hourly, A22_START, A22_END, A22_DETAIL_OLD, A22_DETAIL_NEW, "A22c")
hourly = _swap(hourly, RET_162, RET_168, "R")

# ---- post-asserts on the derived body ---------------------------------------------------------------
assert hourly.count("fails := fails + 1") == 14                                      # tally unchanged
assert hourly.count("(14 - :fails)") == 5 and hourly.count("'/14 rule blocks ok'") == 3
assert ":budget_usd" not in hourly and ":ai_credit_price" not in hourly
assert "budget_usd FLOAT" not in hourly and "ai_credit_price FLOAT" not in hourly
assert "'MONTHLY_BUDGET_USD'" not in hourly and "'AI_CREDIT_PRICE_USD'" not in hourly
assert hourly.count(":credit_price") == 2 and hourly.count("INTO :credit_price\n") == 1
assert "ev.RAISED_AT >=" not in hourly                                               # no age bound in the sweep
assert hourly.count("ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())") == 3  # V091 + the two V157 clears
assert hourly.count("review in Security -> Access") == 1                             # [26] keeps its pointer
_a22 = hourly[hourly.index(A22_START):hourly.index(A22_END)]
assert _a22.count("AS RERAISED") == 1 and _a22.count("x.RERAISED = 1") == 1 and "RERAISED" not in hourly.replace(_a22, "")
assert _a22.count("the run FAILED") == 1 and _a22.count("returned normally, so its task still reads SUCCEEDED") == 1
assert hourly.count("SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY") == 1
_a18 = hourly[hourly.index(A18_START):hourly.index(A18_END)]
assert _a18.count("ROLE IN (") == 1 and "ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" in _a18
_scan_body = hourly[hourly.index("$$") + 2:hourly.rindex("$$")]
assert "$$" not in _scan_body and "\\" not in _scan_body
assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _scan_body)) == {
    "COPY_HISTORY", "CREDENTIALS", "GRANTS_TO_ROLES", "GRANTS_TO_USERS", "LOGIN_HISTORY"}

# ---------------------------------------------------------------------------------------------------
# The ALERT_CONFIG NAME refreshes (guarded on the seed text: an operator edit is never overwritten)
# ---------------------------------------------------------------------------------------------------
NAME_COPY_SEED = "Failed COPY / Snowpipe file loads in 24h (threshold = allowed failures)"          # V011
NAME_COPY_NEW = "Failed COPY / Snowpipe file loads per Central failure day (threshold = allowed failures)"
NAME_NET_SEED = "Admin login from a network unseen in 90 days"                                     # V043
NAME_NET_NEW = "Admin login attempt from a network unseen in 90 days (the title says whether any succeeded)"
for _n in (NAME_COPY_NEW, NAME_NET_NEW):
    assert len(_n) <= 200 and "'" not in _n and ";" not in _n and "$" not in _n and "FALSE" not in _n.upper(), _n

NAMES = f"""
-- Rule NAME text follows the new behaviour. Each refresh touches the row only while NAME still equals its seed (V011 /
-- V043), so an operator's own edit survives; a re-run is a no-op. ALERT_CONFIG has no DESCRIPTION column.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = '{NAME_COPY_NEW}'
 WHERE RULE_ID = 'PIPE_COPY_FAILURES'
   AND NAME = '{NAME_COPY_SEED}';

UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = '{NAME_NET_NEW}'
 WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
   AND NAME = '{NAME_NET_SEED}';
"""

HEADER = f"""-- {NAME}
--
-- Round-2 review, alerts cluster (R2-034, R2-035, R2-036, R2-039, R2-040, R2-091): hourly alert keys and sweeps.
--
-- WHY: (R2-034) the V091 auto-clear sweep only re-checked PERF events raised in the last 48h (V096's bound), so a
-- condition that held for several days, or sat in the hysteresis band past 48h, left its older day-keyed events
-- OPEN forever. (R2-035) PIPE_COPY_FAILURES counted a rolling 24h but keyed on the SCAN day, so the files that
-- failed yesterday afternoon raised a second event after midnight. (R2-036) SEC_NEW_ADMIN_NETWORK keyed on
-- user|IP with no date, so a network quiet for 90+ days -- which the rule name, playbook and Security panel promise
-- to re-flag -- never alerted again. (R2-039) the same arm counted failed attempts as logins and always said
-- 'logged in', and a failures-only event blocked the success that followed it. (R2-091) SEC_NEW_EXPOSURE pointed
-- at Security -> Access, where no PUBLIC-grant panel exists. (R2-040) two dead prologue reads. (Holistic #4/#9)
-- the OPS_PIPELINE_DEGRADED ERR DETAIL told every logged loader failure 'its task still reads SUCCEEDED', but V166's
-- SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH roll back and re-raise, so TASK_HISTORY shows those runs FAILED.
--
--   ~ SP_ALERT_SCAN re-derived from V162 (its current definer), byte-identical except:
--     ~ arm [14] PIPE_COPY_FAILURES: keyed by the Central FAILURE day over whole Central days (yesterday + today,
--       a -50h prune); the TITLE names the day; the |CRIT|/|WARN| band and trailing |YYYY-MM-DD stay (the V067
--       supersede and V117 carry-forward parse them), so a day's event only escalates, never re-raises.
--     ~ arm [18] SEC_NEW_ADMIN_NETWORK: SUCCESSES = COUNT_IF(IS_SUCCESS = 'YES'); the TITLE says 'logged in' only
--       when one succeeded, else 'N failed login attempt(s) from new network <IP> (0 successful)'; DETAIL says
--       'successful S of N attempt(s)'. Key = user|IP[|FAILED]|first-seen Central day (user cut to 200 chars,
--       at most 286 chars); a 48h same-episode guard on the EXACT date-stripped base (never a STARTSWITH, which
--       would let a failures-only key swallow the success); the three-role list, 90d baseline and 24h first-seen
--       window are unchanged.
--     ~ arm [20] SEC_NEW_EXPOSURE DETAIL: review in Security -> Changes (Recent grant changes).
--     ~ V067 supersede sweep: a failures-only SEC_NEW_ADMIN_NETWORK event resolves (SUPERSEDED) once the same user
--       + IP success event opens within 48h of it.
--     ~ V091 auto-clear sweep: no RAISED_AT lower bound -- every OPEN PERF_QUERY_FAIL_PCT / PERF_QUEUED_MINUTES /
--       PERF_SPILL_GB event is re-checked on its date-stripped identity (the 1h dwell, the still-firing NOT IN and
--       the three -24h windows are unchanged).
--     - the dead budget_usd / ai_credit_price prologue reads (arm [17] keeps :credit_price; the daily scan keeps
--       its own copies).
--     ~ arm [22] OPS_PIPELINE_DEGRADED ERR leg: errs carries RERAISED (a PAGE 'AppCost' / 'StorageTruth' row, the
--       V166 loaders that roll back, log and re-raise); the DETAIL says that run FAILED (a scheduled run shows
--       FAILED in TASK_HISTORY; a hand CALL raised the error to its caller), and keeps 'returned normally, so its
--       task still reads SUCCEEDED' for every other loader. Keys, sources, cadence gate and windows unchanged;
--       byte-identical to V169's daily twin.
--     ~ the RETURN label names V168; the 14-block tally is unchanged.
--   ~ ALERT_CONFIG NAME of PIPE_COPY_FAILURES and SEC_NEW_ADMIN_NETWORK, only while it still equals the seed text.
--
-- COST: unchanged in kind. Arm [14] prunes COPY_HISTORY at 50h instead of 24h; arm [18] adds one COUNT_IF over rows
-- it already reads and a 48h ALERT_EVENTS probe bounded by RULE_ID; the sweeps add predicates only.
-- LATENCY: hourly, as before.
-- FIRST RUN: the first hourly scan after the apply auto-clears every stranded OPEN PERF event whose scope is now
-- below its CLEAR threshold (PREFLIGHT P168.1 counts them; nothing is written at apply time). A failures-only or
-- success pair first seen in the last 24h does not re-raise (the guard reads its V162 undated key). The apply day
-- can raise one legitimate CRIT|<yesterday> PIPE_COPY_FAILURES event for a day whose full count first reaches 10.
-- Two apply-window suppressions, once each: (1) PIPE_COPY_FAILURES -- a table whose yesterday failures V162 re-raised
-- after midnight under today's date (title '... (24h)', the R2-035 carry-over) folds the apply day's new failures of
-- the same band into that event, so nothing pages for them that day unless they cross into the other band; PREFLIGHT
-- P168.2 flags those keys (HELD_BY_A_V162_EVENT). (2) SEC_NEW_ADMIN_NETWORK -- the guard cannot tell a V162 undated
-- event's outcome, so a success that follows a failures-only V162 event inside its 48h does not raise its own event
-- (R2-039's separate success event holds from the first V168 key on); PREFLIGHT P168.4 (second grid) lists the
-- candidates. Standing (as under V162): within one failure day, a new failure that lands after an operator resolved
-- that day's PIPE_COPY_FAILURES event stays suppressed until the next Central day, unless it crosses WARN -> CRIT.
-- ROLLBACK: re-run V162's SP_ALERT_SCAN (V162__security_takeover_admin_grant.sql, the second CREATE PROCEDURE). A
-- pair raised under V168 in the last 24h may raise once more under the undated key; the NAME text can stay.
-- Apply AFTER V167. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20168, 'V168 requires V167 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 167) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARKER = ("-- >>> derived:SP_ALERT_SCAN  (from V162; [14] failure-day key, [18] outcome + first-seen-day key + 48h "
          "episode guard, [20] pointer, V067 FAILED supersede, V091 sweep any raise day, dead prologue reads, [22] ERR "
          "re-raise wording, V168)\n")

DESCRIPTION = (
    "Round-2 review, alerts cluster (R2-034, R2-035, R2-036, R2-039, R2-040, R2-091). SP_ALERT_SCAN re-derived from "
    "V162, byte-identical except: arm [14] PIPE_COPY_FAILURES keyed by the Central failure day over whole Central days "
    "(yesterday and today), TITLE names the day, band and trailing date kept; arm [18] SEC_NEW_ADMIN_NETWORK counts "
    "SUCCESSES, says logged in only when one succeeded (else N failed login attempts, 0 successful), keys on "
    "user, IP, FAILED for a failures-only pair, and the first-seen Central day (a network quiet 90 days alerts "
    "again), with a 48h same-episode guard on the exact date-stripped base; arm [20] SEC_NEW_EXPOSURE DETAIL points "
    "at Security, Changes; the V067 sweep supersedes a failures-only SEC_NEW_ADMIN_NETWORK event once the success "
    "event opens; the V091 auto-clear sweep re-checks every OPEN PERF event whatever its raise day; the dead "
    "budget and AI-price prologue reads are gone; the OPS_PIPELINE_DEGRADED ERR detail says a run of the V166 "
    "app-cost or storage-truth loader rolled back and FAILED instead of claiming its task still reads SUCCEEDED; "
    "RETURN names V168, tally 14 unchanged. ALERT_CONFIG NAME of "
    "PIPE_COPY_FAILURES and SEC_NEW_ADMIN_NETWORK refreshed only while it equals the seed text. No task change, no "
    "new object, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 168 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168);
"""

out = HEADER + MARKER + hourly + "\n" + NAMES + VERSION_ROW

# ---- post-asserts on the file -----------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.count("-- >>> derived:") == 1 and "LINEAGE-WAIVER" not in out
_top = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)        # outside every $$ body
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK|CALL |DROP |DELETE )", _top,
                     re.M | re.I)
assert "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" not in _top
assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" not in _top
assert len(re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.(\w+)", _top)) == 2
assert set(re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.(\w+)", _top)) == {"ALERT_CONFIG"}
assert "V168 requires V167 first" in out and "SELECT 168 AS VERSION" in out
for _new in (HEADER.replace("—", ""), MARKER, P1_READ_NEW, A14_COMMENT_NEW, A14_TITLE_NEW, A14_KEY_NEW,
             A14_COL_NEW, A14_WHERE_NEW, A14_GROUP_NEW, A18_HEAD_NEW.replace("—", ""), A18_SELECT_NEW,
             A18_COUNT_NEW, A18_DEDUPE_NEW, A20_NEW, S67_NEW, S91_HEAD_NEW, S91_TODAY_NEW, S117_NEW, A22_HEAD_NEW,
             A22_ERRS_NEW, A22_DETAIL_NEW, RET_168, NAMES, VERSION_ROW):
    assert _new.isascii(), _new[:60]                       # (V162's carried body keeps its own em dashes)
for line in out.splitlines():
    assert not line.lstrip().upper().startswith("CALL ") or line.startswith("        "), line

target = Path(os.environ.get("V168_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------------------------------
# Shared arm text for the read-only grids (taken from the DERIVED body, so a preview is the arm's own SQL)
# ---------------------------------------------------------------------------------------------------


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_arm14 = _between(hourly, A14_START, A14_END)
_arm18 = _between(hourly, A18_START, A18_END)
P14_SRC = _between(_arm14, "            SELECT TABLE_CATALOG_NAME AS DB", "        ) p ON c.RULE_ID")
P18_SRC = _between(_arm18, "            SELECT L.USER_NAME,", "        ) nn\n")
_sweep = _between(hourly, "    -- [auto-clear sweep] V091:", "    EXCEPTION\n")
_firing_open = "           AND (ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)) NOT IN (\n"
FIRING = _sweep[_sweep.index(_firing_open) + len(_firing_open):_sweep.rindex("           );\n")]
# the still-firing set as two top-level CTEs (cfg + firing): its own WITH is hoisted, never nested
_cfg_open, _cfg_close = "               WITH cfg AS (\n", "               )\n               SELECT c.RULE_ID"
FIRING_CFG = FIRING[FIRING.index(_cfg_open) + len(_cfg_open):FIRING.index(_cfg_close)]
FIRING_SEL = FIRING[FIRING.index(_cfg_close) + len("               )\n"):]
assert FIRING_CFG.strip().startswith("SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG")
assert FIRING_SEL.count("UNION ALL") == 2
assert FIRING_SEL.count("HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())") == 3
FIRING_CTES = f"""WITH cfg AS (
{FIRING_CFG}),
firing AS (
{FIRING_SEL})"""
KEY14 = ("'PIPE_COPY_FAILURES|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || "
         "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(p.FAIL_DAY)")
assert KEY14.replace("'PIPE_COPY_FAILURES|' || ", "c.RULE_ID || '|' || ") in _arm14
KEY18 = ("'SEC_NEW_ADMIN_NETWORK|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, '', "
         "'|FAILED')\n                   || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', "
         "nn.FIRST_SEEN)), 'YYYY-MM-DD')")
assert KEY18.replace("'SEC_NEW_ADMIN_NETWORK|' || ", "c.RULE_ID || '|' || ") in _arm18

PREFLIGHT = f"""-- ====================================================================================================
--  V168 PREFLIGHT (read-only; run BEFORE applying V168, in a Central session). SP_ALERT_SCAN keys and sweeps.
--  P168.1 and the second P168.4 grid compare ALERT_EVENTS.RAISED_AT (Central wall-clock TIMESTAMP_NTZ) with
--  CURRENT_TIMESTAMP(): a UTC worksheet shifts them 5-6h. Each grid runs the arm's OWN text (outputs/gen_v168.py).
--  Changes nothing.
-- ====================================================================================================

-- P168.1 R2-034 census: OPEN PERF events older than 48h that V162's sweep can no longer clear. WILL_AUTO_CLEAR =
--        the ones the first hourly scan after V168 resolves AUTO_CLEARED (their scope is below CLEAR now);
--        the rest are still firing and stay OPEN, which is correct. Re-run after the first scan: WILL_AUTO_CLEAR -> 0.
{FIRING_CTES}
SELECT ev.RULE_ID, COUNT(*) AS OPEN_OLDER_THAN_48H,
       COUNT_IF(f.DEDUPE_KEY IS NULL) AS WILL_AUTO_CLEAR,
       MIN(ev.RAISED_AT) AS OLDEST_RAISED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
LEFT JOIN (SELECT DISTINCT DEDUPE_KEY FROM firing) f
       ON f.DEDUPE_KEY = ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)
WHERE ev.STATUS = 'OPEN'
  AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')
  AND ev.RAISED_AT < DATEADD('hour', -48, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY 1;

-- P168.2 R2-035: what arm [14] keys now (per table and Central failure day, yesterday + today) and whether the key
--        already exists. A NULL EXISTING_STATUS on a yesterday row is the one legitimate apply-day event (a day
--        whose full count first crossed the band). HELD_BY_A_V162_EVENT on a today row = a V162 event (title
--        '... (24h)', possibly yesterday's carry-over re-minted after midnight) that absorbs today's new failures of
--        that band until midnight (V168 FIRST RUN (1)): watch that table on Operations today.
WITH p AS (
{P14_SRC}        )
SELECT p.DB, p.SCH, p.TBL, p.FAIL_DAY, p.FAILED_FILES, p.PIPE,
       {KEY14} AS DEDUPE_KEY_PREVIEW,
       e.STATUS AS EXISTING_STATUS, e.TITLE AS EXISTING_TITLE,
       e.TITLE LIKE '%failed file load(s) (24h)' AS HELD_BY_A_V162_EVENT
FROM p
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
       ON e.DEDUPE_KEY = {KEY14}
ORDER BY p.FAIL_DAY DESC, p.FAILED_FILES DESC;

-- P168.3 R2-035 carry-over duplicates (last 14 days): PIPE_COPY_FAILURES events whose key day had NO failure for
--        that table (a scan-day key over yesterday's files). Still-OPEN / ACK ones may be closed with the optional
--        owner repair R168.1; SNOOZED ones are left to wake.
WITH f AS (
    SELECT TABLE_CATALOG_NAME || '.' || TABLE_SCHEMA_NAME || '.' || TABLE_NAME AS FQN,
           TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY, COUNT(*) AS N
    FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
    WHERE LAST_LOAD_TIME >= DATEADD('day', -16, CURRENT_TIMESTAMP())
      AND STATUS IN ('Load failed', 'Partially loaded')
    GROUP BY 1, 2
)
SELECT e.EVENT_ID, e.STATUS, e.SEVERITY, e.RAISED_AT, e.DEDUPE_KEY, e.TITLE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN f ON f.FQN = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
           AND f.FAIL_DAY = TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 4))
WHERE e.RULE_ID = 'PIPE_COPY_FAILURES' AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND e.RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
  AND f.FQN IS NULL
ORDER BY e.RAISED_AT;

-- P168.4 R2-036 / R2-039: the pairs arm [18] sees now (first seen in the last 24h), with SUCCESSES and the V168 key,
--        and every past SEC_NEW_ADMIN_NETWORK event whose 26h login window held no success (a failures-only event
--        that said 'logged in'; resolve still-OPEN ones in Alerts as EXPECTED or NOISE).
WITH nn AS (
{P18_SRC}        )
SELECT nn.USER_NAME, nn.CLIENT_IP, nn.FIRST_SEEN, nn.LOGINS, nn.SUCCESSES,
       {KEY18} AS DEDUPE_KEY_PREVIEW
FROM nn
ORDER BY nn.FIRST_SEEN DESC;
SELECT e.EVENT_ID, e.RAISED_AT, e.STATUS, e.TITLE, e.METRIC_VALUE AS ATTEMPTS,
       COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
  ON L.USER_NAME = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
 AND COALESCE(L.CLIENT_IP, '(none)') = SPLIT_PART(e.DEDUPE_KEY, '|', 3)
 AND L.EVENT_TIMESTAMP BETWEEN DATEADD('hour', -26, e.RAISED_AT) AND e.RAISED_AT
WHERE e.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
GROUP BY 1, 2, 3, 4, 5
HAVING COUNT_IF(L.IS_SUCCESS = 'YES') = 0
ORDER BY e.RAISED_AT DESC;
"""

# ---------------------------------------------------------------------------------------------------
# RUN_NEXT PART B (read-only; after the apply). GET_DDL fragments carry no quote, backslash or newline.
# ---------------------------------------------------------------------------------------------------
PART_B_PRESENT = ("alert scan v14 (V168:", "AS FAIL_DAY", "GROUP BY 1, 2, 3, 4", "AS SUCCESSES", "(0 successful)",
                  "LEFT(nn.USER_NAME, 200)", "Changes (Recent grant changes)", "____-__-__", "/14 rule blocks ok",
                  "SP_SCAN_ETL_CYCLE", "AS RERAISED")
PART_B_ABSENT = ("failed file load(s) (24h)", "ev.RAISED_AT >= DATEADD", "budget_usd", "alert scan v13 (V162:")
_body162 = extract_proc(V162, "SP_ALERT_SCAN()")
_body162 = _body162[_body162.index("$$") + 2:_body162.rindex("$$")]
for _f in (*PART_B_PRESENT, *PART_B_ABSENT):
    assert not set(_f) & {"'", "\\", "\n", "\r"}, _f
assert all(f in _scan_body for f in PART_B_PRESENT) and not any(f in _scan_body for f in PART_B_ABSENT)
assert all(f in _body162 for f in PART_B_ABSENT) and not all(f in _body162 for f in PART_B_PRESENT)
_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()')"
_ddl_rows = "\nUNION ALL\n".join(
    [f"SELECT 'V168.2 hourly scan DDL has: {f}', IFF(CONTAINS({_DDL}, '{f}'), 'OK', 'FAIL: not the V168 body')"
     for f in PART_B_PRESENT]
    + [f"SELECT 'V168.2 hourly scan DDL lacks: {f}', IFF(NOT CONTAINS({_DDL}, '{f}'), 'OK', 'FAIL: still V162 text')"
       for f in PART_B_ABSENT])

PART_B = f"""\
-- PART B -- V168 verify (READ-ONLY; run in a Central session: V168.4 compares RAISED_AT, Central wall-clock, with
-- CURRENT_TIMESTAMP()). V168.1 + V168.2 right after the apply; V168.3 + V168.4 after the next :07 Central hourly
-- scan. Every RESULT should read OK (or the count named); paste the grids back.
SELECT 'V168.1 SCHEMA_VERSION has 168' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168) = 1,
           'OK', 'FAIL: V168 did not finish') AS RESULT
UNION ALL
SELECT 'V168.1 PIPE_COPY_FAILURES NAME (refreshed unless edited)',
       (SELECT IFF(MAX(NAME) = '{NAME_COPY_NEW}', 'OK', 'CHECK: kept operator text: ' || MAX(NAME))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'PIPE_COPY_FAILURES')
UNION ALL
SELECT 'V168.1 SEC_NEW_ADMIN_NETWORK NAME (refreshed unless edited)',
       (SELECT IFF(MAX(NAME) = '{NAME_NET_NEW}', 'OK', 'CHECK: kept operator text: ' || MAX(NAME))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK')
UNION ALL
{_ddl_rows};

-- V168.3 after the next hourly scan: the heartbeat reads 14/14 and no rule block failed.
SELECT 'V168.3 hourly scan heartbeat reads 14/14' AS CHECK_NAME,
       COALESCE((SELECT IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row') AS RESULT
UNION ALL
SELECT 'V168.3 no rule_block_failed / sweep failure (3h)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'AlertScan'
               AND ERROR_TYPE IN ('rule_block_failed', 'supersede_sweep_failed', 'autoclear_sweep_failed')
               AND LOGGED_AT >= DATEADD('hour', -3, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE');

-- V168.4 after the next hourly scan: the R2-034 census (PREFLIGHT P168.1) has nothing left to clear.
{FIRING_CTES}
SELECT 'V168.4 stranded OPEN PERF events left to clear' AS CHECK_NAME,
       IFF(COUNT_IF(f.DEDUPE_KEY IS NULL) = 0, 'OK', 'CHECK: the sweep has not run or failed') AS RESULT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
LEFT JOIN (SELECT DISTINCT DEDUPE_KEY FROM firing) f
       ON f.DEDUPE_KEY = ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)
WHERE ev.STATUS = 'OPEN'
  AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')
  AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP());
"""

# ---------------------------------------------------------------------------------------------------
# OPTIONAL owner repair (owner question: bulk-resolve historic duplicates?). Every statement is COMMENTED OUT;
# uncomment only on the owner's yes. A machine close (SUPERSEDED) is excluded from per-rule precision.
# ---------------------------------------------------------------------------------------------------
REPAIR = """\
-- Run in a Central session (RAISED_AT is Central wall-clock; the RUN_NEXT file leads with the timezone pin).
-- R168.1 OPTIONAL (owner decision): close the still-OPEN / ACK PIPE_COPY_FAILURES carry-over duplicates that
-- PREFLIGHT P168.3 lists (a scan-day key whose day had no failure for that table). SNOOZED rows are left to wake.
-- Uncomment to run; never DELETE, never rewrite DEDUPE_KEY.
-- UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
--    SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
--  WHERE e.RULE_ID = 'PIPE_COPY_FAILURES' AND e.STATUS IN ('OPEN', 'ACK')
--    AND e.RAISED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
--    AND NOT EXISTS (
--        SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY h
--        WHERE h.LAST_LOAD_TIME >= DATEADD('day', -16, CURRENT_TIMESTAMP())
--          AND h.STATUS IN ('Load failed', 'Partially loaded')
--          AND h.TABLE_CATALOG_NAME || '.' || h.TABLE_SCHEMA_NAME || '.' || h.TABLE_NAME
--              = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
--          AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', h.LAST_LOAD_TIME))
--              = TRY_TO_DATE(SPLIT_PART(e.DEDUPE_KEY, '|', 4)));
-- R168.2 SEC_NEW_ADMIN_NETWORK failures-only events (PREFLIGHT P168.4, second grid) and the R2-034 stranded PERF
-- events need no statement: the hourly scan clears the PERF ones itself; resolve the others in Alerts.
"""

for _name, _sql in (("PREFLIGHT", PREFLIGHT), ("PART B", PART_B), ("REPAIR", REPAIR)):
    assert "\r" not in _sql and _sql.isascii() and "$$" not in _sql, _name
_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")
_rp = os.environ.get("REPAIR_OUT")
if _rp:
    Path(_rp).write_text(REPAIR, encoding="utf-8", newline="\n")
    print(f"wrote REPAIR {_rp}")

print(f"V168 written: {target} ({len(out)} bytes)")
