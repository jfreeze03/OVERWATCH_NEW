"""Generate V146 (repoint the FACT_AI_USAGE_DAILY loader's AI-Functions arm to the canonical view).

SP_LOAD_MARTS_V27's [9] ai_functions arm reads the FROZEN
SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY ("no longer updated"), so the mart's
'Functions' rows go stale going forward. This re-derives the proc from V142, repointing ONLY that
arm onto the canonical CORTEX_AI_FUNCTIONS_USAGE_HISTORY. Everything else is byte-identical to V142.

The canonical view is NOT a drop-in (verified in-account 2026-09-18, owner probe):
  * credits column is CREDITS, not TOKEN_CREDITS;
  * START_TIME is TIMESTAMP_LTZ (the frozen view was NTZ) -> FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ
    (the fact columns are NTZ; MERGE will not coerce TZ->NTZ -- the same guard the ai_code arm
    applies to CORTEX_CODE_* USAGE_TIME, V078);
  * there is NO scalar TOKENS column -- token counts live in the METRICS ARRAY as
    {"key":{"metric":"input"|"output","unit":"tokens"},"value":N}, so LATERAL FLATTEN sums value
    where unit='tokens' (non-token metrics excluded), and CREDITS+REQUESTS are deduped to once per
    source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for empty METRICS,
    still counted once) so the fan-out cannot multiply them.

Run: python outputs/gen_v146.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V142 = (MIG / "V142__posture_arm_single_scan.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


proc = extract_proc(V142, "SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)")

# --- ai_functions arm: FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY -> canonical AI_FUNCTIONS -------
_old = """                SELECT START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(MODEL_NAME, 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(START_TIME) AS FIRST_TS,
                       MAX(START_TIME) AS LAST_TS,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4"""

_new = """                -- V146: repointed off the FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY onto the canonical
                -- CORTEX_AI_FUNCTIONS_USAGE_HISTORY. Not a drop-in: TOKEN_CREDITS -> CREDITS; START_TIME
                -- is TIMESTAMP_LTZ (was NTZ) so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ (same TZ->NTZ MERGE
                -- guard as the ai_code arm, V078); and there is NO scalar TOKENS column -- token counts
                -- live in the METRICS ARRAY as {"key":{"metric":"input"|"output","unit":"tokens"},"value":N},
                -- so LATERAL FLATTEN sums value where unit='tokens'. CREDITS + REQUESTS are deduped to
                -- once per source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for
                -- empty METRICS, still counted once) so the FLATTEN fan-out cannot multiply them.
                SELECT f.START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(NULLIF(f.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(f.START_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(f.START_TIME)::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(CASE WHEN COALESCE(m.INDEX, 0) = 0 THEN 1 END) AS REQUESTS,
                       SUM(CASE WHEN m.VALUE:key:unit::STRING = 'tokens'
                                THEN m.VALUE:value::NUMBER ELSE 0 END) AS TOKENS,
                       ROUND(SUM(CASE WHEN COALESCE(m.INDEX, 0) = 0
                                      THEN COALESCE(f.CREDITS, 0) ELSE 0 END), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY f,
                     LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m
                WHERE f.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4"""

assert proc.count(_old) == 1, "ai_functions arm anchor not unique/found"
proc = proc.replace(_old, _new)

# --- verify the collapse: frozen view gone, canonical view in, one FLATTEN, dedup present ----
assert proc.count("SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY") == 0
assert proc.count("SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY") == 1
assert proc.count("LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m") == 1
assert "m.VALUE:key:unit::STRING = 'tokens'" in proc
assert "COALESCE(m.INDEX, 0) = 0" in proc
# the ai_code arm (CORTEX_CODE_* views) is untouched and still present
assert proc.count("CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY") == 1

HEADER = """\
-- V146__ai_usage_loader_repoint_ai_functions.sql
--
-- Repoint the FACT_AI_USAGE_DAILY mart loader's AI-Functions arm off the FROZEN
-- ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY ("no longer updated") onto the canonical
-- ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY, so the mart's 'Functions' rows keep updating.
-- Re-derives SP_LOAD_MARTS_V27 from V142; ONLY the [9] ai_functions MERGE arm's USING(...) read
-- changes -- everything else is byte-identical to V142 (test_v146 proves it).
--
-- The canonical view is NOT a drop-in (verified in-account 2026-09-18, owner probe):
--   * credits column is CREDITS, not TOKEN_CREDITS;
--   * START_TIME is TIMESTAMP_LTZ (the frozen view was NTZ), so FIRST_TS/LAST_TS cast
--     ::TIMESTAMP_NTZ -- the fact columns are NTZ and MERGE will not coerce TZ->NTZ (the same
--     guard the ai_code arm already applies to CORTEX_CODE_* USAGE_TIME, V078);
--   * there is NO scalar TOKENS column -- token counts live in the METRICS ARRAY as
--     {"key":{"metric":"input"|"output","unit":"tokens"},"value":N}, so LATERAL FLATTEN sums
--     value where unit='tokens' (non-token metrics excluded), and CREDITS/REQUESTS are deduped
--     to once per source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for
--     empty METRICS, still counted once) so the fan-out cannot multiply them.
--
-- Proc-only; no schema/rule/task change. Apply AFTER V145. Idempotent; safe to re-run.
-- Backfill after apply (RUN AS ONE STEP, order matters): the loader MERGE is UPSERT-only, so a plain
-- re-CALL would INSERT the new canonical per-model rows while leaving any stale frozen-view 'Functions'
-- rows (relabeled MODEL_NAME, or days the frozen view still returned) as ORPHANS that DOUBLE-COUNT in
-- SUM-by-day/model readers. First purge the canonical-coverage window, THEN reload:
--     DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY WHERE SOURCE = 'Functions' AND DAY >= '2026-01-05';
--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
-- The DELETE is SCOPED to >= 2026-01-05 (the canonical view's data horizon) so genuine pre-horizon
-- 'Functions' history the canonical view cannot reproduce is PRESERVED. Note: REQUESTS changes basis at
-- the horizon (frozen view counted hourly buckets; canonical view counts per-query rows) -- expected;
-- the purge normalizes it within coverage, pre-horizon days keep the old hourly-bucket basis.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20146, 'V146 requires V145 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 145) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_MARTS_V27 (from V142; ai_functions arm repointed to CORTEX_AI_FUNCTIONS_USAGE_HISTORY, V146)
"""

SCHEMA_INSERT = """\

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 146 AS VERSION,
       'AI-usage loader repoint: SP_LOAD_MARTS_V27 FACT_AI_USAGE_DAILY ai_functions arm now reads the canonical ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY instead of the FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY (no longer updated). Not a drop-in: CREDITS (not TOKEN_CREDITS); START_TIME is TIMESTAMP_LTZ so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ (TZ->NTZ MERGE guard, like the ai_code arm); tokens summed from the METRICS array via LATERAL FLATTEN where unit=tokens (no scalar TOKENS column), CREDITS+REQUESTS deduped once per source row via COALESCE(m.INDEX,0)=0. Re-derived from V142, byte-identical outside the ai_functions arm. Proc-only, no schema/rule/task change. Backfill after apply as ONE step (loader is upsert-only): DELETE FROM FACT_AI_USAGE_DAILY WHERE SOURCE=Functions AND DAY>=2026-01-05, then CALL SP_LOAD_MARTS_V27(DAILY,365) -- the scoped purge clears stale frozen-view orphans (relabeled model / covered days) that would otherwise double-count, while preserving pre-2026-01-05 history the canonical view cannot reproduce.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 146);
"""

out = HEADER + proc + "\n" + SCHEMA_INSERT
(MIG / "V146__ai_usage_loader_repoint_ai_functions.sql").write_text(out, encoding="utf-8")
print("wrote V146 | frozen-view scans:", proc.count("CORTEX_FUNCTIONS_USAGE_HISTORY"),
      "| canonical-view scans:", proc.count("SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY"))
