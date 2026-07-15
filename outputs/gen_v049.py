#!/usr/bin/env python3
"""Forward-generate V049__write_target_attribution.sql (r28+ queue item).

Owner 2026-07-15: "let's do both" — deploy corrected V048 and build V049.

V048 split measured query compute across ACCESS_HISTORY.BASE_OBJECTS_ACCESSED
(reads only). Write-only ETL — COPY INTO, INSERT ... VALUES, CTAS from
constants — reads no base table, so its credits landed in
QUERY_COMPUTE_RESIDUAL instead of on the tables it builds. V049 folds
ACCESS_HISTORY.OBJECTS_MODIFIED (write targets) into the same equal split:
loads attribute to their targets, and the residual shrinks to genuinely
unattributable compute (no read, no write).

Derivation law: SP_LOAD_OBJECT_COST from V048 verbatim + two enumerated edits
(the dedup CTE and the obj_q CTE — the split and the residual must agree on
what "attributed" means, or credits double-count or vanish);
tests/test_v049_write_targets.py re-derives and byte-compares.
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


proc = apply(extract_proc("V048__object_cost_ledger.sql", "SP_LOAD_OBJECT_COST"), [
    # Edit 1 — the split: write targets join the per-query object set.
    ("""    dedup AS (
        SELECT DISTINCT ah.QUERY_ID,
               f.value:"objectName"::STRING AS OBJECT_FQN,
               f.value:"objectDomain"::STRING AS OBJECT_DOMAIN
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectName" IS NOT NULL
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
    ),""",
     """    dedup AS (
        -- V049: write targets join the split — OBJECTS_MODIFIED alongside
        -- BASE_OBJECTS_ACCESSED, so write-only ETL (COPY INTO, INSERT..VALUES,
        -- CTAS from constants) attributes to the tables it builds. DISTINCT
        -- over the union keeps a read+write of one table to a single share.
        SELECT DISTINCT QUERY_ID, OBJECT_FQN, OBJECT_DOMAIN
        FROM (
            SELECT ah.QUERY_ID,
                   f.value:"objectName"::STRING AS OBJECT_FQN,
                   f.value:"objectDomain"::STRING AS OBJECT_DOMAIN
            FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
                 LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
            WHERE ah.QUERY_START_TIME >= :lo
              AND f.value:"objectName" IS NOT NULL
              AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
            UNION ALL
            SELECT ah.QUERY_ID,
                   f.value:"objectName"::STRING,
                   f.value:"objectDomain"::STRING
            FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
                 LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED) f
            WHERE ah.QUERY_START_TIME >= :lo
              AND f.value:"objectName" IS NOT NULL
              AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
        )
    ),"""),
    # Edit 2 — the residual: a query counts as attributed if it read OR wrote.
    # Must mirror edit 1, or write-only credits appear in BOTH the split and
    # the residual (double count) — additivity is the whole point of the ledger.
    ("""    obj_q AS (
        SELECT DISTINCT ah.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
    )""",
     """    obj_q AS (
        -- V049: attributed = read OR wrote a base object; the residual is
        -- only what genuinely touched nothing.
        SELECT ah.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.BASE_OBJECTS_ACCESSED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
        UNION
        SELECT ah.QUERY_ID
        FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
             LATERAL FLATTEN(input => ah.OBJECTS_MODIFIED) f
        WHERE ah.QUERY_START_TIME >= :lo
          AND f.value:"objectDomain"::STRING IN ('Table', 'Materialized view')
    )"""),
], "proc")

out = f"""-- V049__write_target_attribution.sql — writes join the object-cost split
-- (r28+ queue; owner go 2026-07-15: "let's do both").
--
--   V048's split used ACCESS_HISTORY.BASE_OBJECTS_ACCESSED (reads only), so
--   write-only ETL — COPY INTO, INSERT ... VALUES, CTAS from constants —
--   read no base table and its credits landed in QUERY_COMPUTE_RESIDUAL
--   instead of on the tables it builds. V049 folds
--   ACCESS_HISTORY.OBJECTS_MODIFIED (write targets) into the same equal
--   split: loads attribute to their targets; the residual shrinks to
--   genuinely unattributable compute (no read, no write). DISTINCT over the
--   union keeps a read+write of one table to a single share. Credits stay
--   additive across arms and companies.
--
--   Proc swap + 14-day reload; no new objects (table and task are V048's).
--   The reload window matches the V048 first fill, so the working window is
--   re-attributed under the new split in one pass.
--
-- Derivation law: SP_LOAD_OBJECT_COST from V048 verbatim + two enumerated
-- edits (dedup CTE + obj_q CTE — split and residual must agree on what
-- "attributed" means); tests/test_v049_write_targets.py re-derives and
-- byte-compares. Apply AFTER V048. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20049, 'V049 requires V048 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 48) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_OBJECT_COST
{proc}
-- Reload the working window under the new split: write targets attributed,
-- residual re-derived. Same 14-day horizon as the V048 first fill.
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(14);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 49 AS VERSION,
       'Write-target attribution: ACCESS_HISTORY.OBJECTS_MODIFIED joins the object-cost equal split, so write-only ETL attributes to its target tables; QUERY_COMPUTE_RESIDUAL shrinks to no-read-no-write compute (r28+ queue)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 49);
"""
target = Path(os.environ.get("V049_OUT") or (MIG / "V049__write_target_attribution.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target.name}: {len(out.splitlines())} lines")
