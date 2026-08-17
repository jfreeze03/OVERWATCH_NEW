#!/usr/bin/env python3
"""Forward-generate V084: SEC_NEW_EXPOSURE proactive alert (CoCo Sec36).

The app already SHOWS grants to PUBLIC passively (Security -> Access). This turns
that passive visibility into proactive alerting: a privilege granted to PUBLIC is
inherited by every role in the account, so a NEW one is a real widening of the
blast radius that a DBA wants paged on, not something to notice on their next
manual review.

Two additive changes, both idempotent:
  * Seed a SECURITY rule SEC_NEW_EXPOSURE into ALERT_CONFIG (HIGH, threshold 1,
    24h window) via the house WHEN-NOT-MATCHED MERGE, so it never clobbers an
    operator edit and re-apply is a no-op.
  * Re-derive SP_ALERT_SCAN from its latest base (V079) with a new arm [20] that
    raises the rule for each new grant to PUBLIC in the last 24h, reading
    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES. Deduped on grant IDENTITY
    (RULE_ID | PRIVILEGE | GRANTED_ON | CREATED_ON), NOT on the calendar date:
    the signal is discrete (like the new-admin-network arm [18]), not a
    cumulative daily volume (like the egress arm [19]), so each distinct grant
    alerts exactly once - a second grant later the same day still pages, and a
    grant already alerted never re-pages as it ages through the rolling window. A
    batch GRANT ON ALL ... collapses to one event (shared CREATED_ON) counting
    its objects, so it does not flood. Scope note: this watches new grants to the
    PUBLIC *role*; new/broadened outbound shares are a separate future signal
    under the same rule.

Derivation law: SP_ALERT_SCAN is extracted verbatim from V079 and the ONLY edits
are (a) the new arm spliced in before the fails self-alert and (b) the hardcoded
rule-block count 14 -> 15 in the self-alert text and RETURN string. Reversing
those two edits reproduces the V079 body byte-for-byte (locked by the test).

No table or task change (SP_ALERT_SCAN is already wired to TASK_ALERT_SCAN). The
scanner is NOT called at apply time -- it would fire alerts -- and self-corrects
on its next hourly run (same precedent as V079).

Owner applies in Snowsight after V083. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_ALERTSCAN = MIG / "V079__ai_predicate_coco_historical_split.sql"


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


# The new scan arm. Discrete per-grant signal (like arm [18] SEC_NEW_ADMIN_NETWORK)
# NOT a cumulative daily one (arm [19]): one row per distinct new grant to PUBLIC,
# deduped on grant identity so a second grant the same day still pages and an
# already-alerted grant never re-pages as it ages through the rolling 24h window.
# A batch GRANT ON ALL ... collapses to one event (shared CREATED_ON) counting its
# objects. Its own isolated BEGIN..EXCEPTION so one broken rule logs and increments
# `fails` instead of killing the whole scan.
NEW_ARM = """    -- [20] SEC_NEW_EXPOSURE (V084 - CoCo Sec36: a new grant to PUBLIC widens the blast radius)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        pub AS (
            -- One row per distinct new grant to PUBLIC. A batch GRANT ON ALL ...
            -- shares one CREATED_ON, so it collapses to a single event counting
            -- its objects (N_OBJECTS) rather than flooding one alert per object.
            SELECT PRIVILEGE, GRANTED_ON, CREATED_ON,
                   COUNT(*) AS N_OBJECTS,
                   MAX(GRANTED_BY) AS GRANTED_BY,
                   MAX(NAME) AS SAMPLE_NAME
            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
            WHERE GRANTEE_NAME = 'PUBLIC'
              AND DELETED_ON IS NULL
              AND CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY PRIVILEGE, GRANTED_ON, CREATED_ON
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'New grant to PUBLIC: ' || p.PRIVILEGE || ' ON ' || p.GRANTED_ON
                   || IFF(p.N_OBJECTS > 1, ' (x' || p.N_OBJECTS || ' objects)',
                          ' ' || COALESCE(p.SAMPLE_NAME, '')),
               'A privilege granted to PUBLIC is inherited by every role in the account. '
                   || 'Granted ' || p.CREATED_ON || ' by ' || COALESCE(p.GRANTED_BY, '?')
                   || '. Source: ACCOUNT_USAGE.GRANTS_TO_ROLES - review in Security -> Access.',
               p.N_OBJECTS,
               c.RULE_ID || '|' || p.PRIVILEGE || '|' || p.GRANTED_ON || '|' || TO_VARCHAR(p.CREATED_ON)
        FROM cfg c
        JOIN pub p
          ON c.RULE_ID = 'SEC_NEW_EXPOSURE'
         AND p.N_OBJECTS >= c.THRESHOLD_NUM

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
                   'rule SEC_NEW_EXPOSURE - other rules unaffected', CURRENT_ROLE();
    END;
"""

_ANCHOR = "    IF (fails > 0) THEN\n"       # start of the fails self-alert block


def derive_scan(base: Path) -> str:
    """Extract SP_ALERT_SCAN from V079 and splice arm [20] + bump the count 14->15."""
    proc = extract_proc(base.read_text(encoding="utf-8"), "SP_ALERT_SCAN")
    assert "SEC_NEW_EXPOSURE" not in proc, "arm already present in base"
    assert proc.count(_ANCHOR) == 1, f"expected 1 fails-block anchor, got {proc.count(_ANCHOR)}"
    # Splice the new arm in right after the last existing arm ([19]) and before the
    # fails self-alert (so the V067 escalation-supersede sweep stays last).
    proc = proc.replace(_ANCHOR, NEW_ARM + _ANCHOR, 1)
    # Bump the hardcoded rule-block count (14 -> 15) in the self-alert text and the
    # RETURN string. Full-phrase replacements so the egress arm's "14d avg" and the
    # DATEADD('day', -14, ...) windows are untouched.
    assert proc.count("' of 14 alert rule block(s) failed this run'") == 1
    proc = proc.replace("' of 14 alert rule block(s) failed this run'",
                        "' of 15 alert rule block(s) failed this run'")
    assert proc.count("(14 - :fails) || '/14 rule blocks ok'") == 1
    proc = proc.replace("(14 - :fails) || '/14 rule blocks ok'",
                        "(15 - :fails) || '/15 rule blocks ok'")
    # No stale count survives; the new arm and rule id are present exactly once as a marker.
    assert "of 14 alert" not in proc and "/14 rule blocks" not in proc and "(14 - :fails)" not in proc
    assert proc.count("-- [20] SEC_NEW_EXPOSURE") == 1
    assert proc.count("'SEC_NEW_EXPOSURE'") == 1          # the ON c.RULE_ID join literal
    return proc


alert_scan = derive_scan(BASE_ALERTSCAN)

# Sanity: the proc kept its identity and its untouched anchors.
assert "COST_EGRESS_SPIKE" in alert_scan and "SEC_NEW_ADMIN_NETWORK" in alert_scan
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()" in alert_scan

out = f"""-- V084__security_new_exposure_alert.sql
--
-- SEC_NEW_EXPOSURE proactive alert (CoCo Sec36). A privilege granted to PUBLIC is
-- inherited by every role in the account, so a NEW grant to PUBLIC is a real
-- widening of the blast radius. The app already shows PUBLIC grants passively
-- (Security -> Access); this makes that visibility proactive.
--
-- Two additive, idempotent changes:
--   * Seed SECURITY rule SEC_NEW_EXPOSURE into ALERT_CONFIG (HIGH, threshold 1,
--     24h) via the house WHEN-NOT-MATCHED MERGE (never clobbers an operator edit).
--   * Re-derive SP_ALERT_SCAN from V079 with a new arm [20] that raises the rule
--     once per distinct new grant to PUBLIC in the last 24h, reading
--     SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES. Deduped on grant identity
--     (RULE_ID | PRIVILEGE | GRANTED_ON | CREATED_ON) - a discrete signal like
--     arm [18], not a cumulative daily one like arm [19] - so distinct same-day
--     grants each page and an already-alerted grant never re-pages as it ages
--     through the rolling window; a batch GRANT ON ALL collapses to one event.
--     Scope: new grants to the PUBLIC *role* (shares are a separate future signal).
--
-- The ONLY edits to SP_ALERT_SCAN vs V079 are the new arm and the rule-block
-- count 14 -> 15 in the self-alert text + RETURN string; reversing both
-- reproduces the V079 body byte-for-byte. No table or task change (the proc is
-- already wired to TASK_ALERT_SCAN). The scanner is NOT fired at apply time -- it
-- would raise alerts -- and self-corrects on its next hourly run.
--
-- Owner applies in Snowsight after V083. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20084, 'V084 requires V083 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 83) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Seed the SECURITY rule (idempotent WHEN NOT MATCHED; never clobbers operator edits).
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT 'SEC_NEW_EXPOSURE' AS RULE_ID, 'SECURITY' AS FAMILY,
           'A privilege was newly granted to PUBLIC (inherited by every role)' AS NAME,
           TRUE AS ENABLED, 'HIGH' AS SEVERITY, 1 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- >>> derived:SP_ALERT_SCAN  (+ arm [20] SEC_NEW_EXPOSURE, from V079)
{alert_scan}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 84 AS VERSION,
       'SEC_NEW_EXPOSURE proactive alert (CoCo Sec36): seeds a SECURITY rule and adds arm [20] to SP_ALERT_SCAN that raises a HIGH alert once per new privilege granted to PUBLIC in the last 24h (inherited by every role), reading ACCOUNT_USAGE.GRANTS_TO_ROLES and deduped on grant identity (a batch GRANT ON ALL collapses to one event). Turns passive PUBLIC-grant visibility into proactive alerting. Re-derives SP_ALERT_SCAN from V079 (arm count 14->15); no table/task change; the scanner is not fired at apply time and self-corrects on its hourly schedule.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 84);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20084" in out and "IF (v < 83) THEN" in out
assert "SELECT 84 AS VERSION" in out and "WHERE VERSION = 84)" in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in out   # never fire at apply
assert out.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG") == 1
assert "of 15 alert rule block(s)" in out and "/15 rule blocks ok" in out
assert "of 14 alert" not in out and "/14 rule blocks" not in out
# Discrete per-grant signal: identity dedupe + object-count collapse + threshold gate.
assert "GROUP BY PRIVILEGE, GRANTED_ON, CREATED_ON" in out
assert "p.N_OBJECTS >= c.THRESHOLD_NUM" in out
assert "c.RULE_ID || '|' || p.PRIVILEGE || '|' || p.GRANTED_ON || '|' || TO_VARCHAR(p.CREATED_ON)" in out
assert "TO_VARCHAR(CURRENT_DATE())" not in NEW_ARM   # not the daily-date grain

target = Path(os.environ.get("V084_OUT") or (MIG / "V084__security_new_exposure_alert.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
