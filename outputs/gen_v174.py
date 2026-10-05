#!/usr/bin/env python3
"""Forward-generate V174: SNOW_PRI_GFR_PRD_ALFA_DSA joins the hourly security arms' admin-role lists (2026-10-05).

The owner's 2026-10-05 access decision makes every DIRECT holder of SNOW_PRI_GFR_PRD_ALFA_DSA an OVERWATCH admin
(app 4.610: every page, the in-app writes and the account-level levers, like OPERATOR_USERS). A grant of that role
is now an admin grant and its holders are admin users, so the hourly arms that enumerate admin roles watch it too
(design D14; until V174 a DSA grant showed only as a risk-80 PRIVILEGE change, V166).

Reads V173__alert_scan_supported_subquery_and_div0.sql (the CURRENT definer of SP_ALERT_SCAN) ONLY --
tests/test_proc_lineage.py -- and emits, in order:

  guard (-20174, v < 173) -> marker + SP_ALERT_SCAN re-derived from V173 -> one guarded rule NAME refresh
  (SEC_ADMIN_GRANT) -> SCHEMA_VERSION 174.

SP_ALERT_SCAN, byte-identical to V173 except three arms -- the only arms of either scan whose SQL enumerates admin
roles -- each gaining the role at the END of its one ROLE IN list, plus a two-line V174 comment above the arm's BEGIN:
  R18  [18] SEC_NEW_ADMIN_NETWORK: the admin users whose new networks it watches (ACCOUNTADMIN, SNOW_ACCOUNTADMINS,
       SNOW_SYSADMINS) -- the SQL twin of the app's admin new-network panel (security_sql.ADMIN_HOLDER_ROLES).
  R26  [26] SEC_LOGIN_TAKEOVER: the adm CTE's admin-tier roles (a takeover of a direct holder is CRITICAL).
  R27  [27] SEC_ADMIN_GRANT: the ag CTE's admin-tier roles (one event per direct grant).
  [26] and [27] keep ONE list, app/data/security_sql.ALERT_ADMIN_ROLES (tests/test_security_alert_parity.py).
SP_ALERT_SCAN_DAILY is not re-derived (no daily arm enumerates admin roles). The V166 / V167 break-glass lists
(ACCOUNTADMIN, SNOW_ACCOUNTADMINS: change-risk scoring and posture counts) are a different tier and stay.
The RETURN labels, the 14-block tally, every other arm, sweep and gate are unchanged.

No CALL, DROP, task change or ALERT_EVENTS write at apply time; the one data write is the SEC_ADMIN_GRANT NAME refresh,
guarded on its V162 seed text (an operator's own edit survives). With PREFLIGHT_OUT / PART_B_OUT set, also writes the
read-only PREFLIGHT (P174.1-P174.4) and the RUN_NEXT PART B verify grids (V174.1-V174.3), built from the SAME derived
arm text; both open with the Central session pin. The byte-identity test never sets them.

Run: python outputs/gen_v174.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V173 = (MIG / "V173__alert_scan_supported_subquery_and_div0.sql").read_text(encoding="utf-8")
NAME = "V174__alert_scan_dsa_admin_role.sql"
DSA = "SNOW_PRI_GFR_PRD_ALFA_DSA"


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


def _in_list(arm: str) -> list[str]:
    """The roles of the arm's one ROLE IN (...) list, in order."""
    (inlist,) = re.findall(r"\bROLE IN \(([^)]*)\)", arm)
    return re.findall(r"'(\w+)'", inlist)


# ---------------------------------------------------------------------------------------------------
# Arm slices (each start unique in the body; each end the first such line after it)
# ---------------------------------------------------------------------------------------------------
A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
A26 = ("    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
A27 = ("    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN   -- V157 cadence gate: [22]")

# ---- R18: arm [18]'s admin-holder list + its header note ---------------------------------------------------
R18_OLD = "                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')\n"
R18_NEW = f"                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', '{DSA}')\n"
H18_OLD = ("    --      key, never raises one episode twice, and a failures-only key never swallows the success.\n"
           "    BEGIN\n")
H18_NEW = ("    --      key, never raises one episode twice, and a failures-only key never swallows the success.\n"
           f"    --      V174 (owner 2026-10-05): + {DSA}. Its direct holders are OVERWATCH admins (in-app\n"
           "    --      writes and the account levers), so the arm watches their logins like the three it watched.\n"
           "    BEGIN\n")

# ---- R26: arm [26]'s admin-tier list (the adm CTE) + its header note ----------------------------------------
R26_OLD = ("             AND g.ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',\n"
           "                            'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')\n")
R26_NEW = ("             AND g.ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',\n"
           f"                            'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', '{DSA}')\n")
H26_OLD = ("    --      it; a CRIT/WARN band crossing re-fires and the V067 sweep supersedes the WARN row.\n"
           "    BEGIN\n")
H26_NEW = ("    --      it; a CRIT/WARN band crossing re-fires and the V067 sweep supersedes the WARN row.\n"
           f"    --      V174 (owner 2026-10-05): the admin-tier list adds {DSA} (its direct\n"
           "    --      holders are OVERWATCH admins), so a takeover of one is CRITICAL at any hour.\n"
           "    BEGIN\n")

# ---- R27: arm [27]'s admin-tier list (the ag CTE) + its header note -----------------------------------------
R27_OLD = ("            WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',\n"
           "                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')\n")
R27_NEW = ("            WHERE ROLE IN ('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',\n"
           f"                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', '{DSA}')\n")
H27_OLD = ("    --      ends in the grant's Central CREATED_ON to the millisecond with an explicit format.\n"
           "    BEGIN\n")
H27_NEW = ("    --      ends in the grant's Central CREATED_ON to the millisecond with an explicit format.\n"
           f"    --      V174 (owner 2026-10-05): the owner list adds {DSA}; a direct grant of it\n"
           "    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.\n"
           "    BEGIN\n")

SWAPS = (("R18", A18, R18_OLD, R18_NEW), ("H18", A18, H18_OLD, H18_NEW),
         ("R26", A26, R26_OLD, R26_NEW), ("H26", A26, H26_OLD, H26_NEW),
         ("R27", A27, R27_OLD, R27_NEW), ("H27", A27, H27_OLD, H27_NEW))

# ---------------------------------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------------------------------
hourly173 = extract_proc(V173, "SP_ALERT_SCAN()")
assert DSA not in V173 and hourly173.count("fails := fails + 1") == 14
# the three arms are the only ROLE IN lists of either scan (find them all: the daily scan names no admin role)
assert len(re.findall(r"\bROLE IN \(", hourly173)) == 3
assert not re.search(r"\bROLE IN \(", extract_proc(V173, "SP_ALERT_SCAN_DAILY()"))
assert not re.search(r"'SNOW_SYSADMINS'|'SNOW_ACCOUNTADMINS'", extract_proc(V173, "SP_ALERT_SCAN_DAILY()"))
_spans = {"R18": A18, "R26": A26, "R27": A27}
BASE_LISTS = {k: _in_list(_between(hourly173, *span)) for k, span in _spans.items()}
assert BASE_LISTS["R18"] == ["ACCOUNTADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"]
assert BASE_LISTS["R26"] == BASE_LISTS["R27"] and len(BASE_LISTS["R27"]) == 7
_outside = hourly173
for _span in _spans.values():
    _outside = _outside.replace(_between(hourly173, *_span), "")
assert "ROLE IN (" not in _outside and "'SNOW_SYSADMINS'" not in _outside      # no fourth admin list anywhere

hourly = hourly173
for _label, _span, _old, _new in SWAPS:
    hourly = _swap_in(hourly, *_span, _old, _new, _label)

# ---- post-asserts on the derived body -------------------------------------------------------------------
for _k, _span in _spans.items():
    assert _in_list(_between(hourly, *_span)) == [*BASE_LISTS[_k], DSA], _k               # appended, nothing else
_new_outside = hourly
for _span in _spans.values():
    _new_outside = _new_outside.replace(_between(hourly, *_span), "")
assert _new_outside == _outside                                                         # nothing outside moved
assert hourly.count(f"'{DSA}'") == 3 and hourly.count(DSA) == 6                         # 3 lists + 3 notes
_body = hourly[hourly.index("$$") + 2:hourly.rindex("$$")]
assert "$$" not in _body and "\\" not in _body
assert hourly.count("fails := fails + 1") == 14                                         # tally unchanged
assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", hourly)) == set(
    re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", hourly173))
assert re.findall(r"RETURN '[^']*'", hourly) == re.findall(r"RETURN '[^']*'", hourly173)   # labels carried
for _label, _span, _old, _new in SWAPS:
    assert _new.isascii(), _label
    if _label.startswith("H"):                     # a note only adds comment lines, and never opens a ROLE IN list
        _added = _new[len(_old) - len("    BEGIN\n"):-len("    BEGIN\n")]
        assert _new == _old.replace("    BEGIN\n", _added + "    BEGIN\n"), _label
        assert _added.startswith("    --      V174 (owner 2026-10-05): ") and "ROLE IN" not in _added, _label
        assert len(_added.splitlines()) == 2 and all(ln.startswith("    --      ") for ln in _added.splitlines())

# ---------------------------------------------------------------------------------------------------
# The one data write: the SEC_ADMIN_GRANT rule NAME follows the new list (V168's guarded-refresh shape)
# ---------------------------------------------------------------------------------------------------
GRANT_NAME_OLD = ("Admin-tier role granted directly to a user (ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, "
                  "ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS), one event per grant")
GRANT_NAME_NEW = ("Admin-tier role granted directly to a user (ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, "
                  f"ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS, {DSA}), one event per grant")
assert f"({', '.join(BASE_LISTS['R27'])})" in GRANT_NAME_OLD
assert f"({', '.join([*BASE_LISTS['R27'], DSA])})" in GRANT_NAME_NEW
assert len(GRANT_NAME_NEW) <= 200 and "'" not in GRANT_NAME_NEW                        # V004 NAME VARCHAR(200)
NAME_REFRESH = f"""
-- Rule NAME text follows the new list. The refresh touches the row only while NAME still equals its V162 seed, so an
-- operator's own edit survives; a re-run is a no-op. ALERT_CONFIG has no DESCRIPTION column.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET NAME = '{GRANT_NAME_NEW}'
 WHERE RULE_ID = 'SEC_ADMIN_GRANT'
   AND NAME = '{GRANT_NAME_OLD}';
"""

# ---------------------------------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------------------------------
HEADER = f"""-- {NAME}
--
-- The owner's 2026-10-05 access decision: every direct holder of {DSA} is an OVERWATCH
-- admin (app 4.610: every page, the in-app writes and the account-level levers, like the named admins). So a grant
-- of that role is an admin grant and its holders are admin users, and the hourly security arms that enumerate admin
-- roles now watch it too (design D14).
--
-- WHY: until V174 a grant of {DSA} to a user raised no SEC_ADMIN_GRANT (only a risk-80 PRIVILEGE
-- change in Security > Changes, V166), a takeover of a holder was CRITICAL only off-hours, and a holder's login from a
-- new network raised no SEC_NEW_ADMIN_NETWORK: an OVERWATCH admin, watched as a regular user.
--
--   ~ SP_ALERT_SCAN re-derived from V173 (its current definer), byte-identical except three arms -- the only arms of
--     either scan whose SQL lists admin roles -- each adding {DSA} at the end of its one list, plus
--     a two-line V174 note above the arm's BEGIN: [18] SEC_NEW_ADMIN_NETWORK (the admin users whose new networks it
--     watches), [26] SEC_LOGIN_TAKEOVER (the admin-tier roles that make a takeover CRITICAL) and [27] SEC_ADMIN_GRANT
--     (the admin-tier roles whose direct grant raises). [26] and [27] keep one list, the app's ALERT_ADMIN_ROLES.
--   ~ The SEC_ADMIN_GRANT rule NAME lists the role too (only while NAME still equals its V162 seed).
--   SP_ALERT_SCAN_DAILY is unchanged (no daily arm lists admin roles); so are the V166 / V167 break-glass lists
--   (ACCOUNTADMIN and SNOW_ACCOUNTADMINS, a different tier). The RETURN labels, the 14-block tally, every other arm,
--   sweep and gate are unchanged.
--
-- COST: none (three IN lists one role longer; no new read).
-- LATENCY: hourly (:07 Central), as before.
-- FIRST RUN: the next hourly scan raises, for {DSA} only, what a watched role would have raised in
-- the arms' own windows: one SEC_ADMIN_GRANT (HIGH) per direct grant created in the last 26h (a grant made earlier
-- never raises), a SEC_NEW_ADMIN_NETWORK for a holder's user + IP pair first seen in the last 24h, and a CRITICAL
-- SEC_LOGIN_TAKEOVER for a holder's episode in the last 24h -- one already raised as the WARN band re-raises as CRIT.
-- The V067 sweep supersedes that WARN only while it is OPEN or ACK: a WARN already resolved or snoozed re-opens as a
-- fresh CRITICAL that stays OPEN (the snooze does not carry over: the CRIT key is not the WARN key) and routes and
-- escalates like any CRITICAL, so resolve or snooze it the same way. PREFLIGHT P174.1 lists the direct holders,
-- P174.2-P174.4 what each arm will raise (P174.3 with each CRIT twin's WARN state); PART B V174.3 lists what it did
-- raise. SEC_ADMIN_GRANT and SEC_LOGIN_TAKEOVER never auto-declare an incident (V162).
-- ROLLBACK: re-run V173's SP_ALERT_SCAN (the CREATE PROCEDURE in V173__alert_scan_supported_subquery_and_div0.sql);
-- the rule NAME refresh is cosmetic and can stay. Prefer disabling a rule in Alerts > Rules.
-- Apply AFTER V173 (alone, any time; no repairs). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20174, 'V174 requires V173 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 173) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARK = (f"-- >>> derived:SP_ALERT_SCAN  (from V173; + {DSA} at the end of the admin-role lists of arms [18] "
        "SEC_NEW_ADMIN_NETWORK, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT -- owner access decision 2026-10-05, "
        "V174)\n")

DESCRIPTION = (
    f"Owner access decision 2026-10-05: a direct holder of {DSA} is an OVERWATCH admin, so the hourly "
    "security arms watch the role. SP_ALERT_SCAN re-derived from V173, byte-identical except the role added at the end "
    "of the admin-role lists of arms [18] SEC_NEW_ADMIN_NETWORK, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT (one "
    "two-line note per arm): a direct grant raises SEC_ADMIN_GRANT, a holder takeover is CRITICAL, a holder new "
    "network raises. The SEC_ADMIN_GRANT rule NAME lists it (guarded on the V162 seed). SP_ALERT_SCAN_DAILY, RETURN "
    "labels and tallies unchanged. No task change, no new object, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION and "$" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 174 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174);
"""

out = HEADER + MARK + hourly + "\n" + NAME_REFRESH + VERSION_ROW

# ---- post-asserts on the file ---------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.count("-- >>> derived:") == 1 and "LINEAGE-WAIVER" not in out
_top = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)        # outside every $$ body
_top_code = re.sub(r"--[^\n]*", "", _top)
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER |EXECUTE TASK|CALL |DROP |DELETE |MERGE )",
                     _top_code, re.M | re.I)
assert len(re.findall(r"^UPDATE ", _top_code, re.M)) == 1 and "ALERT_EVENTS" not in _top_code   # one NAME refresh
assert "V174 requires V173 first" in out and "SELECT 174 AS VERSION" in out
for _new in (HEADER, MARK, R18_NEW, H18_NEW, R26_NEW, H26_NEW, R27_NEW, H27_NEW, NAME_REFRESH, VERSION_ROW):
    assert _new.isascii(), _new[:60]                       # (the carried body keeps its own em dashes)
for line in out.splitlines():
    assert not line.lstrip().upper().startswith("CALL ") or line.startswith("        "), line

target = Path(os.environ.get("V174_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------------------------------
# Read-only PREFLIGHT (P174.1-P174.4), built from the DERIVED arm text. Central session first: the arms' titles and
# keys read Central wall clocks, and CREATED_ON / EVENT_TIMESTAMP render in the session zone.
# ---------------------------------------------------------------------------------------------------
TZ_PIN = "ALTER SESSION SET TIMEZONE = 'America/Chicago';"
APPLIED_174 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)"
_A18_TEXT, _A26_TEXT, _A27_TEXT = (_between(hourly, *s) for s in (A18, A26, A27))


def _statement(arm: str) -> str:
    """The arm's ``WITH ... SELECT`` (its INSERT column list dropped), dedented 8 spaces."""
    stmt = arm[arm.index("        WITH cfg AS (\n"):arm.index(";\n    EXCEPTION")]
    assert all(not ln or ln.startswith("        ") for ln in stmt.splitlines())
    return "".join(ln[8:] + "\n" for ln in stmt.splitlines()).rstrip("\n")


STMT18, STMT26, STMT27 = (_statement(a) for a in (_A18_TEXT, _A26_TEXT, _A27_TEXT))
for _s in (STMT18, STMT26, STMT27):
    assert _s.count("SELECT b.RULE_ID") == 1 and not re.search(r"(?<![:\w]):[a-z_]+\b", _s.replace("HH24:MI", ""))
    assert _s.count(f"'{DSA}'") == 1
_q = ", ".join(f"'{r}'" for r in BASE_LISTS["R27"])
_q18 = ", ".join(f"'{r}'" for r in BASE_LISTS["R18"])

PREFLIGHT = f"""\
-- ====================================================================================================
--  V174 PREFLIGHT (read-only; run BEFORE applying V174). P174.2-P174.4 run the three arms' OWN V174 text
--  (outputs/gen_v174.py), so each grid is what the first hourly scan after the apply raises. The first statement pins
--  the session to Central, the zone of the arms' titles and keys. Changes nothing.
-- ====================================================================================================
{TZ_PIN}

-- P174.1 who V174 starts watching: every user holding {DSA} by a DIRECT grant (GRANTS_TO_USERS
--        lags up to ~2h), with the admin-tier roles they already hold directly. NEW_TO_ARM_18: arm [18] never watched
--        their logins; NEW_TO_ARM_26: no role of theirs made a takeover CRITICAL. The app treats the same direct
--        holders as OVERWATCH admins (a role granted to a role is not expanded).
WITH dsa AS (
    SELECT GRANTEE_NAME, MIN(CREATED_ON) AS FIRST_GRANTED_ON, MAX_BY(GRANTED_BY, CREATED_ON) AS LAST_GRANTED_BY
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE = '{DSA}'
    GROUP BY GRANTEE_NAME
),
other AS (
    SELECT GRANTEE_NAME,
           LISTAGG(DISTINCT ROLE, ', ') AS OTHER_ADMIN_ROLES,
           COUNT_IF(ROLE IN ({_q18})) AS N_ARM18_ROLES
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE DELETED_ON IS NULL
      AND ROLE IN ({_q})
    GROUP BY GRANTEE_NAME
)
SELECT d.GRANTEE_NAME AS USER_NAME, d.FIRST_GRANTED_ON, d.LAST_GRANTED_BY, o.OTHER_ADMIN_ROLES,
       COALESCE(o.N_ARM18_ROLES, 0) = 0 AS NEW_TO_ARM_18,
       o.GRANTEE_NAME IS NULL AS NEW_TO_ARM_26
FROM dsa d
LEFT JOIN other o
       ON o.GRANTEE_NAME = d.GRANTEE_NAME
ORDER BY NEW_TO_ARM_26 DESC, USER_NAME;

-- P174.2 what the next hourly scan raises for SEC_ADMIN_GRANT after V174 (arm [27]'s own statement): every direct grant
--        of a listed role created in the last 26h that has no event yet. Expect only {DSA} grants
--        (the other roles' grants were raised by the scans before); each is one HIGH event, reviewed in Alerts.
{STMT27};

-- P174.3 what the next hourly scan raises for SEC_LOGIN_TAKEOVER after V174 (arm [26]'s own statement, wrapped): the CRIT
--        twin of a holder's episode already raised as WARN in the last 24h, and any episode the scan has not raised
--        yet. WARN_TWIN_STATUS / WARN_TWIN_RESOLUTION_KIND are that WARN row's: the V067 sweep supersedes it only
--        while it is OPEN or ACK; one already resolved or snoozed stays as it is and the CRIT re-opens the episode
--        (it routes and escalates like any CRITICAL): resolve or snooze the CRIT the same way.
SELECT p.*,
       w.STATUS AS WARN_TWIN_STATUS,
       w.RESOLUTION_KIND AS WARN_TWIN_RESOLUTION_KIND,
       CASE WHEN w.STATUS IS NULL THEN NULL
            WHEN w.STATUS IN ('OPEN', 'ACK') THEN 'superseded by this CRIT (V067 sweep, OPEN or ACK)'
            ELSE 'WARN already ' || LOWER(w.STATUS) || ': this CRIT re-opens the episode (resolved or snoozed '
                 || 'WARNs are not superseded) - resolve or snooze it the same way' END AS WARN_TWIN_NOTE
FROM (
{STMT26}
) p
LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS w
       ON w.RULE_ID = p.RULE_ID
      AND p.DEDUPE_KEY LIKE '%|CRIT|%'
      AND w.DEDUPE_KEY = REPLACE(p.DEDUPE_KEY, '|CRIT|', '|WARN|')
ORDER BY p.DEDUPE_KEY;

-- P174.4 what the next hourly scan raises for SEC_NEW_ADMIN_NETWORK after V174 (arm [18]'s own statement): a holder's
--        user + IP pairs first seen (against 90 days) in the last 24h, at or over the rule's threshold.
{STMT18};
"""

# ---------------------------------------------------------------------------------------------------
# RUN_NEXT PART B (read-only; after the apply). GET_DDL quotes a procedure body, so every fragment it is searched for
# carries no quote, backslash or newline.
# ---------------------------------------------------------------------------------------------------
_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()')"
N_DSA = hourly.count(DSA)
KEEP = ("alert scan v14 (V168:", "FROM recent r", "V174 (owner 2026-10-05)")
for _f in (*KEEP, DSA):
    assert not set(_f) & {"'", "\\", "\n", "\r"}, _f
assert all(_f in hourly for _f in KEEP) and N_DSA == 6 and hourly173.count(DSA) == 0
assert "V174 (owner 2026-10-05)" not in hourly173
# A scan already running at the apply finishes on its OLD body (CREATE OR REPLACE PROCEDURE leaves a CALL in flight
# alone), so V174.2 reads only a heartbeat 55+ minutes after the apply and failures logged from 30 minutes after it (the
# V173 PART B rule: both hold while a scan runs under 25 minutes).
RUN_AFTER = f"DATEADD('minute', 55, {APPLIED_174})"
LOG_AFTER = f"DATEADD('minute', 30, {APPLIED_174})"
RULES = ("SEC_NEW_ADMIN_NETWORK", "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT")
for _rule in RULES:
    assert f"'rule {_rule} - other rules unaffected'" in hourly, _rule
assert "'ALERT_SCAN_HOURLY'" in hourly
assert hourly.count("'alert scan ' || (14 - :fails) || '/14 rule blocks ok'") == 2
_rule_like = "\n             OR ".join(f"CONTEXT LIKE 'rule {r} %'" for r in RULES)
_rules_in = ", ".join(f"'{r}'" for r in RULES)

PART_B = f"""\
-- PART B -- V174 verify (READ-ONLY after the session pin). V174.1 right after the apply; V174.2 after the first hourly
-- scan that started after the apply (its heartbeat lands 55+ minutes after it: a scan already running at the apply
-- finishes on its old body, so V174.2 reads WAIT until then); V174.3 once V174.2 reads OK. Every RESULT should read OK;
-- paste the grids back. APPLIED_AT, LAST_LOAD_TS, LOGGED_AT and RAISED_AT are all Central wall-clock.
{TZ_PIN}

SELECT 'V174.1 SCHEMA_VERSION has 174' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174) = 1,
           'OK', 'FAIL: V174 did not finish') AS RESULT
UNION ALL
SELECT 'V174.1 SP_ALERT_SCAN DDL names {DSA} {N_DSA} times (three admin lists, three notes)',
       IFF(REGEXP_COUNT({_DDL}, '{DSA}') = {N_DSA}, 'OK',
           'FAIL: not the V174 body (' || REGEXP_COUNT({_DDL}, '{DSA}') || ' found)')
UNION ALL
{chr(10).join(f"SELECT 'V174.1 SP_ALERT_SCAN DDL has: {_f}', IFF(CONTAINS({_DDL}, '{_f}'), 'OK', 'FAIL: not the V174 body')" + chr(10) + "UNION ALL" for _f in KEEP)}
SELECT 'V174.1 SEC_ADMIN_GRANT rule name lists {DSA}',
       (SELECT CASE WHEN COUNT(*) = 0 THEN 'FAIL: no SEC_ADMIN_GRANT rule'
                    WHEN CONTAINS(MAX(NAME), '{DSA}') THEN 'OK'
                    ELSE 'CHECK: the rule name was edited by hand, so V174 kept it (the arm watches the role anyway)' END
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'SEC_ADMIN_GRANT');

-- V174.2 after the first hourly scan (graph from :07 Central) whose heartbeat lands 55+ minutes after the apply: it
--        reads 14/14 and none of the three arms V174 changed logged a failure.
SELECT 'V174.2 hourly scan started after the apply' AS CHECK_NAME,
       CASE WHEN {APPLIED_174} IS NULL THEN 'FAIL: SCHEMA_VERSION has no 174 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= {RUN_AFTER}, 'OK',
                                      'WAIT: no hourly scan that started after the apply yet (last heartbeat '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                          'FAIL: no ALERT_SCAN_HOURLY row') END AS RESULT
UNION ALL
SELECT 'V174.2 hourly heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= {RUN_AFTER},
                            IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS)),
                            'WAIT: the heartbeat (' || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ') is an older scan')
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row')
UNION ALL
SELECT 'V174.2 no rule_block_failed of the three V174 arms since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
          AND ({_rule_like})
          AND LOGGED_AT >= {LOG_AFTER})
UNION ALL
SELECT 'V174.2 no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= {LOG_AFTER});

-- V174.3 once V174.2 reads OK (informational): what the three arms raised since the apply for a user who holds or held
--        {DSA} directly. Each row is a real event: review it in Alerts (an approved grant resolves as
--        EXPECTED); none of them auto-declares an incident.
WITH dsa AS (
    SELECT DISTINCT LEFT(GRANTEE_NAME, 200) AS USER_PART
    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
    WHERE ROLE = '{DSA}'
)
SELECT e.RULE_ID, e.SEVERITY, e.STATUS, e.RAISED_AT, e.TITLE
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
JOIN dsa d
  ON d.USER_PART = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
WHERE e.RULE_ID IN ({_rules_in})
  AND e.RAISED_AT >= {APPLIED_174}
ORDER BY e.RAISED_AT, e.RULE_ID;
"""

for _name, _sql in (("PREFLIGHT", PREFLIGHT), ("PART B", PART_B)):
    assert "\r" not in _sql and "$$" not in _sql, _name
assert PART_B.isascii() and PREFLIGHT.count(TZ_PIN) == 1 and PART_B.count(TZ_PIN) == 1
_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")

print(f"V174 written: {target} ({len(out)} bytes)")
