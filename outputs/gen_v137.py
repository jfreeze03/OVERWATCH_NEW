#!/usr/bin/env python3
"""Forward-generate V137: the DQ_RECON_ERROR daily alert (ETL Phase 3, DB-side twin).

The Operations ▸ Pipeline ▸ "Reconciliation errors" panel (v4.513.0) shows recent
RECON_MTRC_ERROR rows on page-open. This adds the DAILY ALERT so a fresh reconciliation
break PAGES/EMAILS autonomously — source and target layers not tying out means the numbers
can't be trusted downstream.

Config-driven, same SETTINGS as the panel (ETL_RECON_ERROR_FQN). The dynamic read of the
customer recon table is isolated in a dedicated SP_SCAN_RECON_ERRORS() proc that writes the
recent per-metric error counts to ETL_RECON_RESULTS; SP_ALERT_SCAN_DAILY gets an 8th rule arm
(after the V129 PIPE_REF_GAP arm) that CALLs it and raises one SUMMARY ALERT_EVENTS row. The arm
sits inside the SAME per-arm EXCEPTION guard as every other rule, so a missing SELECT grant on
RECON_MTRC_ERROR logs to APP_ERROR_LOG and NEVER breaks the other alert rules, and — like the
ref-gap arm — it does NOT increment :fails, so a grant gap never trips OPS_SCAN_DEGRADED.

The FQN is allowlist-validated (RLIKE) before it reaches the built SQL; the generated SQL carries
no backslash escapes. SP_ALERT_SCAN_DAILY is re-derived from V129 (its latest CREATE OR REPLACE,
which already carries the PIPE_REF_GAP arm) with ONLY the new arm inserted before the
OPS_SCAN_DEGRADED self-alert; everything else is byte-identical. DQ_RECON_ERROR is HIGH severity,
so the existing NATIVE_ALERT_NEW_EVENTS email path delivers it to JDees. Owner applies after V136.
This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V129__pipe_ref_gap_alert.sql"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ALERT_SCAN_DAILY\(")

# ---- the new [18] DQ_RECON_ERROR arm -------------------------------------------------
# Mirrors the ref-gap arm: CALLs the isolated scan proc, then raises ONE summary alert from
# ETL_RECON_RESULTS, deduped per day. Same EXCEPTION guard; does NOT bump :fails.
NEW_ARM = """\
    -- [18] DQ_RECON_ERROR  (optional external-dependency add-on: NOT counted toward the
    -- 6-core-rule scan-health tally, because its scan reads the customer RECON_MTRC_ERROR table
    -- SELECT-granted out-of-band -- a grant gap must not trip the OPS_SCAN_DEGRADED self-alert)
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS();
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        ),
        r AS (
            SELECT COUNT(*) AS METRICS,
                   COALESCE(SUM(N), 0) AS ERRORS,
                   LISTAGG(MTRC, ', ') WITHIN GROUP (ORDER BY N DESC) AS TOP_METRICS
            FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               r.ERRORS || ' reconciliation error(s) across ' || r.METRICS || ' metric(s)',
               'Source and target layers did not reconcile in the last '
                   || COALESCE(c.WINDOW_HOURS, 48) || 'h -- investigate before the numbers are '
                   || 'trusted downstream. Metric(s): ' || LEFT(r.TOP_METRICS, 1700),
               r.ERRORS,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN r ON c.RULE_ID = 'DQ_RECON_ERROR' AND r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            -- Deliberately does NOT increment :fails (like the ref-gap arm). The recon scan depends
            -- on a SELECT grant on the EXTERNAL RECON_MTRC_ERROR table applied out-of-band, so a grant
            -- gap (or any recon-specific error) is recorded in APP_ERROR_LOG but must not trip the
            -- OPS_SCAN_DEGRADED self-alert about the core internal rules. Self-heals when grants land.
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'recon_scan_failed', :emsg,
                   'rule DQ_RECON_ERROR - optional external add-on; needs SELECT on RECON_MTRC_ERROR', CURRENT_ROLE();
    END;
"""

# ---- transform: insert the new arm before the self-alert (after the ref-gap arm) -----
ANCHOR = "    END;\n    IF (fails > 0) THEN"
assert proc.count(ANCHOR) == 1, f"self-alert anchor: got {proc.count(ANCHOR)}"
proc = proc.replace(ANCHOR, "    END;\n" + NEW_ARM + "    IF (fails > 0) THEN")

# post-conditions on the transformed proc
assert "-- [18] DQ_RECON_ERROR" in proc
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS()" in proc
assert "-- [17] PIPE_REF_GAP" in proc  # the V129 arm is preserved
assert proc.count("EXCEPTION\n        WHEN OTHER THEN") == 8  # 6 core + ref-gap + recon
assert proc.count("fails := fails + 1") == 6  # only the 6 core arms bump fails
assert "/6 rule blocks ok (daily)" in proc  # core tally unchanged
assert "COST_CONTRACT_BREACH" in proc and "PIPE_TASK_FAILURES" in proc  # nothing dropped

# ---- the isolated config-driven scan proc + its results table + the rule seed ---------
RESULTS_DDL = """\
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS (
    MTRC        VARCHAR(500)  NOT NULL,
    N           NUMBER        NOT NULL,
    LATEST_LOAD TIMESTAMP_NTZ,
    SCANNED_AT  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);
"""

RULE_MERGE = """\
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('DQ_RECON_ERROR', 'PIPELINE', 'Reconciliation errors: source vs target layer mismatches (numbers do not tie out)', TRUE, 'HIGH', 1, 48)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);
"""

# SP_SCAN_RECON_ERRORS: read ETL_RECON_ERROR_FQN from SETTINGS, allowlist-validate it, and replace
# ETL_RECON_RESULTS with per-metric counts of RECON_MTRC_ERROR rows loaded in the last 2 days. The
# window is a fixed 2 days (a missed scan day is still caught). No backslashes: the '' escapes the
# quote around 'day' inside the built string.
SCAN_PROC = """\
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Config-driven reconciliation-error scan (ETL Phase 3). Reads ETL_RECON_ERROR_FQN from SETTINGS
-- and replaces ETL_RECON_RESULTS with a per-metric count of RECON_MTRC_ERROR rows loaded within the
-- DQ_RECON_ERROR rule's WINDOW_HOURS (default 48h -- recent source-vs-target mismatches; an operator
-- can widen it toward the 30-day panel view by editing ALERT_CONFIG). The DQ_RECON_ERROR arm of
-- SP_ALERT_SCAN_DAILY reads that table and raises one summary alert. The FQN is allowlist-validated
-- so the built SQL is always well-formed; the caller wraps CALL in its own EXCEPTION guard, so a
-- missing grant is contained. A NULL metric key is kept (labelled) so one anomalous row can never
-- void the whole scan.
DECLARE
    recon_fqn STRING;
    enabled_cnt INT;
    window_hours INT;
    ins_sql STRING;
BEGIN
    -- gate: only scan when DQ_RECON_ERROR exists AND is enabled; read its tunable window.
    SELECT COUNT(*), MAX(WINDOW_HOURS) INTO :enabled_cnt, :window_hours
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'DQ_RECON_ERROR' AND ENABLED;

    -- always clear last run's rows first, so stale errors never linger after a fix or a disable.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS;
    IF (:enabled_cnt = 0) THEN
        RETURN 'recon scan skipped (rule disabled)';
    END IF;
    window_hours := COALESCE(:window_hours, 48);

    SELECT MAX(IFF(KEY = 'ETL_RECON_ERROR_FQN', VALUE, NULL)) INTO :recon_fqn
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    IF (:recon_fqn IS NULL OR TRIM(:recon_fqn) = ''
        OR NOT RLIKE(TRIM(:recon_fqn), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')) THEN
        RETURN 'recon scan skipped (unconfigured or invalid FQN)';
    END IF;

    -- per-metric recent error counts. The FQN is validated above (a bare, well-formed identifier),
    -- so it is safe to concatenate; window_hours is a NUMBER from ALERT_CONFIG. A NULL MTRC groups
    -- under '(unknown metric)' (never dropped, never a NOT NULL violation on ETL_RECON_RESULTS.MTRC).
    -- LOAD_DTTM is the recon's per-row load timestamp.
    ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS (MTRC, N, LATEST_LOAD) '
               || 'SELECT COALESCE(TO_VARCHAR(MTRC), ''(unknown metric)''), COUNT(*), MAX(LOAD_DTTM) FROM '
               || :recon_fqn
               || ' WHERE LOAD_DTTM >= DATEADD(''hour'', -' || :window_hours || ', CURRENT_TIMESTAMP()) '
               || 'GROUP BY MTRC';
    EXECUTE IMMEDIATE :ins_sql;

    RETURN 'recon scan complete';
END;
$$;
"""

out = f"""-- V137__dq_recon_error_alert.sql
--
-- The DQ_RECON_ERROR daily alert (ETL Phase 3, DB-side twin of the panel). The v4.513.0 panel
-- (Operations ▸ Pipeline ▸ "Reconciliation errors") shows recent RECON_MTRC_ERROR rows on
-- page-open; this makes a fresh reconciliation break PAGE/EMAIL autonomously, because a source-vs-
-- target layer mismatch means the numbers don't tie out and can't be trusted downstream.
--
-- Config-driven, same SETTINGS as the panel (ETL_RECON_ERROR_FQN). Adds:
--   * ETL_RECON_RESULTS      - a small table holding the recent per-metric error counts.
--   * SP_SCAN_RECON_ERRORS() - isolates the dynamic read of the configured RECON_MTRC_ERROR table:
--                              allowlist-validates the FQN and replaces ETL_RECON_RESULTS with the
--                              per-metric count of errors loaded within the rule's WINDOW_HOURS
--                              (default 48h); a NULL metric key is kept as '(unknown metric)'.
--   * DQ_RECON_ERROR rule    - ALERT_CONFIG row (PIPELINE, HIGH, threshold 1, 48h), WHEN NOT MATCHED.
--   * SP_ALERT_SCAN_DAILY re-derived from V129 with an 8th arm that CALLs SP_SCAN_RECON_ERRORS()
--     and raises ONE summary ALERT_EVENTS row (N errors across M metrics), deduped per day. The arm
--     sits inside the SAME per-arm EXCEPTION guard as every other rule, so a missing SELECT grant on
--     RECON_MTRC_ERROR logs to APP_ERROR_LOG and never breaks the other rules. Like the ref-gap arm,
--     it does NOT count toward the OPS_SCAN_DEGRADED self-alert (optional external-dependency add-on),
--     so the 6-core-rule tally is unchanged. Everything else in the proc is byte-identical.
--
-- DQ_RECON_ERROR is HIGH, so the existing (opt-in) NATIVE_ALERT_NEW_EVENTS path emails it to the
-- OVERWATCH_EMAIL recipient (JDees@alfains.com) when email is enabled; it always reaches the in-app
-- Alerts feed. The app role needs SELECT on RECON_MTRC_ERROR (granted separately, DB_T_PROD_CORE
-- schema) for the scan to read it. Owner applies in Snowsight after V136; the next daily
-- SP_ALERT_SCAN_DAILY evaluates the new rule. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20137, 'V137 requires V136 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 136) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{RESULTS_DDL}
{RULE_MERGE}
{SCAN_PROC}
{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 137 AS VERSION,
       'DQ_RECON_ERROR daily alert (ETL Phase 3, DB-side twin of the Reconciliation-errors panel): ETL_RECON_RESULTS table + SP_SCAN_RECON_ERRORS() (isolated config-driven read of the ETL_RECON_ERROR_FQN table, FQN-allowlisted, per-metric counts of errors loaded within the rule WINDOW_HOURS window default 48h, NULL metric kept as unknown) + a DQ_RECON_ERROR ALERT_CONFIG rule (PIPELINE/HIGH) + SP_ALERT_SCAN_DAILY re-derived from V129 with an 8th arm that CALLs the scan and raises one summary alert (N errors across M metrics), deduped per day. Same per-arm EXCEPTION isolation, and NOT counted toward OPS_SCAN_DEGRADED (external-dependency add-on), so a missing grant never breaks the other rules. HIGH severity routes to the existing OVERWATCH_EMAIL path (JDees). App role needs SELECT on RECON_MTRC_ERROR (granted separately, DB_T_PROD_CORE). Everything else in the proc byte-identical; forward-healing on the next daily scan.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 137);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2  # SP_SCAN_RECON_ERRORS + SP_ALERT_SCAN_DAILY
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS" in out
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" in out
assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS" in out
assert "('DQ_RECON_ERROR', 'PIPELINE'" in out
assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "\\" not in out, "generated SQL must carry no backslash escapes"
assert "EXCEPTION (-20137" in out and "IF (v < 136) THEN" in out
assert "SELECT 137 AS VERSION" in out and "WHERE VERSION = 137)" in out

target = Path(os.environ.get("V137_OUT") or (MIG / "V137__dq_recon_error_alert.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
