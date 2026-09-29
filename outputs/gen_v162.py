#!/usr/bin/env python3
"""Forward-generate V162: SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT, the hourly identity alerts (Next-Fifty #39).

Owner decisions (2026-09-29): an HOURLY account-takeover arm (a failed-login burst followed by a success; CRITICAL
off-hours -- 20:00-06:00 America/Chicago plus weekends -- or for a user holding an admin-tier role directly) and
one SEC_ADMIN_GRANT event per direct grant of an admin-tier role to a user (flat HIGH). Admin tier = ACCOUNTADMIN,
SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS. Every IS_SUCCESS = 'NO' row
counts toward a burst. Both arms stamp COMPANY 'ALL'. NEITHER rule ever auto-declares an incident.

Reads ONLY the two current definers (tests/test_proc_lineage.py):
  V154__incident_attach_automitigate.sql      SP_INCIDENT_AUTODECLARE (lineage V032 -> V098 -> V099 -> V154)
  V157__alert_scan_self_watch_idle_push.sql   SP_ALERT_SCAN           (V158-V161 never touch it)
and emits, in order (every partial apply is safe -- the owner stops on the first error):

  guard (-20162, v < 161) -> ALERT_CONFIG seeds (WHEN NOT MATCHED only) -> marker + SP_INCIDENT_AUTODECLARE
  re-derived from V154 -> marker + SP_ALERT_SCAN re-derived from V157 -> SCHEMA_VERSION 162.

The autodeclare lands BEFORE the scan, so a CRITICAL takeover can never meet the old autodeclare. Rollback runs
the other way round (scan first).

SP_INCIDENT_AUTODECLARE delta (asserted count == 1; everything else byte-identical to V154):
  A1  in the crit CTE only: AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')
      ([attach] and [auto-mitigate] untouched: a later takeover CRITICAL still links to an incident a human
      declared for that family)

SP_ALERT_SCAN deltas (each asserted by count; the V162 test normalizes the body back to V157):
  H1  insert the ungated counting arms [26] SEC_LOGIN_TAKEOVER + [27] SEC_ADMIN_GRANT after [21]'s END, before
      the [22] gate (so the self-alert, the V067 supersede sweep and the V117 carry-forward see them this pass)
  H2  self-alert denominator ' of 12 alert rule block(s)' -> 14
  H3  (12 - :fails) x5 -> (14 - :fails); '/12 rule blocks ok' x3 -> /14
  H4  the RETURN label names V162

The two dedupe keys end in a millisecond timestamp with an EXPLICIT format ([26] the anchor login in UTC, [27] the
grant's Central CREATED_ON), never a bare LOGIN_HISTORY EVENT_ID: V117's carry-forward treats a key whose last 10
characters parse as a date (a 10-digit integer is epoch seconds to TRY_TO_DATE) as date-banded, so a snooze would
silently carry to the user's NEXT takeover episode.

No task change, no SETTINGS key, no CALL, no new object. The role list and the off-hours window are SQL literals,
mirrored by app/data/security_sql.ALERT_ADMIN_ROLES / OFF_HOURS_* (tests/test_security_alert_parity.py locks them
against the LATEST SP_ALERT_SCAN); this generator never imports app/.

Optional outputs (the byte-identity test never sets them):
  PREFLIGHT_OUT  the read-only PREFLIGHT section P162.1-P162.3 (the arms' own CTE chains, windows widened to 30
                 days), for snowflake/run/PREFLIGHT_WAVE4.sql
  PARTB_OUT      the RUN_NEXT PART B verify grids V162.1-V162.6

Run: python outputs/gen_v162.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
NAME = "V162__security_takeover_admin_grant.sql"
V154 = (MIG / "V154__incident_attach_automitigate.sql").read_text(encoding="utf-8")
V157 = (MIG / "V157__alert_scan_self_watch_idle_push.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert text.count(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}") == 1, name
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


def _q(text: str) -> str:
    return text.replace("'", "''")


TAKEOVER = "SEC_LOGIN_TAKEOVER"
GRANT = "SEC_ADMIN_GRANT"
# owner 2026-09-29 (app twin: app/data/security_sql.ALERT_ADMIN_ROLES)
ADMIN_ROLES = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN", "SNOW_ACCOUNTADMINS",
               "SNOW_SYSADMINS")
ROLE_IN = ("('ACCOUNTADMIN', 'SECURITYADMIN', 'SYSADMIN', 'USERADMIN', 'ORGADMIN',\n"
           "{pad}'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')")
assert re.findall(r"'(\w+)'", ROLE_IN) == list(ADMIN_ROLES)
BURST_MIN, GAP_MIN, EVIDENCE_MIN = 15, 60, 75
EV_HOURS, ANCHOR_HOURS, GRANT_HOURS = 27, 24, 26            # the arms' windows (PREFLIGHT: 723 / 720 / 720)
PF_EV_HOURS, PF_HOURS = 723, 720
KEY_TS = "'YYYY-MM-DD HH24:MI:SS.FF3'"                       # explicit: session TIMESTAMP_OUTPUT_FORMAT never leaks in


# ---------------------------------------------------------------------------------------------------
# Arm [26] SEC_LOGIN_TAKEOVER. The CTE chain ev .. x is shared VERBATIM with PREFLIGHT P162.1 (only the two
# window constants move), so what the owner previews is what the hourly scan will raise.
# ---------------------------------------------------------------------------------------------------
ARM26_HEAD = f"""\
    -- [26] {TAKEOVER} (V162, Next-Fifty #39a: a failed-login burst that ends in a success -- the brute-force
    --      breakthrough the daily count [07] cannot see). Hourly, ungated (owner 2026-09-29). A burst = at least
    --      THRESHOLD_NUM (floor 2) failed logins by one user inside {BURST_MIN} minutes; a takeover = a successful login by that
    --      user within {GAP_MIN} minutes after a burst ends. One event per EPISODE: the first such success with no other such
    --      success by the user in the {GAP_MIN} minutes before it (the anchor). CRITICAL band when the anchor falls off-hours
    --      (20:00-06:00 America/Chicago, or Saturday/Sunday) or the user held an admin-tier role at that moment
    --      (direct grant; owner list); otherwise the rule's severity (HIGH). Never auto-declares an incident:
    --      SP_INCIDENT_AUTODECLARE excludes this rule (V162); a human declares after contacting the user. Company
    --      ALL, so the event reaches the Teams route whoever the user is. Reads {EV_HOURS}h of LOGIN_HISTORY = {ANCHOR_HOURS}h of
    --      alertable anchors + 3h of evidence behind the oldest ({GAP_MIN}-min prior-anchor check + {GAP_MIN}-min gap + {BURST_MIN}-min
    --      burst), so every raised anchor is judged on complete evidence; ACCOUNT_USAGE lags up to ~2h, so an episode
    --      surfaces 1-3h after it happens and stays in the {ANCHOR_HOURS}h window for about 20 more hourly runs. The key ends in
    --      the anchor's UTC time to the millisecond (never the run date, never a bare EVENT_ID -- the V117 snooze
    --      carry-forward would read a 10-digit tail as a date), so re-reads, overlaps and late rows never re-raise
    --      it; a CRIT/WARN band crossing re-fires and the V067 sweep supersedes the WARN row.
"""

ARM26_CHAIN = """\
        ev AS (
            SELECT USER_NAME, EVENT_ID, EVENT_TIMESTAMP, IS_SUCCESS, CLIENT_IP, REPORTED_CLIENT_TYPE,
                   FIRST_AUTHENTICATION_FACTOR, ERROR_MESSAGE
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY
            WHERE EVENT_TIMESTAMP >= DATEADD('hour', -{EV_H}, CURRENT_TIMESTAMP())
        ),
        fl AS (
            SELECT USER_NAME, EVENT_TIMESTAMP,
                   ROW_NUMBER() OVER (PARTITION BY USER_NAME ORDER BY EVENT_TIMESTAMP, EVENT_ID) AS RN
            FROM ev
            WHERE IS_SUCCESS = 'NO'
        ),
        bend AS (
            -- a failure that closes >= N failures by the same user inside 15 minutes (the one N-1 rows back is <= 15 min older)
            SELECT f2.USER_NAME, f2.EVENT_TIMESTAMP AS TS
            FROM fl f2
            CROSS JOIN k
            JOIN fl f1
              ON f1.USER_NAME = f2.USER_NAME
             AND f1.RN = f2.RN - (k.N - 1)
             AND f1.EVENT_TIMESTAMP >= DATEADD('minute', -15, f2.EVENT_TIMESTAMP)
        ),
        seq AS (
            -- one time-ordered stream per burst user: burst ends, then successes; LAST_BEND = newest burst end before the row
            SELECT s.USER_NAME, s.TS, s.IS_SUCC, s.EVENT_ID, s.CLIENT_IP, s.CLIENT_TYPE, s.AUTH_FACTOR,
                   MAX(IFF(s.IS_SUCC, NULL, s.TS)) OVER (PARTITION BY s.USER_NAME ORDER BY s.TS, s.IS_SUCC
                                                         ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS LAST_BEND
            FROM (
                SELECT USER_NAME, TS, FALSE AS IS_SUCC, NULL AS EVENT_ID, NULL AS CLIENT_IP, NULL AS CLIENT_TYPE,
                       NULL AS AUTH_FACTOR
                FROM bend
                UNION ALL
                SELECT USER_NAME, EVENT_TIMESTAMP, TRUE, EVENT_ID, CLIENT_IP, REPORTED_CLIENT_TYPE,
                       FIRST_AUTHENTICATION_FACTOR
                FROM ev
                WHERE IS_SUCCESS = 'YES'
                  AND USER_NAME IN (SELECT USER_NAME FROM bend)
            ) s
        ),
        brk AS (
            -- a success within 60 minutes after a burst ended; PREV_TS = the user's previous such success
            SELECT USER_NAME, TS, EVENT_ID, CLIENT_IP, CLIENT_TYPE, AUTH_FACTOR, LAST_BEND,
                   LAG(TS) OVER (PARTITION BY USER_NAME ORDER BY TS, EVENT_ID) AS PREV_TS
            FROM seq
            WHERE IS_SUCC
              AND LAST_BEND >= DATEADD('minute', -60, TS)
        ),
        anc AS (
            -- the episode anchor, raised only inside the alert window (its evidence lies wholly inside ev)
            SELECT USER_NAME, TS, EVENT_ID, CLIENT_IP, CLIENT_TYPE, AUTH_FACTOR, LAST_BEND,
                   CONVERT_TIMEZONE('America/Chicago', TS)::TIMESTAMP_NTZ AS TS_CT
            FROM brk
            WHERE (PREV_TS IS NULL OR PREV_TS < DATEADD('minute', -60, TS))
              AND TS >= DATEADD('hour', -{ANCHOR_H}, CURRENT_TIMESTAMP())
        ),
        det AS (
            -- the anchor's evidence: every failure in the 75 minutes up to it (>= N by construction, a failure
            -- stamped in the same millisecond as the success included: seq orders it first)
            SELECT a.EVENT_ID, COUNT(*) AS N_FAIL, COUNT(DISTINCT f.CLIENT_IP) AS N_FAIL_IPS,
                   MIN(f.EVENT_TIMESTAMP) AS FIRST_FAIL, MAX(LEFT(COALESCE(f.ERROR_MESSAGE, ''), 200)) AS SAMPLE_ERROR
            FROM anc a
            JOIN ev f
              ON f.USER_NAME = a.USER_NAME
             AND f.IS_SUCCESS = 'NO'
             AND f.EVENT_TIMESTAMP <= a.TS
             AND f.EVENT_TIMESTAMP >= DATEADD('minute', -75, a.TS)
            GROUP BY a.EVENT_ID
        ),
        adm AS (
            -- admin-tier roles the user held AT the anchor (direct grants; owner list, same as SEC_ADMIN_GRANT)
            SELECT a.EVENT_ID, MIN(g.ROLE) AS ADMIN_ROLE, COUNT(DISTINCT g.ROLE) AS N_ADMIN_ROLES
            FROM anc a
            JOIN SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
              ON g.GRANTEE_NAME = a.USER_NAME
             AND g.ROLE IN {ROLES}
             AND g.CREATED_ON <= a.TS
             AND (g.DELETED_ON IS NULL OR g.DELETED_ON > a.TS)
            GROUP BY a.EVENT_ID
        ),
        x AS (
            SELECT a.USER_NAME, a.EVENT_ID, a.TS_CT, a.LAST_BEND, a.TS, a.CLIENT_IP, a.CLIENT_TYPE, a.AUTH_FACTOR,
                   d.N_FAIL, d.N_FAIL_IPS, d.FIRST_FAIL, d.SAMPLE_ERROR, m.ADMIN_ROLE, m.N_ADMIN_ROLES,
                   (HOUR(a.TS_CT) >= 20 OR HOUR(a.TS_CT) < 6 OR DAYOFWEEKISO(a.TS_CT) >= 6) AS OFF_HOURS
            FROM anc a
            JOIN det d ON d.EVENT_ID = a.EVENT_ID
            LEFT JOIN adm m ON m.EVENT_ID = a.EVENT_ID
        )""".replace("{ROLES}", ROLE_IN.replace("{pad}", " " * 28))


def _chain26(ev_h: int, anchor_h: int) -> str:
    return ARM26_CHAIN.replace("{EV_H}", str(ev_h)).replace("{ANCHOR_H}", str(anchor_h))


BAND26 = "IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRIT', 'WARN')"
KEY26 = ("{RULE} || '|' || LEFT(x.USER_NAME, 200) || '|' || " + BAND26 + " ||\n"
         "                   '|' || TO_VARCHAR(CONVERT_TIMEZONE('UTC', x.TS)::TIMESTAMP_NTZ, " + KEY_TS + ")")
SCAN_KEY26 = KEY26.replace("{RULE}", "c.RULE_ID")
PF_KEY26 = KEY26.replace("{RULE}", f"'{TAKEOVER}'")
CHAIN26 = _chain26(EV_HOURS, ANCHOR_HOURS)
PF_CHAIN26 = _chain26(PF_EV_HOURS, PF_HOURS)
TITLE26 = """\
LEFT('Possible account takeover: ' || x.USER_NAME || ' logged in ' ||
                    DATEDIFF('minute', x.LAST_BEND, x.TS) || ' min after a burst of ' || x.N_FAIL || ' failed logins' ||
                    IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL,
                        ' (' || IFF(x.OFF_HOURS, 'off-hours', '') ||
                        IFF(x.OFF_HOURS AND x.ADMIN_ROLE IS NOT NULL, ', ', '') ||
                        IFF(x.ADMIN_ROLE IS NOT NULL, 'admin role ' || x.ADMIN_ROLE, '') || ')', ''), 300)"""
DETAIL26 = """\
LEFT('Success ' || TO_VARCHAR(x.TS_CT, 'YYYY-MM-DD HH24:MI') || ' Central from ' ||
                    COALESCE(x.CLIENT_IP, '?') || ' (' || COALESCE(x.CLIENT_TYPE, '?') || ', ' ||
                    COALESCE(x.AUTH_FACTOR, '?') || ') after ' || x.N_FAIL || ' failed logins from ' ||
                    x.N_FAIL_IPS || ' IP(s) since ' ||
                    TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', x.FIRST_FAIL)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI') ||
                    ' Central. Off-hours: ' || IFF(x.OFF_HOURS, 'yes', 'no') || '. Admin roles held: ' ||
                    COALESCE(x.ADMIN_ROLE || IFF(x.N_ADMIN_ROLES > 1, ' +' || (x.N_ADMIN_ROLES - 1) || ' more', ''), 'none') ||
                    '. Sample error: ' || COALESCE(NULLIF(x.SAMPLE_ERROR, ''), 'n/a') ||
                    ' | Confirm with the user; unexplained = disable the user and rotate credentials (playbook). Never auto-declares an incident: declare one by hand. Source: ACCOUNT_USAGE.LOGIN_HISTORY - review in Security -> Access', 2000)"""

ARM26 = ARM26_HEAD + f"""\
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        k AS (
            SELECT GREATEST(CEIL(COALESCE(MAX(THRESHOLD_NUM), 5)), 2) AS N
            FROM cfg WHERE RULE_ID = '{TAKEOVER}'
            HAVING COUNT(*) > 0
        ),
{CHAIN26}
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               'ALL',
               IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRITICAL', c.SEVERITY),
               {TITLE26},
               {DETAIL26},
               x.N_FAIL,
               {SCAN_KEY26}
        FROM cfg c
        JOIN x ON c.RULE_ID = '{TAKEOVER}'

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        )
          -- never mint the WARN band once this anchor's CRIT event exists (any status): a CRIT row is never downgraded
          AND NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS h
            WHERE b.DEDUPE_KEY LIKE '%|WARN|%'
              AND h.RULE_ID = b.RULE_ID
              AND h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|WARN|', '|CRIT|')
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule {TAKEOVER} - other rules unaffected', CURRENT_ROLE();
    END;
"""

# ---------------------------------------------------------------------------------------------------
# Arm [27] SEC_ADMIN_GRANT. The ag CTE is shared VERBATIM with PREFLIGHT P162.2.
# ---------------------------------------------------------------------------------------------------
ARM27_HEAD = f"""\
    -- [27] {GRANT} (V162, Next-Fifty #39b: every new grant of an admin-tier role to a user, named -- the
    --      30-day BREAKGLASS_GRANTS_30D posture count never names the grantee). Hourly, ungated. Owner list:
    --      ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS. One event
    --      per grant row (grantee, role, CREATED_ON), raised once however many hourly reads see it: CREATED_ON inside
    --      the last {GRANT_HOURS}h (GRANTS_TO_USERS lags up to ~2h; the rest tolerates missed runs). A grant already revoked
    --      still raises (a short-lived elevation is the suspicious shape). PRIOR_GRANTS reads the same view's
    --      history (revoked rows kept) to tell a first-ever elevation from a re-grant. Severity is the rule's
    --      (HIGH); company ALL; never auto-declares an incident (SP_INCIDENT_AUTODECLARE excludes it, V162). The key
    --      ends in the grant's Central CREATED_ON to the millisecond with an explicit format.
"""

ARM27_AG = """\
        ag AS (
            SELECT GRANTEE_NAME, ROLE, CREATED_ON, DELETED_ON, GRANTED_BY,
                   ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1 AS PRIOR_GRANTS,
                   CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT
            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
            WHERE ROLE IN {ROLES}
        )""".replace("{ROLES}", ROLE_IN.replace("{pad}", " " * 27))

OFF27 = "HOUR(g.CREATED_CT) >= 20 OR HOUR(g.CREATED_CT) < 6 OR DAYOFWEEKISO(g.CREATED_CT) >= 6"
TITLE27 = f"""\
LEFT('Admin role ' || g.ROLE || ' granted to ' || g.GRANTEE_NAME ||
                    IFF({OFF27},
                        ' (off-hours)', '') ||
                    IFF(g.PRIOR_GRANTS = 0, ' - first time', ''), 300)"""
DETAIL27 = """\
LEFT('Granted ' || TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI') || ' Central by ' ||
                    COALESCE(g.GRANTED_BY, '?') || '. ' ||
                    IFF(g.PRIOR_GRANTS = 0, 'First grant of this role to this user on record.',
                        'Re-grant: ' || g.PRIOR_GRANTS || ' earlier grant(s) of this role to this user on record.') ||
                    IFF(g.DELETED_ON IS NULL, ' Still held.',
                        ' Since revoked ' ||
                        TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', g.DELETED_ON)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI') ||
                        ' Central.') ||
                    ' | Confirm the change was approved; unexplained = revoke it and review what the user ran (playbook). Source: ACCOUNT_USAGE.GRANTS_TO_USERS - review in Security -> Changes', 2000)"""
KEY27 = ("{RULE} || '|' || LEFT(g.GRANTEE_NAME, 200) || '|' || g.ROLE || '|' ||\n"
         "                   TO_VARCHAR(g.CREATED_CT, " + KEY_TS + ")")
SCAN_KEY27 = KEY27.replace("{RULE}", "c.RULE_ID")
PF_KEY27 = KEY27.replace("{RULE}", f"'{GRANT}'")

ARM27 = ARM27_HEAD + f"""\
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
{ARM27_AG}
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID,
               'ALL',
               c.SEVERITY,
               {TITLE27},
               {DETAIL27},
               1,
               {SCAN_KEY27}
        FROM cfg c
        JOIN ag g
          ON c.RULE_ID = '{GRANT}'
         AND g.CREATED_ON >= DATEADD('hour', -{GRANT_HOURS}, CURRENT_TIMESTAMP())

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
                   'rule {GRANT} - other rules unaffected', CURRENT_ROLE();
    END;
"""

# ---------------------------------------------------------------------------------------------------
# SP_INCIDENT_AUTODECLARE (from V154): A1, the crit CTE only.
# ---------------------------------------------------------------------------------------------------
auto = extract_proc(V154, "SP_INCIDENT_AUTODECLARE()")
# intervening changes that MUST survive (V098 re-link guard, V099 company scope, V154 attach + mitigate)
assert "          AND i.COMPANY = c.COMPANY\n" in auto and "RETURN 'auto-declare off';" in auto
assert auto.count("m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID") == 2
assert "    -- [attach] V154" in auto and "    -- [auto-mitigate] V154" in auto
A1_OLD = ("          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m\n"
          "                          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)\n"
          "    )\n"
          "    SELECT UUID_STRING() AS INCIDENT_ID, FAMILY, COMPANY,\n")
A1_BLOCK = ("          -- V162 (Next-Fifty #39, owner 2026-09-29): identity alerts never auto-declare -- a human declares after\n"
            "          -- contacting the user. [attach] below still links them to an incident a human opened for that family.\n"
            f"          AND e.RULE_ID NOT IN ('{TAKEOVER}', '{GRANT}')\n")
A1_NEW = A1_OLD.replace("    )\n    SELECT UUID_STRING()", A1_BLOCK + "    )\n    SELECT UUID_STRING()", 1)
auto = _swap(auto, A1_OLD, A1_NEW, "A1")
assert auto.count(f"AND e.RULE_ID NOT IN ('{TAKEOVER}', '{GRANT}')") == 1

# ---------------------------------------------------------------------------------------------------
# SP_ALERT_SCAN (from V157): H1-H4.
# ---------------------------------------------------------------------------------------------------
hourly = extract_proc(V157, "SP_ALERT_SCAN()")
assert hourly.count("fails := fails + 1") == 12
assert hourly.count("IF (MOD(ct_hour, 4) = 1) THEN") == 4 and hourly.count("IF (MOD(ct_hour, 3) = 2) THEN") == 1
H1_ANCHOR = "    IF (MOD(ct_hour, 3) = 2) THEN   -- V157 cadence gate: [22]"
assert hourly[:hourly.index(H1_ANCHOR)].endswith(
    "                   'rule posture-metric (generic) - other rules unaffected', CURRENT_ROLE();\n    END;\n")
hourly = _swap(hourly, H1_ANCHOR, ARM26 + ARM27 + H1_ANCHOR, "H1")
hourly = _swap(hourly, "' of 12 alert rule block(s) failed this run'", "' of 14 alert rule block(s) failed this run'", "H2")
hourly = _swap(hourly, "(12 - :fails)", "(14 - :fails)", "H3a", n=5)
hourly = _swap(hourly, "'/12 rule blocks ok'", "'/14 rule blocks ok'", "H3b", n=3)
RET_157 = ("'alert scan v12 (V157: + OPS_PIPELINE_DEGRADED self-watch + ETL-cycle add-on + condition-ended sweep + "
           "heartbeat, - dead break-glass arm, - retired cloud-services ratio arm, security arms every 4h, self-watch "
           "every 3h): '")
RET_162 = "'alert scan v13 (V162: + SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT hourly; V157 gates unchanged): '"
hourly = _swap(hourly, RET_157, RET_162, "H4")
assert hourly.count("fails := fails + 1") == 14
assert "(12 - :fails)" not in hourly and "/12 rule blocks ok" not in hourly

# ---------------------------------------------------------------------------------------------------
# The file.
# ---------------------------------------------------------------------------------------------------
TAKEOVER_NAME = ("Possible account takeover: at least THRESHOLD_NUM failed logins by one user within 15 minutes, then a "
                 "successful login within 60 minutes (CRITICAL off-hours Central or for an admin-role holder)")
GRANT_NAME = ("Admin-tier role granted directly to a user (ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, "
              "SNOW_ACCOUNTADMINS, SNOW_SYSADMINS), one event per grant")
for _n in (TAKEOVER_NAME, GRANT_NAME):
    assert len(_n) <= 200 and ";" not in _n and "'" not in _n and "FALSE" not in _n.upper(), _n

HEADER = f"""-- {NAME}
--
-- Next-Fifty #39 (owner decisions 2026-09-29): two HOURLY identity alerts that never auto-declare an incident.
--
-- WHY: the nightly SEC_FAILED_LOGINS [07] counts a user's failed logins per day, so a password spray or brute force
-- that GOT IN reads the same as a locked-out job and surfaces the next morning, and nothing names a user who was
-- just handed an admin-tier role (the BREAKGLASS_GRANTS_30D posture metric is a 30-day count). The app's Security >
-- Access "Account-takeover candidates" lens already correlates a failed burst with a later success, but only when
-- someone opens it.
--
--   + {TAKEOVER} (arm [26], hourly, ungated): at least THRESHOLD_NUM (seed 5, floor 2) failed logins by
--     one user within 15 minutes, then a successful login within 60 minutes of the burst's end. One event per episode,
--     anchored on the first such success (a further success within 60 minutes joins the episode). Severity is the
--     rule's (HIGH), CRITICAL when the anchor is off-hours (20:00-06:00 America/Chicago, or a Saturday/Sunday) or
--     the user directly held ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS or
--     SNOW_SYSADMINS at that moment. Every IS_SUCCESS = 'NO' row counts toward a burst (network-policy blocks and
--     JWT/SAML failures included, as in the app lens). Reads the last 27h of LOGIN_HISTORY; raises anchors from the
--     last 24h. The burst test is a linear equi-join on the per-user failure number, never a quadratic self-join,
--     so a brute-force storm cannot blow up the scan.
--   + {GRANT} (arm [27], hourly, ungated): one event per direct grant of one of those seven roles to a user
--     (GRANTS_TO_USERS CREATED_ON inside the last 26h), raised even when the grant was already revoked; flat HIGH;
--     the title flags an off-hours grant and a first-ever grant of that role to that user.
--   Both events are account-level (COMPANY 'ALL'): the Teams routes carry ALFA plus ALL, so a takeover of any user
--   still reaches Teams. The two dedupe keys end in a millisecond timestamp with an explicit format (the anchor
--   login in UTC; the grant's Central CREATED_ON), never a bare EVENT_ID: V117's snooze carry-forward reads a
--   10-character tail that parses as a date (a 10-digit integer is epoch seconds) as a date band, and would carry a
--   snooze to the user's NEXT takeover.
--   ~ SP_INCIDENT_AUTODECLARE re-derived from V154 (its current definer), byte-identical except ONE predicate in
--     the crit CTE: AND e.RULE_ID NOT IN ('{TAKEOVER}', '{GRANT}'). Neither rule ever opens an
--     incident, even if an operator later edits its severity to CRITICAL: a human declares after contacting the
--     user. The [attach] arm and the [auto-mitigate] sweep are unchanged, so a later takeover CRITICAL still links
--     to an incident a person declared for that family.
--   ~ SP_ALERT_SCAN re-derived from V157 (its current definer), byte-identical except: + counting arms [26] and
--     [27] after [21], before the [22] gate (so the self-alert, the V067 supersede sweep and the V117 carry-forward
--     see them in the same pass: a WARN -> CRIT crossing is superseded at once); tally 12 -> 14 (self-alert, [hb],
--     RETURN). The V157 cadence gates, [18]'s three roles, every sweep and [hb] are unchanged; the hourly
--     ACCOUNT_USAGE footprint stays COPY_HISTORY, CREDENTIALS, GRANTS_TO_ROLES, GRANTS_TO_USERS, LOGIN_HISTORY.
--   + ALERT_CONFIG {TAKEOVER} (SECURITY, HIGH, 5, 24h) and {GRANT} (SECURITY, HIGH, 0 = unused,
--     24h), WHEN NOT MATCHED only.
--
-- FILE ORDER: the autodeclare exclusion is replaced BEFORE the scan, so a CRITICAL takeover never meets the old
-- autodeclare; a partial apply (stop on the first error) always leaves a coherent prefix.
-- COST (estimated): ~3-5 s compile for [26] and ~2-3 s for [27] per hourly run (168 runs a week), plus XS execution
-- over 27h of LOGIN_HISTORY and GRANTS_TO_USERS -- about 0.1-0.2 cloud-services credits a week, roughly 1 USD a
-- week. Re-run DIAG_CS_SELF_COST a week after the apply; the lever is the 4-hourly security slot for [27].
-- LATENCY: hourly; ACCOUNT_USAGE lags up to ~2h, so an event arrives 1-3h after the login or grant.
-- FIRST RUN: the first hourly scan after apply raises every takeover episode of the last 24h and every admin grant
-- of the last 26h (CRITICAL ones included -- no incident is opened). PREFLIGHT_WAVE4.sql P162.1 / P162.2 list them.
-- No procedure runs at apply time.
-- ROLLBACK (order matters): FIRST re-run V157's SP_ALERT_SCAN (tally back to 12; the two rules stop raising), THEN
-- optionally V154's SP_INCIDENT_AUTODECLARE. Reversed, an hourly run in between could auto-declare a CRITICAL
-- takeover. Optionally disable the two rules in Alerts > Rules; the seeds can stay.
-- Apply AFTER V161. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20162, 'V162 requires V161 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 161) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The two rules. WHEN NOT MATCHED only -- an operator's edits are never clobbered. SEC_ADMIN_GRANT's THRESHOLD_NUM
-- is unused (one event per grant); AUTO_CLEAR_ENABLED keeps its default.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('{TAKEOVER}', 'SECURITY', '{TAKEOVER_NAME}', TRUE, 'HIGH', 5, 24),
        ('{GRANT}', 'SECURITY', '{GRANT_NAME}', TRUE, 'HIGH', 0, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

"""

MARK_AUTO = (f"-- >>> derived:SP_INCIDENT_AUTODECLARE  (from V154; + no auto-declare for {TAKEOVER} / {GRANT}, "
             "V162)\n")
MARK_SCAN = (f"-- >>> derived:SP_ALERT_SCAN  (from V157; + [26] {TAKEOVER} + [27] {GRANT} hourly counting arms, "
             "tally 12 -> 14, V162)\n")

DESCRIPTION = (
    f"Next-Fifty #39 (owner 2026-09-29): two hourly identity alerts that never auto-declare an incident. {TAKEOVER} "
    "(SP_ALERT_SCAN arm [26], ungated): at least THRESHOLD_NUM (seed 5, floor 2) failed logins by one user within 15 "
    "minutes, then a successful login within 60 minutes; one event per episode; CRITICAL when the login is off-hours "
    "(20:00-06:00 America/Chicago or a weekend) or the user directly held ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, "
    "USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS or SNOW_SYSADMINS, else the rule severity (HIGH); 27h LOGIN_HISTORY read, "
    f"24h anchor window. {GRANT} (arm [27], ungated): one event per direct grant of one of those roles to a user in "
    "the last 26h, revoked or not; flat HIGH; the title flags off-hours and first-time grants. Both company ALL; keys "
    "end in an explicit millisecond timestamp (never a bare EVENT_ID, which the V117 carry-forward would read as a "
    "date). SP_INCIDENT_AUTODECLARE re-derived from V154, byte-identical except the crit CTE excludes both rules "
    "([attach] and [auto-mitigate] unchanged). SP_ALERT_SCAN re-derived from V157, byte-identical except the two "
    "counting arms and the tally 12 -> 14. Seeds both rules WHEN NOT MATCHED only. No task change, no SETTINGS key, "
    "no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 162 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 162);
"""

out = HEADER + MARK_AUTO + auto + "\n\n" + MARK_SCAN + hourly + "\n" + VERSION_ROW

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n")
assert out.count("CREATE OR REPLACE PROCEDURE") == 2 and out.count("$$") == 6
assert "$$" not in out[:out.index("EXECUTE IMMEDIATE")]
assert out.index(MARK_AUTO) < out.index(MARK_SCAN)                   # the exclusion lands before the new arms
assert out.count("-- >>> derived:") == 2 and "LINEAGE-WAIVER" not in out
_top = "".join(part for i, part in enumerate(out.split("$$")) if i % 2 == 0)      # outside every $$ body
assert not re.search(r"^\s*(?:CREATE(?: OR REPLACE)? TASK|ALTER TASK|EXECUTE TASK|CALL |DROP )", _top, re.M | re.I)
assert "COMPANY_FOR_USER" not in ARM26 + ARM27
assert "V162 requires V161 first" in out and "SELECT 162 AS VERSION" in out
assert "\r" not in out
for _new in (HEADER, ARM26, ARM27, A1_BLOCK, MARK_AUTO, MARK_SCAN, RET_162, VERSION_ROW):
    assert _new.isascii(), _new[:60]                             # (the carried V157 body has its own em-dashes)
_scan_body = hourly[hourly.index("$$") + 2:hourly.rindex("$$")]
assert "$$" not in _scan_body
assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _scan_body)) == {
    "COPY_HISTORY", "CREDENTIALS", "GRANTS_TO_ROLES", "GRANTS_TO_USERS", "LOGIN_HISTORY"}

target = Path(os.environ.get("V162_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")

# ---- optional read-only PREFLIGHT (P162.1-P162.3) -----------------------------------------------------
pf = os.environ.get("PREFLIGHT_OUT")
if pf:
    pf_k = f"""\
        k AS (
            SELECT GREATEST(CEIL(COALESCE(MAX(c.THRESHOLD_NUM), 5)), 2) AS N
            FROM (SELECT 1 AS ONE) o
            LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = '{TAKEOVER}'
        ),
"""
    preflight = f"""-- ====================================================================================================
--  V162 PREFLIGHT (read-only; run BEFORE applying V162). Next-Fifty #39: {TAKEOVER} + {GRANT}.
--  Each grid runs the arm's OWN CTE chain (outputs/gen_v162.py) with the windows widened to 30 days.
--  IN_FIRST_RUN_WINDOW = TRUE rows are exactly what the first hourly scan after the apply raises. Changes nothing.
-- ====================================================================================================

-- P162.1 {TAKEOVER}: every takeover episode anchored in the last 30 days (arm [26], 27h -> {PF_EV_HOURS}h read,
-- 24h -> {PF_HOURS}h anchor window). N = the rule's THRESHOLD_NUM, 5 until V162 seeds it. BAND CRIT = CRITICAL
-- (off-hours Central or an admin-tier role held directly at the login); WARN = the rule severity (HIGH).
-- WEEK_BAND_EPISODES = episodes in the same ISO week and band (the noise estimate per week).
WITH
{pf_k}{PF_CHAIN26}
SELECT p.*, COUNT(*) OVER (PARTITION BY p.WEEK_START, p.BAND) AS WEEK_BAND_EPISODES
FROM (
    SELECT x.USER_NAME,
           {BAND26} AS BAND,
           TRIM(IFF(x.OFF_HOURS, 'off-hours ', '') || IFF(x.ADMIN_ROLE IS NOT NULL, 'admin role ' || x.ADMIN_ROLE, ''))
               AS CRIT_REASON,
           x.TS_CT AS SUCCESS_CENTRAL,
           DATEDIFF('minute', x.LAST_BEND, x.TS) AS MIN_AFTER_BURST,
           x.N_FAIL, x.N_FAIL_IPS, x.CLIENT_IP, x.CLIENT_TYPE, x.AUTH_FACTOR, x.SAMPLE_ERROR,
           TO_DATE(DATEADD('day', 1 - DAYOFWEEKISO(x.TS_CT), x.TS_CT)) AS WEEK_START,
           x.TS >= DATEADD('hour', -{ANCHOR_HOURS}, CURRENT_TIMESTAMP()) AS IN_FIRST_RUN_WINDOW,
           {PF_KEY26} AS DEDUPE_KEY_PREVIEW
    FROM x
) p
ORDER BY p.SUCCESS_CENTRAL DESC;

-- P162.2 {GRANT}: every direct admin-tier grant to a user in the last 30 days (arm [27], 26h -> {PF_HOURS}h),
-- revoked or not, with the counts by role and by grantor (a SCIM / provisioning user shows up as one big grantor).
WITH
{ARM27_AG}
SELECT q.*,
       COUNT(*) OVER (PARTITION BY q.ROLE) AS GRANTS_OF_ROLE_30D,
       COUNT(*) OVER (PARTITION BY q.GRANTED_BY) AS GRANTS_BY_GRANTOR_30D
FROM (
    SELECT g.GRANTEE_NAME, g.ROLE, g.GRANTED_BY, g.CREATED_CT AS GRANTED_CENTRAL,
           CONVERT_TIMEZONE('America/Chicago', g.DELETED_ON)::TIMESTAMP_NTZ AS REVOKED_CENTRAL,
           ({OFF27}) AS OFF_HOURS,
           g.PRIOR_GRANTS = 0 AS FIRST_TIME, g.PRIOR_GRANTS,
           g.CREATED_ON >= DATEADD('hour', -{GRANT_HOURS}, CURRENT_TIMESTAMP()) AS IN_FIRST_RUN_WINDOW,
           {TITLE27} AS TITLE_PREVIEW,
           {PF_KEY27} AS DEDUPE_KEY_PREVIEW
    FROM ag g
    WHERE g.CREATED_ON >= DATEADD('hour', -{PF_HOURS}, CURRENT_TIMESTAMP())
) q
ORDER BY q.GRANTED_CENTRAL DESC;

-- P162.3 What SP_INCIDENT_AUTODECLARE sees now: OPEN/ACK CRITICAL events of the last 24h by rule and company, and
-- whether each rule still auto-declares after V162 (the two identity rules never do). INCIDENT_AUTO_DECLARE_CRITICAL
-- is the global switch (absent = TRUE).
SELECT e.RULE_ID, e.COMPANY, COUNT(*) AS OPEN_OR_ACK_CRITICAL_24H,
       COUNT_IF(m.REF_ID IS NULL) AS NOT_LINKED_TO_AN_INCIDENT,
       MIN(e.RAISED_AT) AS FIRST_RAISED, MAX(e.RAISED_AT) AS LAST_RAISED,
       IFF(e.RULE_ID IN ('{TAKEOVER}', '{GRANT}'), 'never (V162)', 'yes') AS AUTO_DECLARES_AFTER_V162,
       MAX(sw.SWITCH_VALUE) AS INCIDENT_AUTO_DECLARE_CRITICAL
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
LEFT JOIN (SELECT DISTINCT REF_ID FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE MEMBER_KIND = 'ALERT') m
       ON m.REF_ID = e.EVENT_ID
CROSS JOIN (SELECT COALESCE(MAX(VALUE), 'TRUE') AS SWITCH_VALUE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
            WHERE KEY = 'INCIDENT_AUTO_DECLARE_CRITICAL') sw
WHERE UPPER(e.SEVERITY) = 'CRITICAL'
  AND e.STATUS IN ('OPEN', 'ACK')
  AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
GROUP BY e.RULE_ID, e.COMPANY
ORDER BY OPEN_OR_ACK_CRITICAL_24H DESC, e.RULE_ID;
"""
    assert "\r" not in preflight and preflight.isascii()
    Path(pf).write_text(preflight, encoding="utf-8", newline="\n")

# ---- optional RUN_NEXT PART B (V162.1-V162.6) -----------------------------------------------------------
pb = os.environ.get("PARTB_OUT")
if pb:
    def _ddl(proc: str, frag: str, alias: str) -> str:
        assert not set(frag) & {"'", "\\", "\n", "\r"}, frag
        return f"CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{proc}'), '{frag}') AS {alias}"

    scan_frags = (("SEC_LOGIN_TAKEOVER", "ARM26"), ("SEC_ADMIN_GRANT", "ARM27"),
                  ("alert scan v13 (V162:", "RETURN_V162"), ("/14 rule blocks ok", "TALLY_14"),
                  ("LAST_BEND", "BURST_CHAIN"), ("HH24:MI:SS.FF3", "MS_KEY_FORMAT"),
                  ("IF (MOD(ct_hour, 4) = 1) THEN", "V157_GATES_KEPT"), ("SP_SCAN_ETL_CYCLE", "V157_ADDON_KEPT"))
    scan_cols = ",\n       ".join(_ddl("SP_ALERT_SCAN()", f, a) for f, a in scan_frags)
    scan_old = _ddl("SP_ALERT_SCAN()", "/12 rule blocks ok", "OLD_TALLY_12")
    auto_cols = ",\n       ".join(_ddl("SP_INCIDENT_AUTODECLARE()", f, a) for f, a in (
        ("e.RULE_ID NOT IN (", "CRIT_EXCLUSION"), ("V162 (Next-Fifty #39", "V162_COMMENT"),
        ("incident_attach_failed", "V154_ATTACH_KEPT"), ("incident_mitigate_failed", "V154_MITIGATE_KEPT")))
    partb = f"""-- ---- V162 checks -----------------------------------------------------
-- (V162.1, now) the two rules are seeded.
SELECT RULE_ID, FAMILY, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS
FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
WHERE RULE_ID IN ('{TAKEOVER}', '{GRANT}')
ORDER BY RULE_ID;   -- 2 rows: {GRANT} SECURITY TRUE HIGH 0 24; {TAKEOVER} SECURITY TRUE HIGH 5 24

-- (V162.2, now) SP_ALERT_SCAN is V162's.
SELECT {scan_cols},
       {scan_old};   -- all TRUE except OLD_TALLY_12 = FALSE

-- (V162.3, now) SP_INCIDENT_AUTODECLARE excludes the two rules.
SELECT {auto_cols};   -- all TRUE

-- (V162.4, now, OPTIONAL) the same work the hourly TASK_INCIDENT_AUTODECLARE does -- it raises no alert and sends
-- no email (the V154 precedent). Expect 'auto-declared N incident(s); attached N later critical(s); ...'.
-- CALL DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE();

-- (V162.5, after the next :07 Central hourly graph, ~1h) the scan ran all 14 blocks; compare the new events with
-- PREFLIGHT P162.1 / P162.2 (IN_FIRST_RUN_WINDOW rows).
SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, STATUS
FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
WHERE SOURCE_NAME = 'ALERT_SCAN_HOURLY';   -- STATUS = alert scan 14/14 rule blocks ok; LAST_LOAD_TS within the hour
SELECT LOGGED_AT, CONTEXT, LEFT(ERROR_MESSAGE, 300) AS MSG
FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
WHERE PAGE = 'AlertScan' AND ERROR_TYPE = 'rule_block_failed'
  AND (CONTEXT LIKE 'rule {TAKEOVER}%' OR CONTEXT LIKE 'rule {GRANT}%')
  AND LOGGED_AT >= DATEADD('hour', -3, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
ORDER BY LOGGED_AT DESC;   -- expect 0 rows
SELECT RULE_ID, SEVERITY, STATUS, COUNT(*) AS N, MIN(RAISED_AT) AS FIRST_RAISED, MAX(RAISED_AT) AS LAST_RAISED
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID IN ('{TAKEOVER}', '{GRANT}')
GROUP BY RULE_ID, SEVERITY, STATUS
ORDER BY RULE_ID, SEVERITY, STATUS;   -- the counts P162.1 / P162.2 flagged IN_FIRST_RUN_WINDOW
SELECT RULE_ID, SEVERITY, TITLE, DEDUPE_KEY, RAISED_AT
FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
WHERE RULE_ID IN ('{TAKEOVER}', '{GRANT}')
ORDER BY RAISED_AT DESC
LIMIT 50;   -- keys end in a YYYY-MM-DD HH24:MI:SS.FF3 timestamp

-- (V162.6, after 24h) neither rule auto-declared an incident.
SELECT i.INCIDENT_ID, i.DETECTED_AT, e.RULE_ID, e.SEVERITY, m.LINKED_BY
FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e ON e.EVENT_ID = m.REF_ID
WHERE m.MEMBER_KIND = 'ALERT'
  AND m.LINKED_BY = 'SP_INCIDENT_AUTODECLARE'
  AND i.DECLARED_BY = 'SP_INCIDENT_AUTODECLARE'
  AND e.RULE_ID IN ('{TAKEOVER}', '{GRANT}');   -- expect 0 rows
"""
    assert "\r" not in partb and partb.isascii()
    Path(pb).write_text(partb, encoding="utf-8", newline="\n")

print(f"V162 written: {target} ({len(out)} bytes)")
