#!/usr/bin/env python3
"""Forward-generate V176: SEC_ADMIN_GRANT skips Snowflake's own Streamlit runtime grant (owner 2026-10-12).

The owner, 2026-10-12: "Whenever I deploy the app, this keeps popping up in Teams. I want to turn this off." Every
`snow streamlit deploy --replace` makes Snowflake create a short-lived service user for the Streamlit runtime
(STPLATSTREAMLIT<digits>) and grant it, itself (GRANTED_BY NULL), the app owner's role and SYSTEM$MANAGED. Hourly arm
[27] SEC_ADMIN_GRANT raised that grant as a first-time HIGH admin grant on every deploy (a new user, so a new key:
no snooze carries over). The owner must not lose real admin-grant detection.

Reads V174__alert_scan_dsa_admin_role.sql (the CURRENT definer of SP_ALERT_SCAN; V175 does not touch it) ONLY --
tests/test_proc_lineage.py -- and emits, in order:

  guard (-20176, v < 175) -> marker + SP_ALERT_SCAN re-derived from V174 -> SCHEMA_VERSION 176.

SP_ALERT_SCAN, byte-identical to V174 except arm [27] SEC_ADMIN_GRANT, three insertions:
  C27  an uncorrelated CTE rt after ag: one row per user name holding a no-grantor SYSTEM$MANAGED grant whose name is
       exactly STPLATSTREAMLIT + digits, with that grant's time (MARK_FROM / MARK_TO; same view as the admin grant,
       so no cross-view latency race; not time-bounded: a few rows per deploy);
  R27  a LEFT JOIN rt on GRANTEE_NAME and one WHERE NOT COALESCE(...) after the JOIN's ON clause: a row is skipped
       only when (a) GRANTED_BY IS NULL, (b) REGEXP_LIKE(name, 'STPLATSTREAMLIT[0-9]+'), (c) the role is
       SNOW_ACCOUNTADMINS or SNOW_SYSADMINS and (d) rt matched the same user within 60 s of the admin grant (the
       same deploy: a user re-created later under an old runtime name does not inherit the marker, review r1) --
       any NULL keeps the row (fails open, toward alerting);
  H27  a two-line V176 note above the arm's BEGIN.
The ag CTE (PRIOR_GRANTS' full history) is untouched; [18] and [26] are unchanged (the runtime users have no
LOGIN_HISTORY, probe S4); the RETURN labels, the 14-block tally and every other arm are unchanged. The predicate text
is hard-coded here (the generator never imports the app package): tests/test_security_alert_parity.py binds it to
the security_sql fragment builders.

No CALL, DROP, task change or data write at apply time. With PREFLIGHT_OUT / PART_B_OUT set, also writes the
read-only PREFLIGHT (P176.1a, P176.1b, P176.2) and PART B verify grids (V176.1-V176.3), built from the SAME derived
arm text; both open with the Central session pin. The byte-identity test never sets them.

Run: python outputs/gen_v176.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V174 = (MIG / "V174__alert_scan_dsa_admin_role.sql").read_text(encoding="utf-8")
NAME = "V176__alert_scan_streamlit_runtime_grant.sql"
PATTERN = "STPLATSTREAMLIT[0-9]+"
OWNER_ROLES = ("SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")
MARKER_ROLE = "SYSTEM$MANAGED"


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


def _clock_literals(text: str) -> list[str]:
    return sorted(re.findall(r"(?:HOUR|DAYOFWEEKISO)\([\w.]+\) (?:>=|<|>|<=|=) \d+", text))


# ---------------------------------------------------------------------------------------------------
# Arm slices (each start unique in the body; each end the first such line after it)
# ---------------------------------------------------------------------------------------------------
A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
A26 = ("    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
A27 = ("    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN   -- V157 cadence gate: [22]")

# ---- the one predicate (byte-equal to security_sql._not_streamlit_runtime_grant(); the parity test binds them) ------
_ROLES_OR = " OR ".join(f"g.ROLE = '{r}'" for r in OWNER_ROLES)
PREDICATE = (f"NOT COALESCE(g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME, '{PATTERN}') "
             f"AND ({_ROLES_OR}) AND rt.GRANTEE_NAME IS NOT NULL, FALSE)")

# ---- C27: the rt CTE (the system-only co-grant), right after ag --------------------------------------------------
C27_OLD = ("                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')\n"
           "        )\n")
RT_CTE = ("        rt AS (\n"
          "            SELECT GRANTEE_NAME, MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO\n"
          "            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS\n"
          f"            WHERE ROLE = '{MARKER_ROLE}'\n"
          "              AND GRANTED_BY IS NULL\n"
          f"              AND REGEXP_LIKE(GRANTEE_NAME, '{PATTERN}')\n"
          "            GROUP BY GRANTEE_NAME\n"
          "        )\n")
C27_NEW = ("                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')\n"
           "        ),\n" + RT_CTE)

# ---- R27: the LEFT JOIN and the filter, right after the JOIN's ON clause ----------------------------------------
R27_OLD = "         AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())\n"
JOIN_RT = ("        LEFT JOIN rt\n"
           "          ON rt.GRANTEE_NAME = g.GRANTEE_NAME\n"
           "         AND g.CREATED_ON BETWEEN DATEADD('second', -60, rt.MARK_FROM) AND DATEADD('second', 60, rt.MARK_TO)\n")
R27_NEW = R27_OLD + JOIN_RT + f"        WHERE {PREDICATE}\n"

# ---- H27: the note ------------------------------------------------------------------------------------------------
H27_OLD = ("    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.\n"
           "    BEGIN\n")
H27_NEW = ("    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.\n"
           "    --      V176 (owner 2026-10-12): skips only a no-grantor SNOW_ACCOUNTADMINS / SNOW_SYSADMINS grant to a Streamlit\n"
           f"    --      runtime user (STPLATSTREAMLIT<digits>, a new one per deploy, also system-granted {MARKER_ROLE}).\n"
           "    BEGIN\n")

SWAPS = (("C27", A27, C27_OLD, C27_NEW), ("R27", A27, R27_OLD, R27_NEW), ("H27", A27, H27_OLD, H27_NEW))

# ---------------------------------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------------------------------
hourly174 = extract_proc(V174, "SP_ALERT_SCAN()")
assert "STPLAT" not in V174 and MARKER_ROLE not in V174 and "REGEXP_LIKE" not in hourly174
assert hourly174.count("fails := fails + 1") == 14
assert len(re.findall(r"\bROLE IN \(", hourly174)) == 3                        # [18], [26], [27]: one list each
_spans = {"A18": A18, "A26": A26, "A27": A27}
BASE_LISTS = {k: _in_list(_between(hourly174, *span)) for k, span in _spans.items()}
assert BASE_LISTS["A26"] == BASE_LISTS["A27"] and len(BASE_LISTS["A27"]) == 8
assert set(OWNER_ROLES) < set(BASE_LISTS["A27"])                                 # the pair is already watched
BASE_CLOCKS = _clock_literals(hourly174)
_outside = hourly174.replace(_between(hourly174, *A27), "")

hourly = hourly174
for _label, _span, _old, _new in SWAPS:
    hourly = _swap_in(hourly, *_span, _old, _new, _label)

# ---- post-asserts on the derived body -------------------------------------------------------------------
_A27_TEXT = _between(hourly, *A27)
assert len(re.findall(r"\bROLE IN \(", hourly)) == 3                             # no second list in any arm
for _k, _span in _spans.items():
    assert _in_list(_between(hourly, *_span)) == BASE_LISTS[_k], _k               # the three lists unchanged
assert hourly.replace(_A27_TEXT, "") == _outside                                  # nothing outside [27] moved
assert _clock_literals(hourly) == BASE_CLOCKS                                    # no new HOUR / weekday literal
assert hourly.count(PREDICATE) == 1 and _A27_TEXT.count(PREDICATE) == 1          # the predicate, once, in [27]
assert hourly.count(RT_CTE) == 1 and hourly.count(JOIN_RT) == 1
assert hourly.count(f"'{PATTERN}'") == 2 and hourly.count(f"'{MARKER_ROLE}'") == 1
assert "STPLAT" not in _between(hourly, *A26) and "STPLAT" not in _between(hourly, *A18)
_body = hourly[hourly.index("$$") + 2:hourly.rindex("$$")]
assert "$$" not in _body and "\\" not in _body
assert hourly.count("fails := fails + 1") == 14                                         # tally unchanged
assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", hourly)) == set(
    re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", hourly174))                         # footprint unchanged
assert re.findall(r"RETURN '[^']*'", hourly) == re.findall(r"RETURN '[^']*'", hourly174)   # labels carried
# the ag CTE (PRIOR_GRANTS over the full history) is untouched; the filter sits after the JOIN, never inside ag
_ag = _between(_A27_TEXT, "        ag AS (\n", "        rt AS (\n")
assert _ag == _between(_between(hourly174, *A27), "        ag AS (\n", "        )\n") + "        ),\n"
assert _A27_TEXT.index("        rt AS (\n") < _A27_TEXT.index("        SELECT b.RULE_ID") < _A27_TEXT.index(PREDICATE)
# each swap only ADDS text (C27 also turns ag's closing ')' into '),'): the added text, per swap
_note = H27_NEW[len(H27_OLD) - len("    BEGIN\n"):-len("    BEGIN\n")]
assert C27_OLD.replace("        )\n", "        ),\n" + RT_CTE) == C27_NEW
assert R27_NEW.startswith(R27_OLD) and H27_OLD.replace("    BEGIN\n", _note + "    BEGIN\n") == H27_NEW
for _label, _added in (("C27", RT_CTE), ("R27", R27_NEW[len(R27_OLD):]), ("H27", _note)):
    assert _added.isascii() and "THRESHOLD_NUM" not in _added and not re.search(r"\bIN \(", _added), _label
assert _note.startswith("    --      V176 (owner 2026-10-12): ") and "ROLE IN" not in _note
assert len(_note.splitlines()) == 2 and all(ln.startswith("    --      ") for ln in _note.splitlines())
assert max(len(ln) for ln in _note.splitlines()) <= 120

# ---------------------------------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------------------------------
HEADER = f"""-- {NAME}
--
-- The owner, 2026-10-12: "Whenever I deploy the app, this keeps popping up in Teams. I want to turn this off." The
-- page is SEC_ADMIN_GRANT (hourly arm [27]): "[HIGH] Admin role SNOW_ACCOUNTADMINS granted to
-- STPLATSTREAMLIT98004567325 - first time | ALL | Granted ... by ?". Real admin-grant detection must stay.
--
-- WHY: every snow streamlit deploy --replace makes Snowflake create a short-lived service user for the Streamlit
-- runtime (STPLATSTREAMLIT<digits>: TYPE SNOWFLAKE_SERVICE, no owner, no password or key, created about 17 s after
-- the app object and dropped seconds later) and grant it two roles ITSELF (GRANTED_BY NULL, revoked within
-- seconds): the app owner's role (SNOW_ACCOUNTADMINS today) and {MARKER_ROLE}. The arm raises every direct
-- admin-tier grant created in the last 26h, revoked or not, so each deploy was a first-time HIGH grant to a new user:
-- a new key every time, so a snooze never carried over and disabling the rule was the only way to stop it -- which
-- would also silence every real admin grant.
--
--   ~ SP_ALERT_SCAN re-derived from V174 (its current definer), byte-identical except arm [27] SEC_ADMIN_GRANT,
--     which now skips a grant row only when ALL four hold (a NULL in any of them keeps the row: it fails open):
--       (a) GRANTED_BY IS NULL: Snowflake made the grant (a GRANT run by any person or role records its grantor);
--       (b) the grantee name is exactly STPLATSTREAMLIT followed by digits (REGEXP_LIKE: anchored, case-sensitive);
--       (c) the role is SNOW_ACCOUNTADMINS or SNOW_SYSADMINS (the app owner, today and after the planned switch);
--       (d) the same user also got a no-grantor {MARKER_ROLE} grant within 60 s of the admin grant, in the same
--           view (GRANTS_TO_USERS): a system-only role no customer statement can grant, made in the same deploy
--           (the live pair lands milliseconds apart), read from the view the admin grant comes from, so there is no
--           cross-view latency race (USERS.TYPE would be one). The 60 s tie means a user re-created later under an
--           old runtime name (the names are public) does not inherit that deploy's marker (review r1).
--     (d) is an uncorrelated CTE (rt: one row per name with MARK_FROM / MARK_TO, not time-bounded: a few rows per
--     deploy) plus a LEFT JOIN on GRANTEE_NAME and the 60 s window; the filter is one NOT COALESCE(..., FALSE)
--     after the JOIN. The ag CTE is untouched, so PRIOR_GRANTS still
--     counts the full history. Plus a two-line V176 note above the arm's BEGIN.
--   Unchanged: the admin-role list, titles, details, keys and severity; every other arm, [18] SEC_NEW_ADMIN_NETWORK
--   and [26] SEC_LOGIN_TAKEOVER included (these users have no LOGIN_HISTORY); sweeps, gates, the RETURN labels and
--   the 14-block tally; SP_ALERT_SCAN_DAILY. Still raised, exactly as before: any grant made by a person or role
--   (whatever the user is called), any other admin role given to a runtime user, a no-grantor grant to any other
--   user (real users have them too), and a runtime-shaped grant without the {MARKER_ROLE} co-grant.
--
-- COST: none (one more read of GRANTS_TO_USERS, a view the arm already reads).
-- LATENCY: hourly (:07 Central), as before.
-- FIRST RUN: raises nothing new. The runtime-user events already raised stay as they are: resolve them as EXPECTED
-- in Alerts (PREFLIGHT P176.2 lists them; nothing is re-sent to Teams). PREFLIGHT P176.1a / P176.1b show what the
-- arm raises today and after V176 over the last 26h and 30 days; PART B V176.1-V176.3 check the body, the first
-- hourly scan after the apply and the next deploy.
-- ROLLBACK: re-run V174's SP_ALERT_SCAN (the CREATE PROCEDURE in V174__alert_scan_dsa_admin_role.sql, that CREATE
-- only); every deploy pages Teams again. Prefer that over disabling the rule. RUNBOOK section 12, "Rolling back V176".
-- Apply AFTER V175, any time; no repairs; no app redeploy needed. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20176, 'V176 requires V175 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 175) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARK = ("-- >>> derived:SP_ALERT_SCAN  (from V174; [27] SEC_ADMIN_GRANT skips a grant with no grantor of "
        "SNOW_ACCOUNTADMINS or SNOW_SYSADMINS to a Snowflake Streamlit runtime user STPLATSTREAMLIT<digits> that "
        f"also got a no-grantor {MARKER_ROLE} grant within 60 s of it -- owner 2026-10-12, V176)\n")

DESCRIPTION = (
    "Owner 2026-10-12: app deploys no longer page Teams. SP_ALERT_SCAN re-derived from V174, byte-identical except "
    "arm [27] SEC_ADMIN_GRANT (plus a two-line note), which skips a grant row only when all four hold: GRANTED_BY "
    "IS NULL, the grantee is exactly STPLATSTREAMLIT followed by digits (the Snowflake Streamlit runtime user made on "
    "each deploy), the role is SNOW_ACCOUNTADMINS or SNOW_SYSADMINS, and the same user got a no-grantor grant of "
    "the Snowflake system-only managed role within 60 seconds of it in GRANTS_TO_USERS (the same deploy; an "
    "uncorrelated CTE and a LEFT JOIN on name and that window; any NULL keeps the row). Every other grant still raises; the PRIOR_GRANTS history, the role list, the RETURN labels, the "
    "tallies, every other arm and SP_ALERT_SCAN_DAILY are unchanged. No task change, no new object, no procedure run "
    "at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION and "$" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 176 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176);
"""

out = HEADER + MARK + hourly + "\n" + VERSION_ROW

# ---- post-asserts on the file ---------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n") and "\r" not in out
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("$$") == 4
assert out.count("-- >>> derived:") == 1 and "LINEAGE-WAIVER" not in out
_top = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)        # outside every $$ body
_top_code = re.sub(r"--[^\n]*", "", _top)
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER |EXECUTE TASK|CALL |DROP |DELETE |MERGE |UPDATE )",
                     _top_code, re.M | re.I)
assert "ALERT_EVENTS" not in _top_code and "ALERT_CONFIG" not in _top_code              # no data write at apply
assert "V176 requires V175 first" in out and "SELECT 176 AS VERSION" in out
for _new in (HEADER, MARK, VERSION_ROW, *(s[3] for s in SWAPS)):
    assert _new.isascii(), _new[:60]                       # (the carried body keeps its own em dashes)
for line in out.splitlines():
    assert not line.lstrip().upper().startswith("CALL ") or line.startswith("        "), line

target = Path(os.environ.get("V176_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------------------------------
# Read-only PREFLIGHT (P176.1a, P176.1b, P176.2), built from the DERIVED arm text. Central session first: the arm's
# titles and keys read Central wall clocks, and CREATED_ON / RAISED_AT render in the session zone.
# ---------------------------------------------------------------------------------------------------
TZ_PIN = "ALTER SESSION SET TIMEZONE = 'America/Chicago';"
APPLIED_176 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)"


def _dedent(block: str) -> str:
    assert all(not ln or ln.startswith("        ") for ln in block.splitlines())
    return "".join(ln[8:] + "\n" for ln in block.splitlines())


AG_SQL = _dedent(_ag[:-len("        ),\n")] + "        )\n").rstrip("\n")       # 'ag AS (\n ... \n)'
RT_SQL = _dedent(RT_CTE).rstrip("\n")                                             # 'rt AS (\n ... \n)'
JOIN_SQL = _dedent(JOIN_RT).rstrip("\n")
assert AG_SQL.startswith("ag AS (\n") and AG_SQL.endswith("\n)") and RT_SQL.startswith("rt AS (\n")
assert _dedent(_ag + RT_CTE) == f"{AG_SQL},\n{RT_SQL}\n" and _ag + RT_CTE in _A27_TEXT    # the arm's own CTEs
_q_owner = ", ".join(f"'{r}'" for r in OWNER_ROLES)
_q_admin = ", ".join(f"'{r}'" for r in BASE_LISTS["A27"])
_KEY = ("'SEC_ADMIN_GRANT|' || LEFT(g.GRANTEE_NAME, 200) || '|' || g.ROLE || '|' ||\n"
        "           TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI:SS.FF3')")
assert _KEY.replace("'SEC_ADMIN_GRANT|'", "c.RULE_ID || '|'").replace("\n           ", "\n                   ") in \
    _A27_TEXT                                                                     # the arm's own key expression

PREFLIGHT = f"""\
-- ====================================================================================================
--  V176 PREFLIGHT (read-only; run BEFORE applying V176, as SNOW_ACCOUNTADMINS). The grids reuse arm [27]'s OWN V176
--  text (outputs/gen_v176.py): its ag and rt CTEs and its one skip predicate. The first statement pins the session to
--  Central, the zone of the arm's titles and keys. Changes nothing.
-- ====================================================================================================
{TZ_PIN}

-- P176.1a what arm [27] raises today (V174) and after V176, per window: every direct admin-tier grant created in the
--         last 26h (what one hourly scan reads) and in the last 30 days, one HIGH event each today. V176_SKIPS should
--         be the deploys' Streamlit runtime grants only (P176.1b lists every row); V176_STILL_RAISES is every other
--         grant, raised exactly as before. Events the arm already raised are not subtracted (one scan raises each
--         grant once).
WITH {AG_SQL},
{RT_SQL},
v AS (
    SELECT g.GRANTEE_NAME, g.ROLE, g.CREATED_ON,
           {PREDICATE} AS V176_RAISES
    FROM ag g
    {JOIN_SQL.replace(chr(10), chr(10) + '    ')}
    WHERE g.CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
),
w AS (
    SELECT 'last 26h' AS WINDOW_NAME, DATEADD('hour', -26, CURRENT_TIMESTAMP()) AS SINCE
    UNION ALL
    SELECT 'last 30 days', DATEADD('day', -30, CURRENT_TIMESTAMP())
)
SELECT w.WINDOW_NAME,
       COUNT(v.ROLE) AS V174_RAISES,
       COUNT_IF(NOT v.V176_RAISES) AS V176_SKIPS,
       COUNT_IF(v.V176_RAISES) AS V176_STILL_RAISES,
       COUNT(DISTINCT IFF(NOT v.V176_RAISES, v.GRANTEE_NAME, NULL)) AS RUNTIME_USERS_SKIPPED,
       LISTAGG(DISTINCT IFF(v.V176_RAISES, v.GRANTEE_NAME || ' (' || v.ROLE || ')', NULL), ', ') AS STILL_RAISED
FROM w
LEFT JOIN v
       ON v.CREATED_ON >= w.SINCE
GROUP BY w.WINDOW_NAME
ORDER BY w.WINDOW_NAME;

-- P176.1b every direct admin-tier grant of the last 30 days with its V176 verdict. Expect each 'skipped by V176' row
--         to be an STPLATSTREAMLIT<digits> user, GRANTED_BY NULL, SNOW_ACCOUNTADMINS (or SNOW_SYSADMINS) and
--         HAS_SYSTEM_MANAGED_COGRANT TRUE. A 'still raises' row with GRANTED_BY NULL is a real user's no-grantor grant:
--         it raises as before. IN_LAST_26H marks what the next hourly scan reads.
WITH {AG_SQL},
{RT_SQL}
SELECT IFF({PREDICATE}, 'still raises', 'skipped by V176') AS V176_VERDICT,
       g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP()) AS IN_LAST_26H,
       g.GRANTEE_NAME, g.ROLE, g.GRANTED_BY, g.CREATED_CT AS GRANTED_AT_CENTRAL, g.DELETED_ON,
       rt.GRANTEE_NAME IS NOT NULL AS HAS_SYSTEM_MANAGED_COGRANT, g.PRIOR_GRANTS
FROM ag g
{JOIN_SQL}
WHERE g.CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
ORDER BY V176_VERDICT, g.CREATED_ON DESC
LIMIT 500;

-- P176.2 the runtime-user SEC_ADMIN_GRANT events still open: resolve them after the apply. In Alerts > Open events
--        (Group by rule off), turn on Bulk select and tick only the rows whose TITLE and RAISED_AT match this grid
--        (this query already checked the rule, the runtime name and the no-grantor 'by ?' detail), then resolve
--        as EXPECTED with the note: Streamlit runtime user system grant on app deploy (GRANTED_BY NULL);
--        excluded from SEC_ADMIN_GRANT by V176. EXPECTED, not NOISE: NOISE would count against the rule's precision.
--        Nothing is re-sent to Teams. HAS_SYSTEM_MANAGED_COGRANT FALSE = V176 would still raise that grant: check it first.
WITH {RT_SQL}
SELECT e.EVENT_ID, e.STATUS, e.RAISED_AT, e.TITLE, LEFT(e.DETAIL, 120) AS DETAIL_START,
       rt.GRANTEE_NAME IS NOT NULL AS HAS_SYSTEM_MANAGED_COGRANT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN rt
  ON rt.GRANTEE_NAME = SPLIT_PART(e.DEDUPE_KEY, '|', 2)
WHERE e.RULE_ID = 'SEC_ADMIN_GRANT'
  AND e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')
  AND REGEXP_LIKE(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '{PATTERN}')
  AND SPLIT_PART(e.DEDUPE_KEY, '|', 3) IN ({_q_owner})
  AND e.DETAIL LIKE 'Granted % Central by ?. %'
ORDER BY e.RAISED_AT;
"""

# ---------------------------------------------------------------------------------------------------
# PART B (read-only; after the apply). GET_DDL quotes a procedure body, so every fragment it is searched for carries no
# quote, backslash or newline.
# ---------------------------------------------------------------------------------------------------
_DDL = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()')"
N_STPLAT = hourly.count("STPLATSTREAMLIT")
KEEP = (PATTERN, "g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME", "rt.GRANTEE_NAME IS NOT NULL", MARKER_ROLE,
        "V176 (owner 2026-10-12)", "alert scan v14 (V168:", "V174 (owner 2026-10-05)")
for _f in KEEP:
    assert not set(_f) & {"'", "\\", "\n", "\r"} and _f in hourly, _f
assert N_STPLAT == 3 and "STPLATSTREAMLIT" not in hourly174 and "V176 (owner 2026-10-12)" not in hourly174
# A scan already running at the apply finishes on its OLD body (CREATE OR REPLACE PROCEDURE leaves a CALL in flight
# alone), so V176.2 reads only a heartbeat 55+ minutes after the apply and failures logged from 30 minutes after it (the
# V173 / V174 PART B rule: both hold while a scan runs under 25 minutes); V176.3 counts events from 30 minutes after it.
RUN_AFTER = f"DATEADD('minute', 55, {APPLIED_176})"
LOG_AFTER = f"DATEADD('minute', 30, {APPLIED_176})"
assert "'rule SEC_ADMIN_GRANT - other rules unaffected'" in hourly and "'ALERT_SCAN_HOURLY'" in hourly
assert hourly.count("'alert scan ' || (14 - :fails) || '/14 rule blocks ok'") == 2
_V176_3_WITH = (
    "WITH " + AG_SQL + ",\n" + RT_SQL + ",\n"
    "dep AS (\n"                                             # the runtime grants of the deploys since the apply
    "    SELECT DISTINCT GRANTEE_NAME, CREATED_ON\n"
    "    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS\n"
    "    WHERE GRANTED_BY IS NULL\n"
    f"      AND REGEXP_LIKE(GRANTEE_NAME, '{PATTERN}')\n"
    f"      AND ROLE IN ({_q_owner})\n"
    f"      AND CREATED_ON >= {APPLIED_176}\n"
    "),\n"
    "missed AS (\n"                                          # every grant V176 still raises, 4h or older
    "    SELECT g.GRANTEE_NAME || ' (' || g.ROLE || ')' AS GRANT_NAME,\n"
    f"           {_KEY} AS DEDUPE_KEY\n"
    "    FROM ag g\n"
    "    " + JOIN_SQL.replace("\n", "\n    ") + "\n"
    f"    WHERE g.CREATED_ON >= {APPLIED_176}\n"
    "      AND g.CREATED_ON <= DATEADD('hour', -4, CURRENT_TIMESTAMP())\n"
    f"      AND {PREDICATE}\n"
    ")\n")
_JOIN_V3 = JOIN_SQL.replace("\n", "\n" + " " * 18)                  # the arm's own name + 60 s join, re-indented
_NO_DEPLOY = ("WHEN (SELECT COUNT(*) FROM dep) = 0\n"
              "            THEN 'WAIT: no deploy since the apply on record yet (GRANTS_TO_USERS lags up to ~2h)'")

PART_B = f"""\
-- PART B -- V176 verify (READ-ONLY after the session pin; as SNOW_ACCOUNTADMINS). V176.1 right after the apply; V176.2
-- after the first hourly scan that started after the apply (its heartbeat lands 55+ minutes after it: a scan already
-- running at the apply finishes on its old body, so V176.2 reads WAIT until then); V176.3 about 3h after the next app
-- deploy (GRANTS_TO_USERS lags up to ~2h, then the next hourly scan). Every RESULT should read OK; paste the grids back.
-- APPLIED_AT, LAST_LOAD_TS, LOGGED_AT and RAISED_AT are all Central wall-clock.
{TZ_PIN}

SELECT 'V176.1 SCHEMA_VERSION has 176' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176) = 1,
           'OK', 'FAIL: V176 did not finish') AS RESULT
UNION ALL
SELECT 'V176.1 SP_ALERT_SCAN DDL names STPLATSTREAMLIT {N_STPLAT} times (rt CTE, skip predicate, note)',
       IFF(REGEXP_COUNT({_DDL}, 'STPLATSTREAMLIT') = {N_STPLAT}, 'OK',
           'FAIL: not the V176 body (' || REGEXP_COUNT({_DDL}, 'STPLATSTREAMLIT') || ' found)')
UNION ALL
{chr(10).join(f"SELECT 'V176.1 SP_ALERT_SCAN DDL has: {_f}', IFF(CONTAINS({_DDL}, '{_f}'), 'OK', 'FAIL: not the V176 body')" + chr(10) + "UNION ALL" for _f in KEEP)}
SELECT 'V176.1 SEC_ADMIN_GRANT is enabled',
       (SELECT CASE WHEN COUNT(*) = 0 THEN 'FAIL: no SEC_ADMIN_GRANT rule'
                    WHEN COUNT_IF(ENABLED) > 0 THEN 'OK'
                    ELSE 'FAIL: the rule is disabled, so real admin grants raise nothing: re-enable it in Alerts > Rules' END
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE RULE_ID = 'SEC_ADMIN_GRANT');

-- V176.2 after the first hourly scan (graph from :07 Central) whose heartbeat lands 55+ minutes after the apply: it
--        reads 14/14 and the arm V176 changed logged no failure.
SELECT 'V176.2 hourly scan started after the apply' AS CHECK_NAME,
       CASE WHEN {APPLIED_176} IS NULL THEN 'FAIL: SCHEMA_VERSION has no 176 row'
            ELSE COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= {RUN_AFTER}, 'OK',
                                      'WAIT: no hourly scan that started after the apply yet (last heartbeat '
                                      || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ')')
                           FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                          'FAIL: no ALERT_SCAN_HOURLY row') END AS RESULT
UNION ALL
SELECT 'V176.2 hourly heartbeat reads 14/14',
       COALESCE((SELECT IFF(MAX(LAST_LOAD_TS) >= {RUN_AFTER},
                            IFF(MAX(STATUS) = 'alert scan 14/14 rule blocks ok', 'OK', 'CHECK: ' || MAX(STATUS)),
                            'WAIT: the heartbeat (' || TO_VARCHAR(MAX(LAST_LOAD_TS)) || ') is an older scan')
                 FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY'),
                'FAIL: no ALERT_SCAN_HOURLY row')
UNION ALL
SELECT 'V176.2 no rule_block_failed of SEC_ADMIN_GRANT since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || LISTAGG(DISTINCT ERROR_MESSAGE, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
          AND CONTEXT LIKE 'rule SEC_ADMIN_GRANT %'
          AND LOGGED_AT >= {LOG_AFTER})
UNION ALL
SELECT 'V176.2 no rule_block_failed of any rule since the apply',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'CHECK: ' || LISTAGG(DISTINCT CONTEXT, '; '))
        FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
        WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed' AND LOGGED_AT >= {LOG_AFTER});

-- V176.3 about 3h after the next app deploy (the live test; WAIT until one is on record): the deploy's runtime grant is
--        on record with no grantor, carries the {MARKER_ROLE} co-grant and raised NO SEC_ADMIN_GRANT event. The
--        last row is the positive control: every admin grant V176 still raises, made since the apply and 4h or older
--        (GRANTS_TO_USERS can land ~2h late and the hourly read varies), DID raise one (a person's grant to a runtime
--        user included). A FAIL that persists on a re-run an hour later, with the rule enabled, is a real miss.
{_V176_3_WITH}SELECT 'V176.3 a deploy since the apply is on record' AS CHECK_NAME,
       CASE {_NO_DEPLOY}
            ELSE 'OK (' || (SELECT COUNT(DISTINCT GRANTEE_NAME) FROM dep) || ' runtime user(s))' END AS RESULT
UNION ALL
SELECT 'V176.3 each runtime user since the apply has the {MARKER_ROLE} co-grant',
       CASE {_NO_DEPLOY}
            ELSE (SELECT IFF(COUNT(*) = 0, 'OK',
                             'CHECK: ' || LISTAGG(DISTINCT g.GRANTEE_NAME, ', ') || ' lack it within 60 s of the '
                             || 'admin grant, so SEC_ADMIN_GRANT still raises them; re-check in 2h (the co-grant '
                             || 'row may land later), else the runtime grant changed shape')
                  FROM dep g
                  {_JOIN_V3}
                  WHERE rt.GRANTEE_NAME IS NULL) END
UNION ALL
SELECT 'V176.3 no SEC_ADMIN_GRANT event for a no-grantor runtime grant since the apply',
       CASE {_NO_DEPLOY}
            ELSE (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: ' || LISTAGG(DISTINCT TITLE, '; '))
                  FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
                  WHERE RULE_ID = 'SEC_ADMIN_GRANT'
                    AND REGEXP_LIKE(SPLIT_PART(DEDUPE_KEY, '|', 2), '{PATTERN}')
                    AND SPLIT_PART(DEDUPE_KEY, '|', 3) IN ({_q_owner})
                    AND DETAIL LIKE 'Granted % Central by ?. %'
                    AND RAISED_AT >= {LOG_AFTER}) END
UNION ALL
SELECT 'V176.3 every other admin grant since the apply raised',
       (SELECT IFF(COUNT(*) = 0, 'OK', 'FAIL: no event for ' || LISTAGG(DISTINCT m.GRANT_NAME, ', '))
        FROM missed m
        LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          ON e.DEDUPE_KEY = m.DEDUPE_KEY
        WHERE e.DEDUPE_KEY IS NULL);
"""

for _name, _sql in (("PREFLIGHT", PREFLIGHT), ("PART B", PART_B)):
    assert "\r" not in _sql and "$$" not in _sql and _sql.isascii(), _name
    assert _sql.count(TZ_PIN) == 1 and _sql.count(PREDICATE) >= 1, _name
assert PREFLIGHT.count(PREDICATE) == 2 and PREFLIGHT.count(RT_SQL) == 3 and PREFLIGHT.count(AG_SQL) == 2
_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")

print(f"V176 written: {target} ({len(out)} bytes)")
