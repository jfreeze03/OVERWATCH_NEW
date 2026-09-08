#!/usr/bin/env python3
"""Forward-generate V129: the PIPE_REF_GAP daily alert (ETL Phase 1b).

The Operations ▸ Pipeline ▸ "Reference-data gaps" panel (v4.499.0) shows staging codes missing
from the XLAT translation table live on page-open. This adds the DAILY ALERT so the same gap
pages/emails autonomously — a source code with no XLAT row hard-fails the nightly load.

Config-driven, same SETTINGS as the panel (ETL_REF_GAP_XLAT + ETL_REF_GAP_CHECKS). The dynamic,
cross-database MINUS scan is isolated in a dedicated SP_SCAN_REF_GAPS() proc that writes the current
gaps to ETL_REF_GAP_RESULTS; SP_ALERT_SCAN_DAILY gets a 7th rule arm that CALLs it and raises one
ALERT_EVENTS row per code type with a gap. The arm sits inside the SAME per-arm EXCEPTION guard as
every other rule block, so a missing SELECT grant on the customer's tables logs to APP_ERROR_LOG and
NEVER breaks the other six alert rules.

Identifiers parsed from config are allowlist-validated (RLIKE) before they reach the built SQL, and
the family name is a doubled-quote literal — a malformed/hostile config row is dropped, not emitted.
The regex uses CHR(10)/[.]/[*] so the generated SQL carries no backslash escapes.

SP_ALERT_SCAN_DAILY is re-derived from V111 (its latest CREATE OR REPLACE) with ONLY the new arm
inserted before the OPS_SCAN_DEGRADED self-alert; everything else is byte-identical. The 6-core-rule
scan-health tally is intentionally unchanged -- the ref-gap arm is an optional external-dependency
add-on that logs failures to APP_ERROR_LOG WITHOUT incrementing :fails, so an out-of-band grant gap
never trips the OPS_SCAN_DEGRADED self-alert. Owner applies in Snowsight after V128; the next daily
scan evaluates the new rule.
PIPE_REF_GAP is HIGH severity, so the existing NATIVE_ALERT_NEW_EVENTS email path delivers it to the
configured OVERWATCH_EMAIL recipient (JDees@alfains.com) when email is enabled. This file never runs
from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V111__cost_budget_pace_completed_days.sql"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN_DAILY\(")

# ---- the new [17] PIPE_REF_GAP arm ---------------------------------------------------
# Mirrors every other arm: reads ALERT_CONFIG WHERE ENABLED, dedup via WHERE NOT EXISTS,
# and the standard EXCEPTION guard (logs to APP_ERROR_LOG, other rules unaffected). It first
# CALLs SP_SCAN_REF_GAPS() to refresh ETL_REF_GAP_RESULTS, then raises one alert per code type
# whose gap count meets THRESHOLD_NUM.
NEW_ARM = """\
    -- [17] PIPE_REF_GAP  (optional external-dependency add-on: NOT counted toward the
    -- 6-core-rule scan-health tally, because its scan reads customer staging/XLAT tables that
    -- are SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        g AS (
            SELECT CHECK_NAME,
                   COUNT(*) AS N,
                   LISTAGG(NEW_CODE, ', ') WITHIN GROUP (ORDER BY NEW_CODE) AS CODES
            FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS
            GROUP BY CHECK_NAME
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               g.CHECK_NAME || ': ' || g.N || ' new source code(s) missing from XLAT',
               'The nightly load will fail on the missing code(s). Add the XLAT translation '
                   || 'row(s) before the next cycle. New codes: ' || LEFT(g.CODES, 1700),
               g.N,
               c.RULE_ID || '|' || g.CHECK_NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN g ON c.RULE_ID = 'PIPE_REF_GAP' AND g.N >= COALESCE(c.THRESHOLD_NUM, 1)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (unlike the six core arms). The ref-gap
            -- scan depends on SELECT grants on EXTERNAL customer tables applied out-of-band, so a
            -- grant gap (or any ref-gap-specific error) is recorded in APP_ERROR_LOG but must not
            -- trip the OPS_SCAN_DEGRADED self-alert about the core internal rules. Self-heals the
            -- moment grants land.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'ref_gap_scan_failed', :emsg,
                   'rule PIPE_REF_GAP - optional external add-on; needs SELECT on staging/XLAT tables', CURRENT_ROLE();
    END;
"""

# ---- transform the extracted proc ----------------------------------------------------
# T1: insert the new arm after the COST_CONTRACT_BREACH block's END, before the self-alert.
ANCHOR = "    END;\n    IF (fails > 0) THEN"
assert proc.count(ANCHOR) == 1, f"self-alert anchor: got {proc.count(ANCHOR)}"
proc = proc.replace(ANCHOR, "    END;\n" + NEW_ARM + "    IF (fails > 0) THEN")

# The 6-core-rule scan-health tally is intentionally LEFT UNCHANGED: the ref-gap arm is an optional
# external-dependency add-on that must not count toward OPS_SCAN_DEGRADED (see the arm's EXCEPTION
# note), so ONLY the new arm is inserted -- everything else in the proc stays byte-identical to V111.

# post-conditions on the transformed proc
assert "-- [17] PIPE_REF_GAP" in proc
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()" in proc
assert proc.count("EXCEPTION\n        WHEN OTHER THEN") == 7  # 6 original arms + the new one
assert proc.count("fails := fails + 1") == 6  # only the 6 core arms bump fails; ref-gap does NOT
assert "/6 rule blocks ok (daily)" in proc  # core tally unchanged (ref-gap not counted)
assert "COST_CONTRACT_BREACH" in proc and "PIPE_TASK_FAILURES" in proc  # nothing dropped

# ---- the isolated config-driven scan proc + its results table + the rule seed ---------
RESULTS_DDL = """\
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS (
    CHECK_NAME  VARCHAR(200)  NOT NULL,
    NEW_CODE    VARCHAR(500)  NOT NULL,
    SCANNED_AT  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);
"""

RULE_MERGE = """\
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('PIPE_REF_GAP', 'PIPELINE', 'Source codes missing from the XLAT reference table (nightly load will fail)', TRUE, 'HIGH', 1, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);
"""

# SP_SCAN_REF_GAPS: parse ETL_REF_GAP_CHECKS, allowlist-validate identifiers, build a UNION-ALL of
# per-check "(staging codes MINUS XLAT codes) g" subqueries (same shape as the app builder), and
# replace ETL_REF_GAP_RESULTS. No backslashes in the generated SQL: CHR(10) for the entry split,
# [.] / [*] / [ ] character classes for the regex. DELETE (not TRUNCATE) keeps it transactional so
# the caller's CALL + read stay in one statement context.
SCAN_PROC = """\
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Config-driven reference-data gap scan (ETL Phase 1b). Reads ETL_REF_GAP_XLAT + ETL_REF_GAP_CHECKS
-- from SETTINGS and replaces ETL_REF_GAP_RESULTS with the codes present in each configured staging
-- table but MISSING from the XLAT reference table (SRC_IDNTFTN_NM = the check name, value column
-- SRC_IDNTFTN_VAL). The PIPE_REF_GAP arm of SP_ALERT_SCAN_DAILY reads that table and raises one alert
-- per code type. A leading '*' pin marker (a panel-only concern) is stripped; the alert scans every
-- configured check. Identifiers are allowlist-validated so the built SQL is always well-formed; the
-- caller wraps CALL in its own EXCEPTION guard, so a missing grant on the source tables is contained.
DECLARE
    xlat STRING;
    checks STRING;
    enabled_cnt INT;
    scan_sql STRING;
    ins_sql STRING;
BEGIN
    -- gate: only scan when PIPE_REF_GAP exists AND is enabled (skip the cross-DB read otherwise).
    SELECT COUNT(*) INTO :enabled_cnt
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PIPE_REF_GAP' AND ENABLED;

    -- always clear last run's rows first, so stale gaps never linger after a fix or a disable.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS;
    IF (:enabled_cnt = 0) THEN
        RETURN 'ref-gap scan skipped (rule disabled)';
    END IF;

    SELECT MAX(IFF(KEY = 'ETL_REF_GAP_XLAT', VALUE, NULL)),
           MAX(IFF(KEY = 'ETL_REF_GAP_CHECKS', VALUE, NULL))
      INTO :xlat, :checks
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    IF (:xlat IS NULL OR TRIM(:xlat) = '' OR :checks IS NULL OR TRIM(:checks) = ''
        OR NOT RLIKE(TRIM(:xlat), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')) THEN
        RETURN 'ref-gap scan skipped (unconfigured or invalid XLAT)';
    END IF;

    -- Parse entries (newline- or ';'-separated), strip a leading '*' pin, allowlist-validate the
    -- family NAME + staging FQN + code column, and LISTAGG a UNION-ALL of per-check MINUS subqueries.
    -- q_name is the apostrophe-escaped name literal; the name allowlist also excludes backslashes and
    -- quotes, so the built literal can never be broken or injected (matches the app's sql_literal).
    SELECT LISTAGG(
             'SELECT ' || q_name || ' AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM ( '
             || 'SELECT s.' || col || ' AS NEW_CODE FROM ' || fqn || ' s WHERE s.' || col
             || ' IS NOT NULL MINUS SELECT x.SRC_IDNTFTN_VAL FROM ' || :xlat
             || ' x WHERE x.SRC_IDNTFTN_NM = ' || q_name || ' ) g',
             ' UNION ALL ')
      INTO :scan_sql
    FROM (
        SELECT '''' || REPLACE(nm_clean, '''', '''''') || '''' AS q_name, fqn, col
        FROM (
            SELECT
                TRIM(REGEXP_REPLACE(TRIM(SPLIT_PART(entry, '|', 1)), '^[*][ ]*', '')) AS nm_clean,
                TRIM(SPLIT_PART(entry, '|', 2)) AS fqn,
                TRIM(SPLIT_PART(entry, '|', 3)) AS col
            FROM (
                SELECT TRIM(VALUE) AS entry
                FROM TABLE(SPLIT_TO_TABLE(REPLACE(REPLACE(:checks, ';', CHR(10)), CHR(13), ''), CHR(10)))
            )
            WHERE entry <> '' AND NOT STARTSWITH(entry, '#')
              AND ARRAY_SIZE(SPLIT(entry, '|')) = 3
        )
        WHERE nm_clean <> '' AND fqn <> '' AND col <> ''
          AND RLIKE(nm_clean, '^[-A-Za-z0-9_.:/ ]+$')
          AND RLIKE(fqn, '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')
          AND RLIKE(col, '^[A-Za-z0-9_$]+$')
    );

    IF (:scan_sql IS NULL OR TRIM(:scan_sql) = '') THEN
        RETURN 'ref-gap scan: no valid checks configured';
    END IF;

    ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS (CHECK_NAME, NEW_CODE) '
               || :scan_sql;
    EXECUTE IMMEDIATE :ins_sql;

    RETURN 'ref-gap scan complete';
END;
$$;
"""

out = f"""-- V129__pipe_ref_gap_alert.sql
--
-- The PIPE_REF_GAP daily alert (ETL Phase 1b). The v4.499.0 panel (Operations ▸ Pipeline ▸
-- "Reference-data gaps") shows staging codes missing from the XLAT translation table live on
-- page-open; this makes the same gap PAGE/EMAIL autonomously, because a source code with no XLAT
-- translation row hard-fails the nightly load.
--
-- Config-driven, same SETTINGS as the panel (ETL_REF_GAP_XLAT + ETL_REF_GAP_CHECKS). Adds:
--   * ETL_REF_GAP_RESULTS  - a small table holding the current scan's gaps.
--   * SP_SCAN_REF_GAPS()   - isolates the dynamic, cross-database MINUS scan: parses the config,
--                            allowlist-validates identifiers, and replaces ETL_REF_GAP_RESULTS.
--   * PIPE_REF_GAP rule    - ALERT_CONFIG row (PIPELINE, HIGH, threshold 1), WHEN NOT MATCHED.
--   * SP_ALERT_SCAN_DAILY re-derived from V111 with a 7th arm that CALLs SP_SCAN_REF_GAPS() and
--     raises one ALERT_EVENTS row per code type with a gap. The arm sits inside the SAME per-arm
--     EXCEPTION guard as every other rule, so a missing SELECT grant on the customer's staging/XLAT
--     tables logs to APP_ERROR_LOG and never breaks the other six alert rules. The ref-gap arm does
--     NOT count toward the OPS_SCAN_DEGRADED self-alert (optional external-dependency add-on), so the
--     6-core-rule tally is unchanged. Everything else in the proc is byte-identical (only the new arm).
--
-- PIPE_REF_GAP is HIGH, so the existing (opt-in) NATIVE_ALERT_NEW_EVENTS path emails it to the
-- OVERWATCH_EMAIL recipient (JDees@alfains.com) when email is enabled; it always reaches the in-app
-- Alerts feed. The app role needs SELECT on the staging + XLAT tables (granted separately) for the
-- scan to read them. Owner applies in Snowsight after V128; the next daily SP_ALERT_SCAN_DAILY
-- evaluates the new rule. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20129, 'V129 requires V128 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 128) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{RESULTS_DDL}
{RULE_MERGE}
{SCAN_PROC}
{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 129 AS VERSION,
       'PIPE_REF_GAP daily alert (ETL Phase 1b): ETL_REF_GAP_RESULTS table + SP_SCAN_REF_GAPS() (isolated config-driven cross-DB MINUS scan of ETL_REF_GAP_CHECKS vs ETL_REF_GAP_XLAT, identifier-allowlisted) + a PIPE_REF_GAP ALERT_CONFIG rule (PIPELINE/HIGH) + SP_ALERT_SCAN_DAILY re-derived from V111 with a 7th arm that CALLs the scan and raises one alert per code type missing from XLAT. Same per-arm EXCEPTION isolation, so a missing grant on the source tables never breaks the other rules. HIGH severity routes to the existing OVERWATCH_EMAIL path (JDees). App role needs SELECT on the staging + XLAT tables (granted separately). Everything else in the proc byte-identical; forward-healing on the next daily scan.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 129);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2  # SP_SCAN_REF_GAPS + SP_ALERT_SCAN_DAILY
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS" in out
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" in out
assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS" in out
assert "('PIPE_REF_GAP', 'PIPELINE'" in out
assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "\\" not in out, "generated SQL must carry no backslash escapes"
assert "EXCEPTION (-20129" in out and "IF (v < 128) THEN" in out
assert "SELECT 129 AS VERSION" in out and "WHERE VERSION = 129)" in out

target = Path(os.environ.get("V129_OUT") or (MIG / "V129__pipe_ref_gap_alert.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
