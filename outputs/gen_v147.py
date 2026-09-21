"""Generate V147 (stamp USER_NAME/DATABASE_NAME/SCHEMA_NAME onto the operator-stats fact).

QOIE Slice 2's operator profile (Operations ▸ Queries ▸ Operator profile) reads
FACT_QUERY_OPERATOR_STATS_DAILY, which V143/V144 stamped with only COMPANY + WAREHOUSE_NAME.
So the panel could honor the company/warehouse/window scope but NOT the User/Database/Schema
filters — it silently showed data broader than the active scope. This adds the identity grain:

  * ALTER the fact to ADD COLUMN IF NOT EXISTS USER_NAME / DATABASE_NAME / SCHEMA_NAME
    (idempotent; matches the QUERY_HISTORY names the query-level _query_scope filters on).
  * Re-derive SP_LOAD_QUERY_OPERATOR_STATS from V144 (the current live proc) so the set-based
    enrich UPDATE also fills those three from the QUERY_HISTORY row it ALREADY joins — the ONLY
    change to the proc (byte-identical otherwise; test_v147 proves it).
  * One-time backfill of the existing rows (they have QUERY_DAY set, so the proc's own
    QUERY_DAY-IS-NULL enrich never touches them, and the NOT-EXISTS candidate gate blocks
    re-collection). Bounded to -35d (the 30-day retention + 2-day collection lag), idempotent
    (USER_NAME-IS-NULL guard), inside QUERY_HISTORY's 365-day retention.

The app reader (operator_stats_summary / operator_problem_board) already accepts the identity
filters and references the columns ONLY when non-empty; operations.py gates on 147 being in the
applied SCHEMA_VERSION set, so it passes them empty (no column reference) until this is applied
and self-heals the moment it is. Apply AFTER V146. Idempotent; safe to re-run.

Run: python outputs/gen_v147.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V144 = (MIG / "V144__operator_time_pct_scale.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


proc = extract_proc(V144, "SP_LOAD_QUERY_OPERATOR_STATS(DAYS_BACK FLOAT)")

# --- enrich UPDATE: also stamp the identity grain from the QUERY_HISTORY row it already joins.
_old = """        COMPANY = DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qh.WAREHOUSE_NAME),
        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3)
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh"""

_new = """        COMPANY = DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qh.WAREHOUSE_NAME),
        QUERY_ELAPSED_SEC = ROUND(qh.TOTAL_ELAPSED_TIME / 1000.0, 3),
        -- V147: identity grain so the Operator profile honors the User/Database/Schema scope
        -- filters (same QUERY_HISTORY columns the query-level _query_scope filters on).
        USER_NAME = qh.USER_NAME,
        DATABASE_NAME = qh.DATABASE_NAME,
        SCHEMA_NAME = qh.SCHEMA_NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh"""

assert proc.count(_old) == 1, "enrich UPDATE anchor not unique/found"
proc = proc.replace(_old, _new)

# verify the change landed and nothing else moved
assert proc.count("USER_NAME = qh.USER_NAME") == 1
assert proc.count("DATABASE_NAME = qh.DATABASE_NAME") == 1
assert proc.count("SCHEMA_NAME = qh.SCHEMA_NAME") == 1
# the V144 scale fix is preserved (not reverted)
assert "EXECUTION_TIME_BREAKDOWN:overall_percentage::FLOAT * 100, 2)" in proc

HEADER = """\
-- V147__operator_stats_identity_grain.sql
--
-- Stamp USER_NAME / DATABASE_NAME / SCHEMA_NAME onto the QOIE Slice 2 operator-stats fact so
-- the Operator profile (Operations ▸ Queries) can honor the User/Database/Schema scope filters,
-- not just company/warehouse/window. V143/V144 stamped only COMPANY + WAREHOUSE_NAME, so the
-- panel silently showed data broader than the active scope when one of those filters was set.
--
--   * ALTER FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS the three identity columns
--     (idempotent; named to match ACCOUNT_USAGE.QUERY_HISTORY so the app reuses _query_scope's
--     USER_NAME / DATABASE_NAME / SCHEMA_NAME predicates).
--   * Re-derive SP_LOAD_QUERY_OPERATOR_STATS from V144 (the current live proc): the set-based
--     enrich UPDATE, which already joins the query's QUERY_HISTORY row, now also fills the three.
--     That is the ONLY change to the proc (byte-identical otherwise; test_v147 proves it).
--   * One-time backfill of existing rows: they carry QUERY_DAY, so the proc's QUERY_DAY-IS-NULL
--     enrich never revisits them and the NOT-EXISTS candidate gate blocks re-collection — the
--     USER_NAME-IS-NULL backfill is the only fill path. Bounded to -35d (30-day retention +
--     2-day collection lag), inside QUERY_HISTORY's 365-day retention; idempotent.
--
-- App-side (already shipped): operator_stats_summary / operator_problem_board reference the
-- three columns ONLY when the arg is non-empty, and operations.py passes them empty until 147
-- is in the applied SCHEMA_VERSION set — so nothing references the columns before this applies,
-- and the grain self-heals the moment it does (no redeploy). Apply AFTER V146. Safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20147, 'V147 requires V146 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 146) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Identity grain (idempotent). Names mirror ACCOUNT_USAGE.QUERY_HISTORY so the app reuses the
-- same USER_NAME / DATABASE_NAME / SCHEMA_NAME predicates the query-level sections apply.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS USER_NAME VARCHAR(256);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS DATABASE_NAME VARCHAR(256);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY ADD COLUMN IF NOT EXISTS SCHEMA_NAME VARCHAR(256);

-- >>> derived:SP_LOAD_QUERY_OPERATOR_STATS (from V144; enrich UPDATE now also stamps USER_NAME/DATABASE_NAME/SCHEMA_NAME, V147)
"""

BACKFILL = """
-- One-time backfill of the already-collected rows (QUERY_DAY set => the proc's enrich skips
-- them; NOT-EXISTS gate blocks re-collection). Fills only NULLs, so it is idempotent and a
-- re-run is a no-op. -35d covers the 30-day retention + 2-day collection lag, well inside
-- QUERY_HISTORY's 365-day retention.
UPDATE DBA_MAINT_DB.OVERWATCH.FACT_QUERY_OPERATOR_STATS_DAILY f
SET USER_NAME = qh.USER_NAME,
    DATABASE_NAME = qh.DATABASE_NAME,
    SCHEMA_NAME = qh.SCHEMA_NAME
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY qh
WHERE f.QUERY_ID = qh.QUERY_ID
  AND f.USER_NAME IS NULL
  AND qh.START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP());
"""

SCHEMA_INSERT = """
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 147 AS VERSION,
       'Operator-stats identity grain: ADD COLUMN USER_NAME/DATABASE_NAME/SCHEMA_NAME to FACT_QUERY_OPERATOR_STATS_DAILY (V143/V144 stamped only COMPANY + WAREHOUSE_NAME) so the QOIE Slice 2 Operator profile honors the User/Database/Schema scope filters, not just company/warehouse/window. Re-derive SP_LOAD_QUERY_OPERATOR_STATS from V144 to also fill the three in the set-based enrich UPDATE that already joins the query QUERY_HISTORY row (byte-identical to V144 otherwise; the V144 overall_percentage*100 scale fix preserved). One-time -35d idempotent backfill of existing rows (USER_NAME-IS-NULL guard; QUERY_DAY-set rows are skipped by the proc enrich). Column names mirror ACCOUNT_USAGE.QUERY_HISTORY so the app reuses _query_scope predicates; the reader references them only when the filter is set and operations.py gates on 147 in the applied set, so nothing references the columns until this applies and the grain self-heals with no redeploy.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 147);
"""

out = HEADER + proc + "\n" + BACKFILL + SCHEMA_INSERT
(MIG / "V147__operator_stats_identity_grain.sql").write_text(out, encoding="utf-8")
print("wrote V147 | enrich-grain SETs:",
      proc.count("USER_NAME = qh.USER_NAME") + proc.count("DATABASE_NAME = qh.DATABASE_NAME")
      + proc.count("SCHEMA_NAME = qh.SCHEMA_NAME"),
      "| ALTER ADD COLUMN:", out.count("ADD COLUMN IF NOT EXISTS"))
