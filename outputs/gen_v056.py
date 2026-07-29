#!/usr/bin/env python3
"""Forward-generate V056__loader_reconcile_alert_fixes.sql — audit Batch B.

Six confirmed correctness bugs in stored procs (docs/reviews/BUG_AUDIT_2026-07-28.md),
re-derived from the latest definition of each proc under the derivation law.

#6  SP_LOAD_QH_EXTRACT (from V055): the FACT_QUERY_DAILY MERGE re-aggregated the
    oldest calendar day in a rolling 72h window and overwrote its complete row
    with a shrinking PARTIAL as the window slid off it. Day-align the window
    (-2 CURRENT_DATE) so only WHOLE days inside the 72h extract are written; an
    aging day freezes COMPLETE. (Same fix shape as the V055 CS mart.)

#7  SP_NIGHTLY_RECONCILE (from V045) + SP_LOAD_MARTS_V27 (from V045): the reconcile
    deleted the query/extract-sourced day marts for DAY >= -3d (midnight) then
    rebuilt them from OW_QH_EXTRACT (a 72h window) via SP_LOAD_MARTS_V27('HOURLY',
    3) — so day D-3 rebuilt PARTIAL (its pre-72h hours are gone from the extract).
    Fix: cap ONLY the four EXTRACT-fed day arms of SP_LOAD_MARTS_V27 at
    LEAST(:d, 2) (the extract can supply at most the last 2 whole days complete),
    and delete those marts in the reconcile at -2d. The reconcile keeps the d=3
    call, so the FULL-RETENTION marts it also rebuilds (MART_WAREHOUSE_EFFICIENCY_
    DAILY, MART_TASK_GRAPH_DAILY — sourced from METERING/QUERY_HISTORY/TASK_HISTORY/
    QUERY_ATTRIBUTION, not the 72h extract) still get their D-3 rebuilt complete
    (MART_TASK_GRAPH_DAILY genuinely needs it — QUERY_ATTRIBUTION credits land ~a
    day late), as do the hour-grain marts (hour-keyed, so no partial-overwrite) and
    the primary metering facts. The normal hourly call is d=2, so LEAST(:d,2)
    leaves it unchanged; only the reconcile's d=3 is affected.

#14 SP_LOAD_OPS_DIAG (from V042): DELETE keyed on the hour-truncated HOUR_TS but
    bounded by a non-hour-aligned CURRENT_TIMESTAMP, while the INSERT re-added a
    partial bucket for the boundary hour -> a complete + a partial row for the
    same hour that the panels double-count. Hour-align both bounds.

#4/#12/#13 SP_ALERT_SCAN (from V045):
    #4  PIPE_TASK_FAILURES dedupe key omitted SCHEMA_NAME (same-named tasks in
        different schemas collided and got suppressed) -> add SCHEMA_NAME.
    #12 SEC_CRED_EXPIRY bucketed the dedupe key by week and ignored state, so a
        same-week EXPIRED (CRITICAL escalation) collided with the earlier
        EXPIRING event and was dropped -> add an EXPIRED/EXPIRING discriminator.
    #13 COST_CLOUD_SVC_RATIO classified company as IFF(name LIKE 'WH_TRXS%',
        'Trexis','ALFA'), billing UNKNOWN warehouses to ALFA -> use
        COMPANY_FOR_WAREHOUSE (V044 evidence-based).

tests/test_v056_batch_b.py re-derives and byte-compares.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    matches = pat.findall(text)
    assert matches, (path, name)
    return matches[-1]


def apply(body: str, edits: list[tuple[str, str]], name: str) -> str:
    for old, new in edits:
        n = body.count(old)
        assert n == 1, f"{name}: needle x{n}: {old[:80]!r}"
        body = body.replace(old, new)
    return body


def apply_n(body: str, old: str, new: str, want: int, name: str) -> str:
    n = body.count(old)
    assert n == want, f"{name}: needle x{n} (want {want}): {old[:60]!r}"
    return body.replace(old, new)


# --- #6: FACT_QUERY_DAILY day-aligned window --------------------------------
extract = apply(extract_proc("V055__cloud_services_breakdown.sql", "SP_LOAD_QH_EXTRACT"), [
    ("            WHERE START_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())\n            GROUP BY 1, 2, 3, 4",
     "            -- Day-aligned (audit #6): only WHOLE days inside the 72h extract,\n"
     "            -- so an aging day freezes COMPLETE, not at its last partial hour.\n"
     "            WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())\n            GROUP BY 1, 2, 3, 4"),
], "SP_LOAD_QH_EXTRACT")


# --- #7: SP_LOAD_MARTS_V27 — cap ONLY the 4 extract-fed DAY arms at LEAST(:d, 2).
# Each window string is identical; the unique preceding aggregate column anchors it.
marts_windows = [
    "                       COUNT_IF(COALESCE(QUERY_TAG, '') != '') AS TAGGED_RUNS\n"          # MART_QUERY_FAMILY_DAILY
    "                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n"
    "                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())",
    "                                         COALESCE(EXECUTION_TIME, 0), 0)) / 1000, 1) AS UNTAGGED_EXEC_SEC\n"  # MART_TAG_COVERAGE_DAILY
    "                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n"
    "                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())",
    "                       SUM(CREDITS_USED) AS HOUR_CREDITS\n"                                # _OW_ALLOC_BASE (metering arm)
    "                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY\n"
    "                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())",
    "                       SUM(COALESCE(EXECUTION_TIME, 0)) AS EXEC_MS\n"                      # _OW_ALLOC_BASE (extract arm)
    "                FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n"
    "                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())",
]
marts = apply(extract_proc("V045__task_monitoring_restored.sql", "SP_LOAD_MARTS_V27"),
              [(w, w.replace("-:d, CURRENT_DATE()", "-LEAST(:d, 2), CURRENT_DATE()")) for w in marts_windows],
              "SP_LOAD_MARTS_V27")


# --- #6 (+#7): reconcile — delete the extract-fed day marts only as far as the
# extract can rebuild complete (-2d). Full-retention / hour-grain marts and the
# primary metering facts stay -3d and are still rebuilt complete by the d=3 call.
def _recon_delete(table: str) -> tuple[str, str]:
    old = (f"    DELETE FROM DBA_MAINT_DB.OVERWATCH.{table}\n"
           f"     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());")
    new = (f"    DELETE FROM DBA_MAINT_DB.OVERWATCH.{table}\n"
           f"     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());")
    return old, new


recon_edits = [
    _recon_delete("MART_QUERY_FAMILY_DAILY"),
    _recon_delete("FACT_QUERY_DAILY"),          # rebuilt day-aligned by SP_LOAD_QH_EXTRACT (#6)
    _recon_delete("MART_TAG_COVERAGE_DAILY"),
    _recon_delete("MART_COST_ALLOCATION_DAILY"),
    _recon_delete("FACT_COST_ALLOC_XDIM_DAILY"),
    ("    RETURN 'nightly reconcile complete (trailing 3 days rebuilt)';",
     "    RETURN 'nightly reconcile complete (metering + full-retention marts 3 days, extract-fed marts 2)';"),
]
recon = apply(extract_proc("V045__task_monitoring_restored.sql", "SP_NIGHTLY_RECONCILE"),
              recon_edits, "SP_NIGHTLY_RECONCILE")


# --- #14: ops-diag hour-align both bounds -------------------------------------
opsdiag = apply_n(extract_proc("V042__codex_r22.sql", "SP_LOAD_OPS_DIAG"),
                  "DATEADD('day', -:d, CURRENT_TIMESTAMP())",
                  "DATE_TRUNC('hour', DATEADD('day', -:d, CURRENT_TIMESTAMP()))",
                  3, "SP_LOAD_OPS_DIAG")


# --- #4/#12/#13: alert scan ---------------------------------------------------
alert = apply(extract_proc("V045__task_monitoring_restored.sql", "SP_ALERT_SCAN"), [
    # #4 PIPE_TASK_FAILURES dedupe key: add SCHEMA_NAME
    ("c.RULE_ID || '|' || COALESCE(tk.DATABASE_NAME, '') || '.' || tk.TASK_NAME || '|' || tk.DAY",
     "c.RULE_ID || '|' || COALESCE(tk.DATABASE_NAME, '') || '.' || COALESCE(tk.SCHEMA_NAME, '') || '.' || tk.TASK_NAME || '|' || tk.DAY"),
    # #12 SEC_CRED_EXPIRY dedupe key: add EXPIRED/EXPIRING state so the CRITICAL
    # escalation does not collide with the earlier same-week EXPIRING warning
    ("c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || DATE_TRUNC('week', CURRENT_DATE())",
     "c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
     "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING') || '|' || DATE_TRUNC('week', CURRENT_DATE())"),
    # #13 COST_CLOUD_SVC_RATIO: evidence-based company (UNKNOWN warehouses no longer bill ALFA)
    ("IFF(w.WAREHOUSE_NAME LIKE 'WH_TRXS%', 'Trexis', 'ALFA')",
     "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(w.WAREHOUSE_NAME)"),
], "SP_ALERT_SCAN")


out = f"""-- V056__loader_reconcile_alert_fixes.sql — audit Batch B (correctness).
--
--   Six confirmed bugs in stored procs (docs/reviews/BUG_AUDIT_2026-07-28.md),
--   all proc re-derivations, no schema change, no new object:
--     #6  FACT_QUERY_DAILY partial-freeze -> day-aligned window.
--     #7  nightly reconcile rebuilt query/extract marts' D-3 partial from the
--         72h extract -> cap the 4 extract-fed day arms of SP_LOAD_MARTS_V27 at
--         LEAST(:d, 2) and delete those marts in the reconcile at -2d. Full-
--         retention marts (efficiency/task-graph), hour-grain marts, and the
--         primary metering facts keep the d=3 / -3d reconcile.
--     #14 ops-diag hour-boundary double-count -> hour-align the DELETE/INSERT.
--     #4  PIPE_TASK_FAILURES dedupe key gains SCHEMA_NAME.
--     #12 SEC_CRED_EXPIRY dedupe key gains an EXPIRED/EXPIRING discriminator.
--     #13 COST_CLOUD_SVC_RATIO company via COMPANY_FOR_WAREHOUSE (not a name
--         pattern that billed UNKNOWN warehouses to ALFA).
--
--   Apply AFTER V055. Idempotent; safe to re-run. Four CREATE OR REPLACEs.
--
-- Derivation law: each proc re-derived from its CURRENT definition
-- (SP_LOAD_QH_EXTRACT V055, SP_NIGHTLY_RECONCILE/SP_ALERT_SCAN V045,
-- SP_LOAD_OPS_DIAG V042) + the enumerated edits; the test byte-compares.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20056, 'V056 requires V055 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 55) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_QH_EXTRACT
{extract}
-- >>> derived:SP_LOAD_MARTS_V27
{marts}
-- >>> derived:SP_NIGHTLY_RECONCILE
{recon}
-- >>> derived:SP_LOAD_OPS_DIAG
{opsdiag}
-- >>> derived:SP_ALERT_SCAN
{alert}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 56 AS VERSION,
       'Batch B loader/reconcile/alert correctness fixes: FACT_QUERY_DAILY + nightly-reconcile day partial-freeze, ops-diag hour double-count, PIPE_TASK/SEC_CRED dedupe keys, cloud-svc-ratio company classification' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 56);
"""
target = Path(os.environ.get("V056_OUT") or (MIG / "V056__loader_reconcile_alert_fixes.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
