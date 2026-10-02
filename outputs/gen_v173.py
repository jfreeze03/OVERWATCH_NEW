#!/usr/bin/env python3
"""Forward-generate V173: the two production failures of the V162-V172 apply (hotfix, 2026-10-02).

Reads V168__alert_scan_hourly_keys_and_sweeps.sql (the CURRENT definer of SP_ALERT_SCAN) and
V169__alert_scan_daily_windows_and_keys.sql (the CURRENT definer of SP_ALERT_SCAN_DAILY) ONLY -- tests/
test_proc_lineage.py; V170-V172 touch neither scan -- and emits, in order:

  guard (-20173, v < 172) -> marker + SP_ALERT_SCAN re-derived from V168 -> marker + SP_ALERT_SCAN_DAILY re-derived
  from V169 -> SCHEMA_VERSION 173.

SP_ALERT_SCAN, byte-identical to V168 except arm [18] SEC_NEW_ADMIN_NETWORK's dedupe guard:
  G18   V168's guard was ONE correlated NOT EXISTS whose every outer reference sat under an OR
        (A OR (R AND T AND (B1 OR B2))). Snowflake cannot decorrelate a subquery without a top-level equality to the
        outer row: 'SQL compilation error: Unsupported subquery type cannot be evaluated' on every hourly run from
        2026-10-02 07:08 (the arm's EXCEPTION logged rule_block_failed; the security rule raised nothing). The sqlite
        harness evaluates a correlated OR row by row, so CI never saw it. Now: a `recent` CTE (this rule's events of
        the last 48h with the date-stripped head precomputed -- R and T, uncorrelated) and three AND-ed NOT EXISTS
        whose correlations are plain equalities. NOT EXISTS(A OR (R AND T AND (B1 OR B2))) = NOT EXISTS(A) AND
        NOT EXISTS(R AND T AND B1) AND NOT EXISTS(R AND T AND B2) under three-valued logic (a WHERE keeps a row only
        when its predicate is TRUE, and OR is TRUE iff an operand is); R2-036 / R2-039 behaviour is unchanged
        (tests/migrations/test_v173_harness.py replays the V168 harness outcomes on both texts).
SP_ALERT_SCAN_DAILY, byte-identical to V169 except arm [24] COST_IDLE_OPPORTUNITY:
  Z24   ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) and '/ NULLIF(s.COVERED_DAYS, 0)'. Snowflake does
        not promise to apply the HAVING SUM(CREDITS_TOTAL) > 0 (or the WHERE COVERED_DAYS >= 7) before it computes the
        projection, and MART_WAREHOUSE_EFFICIENCY_DAILY holds CREDITS_TOTAL = 0 rows: 'Division by zero' on
        2026-10-01 06:49 under V163 (V169 carried the text). NULLIF returns its argument whenever it is not 0, so
        every surviving row is identical, and a zero reaching the projection gives a NULL IDLE_PCT that
        'IDLE_PCT >= 20' drops. Plus one comment line in the arm's header.
The RETURN labels and every other byte stay (the PART B GET_DDL checks read the V173 fragments instead).

No CALL, DROP, task, data repair or ALERT_EVENTS write at apply time. With PREFLIGHT_OUT / PART_B_OUT set, also
writes the read-only PREFLIGHT (P173.1-P173.5) and the RUN_NEXT PART B verify grids (V173.1-V173.3), built from the
SAME derived arm text; both open with the Central session pin. The byte-identity test never sets them.

Run: python outputs/gen_v173.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V168 = (MIG / "V168__alert_scan_hourly_keys_and_sweeps.sql").read_text(encoding="utf-8")
V169 = (MIG / "V169__alert_scan_daily_windows_and_keys.sql").read_text(encoding="utf-8")
NAME = "V173__alert_scan_supported_subquery_and_div0.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert text.count(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}") == 1, name
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap_in(text: str, start: str, end: str, old: str, new: str, label: str) -> str:
    """One count-1 swap inside the slice [start, end) only (an arm's span), the slice start unique in the body."""
    assert text.count(start) == 1, f"{label}: slice start {start!r} x{text.count(start)}"
    i = text.index(start)
    j = text.index(end, i)
    piece = text[i:j]
    assert piece.count(old) == 1, f"{label}: expected 1 anchor in the slice, got {piece.count(old)}"
    return text[:i] + piece.replace(old, new) + text[j:]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


# ---------------------------------------------------------------------------------------------------
# Arm slices (each start unique in its base body)
# ---------------------------------------------------------------------------------------------------
A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
A24 = ("    -- [24] COST_IDLE_OPPORTUNITY", "    -- [25] COST_SLEEP_POLLING")
RULE18 = "SEC_NEW_ADMIN_NETWORK"

# ---- G18: arm [18]'s dedupe guard ---------------------------------------------------------------------------
G18_CFG_OLD = ("        WITH cfg AS (\n"
               "            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n"
               "        )\n")
G18_CFG_NEW = ("        WITH cfg AS (\n"
               "            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n"
               "        ),\n"
               "        recent AS (\n"
               "            -- V173: this rule's events raised in the last 48h, the date-stripped head precomputed. It\n"
               "            -- carries the RULE_ID = b.RULE_ID (every b row is this rule) and 48h legs of the V168 guard,\n"
               "            -- uncorrelated; a NULL key can never satisfy the two guards that read it.\n"
               "            SELECT DEDUPE_KEY,\n"
               "                   LENGTH(DEDUPE_KEY) AS KEY_LEN,\n"
               "                   LEFT(DEDUPE_KEY, LENGTH(DEDUPE_KEY) - 10) AS KEY_HEAD\n"
               "            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n"
               f"            WHERE RULE_ID = '{RULE18}'\n"
               "              AND RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
               "              AND DEDUPE_KEY IS NOT NULL\n"
               "        )\n")
G18_GUARD_OLD = (
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
G18_GUARD_NEW = (
    "        -- V173: the V168 guard as three AND-ed NOT EXISTS, each correlated by plain equalities only. V168 had one\n"
    "        -- NOT EXISTS whose every outer reference sat under an OR, which Snowflake cannot decorrelate ('Unsupported\n"
    "        -- subquery type cannot be evaluated', every hourly run from 2026-10-02 07:08). NOT EXISTS (A OR B OR C) =\n"
    "        -- NOT EXISTS (A) AND NOT EXISTS (B) AND NOT EXISTS (C): the same rows are kept.\n"
    "        WHERE NOT EXISTS (   -- (1) the exact key (user|IP[|FAILED]|first-seen day), any age: R2-036\n"
    "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "        )\n"
    "          AND NOT EXISTS (   -- (2) this pair's V162 undated key (date and '|FAILED' stripped), last 48h\n"
    "            SELECT 1 FROM recent r\n"
    "            WHERE r.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')\n"
    "        )\n"
    "          AND NOT EXISTS (   -- (3) the same exact base and outcome on another first-seen day, last 48h: R2-039\n"
    "            SELECT 1 FROM recent r\n"
    "            WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)\n"
    "              AND r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)\n"
    "        );\n")

# ---- Z24: arm [24]'s two divisions ---------------------------------------------------------------------------
Z24_HEAD_OLD = ("    --      never-suspend warehouse as a NULL auto_suspend), like insights.show_auto_suspend and the mart "
                "loader.\n    BEGIN\n")
Z24_HEAD_NEW = ("    --      never-suspend warehouse as a NULL auto_suspend), like insights.show_auto_suspend and the mart "
                "loader.\n"
                "    --      V173: both divisions guard their own divisor (NULLIF). Snowflake may compute a projection\n"
                "    --      before the HAVING / WHERE that drops a zero ('Division by zero', 2026-10-01 06:49, a\n"
                "    --      zero-credit warehouse); a NULL IDLE_PCT fails 'IDLE_PCT >= 20', so the events are the same.\n"
                "    BEGIN\n")
Z24_PCT_OLD = "                   ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,\n"
Z24_PCT_NEW = "                   ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) AS IDLE_PCT,\n"
Z24_USD_OLD = "                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,\n"
Z24_USD_NEW = ("                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / NULLIF(s.COVERED_DAYS, 0) * 30, 2) "
               "AS MONTHLY_USD,\n")

# ---------------------------------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------------------------------
hourly168 = extract_proc(V168, "SP_ALERT_SCAN()")
daily169 = extract_proc(V169, "SP_ALERT_SCAN_DAILY()")
assert hourly168.count(G18_GUARD_OLD) == 1 and hourly168.count("fails := fails + 1") == 14
assert daily169.count(Z24_PCT_OLD) == 1 and daily169.count(Z24_USD_OLD) == 1
assert daily169.count("fails := fails + 1") == 14
assert " recent r" not in hourly168 and "recent AS (" not in hourly168

hourly = _swap_in(hourly168, *A18, G18_CFG_OLD, G18_CFG_NEW, "G18a")
hourly = _swap_in(hourly, *A18, G18_GUARD_OLD, G18_GUARD_NEW, "G18b")
daily = _swap_in(daily169, *A24, Z24_HEAD_OLD, Z24_HEAD_NEW, "Z24a")
daily = _swap_in(daily, *A24, Z24_PCT_OLD, Z24_PCT_NEW, "Z24b")
daily = _swap_in(daily, *A24, Z24_USD_OLD, Z24_USD_NEW, "Z24c")

# ---- post-asserts on the derived bodies --------------------------------------------------------------------
_a18 = _between(hourly, *A18)
_code18 = re.sub(r"--[^\n]*", "", _a18)                     # (no '--' inside any of the arm's string literals)
assert "'--" not in _a18 and _code18.count("NOT EXISTS (") == 3
assert " OR " not in _between(_code18, "        WHERE NOT EXISTS (", ";\n")
assert _code18.count("FROM recent r") == 2 and _code18.count("recent AS (") == 1
assert hourly.replace(_a18, "") == hourly168.replace(_between(hourly168, *A18), "")   # nothing outside [18] moved
_a24 = _between(daily, *A24)
assert "/ i.TOTAL_CREDITS" not in _a24 and "/ s.COVERED_DAYS" not in _a24
assert daily.replace(_a24, "") == daily169.replace(_between(daily169, *A24), "")      # nothing outside [24] moved
for _b, _base in ((hourly, hourly168), (daily, daily169)):
    _body = _b[_b.index("$$") + 2:_b.rindex("$$")]
    assert "$$" not in _body and "\\" not in _body
    assert _b.count("fails := fails + 1") == 14                                       # tallies unchanged
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _b)) == set(
        re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _base))
    assert re.findall(r"RETURN '[^']*'", _b) == re.findall(r"RETURN '[^']*'", _base)   # labels carried

# ---------------------------------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------------------------------
HEADER = f"""-- {NAME}
--
-- Hotfix for the two production failures of the V162-V172 apply (2026-10-02). Snowflake-only failures the
-- in-memory sqlite harness cannot see: it runs any correlated subquery row by row and returns NULL for x / 0.
--
-- WHY: (1) Since V168 (applied 2026-10-02), the hourly SP_ALERT_SCAN arm [18] SEC_NEW_ADMIN_NETWORK fails on every run
-- from 07:08 Central with 'SQL compilation error: Unsupported subquery type cannot be evaluated'. Its R2-036 / R2-039
-- dedupe guard was one correlated NOT EXISTS whose every outer reference sat under an OR, and Snowflake can only
-- decorrelate a subquery that has a top-level equality to the outer row. The arm's EXCEPTION logs
-- rule_block_failed and the scan carries on at 13/14, so a SECURITY rule raised nothing. (2) The nightly
-- SP_ALERT_SCAN_DAILY arm [24] COST_IDLE_OPPORTUNITY failed with 'Division by zero' (2026-10-01 06:49, under V163;
-- V169 carried the text): IDLE_CREDITS / TOTAL_CREDITS was guarded only by HAVING SUM(CREDITS_TOTAL) > 0, which
-- Snowflake does not promise to apply before it computes the projection, and MART_WAREHOUSE_EFFICIENCY_DAILY holds
-- CREDITS_TOTAL = 0 rows (a warehouse with queries and no metering).
--
--   ~ SP_ALERT_SCAN re-derived from V168 (its current definer), byte-identical except arm [18]'s dedupe guard: a
--     `recent` CTE (this rule's events of the last 48h, LENGTH and the date-stripped head precomputed) and three
--     AND-ed NOT EXISTS, each correlated by plain equalities: (1) the exact key, any age; (2) the pair's V162
--     undated key, last 48h; (3) the same exact base and outcome on another first-seen day, last 48h. NOT EXISTS
--     (A OR B OR C) = NOT EXISTS (A) AND NOT EXISTS (B) AND NOT EXISTS (C), so the same candidates survive: a
--     network quiet 90+ days alerts again (R2-036), a failures-only key never swallows the success (R2-039), a
--     late earlier login across Central midnight or a V162 undated key never raises one episode twice.
--   ~ SP_ALERT_SCAN_DAILY re-derived from V169 (its current definer), byte-identical except arm [24]:
--     ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) and '/ NULLIF(s.COVERED_DAYS, 0)' (one comment
--     line added). Every surviving row computes the same values; a zero-credit warehouse is still dropped.
--   The RETURN labels, the 14-block tallies, every other arm and sweep are unchanged.
--
-- COST: one 48h ALERT_EVENTS read bounded by RULE_ID in arm [18] (V168 read the same rows under its OR); none in [24].
-- LATENCY: hourly (:07 Central) and daily (06:50 Central), as before.
-- FIRST RUN: the next hourly scan raises the admin new-network pairs first seen in its 24h window that have no event
-- (PREFLIGHT P173.2 lists them). A pair first seen while the arm was failing and more than 24h before that scan is
-- never raised by the arm: PREFLIGHT P173.3 lists every pair first seen since V168's apply, with whether an event
-- exists; review the unalerted ones in Security > Access. The next daily scan evaluates COST_IDLE_OPPORTUNITY again
-- (PREFLIGHT P173.4 lists the zero-credit warehouses that hit the division, P173.5 what the arm would raise). The
-- OPS_SCAN_DEGRADED events the failures raised are true history: resolve them in Alerts once PART B reads OK.
-- ROLLBACK: re-run V168's SP_ALERT_SCAN (the CREATE PROCEDURE in V168__alert_scan_hourly_keys_and_sweeps.sql) and
-- V169's SP_ALERT_SCAN_DAILY (the CREATE PROCEDURE in V169__alert_scan_daily_windows_and_keys.sql) -- which brings
-- both failures back; prefer disabling a rule in Alerts > Rules.
-- Apply AFTER V172 (alone, any time; no repairs). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20173, 'V173 requires V172 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 172) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARK_H = ("-- >>> derived:SP_ALERT_SCAN  (from V168; arm [18] SEC_NEW_ADMIN_NETWORK dedupe guard as three AND-ed "
          "NOT EXISTS with plain-equality correlations over a 48h recent CTE -- Snowflake rejected V168's OR-correlated "
          "subquery, V173)\n")
MARK_D = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V169; arm [24] COST_IDLE_OPPORTUNITY divisions NULLIF-guarded -- "
          "prod Division by zero, V173)\n")

DESCRIPTION = (
    "Hotfix for two production failures of the V162-V172 apply. SP_ALERT_SCAN re-derived from V168, byte-identical "
    "except arm [18] SEC_NEW_ADMIN_NETWORK: its dedupe guard was one correlated NOT EXISTS whose every outer "
    "reference sat under an OR, which Snowflake cannot decorrelate (Unsupported subquery type cannot be evaluated, "
    "every hourly run from 2026-10-02 07:08); now a 48h recent CTE and three AND-ed NOT EXISTS correlated by plain "
    "equalities (the exact key, the V162 undated key, the same base and outcome on another day), the same rows. "
    "SP_ALERT_SCAN_DAILY re-derived from V169, byte-identical except arm [24] COST_IDLE_OPPORTUNITY: both divisions "
    "NULLIF-guarded (Division by zero on 2026-10-01 under V163, a zero-credit warehouse row reaching the projection "
    "before the HAVING). RETURN labels and tallies unchanged. No task change, no new object, no data repair, no "
    "procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION and "$" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 173 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173);
"""

out = HEADER + MARK_H + hourly + "\n" + MARK_D + daily + "\n" + VERSION_ROW

# ---- post-asserts on the file ---------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 2 and out.count("$$") == 6
assert out.count("-- >>> derived:") == 2 and "LINEAGE-WAIVER" not in out
_top = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)        # outside every $$ body
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER |EXECUTE TASK|CALL |DROP |DELETE |UPDATE |MERGE )",
                     _top, re.M | re.I)
assert "ALERT_EVENTS" not in re.sub(r"--[^\n]*", "", _top)                          # no data write at apply
assert "V173 requires V172 first" in out and "SELECT 173 AS VERSION" in out
for _new in (HEADER, MARK_H, MARK_D, G18_CFG_NEW, G18_GUARD_NEW, Z24_HEAD_NEW, Z24_PCT_NEW, Z24_USD_NEW, VERSION_ROW):
    assert _new.isascii(), _new[:60]                       # (the carried bodies keep their own em dashes)
for line in out.splitlines():
    assert not line.lstrip().upper().startswith("CALL ") or line.startswith("        "), line

target = Path(os.environ.get("V173_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------------------------------
# Read-only PREFLIGHT (P173.1-P173.5), built from the DERIVED arm text. Central session first: RAISED_AT, LOGGED_AT
# and APPLIED_AT are Central wall-clock TIMESTAMP_NTZ, compared with CURRENT_TIMESTAMP() and LOGIN_HISTORY times.
# ---------------------------------------------------------------------------------------------------
TZ_PIN = "ALTER SESSION SET TIMEZONE = 'America/Chicago';"
APPLIED_168 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168)"
APPLIED_173 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)"
CREDIT_PRICE = ("(SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) "
                "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)")
assert re.search(r"COALESCE\(TRY_TO_DOUBLE\(MAX\(IFF\(KEY = 'CREDIT_PRICE_USD', VALUE, NULL\)\)\), 3\.68\)", daily)


def _statement(arm: str) -> str:
    """The arm's ``WITH ... SELECT`` (its INSERT column list dropped), dedented 8 spaces."""
    stmt = arm[arm.index("        WITH cfg AS (\n"):arm.index(";\n    EXCEPTION")]
    assert all(not ln or ln.startswith("        ") for ln in stmt.splitlines())
    return "".join(ln[8:] + "\n" for ln in stmt.splitlines()).rstrip("\n")


STMT18 = _statement(_a18)
STMT24 = _statement(_a24).replace(":credit_price", CREDIT_PRICE)
assert STMT18.count("SELECT b.RULE_ID") == 1 and STMT24.count("SELECT b.RULE_ID") == 1
assert not re.search(r"(?<![:\w]):[a-z_]+\b", STMT18 + STMT24.replace("HH24:MI", ""))
NN18 = _between(_a18, "            SELECT L.USER_NAME,", "        ) nn\n")
HAVING18 = "            HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n"
assert NN18.count(HAVING18) == 1
NN_SINCE_168 = NN18.replace(HAVING18, f"            HAVING MIN(L.EVENT_TIMESTAMP) >= {APPLIED_168}\n")
WIN24 = _between(_a24, "        clk AS (\n", "        cov AS (\n").rstrip().rstrip(",")
assert WIN24.count("win AS (") == 1 and WIN24.rstrip().endswith(")")
WIN24 = "".join(ln[8:] + "\n" for ln in WIN24.splitlines()).rstrip("\n")
_WATCH = ("supersede_sweep_failed", "ref_gap_scan_failed", "ref_gap_check_failed")

PREFLIGHT = f"""\
-- ====================================================================================================
--  V173 PREFLIGHT (read-only; run BEFORE applying V173). Each grid runs the arms' OWN V173 text
--  (outputs/gen_v173.py). The first statement pins the session to Central: RAISED_AT, LOGGED_AT and APPLIED_AT are
--  Central wall-clock. Changes nothing.
-- ====================================================================================================
{TZ_PIN}

-- P173.1 the evidence since V168's apply: the two failing arms (rule_block_failed for SEC_NEW_ADMIN_NETWORK hourly,
--        COST_IDLE_OPPORTUNITY daily) and three watch items the subquery sweep rated possible, not likely (the V067
--        supersede sweep, arm [10] SEC_CRED_EXPIRY, the V171 ref-gap scan). Expect rows for the first two only.
SELECT l.ERROR_TYPE, l.CONTEXT, COUNT(*) AS N, MIN(l.LOGGED_AT) AS FIRST_AT, MAX(l.LOGGED_AT) AS LAST_AT,
       LEFT(MAX_BY(l.ERROR_MESSAGE, l.LOGGED_AT), 300) AS LAST_MESSAGE
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG l
WHERE l.LOGGED_AT >= DATEADD('day', -2, {APPLIED_168})
  AND ((l.ERROR_TYPE = 'rule_block_failed'
        AND (l.CONTEXT LIKE 'rule {RULE18} %' OR l.CONTEXT LIKE 'rule COST_IDLE_OPPORTUNITY %'
             OR l.CONTEXT LIKE 'rule SEC_CRED_EXPIRY %'))
       OR l.ERROR_TYPE IN ({", ".join(f"'{t}'" for t in _WATCH)}))
GROUP BY 1, 2
ORDER BY 1, 2;

-- P173.2 what the next hourly scan raises for {RULE18} after V173 (arm [18]'s own statement, its new guard
--        included): admin pairs first seen in the last 24h that have no event yet.
{STMT18};

-- P173.3 every admin user + IP pair first seen (against the 90-day baseline) since V168's apply, while arm [18] was
--        failing, and whether any {RULE18} event exists for it. NOT_ALERTED pairs with RAISED_BY_NEXT_SCAN = FALSE
--        are past the arm's 24h window: review them by hand in Security > Access (login history).
WITH nn AS (
{NN_SINCE_168}        ),
ev AS (
    SELECT DISTINCT SPLIT_PART(DEDUPE_KEY, '|', 2) AS USER_PART, SPLIT_PART(DEDUPE_KEY, '|', 3) AS IP_PART
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    WHERE RULE_ID = '{RULE18}'
      AND RAISED_AT >= DATEADD('day', -2, {APPLIED_168})
)
SELECT nn.USER_NAME, nn.CLIENT_IP, nn.FIRST_SEEN, nn.LOGINS, nn.SUCCESSES,
       nn.FIRST_SEEN >= DATEADD('hour', -24, CURRENT_TIMESTAMP()) AS RAISED_BY_NEXT_SCAN,
       ev.USER_PART IS NULL AS NOT_ALERTED
FROM nn
LEFT JOIN ev
       ON ev.USER_PART = LEFT(nn.USER_NAME, 200)
      AND ev.IP_PART = nn.CLIENT_IP
ORDER BY nn.FIRST_SEEN;

-- P173.4 the zero-credit warehouses of arm [24]'s 14-day window (the arm's own clk + win CTEs): the rows that
--        reached IDLE_CREDITS / TOTAL_CREDITS before the HAVING dropped them. V173 never divides by them.
WITH {WIN24}
SELECT WAREHOUSE_NAME, COUNT(*) AS DAYS, SUM(CREDITS_TOTAL) AS TOTAL_CREDITS, SUM(BILLED_HOURS) AS BILLED_HOURS,
       SUM(ACTIVE_HOURS) AS ACTIVE_HOURS
FROM win
GROUP BY WAREHOUSE_NAME
HAVING SUM(CREDITS_TOTAL) = 0
ORDER BY WAREHOUSE_NAME;

-- P173.5 what the next daily scan raises for COST_IDLE_OPPORTUNITY after V173 (arm [24]'s own statement, the price
--        read from SETTINGS like the scan).
{STMT24};
"""

# ---------------------------------------------------------------------------------------------------
# RUN_NEXT PART B (read-only; after the apply). GET_DDL fragments carry no quote, backslash or newline.
# ---------------------------------------------------------------------------------------------------
DDL_PRESENT = {
    "SP_ALERT_SCAN": ("AS KEY_HEAD", "FROM recent r", "r.KEY_LEN = LENGTH(b.DEDUPE_KEY)", "alert scan v14 (V168:"),
    "SP_ALERT_SCAN_DAILY": ("NULLIF(i.TOTAL_CREDITS, 0)", "NULLIF(s.COVERED_DAYS, 0)", "alert scan daily v6 (V169:"),
}
DDL_ABSENT = {
    "SP_ALERT_SCAN": ("OR (e.RULE_ID = b.RULE_ID",),
    "SP_ALERT_SCAN_DAILY": ("i.IDLE_CREDITS / i.TOTAL_CREDITS", ":credit_price / s.COVERED_DAYS"),
}
_bodies = {"SP_ALERT_SCAN": (hourly, hourly168), "SP_ALERT_SCAN_DAILY": (daily, daily169)}
for _p, (_new_b, _old_b) in _bodies.items():
    for _f in (*DDL_PRESENT[_p], *DDL_ABSENT[_p]):
        assert not set(_f) & {"'", "\\", "\n", "\r"}, _f
    assert all(_f in _new_b for _f in DDL_PRESENT[_p]) and not any(_f in _new_b for _f in DDL_ABSENT[_p]), _p
    assert all(_f in _old_b for _f in DDL_ABSENT[_p]) and not all(_f in _old_b for _f in DDL_PRESENT[_p]), _p
_ddl_rows = []
for _p in DDL_PRESENT:
    _ddl = f"GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{_p}()')"
    _ddl_rows += [f"SELECT 'V173.1 {_p} DDL has: {_f}', IFF(CONTAINS({_ddl}, '{_f}'), 'OK', 'FAIL: not the V173 body')"
                  for _f in DDL_PRESENT[_p]]
    _ddl_rows += [f"SELECT 'V173.1 {_p} DDL lacks: {_f}', "
                  f"IFF(NOT CONTAINS({_ddl}, '{_f}'), 'OK', 'FAIL: still the pre-V173 text')" for _f in DDL_ABSENT[_p]]
_ddl_union = "\nUNION ALL\n".join(_ddl_rows)
HEARTBEAT = {"V173.2": ("ALERT_SCAN_HOURLY", "alert scan 14/14 rule blocks ok", RULE18, "hourly"),
             "V173.3": ("ALERT_SCAN_DAILY", "alert scan daily 14/14 rule blocks ok (daily)", "COST_IDLE_OPPORTUNITY",
                        "daily")}
for _src, _status, _rule, _ in HEARTBEAT.values():
    assert f"'{_src}'" in (hourly if _src.endswith("HOURLY") else daily)
    assert f"'rule {_rule} - other rules unaffected'" in (hourly if _rule == RULE18 else daily)
assert hourly.count("'alert scan ' || (14 - :fails) || '/14 rule blocks ok'") == 2
assert daily.count("'alert scan daily ' || (14 - :fails) || '/14 rule blocks ok (daily)'") == 2


def _scan_grid(check: str) -> str:
    src, status, rule, kind = HEARTBEAT[check]
    return f"""\
SELECT '{check} {kind} scan ran after the apply' AS CHECK_NAME,
       CASE WHEN {APPLIED_173} IS NULL THEN 'FAIL: SCHEMA_VERSION has no 173 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= {APPLIED_173}, 'OK',
                                      'WAIT: no {kind} scan since the apply (last '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = '{src}'),
                          'FAIL: no {src} row') END AS RESULT
UNION ALL
SELECT '{check} {kind} heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(STATUS) = '{status}', 'OK', 'CHECK: ' || MAX(STATUS))
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = '{src}'),
                'FAIL: no {src} row')
UNION ALL
SELECT '{check} no {rule} rule_block_failed since the apply',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
               AND CONTEXT LIKE 'rule {rule} %'
               AND LOGGED_AT >= {APPLIED_173}) = 0,
           'OK', 'FAIL: see APP_ERROR_LOG ERROR_MESSAGE')
UNION ALL
SELECT '{check} no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= {APPLIED_173});"""


PART_B = f"""\
-- PART B -- V173 verify (READ-ONLY after the session pin). V173.1 right after the apply; V173.2 after the next :07
-- Central hourly scan; V173.3 after the next 06:50 Central daily scan. Every RESULT should read OK (a WAIT means that
-- scan has not run since the apply yet); paste the grids back. APPLIED_AT, LAST_LOAD_TS and LOGGED_AT are all Central
-- wall-clock, so the apply itself is the boundary.
{TZ_PIN}

SELECT 'V173.1 SCHEMA_VERSION has 173' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173) = 1,
           'OK', 'FAIL: V173 did not finish') AS RESULT
UNION ALL
{_ddl_union};

-- V173.2 after the next hourly scan (:07 Central): the scan ran, reads 14/14, and arm [18] logged no failure.
{_scan_grid("V173.2")}

-- V173.3 after the next daily scan (06:50 Central): the scan ran, reads 14/14, and arm [24] logged no failure.
{_scan_grid("V173.3")}
"""

for _name, _sql in (("PREFLIGHT", PREFLIGHT), ("PART B", PART_B)):
    assert "\r" not in _sql and "$$" not in _sql, _name
assert PART_B.isascii()
_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")

print(f"V173 written: {target} ({len(out)} bytes)")
