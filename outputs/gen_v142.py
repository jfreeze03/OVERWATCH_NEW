"""Generate V142 (A4: collapse the security-posture arm's CREDENTIALS + GRANTS_TO_USERS double-scans).

SP_LOAD_MARTS_V27's [7] security-posture arm scans SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS TWICE
(EXPIRING_CRED_10D + EXPIRED_CRED) and GRANTS_TO_USERS TWICE (GRANT_CHANGES_24H +
BREAKGLASS_GRANTS_30D) as separate UNION members. This collapses each source to ONE scan with
COUNT_IF conditional aggregation, then UNPIVOTs back to the same (DAY, METRIC, COMPANY, VALUE) rows
the MERGE consumes — provably output-equivalent (COUNT_IF(cond) == COUNT(*) WHERE cond; the GRANTS
outer WHERE is a superset of the rows either metric needs, so no needed row is dropped). Everything
else in the proc is byte-identical to V127.

NOT included: the SP_ANOMALY_SWEEP TABLE_DML_HISTORY "double-scan" — those two scans use different
windows (8d vs 28d), filters, and grains, and live in separately exception-isolated arms on purpose;
collapsing them needs shared temp-staging that couples their failure modes (the A3 pattern, deferred).

Run: python outputs/gen_v142.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V127 = (MIG / "V127__wh_eff_idle_credits_actual_hours.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


proc = extract_proc(V127, "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)")

# --- CREDENTIALS: two scans -> one scan + UNPIVOT ---------------------------
_cred_old = """                SELECT CURRENT_DATE() AS DAY, 'EXPIRING_CRED_10D' AS METRIC, 'ALL' AS COMPANY,
                       COUNT(*)::NUMBER(18,2) AS VALUE
                FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                WHERE EXPIRATION_DATE IS NOT NULL
                  AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())
                UNION ALL
                SELECT CURRENT_DATE(), 'EXPIRED_CRED', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                WHERE EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()"""
_cred_new = """                -- A4: CREDENTIALS scanned ONCE; both metrics via COUNT_IF + UNPIVOT (was two scans).
                SELECT CURRENT_DATE() AS DAY, cu.METRIC AS METRIC, 'ALL' AS COMPANY, cu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(EXPIRATION_DATE IS NOT NULL
                                    AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())) AS "EXPIRING_CRED_10D",
                           COUNT_IF(EXPIRATION_DATE IS NOT NULL AND EXPIRATION_DATE < CURRENT_TIMESTAMP()) AS "EXPIRED_CRED"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS
                ) c
                UNPIVOT (VALUE FOR METRIC IN ("EXPIRING_CRED_10D", "EXPIRED_CRED")) cu"""
assert proc.count(_cred_old) == 1, "cred anchor not unique/found"
proc = proc.replace(_cred_old, _cred_new)

# --- GRANTS_TO_USERS: the GRANT_CHANGES_24H member becomes a one-scan block ---
# emitting BOTH grant metrics; the standalone BREAKGLASS member is then removed.
_grant_changes_old = """                UNION ALL
                SELECT CURRENT_DATE(), 'GRANT_CHANGES_24H', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                   OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())"""
_grant_changes_new = """                UNION ALL
                -- A4: GRANTS_TO_USERS scanned ONCE; both grant metrics via COUNT_IF + UNPIVOT (was two
                -- scans). The outer WHERE is a superset of the rows either metric needs (created >= -30d
                -- covers the -24h change window; deleted >= -24h keeps revoked-in-24h rows), and each
                -- COUNT_IF re-applies its exact original predicate, so both counts are unchanged.
                SELECT CURRENT_DATE() AS DAY, gu.METRIC AS METRIC, 'ALL' AS COMPANY, gu.VALUE::NUMBER(18,2) AS VALUE
                FROM (
                    SELECT COUNT_IF(CREATED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                                    OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())) AS "GRANT_CHANGES_24H",
                           COUNT_IF(DELETED_ON IS NULL
                                    AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                                    AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())) AS "BREAKGLASS_GRANTS_30D"
                    FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                    WHERE CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
                       OR DELETED_ON >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
                ) g
                UNPIVOT (VALUE FOR METRIC IN ("GRANT_CHANGES_24H", "BREAKGLASS_GRANTS_30D")) gu"""
assert proc.count(_grant_changes_old) == 1, "grant-changes anchor not unique/found"
proc = proc.replace(_grant_changes_old, _grant_changes_new)

_breakglass_old = """
                UNION ALL
                SELECT CURRENT_DATE(), 'BREAKGLASS_GRANTS_30D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                  AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())"""
assert proc.count(_breakglass_old) == 1, "breakglass anchor not unique/found"
proc = proc.replace(_breakglass_old, "")   # its metric now comes from the grants block above

# --- verify the collapse: each source scanned once now; all 4 metrics kept ---
assert proc.count("FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS") == 1
assert proc.count("FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS") == 1
for m in ('"EXPIRING_CRED_10D"', '"EXPIRED_CRED"', '"GRANT_CHANGES_24H"', '"BREAKGLASS_GRANTS_30D"'):
    assert m in proc, m

HEADER = """\
-- V142__posture_arm_single_scan.sql
--
-- A4 (owner-approved, half 2 of the A1+A4 bundle): the SP_LOAD_MARTS_V27 [7] security-posture arm
-- scanned SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS twice (EXPIRING_CRED_10D + EXPIRED_CRED) and
-- GRANTS_TO_USERS twice (GRANT_CHANGES_24H + BREAKGLASS_GRANTS_30D) as separate UNION members.
-- This re-derives the proc so each source is scanned ONCE with COUNT_IF conditional aggregation and
-- UNPIVOTed back to the identical (DAY, METRIC, COMPANY, VALUE) rows the MERGE consumes.
--
-- Output-equivalent by construction: COUNT_IF(cond) == COUNT(*) WHERE cond, and the GRANTS one-scan
-- WHERE (created >= -30d OR deleted >= -24h) is a SUPERSET of the rows either metric counts, with each
-- COUNT_IF re-applying its exact original predicate. The MART_SECURITY_POSTURE_DAILY MERGE still lands
-- one row per (DAY, METRIC, COMPANY). Everything else in the proc is byte-identical to V127 (test_v142
-- proves it). Run the equivalence check staged with the apply (old vs new counts) before trusting it.
--
-- NOT included: the SP_ANOMALY_SWEEP TABLE_DML_HISTORY double-scan -- its two scans use different
-- windows/filters/grains and are separately exception-isolated on purpose, so collapsing them needs
-- shared temp-staging that couples their failure modes (the A3 pattern, deferred). Left as-is.
--
-- Proc-only; no schema/rule/task change. Apply AFTER V141. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20142, 'V142 requires V141 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 141) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27 (from V127; posture arm CREDENTIALS + GRANTS single-scanned, V142)
"""

SCHEMA_INSERT = """\

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 142 AS VERSION,
       'A4 posture-arm single-scan: SP_LOAD_MARTS_V27 [7] security-posture arm now scans ACCOUNT_USAGE.CREDENTIALS once (EXPIRING_CRED_10D + EXPIRED_CRED via COUNT_IF + UNPIVOT) and GRANTS_TO_USERS once (GRANT_CHANGES_24H + BREAKGLASS_GRANTS_30D, one-scan superset WHERE + COUNT_IF), instead of two scans each. Output-equivalent (COUNT_IF(cond)==COUNT(*) WHERE cond; the grants WHERE is a superset so no counted row is dropped); the posture MERGE still lands one row per (DAY, METRIC, COMPANY). Re-derived from V127, byte-identical outside the posture arm. Did NOT touch the SP_ANOMALY_SWEEP TABLE_DML_HISTORY scans (different windows/grains + deliberate per-arm isolation; the A3 temp-staging pattern). Proc-only, no schema/rule/task change.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 142);
"""

out = HEADER + proc + "\n" + SCHEMA_INSERT
(MIG / "V142__posture_arm_single_scan.sql").write_text(out, encoding="utf-8")
print("wrote V142 | CREDENTIALS scans:", proc.count("FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS"),
      "| GRANTS scans:", proc.count("FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS"))
