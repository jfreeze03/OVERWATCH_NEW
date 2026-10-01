#!/usr/bin/env python3
"""Forward-generate V171: ops self-watch honesty, a complete-day warehouse digest, isolated ref-gap checks, and the
CREDIT_PRICE_OVERRIDE seed (round-2 items R2-026, R1-228, CORTEX-NULLIF digest half, R2-019 + R2-104,
CREDIT-PRICE-SEED).

Reads ONLY the three current definers (tests/test_proc_lineage.py: nothing after them re-defines these procs):
  V017__hardening_v7.sql            SP_CANARY_SENTINEL   (NOT V016: V016 has no render-SLA block)
  V165__daily_digest_grounding.sql  SP_DAILY_DIGEST
  V129__pipe_ref_gap_alert.sql      SP_SCAN_REF_GAPS     (the file also defines SP_ALERT_SCAN_DAILY: extract by name)
and emits, in order:

  guard (-20171, v < 170) -> SETTINGS seed CREDIT_PRICE_OVERRIDE (WHEN NOT MATCHED only) -> marker +
  SP_CANARY_SENTINEL -> marker + SP_DAILY_DIGEST -> marker + SP_SCAN_REF_GAPS -> SCHEMA_VERSION 171.

Deltas, each asserted by count (everything else byte-identical to its base; tests/migrations/test_v171_* reverses
every one with its OWN copies of the old text):
  SP_CANARY_SENTINEL (V017)
    K1  the OPS_CANARY_FAIL DETAIL no longer blames ACCOUNT_USAGE column drift (SELECT 1 names no column)
    K2  the render-SLA handler captures SQLERRM and logs it (it always said 'APP_USAGE.RENDER_MS not readable')
  SP_DAILY_DIGEST (V165)
    C1  CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a valid name else the default)
    E1  the facts are the 7 COMPLETE days ending yesterday: spend from the board's ALL / 7-day DAILY_SPEND rows
        for those days, queries from FACT_QUERY_DAILY, task runs from FACT_TASK_DAILY (the board's today-INCLUSIVE
        KPI rows are no longer read)
    E2  the spend keys say warehouse compute: WAREHOUSE_SPEND_USD, WAREHOUSE_CREDITS
    E3  the prompt names the window and says warehouse compute only, never total spend
    E4  the template names the window and the scope
  SP_SCAN_REF_GAPS (V129)
    R1  DECLAREs: a RESULTSET, the per-check counters, all_failed EXCEPTION (-20662)
    R2  one MINUS statement per check, both operands TO_VARCHAR (the app twin etl_control_sql._check_sql's shape)
    R3  one EXECUTE IMMEDIATE per check in its own EXCEPTION block (ref_gap_check_failed names the check); raise
        all_failed only when every check failed; RETURN 'ref-gap scan complete (N ok, M failed)'

The name-allowlist RLIKE line of SP_SCAN_REF_GAPS stays byte-identical and is the ONLY occurrence of that call
in the file (tests/test_p608_ops_etl.py pins etl_control_sql.ALERT_NAME_PATTERN to it), so no prose here or in the
migration quotes it. The CORTEX_MODEL expression is the same literal the V172 SP_ANOMALY_SWEEP half uses.

Nothing runs at apply time: no CALL of SP_DAILY_DIGEST (Cortex credits + a Teams post), SP_CANARY_SENTINEL or
SP_SCAN_REF_GAPS. The generator never imports app/. With PREFLIGHT_OUT set, also writes the read-only V171
PREFLIGHT section (P171.1-P171.4) built from the SAME E1 / C1 text; with PART_B_OUT set, the read-only PART B
verify grids (V171.1-V171.4). The byte-identity test never sets either.

Run: python outputs/gen_v171.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_CANARY = MIG / "V017__hardening_v7.sql"
BASE_DIGEST = MIG / "V165__daily_digest_grounding.sql"
BASE_REFGAP = MIG / "V129__pipe_ref_gap_alert.sql"
V017, V165, V129 = (p.read_text(encoding="utf-8") for p in (BASE_CANARY, BASE_DIGEST, BASE_REFGAP))
NAME = "V171__ops_selfwatch_digest_refgaps_seed.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert text.count(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}") == 1, name
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


# ===================================================================================================
# SP_CANARY_SENTINEL (from V017) -- R2-026
# ===================================================================================================
OLD_K1 = """\
               'CANARY_RESULTS has the errors. Likely ACCOUNT_USAGE column drift after a ' ||
                   'Snowflake release, or a revoked grant. Run the Admin canary for the ' ||
                   'per-builder picture.',
"""
NEW_K1 = """\
               'CANARY_RESULTS.ERROR holds the error for each failing object. This probe (SELECT 1) ' ||
                   'only sees a missing or renamed object or lost access (a revoked grant, or ' ||
                   'IMPORTED PRIVILEGES on SNOWFLAKE); it cannot see column drift. Run the Admin ' ||
                   'canary for the per-builder picture.',
"""
OLD_K2 = """\
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'CanarySentinel', 'render_check_unavailable',
                   'APP_USAGE.RENDER_MS not readable', 'source probes unaffected', CURRENT_ROLE();
"""
NEW_K2 = """\
        WHEN OTHER THEN
            emsg := SQLERRM;   -- K2: V171 R2-026 - log the real cause (the probe loop is done, emsg is free)
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'CanarySentinel', 'render_check_unavailable',
                   'render SLA check failed: ' || LEFT(:emsg, 500), 'source probes unaffected', CURRENT_ROLE();
"""

canary = extract_proc(V017, "SP_CANARY_SENTINEL()")
assert "\\" not in canary and canary.count("fails := fails + 1;") == 1
canary = _swap(canary, OLD_K1, NEW_K1, "K1 OPS_CANARY_FAIL detail")
canary = _swap(canary, OLD_K2, NEW_K2, "K2 render-SLA handler")
_cb = _body(canary)
assert "column drift after" not in _cb and "cannot see column drift" in _cb
assert _cb.count("fails := fails + 1;") == 1 and "RETURN 'sentinel v2: ' || :fails || ' failure(s)';" in _cb
assert _cb.count("'SNOWFLAKE.ACCOUNT_USAGE.") == 20 and _cb.count("'DBA_MAINT_DB.OVERWATCH.") == 4
_detail = "".join(re.findall(r"'([^']*)'", NEW_K1))
assert NEW_K1.count("'") == 8 and len(_detail) < 2000      # four apostrophe-free literals; DETAIL is VARCHAR(2000)

# ===================================================================================================
# SP_DAILY_DIGEST (from V165) -- CORTEX-NULLIF digest half + R1-228
# ===================================================================================================
# C1 (shared literal with the V172 SP_ANOMALY_SWEEP half). Snowflake RLIKE matches the WHOLE string, so this is
# app.core.ai._MODEL_RE (^[a-z0-9][a-z0-9.\-]{1,60}$) without a backslash: '-' is last in the class.
MODEL_RE_LITERAL = "[a-z0-9][a-z0-9.-]{1,60}"
DEFAULT_MODEL = "llama3.1-8b"
OLD_C1 = """\
    SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
      INTO :model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
"""
C1_SELECT = (f"SELECT IFF(RLIKE(cm, '{MODEL_RE_LITERAL}'), cm, '{DEFAULT_MODEL}')",
             "FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm\n"
             "          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)")
NEW_C1 = f"""\
    -- C1: V171 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a valid
    -- name else the default): a blank, padded, mixed-case or invalid stored value no longer reaches COMPLETE
    {C1_SELECT[0]}
      INTO :model
    {C1_SELECT[1]};
"""

OLD_E1 = """\
    SELECT MAX(IFF(METRIC = 'CREDITS', VALUE_USD, NULL)), MAX(IFF(METRIC = 'CREDITS', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUERIES', VALUE, NULL)), MAX(IFF(METRIC = 'FAILED_QUERIES', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUEUED_MINUTES', VALUE, NULL)), MAX(IFF(METRIC = 'SPILL_GB', VALUE, NULL)),
           MAX(IFF(METRIC = 'TASK_RUNS', VALUE, NULL)), MAX(IFF(METRIC = 'TASK_FAILURES', VALUE, NULL))
      INTO :f_spend_usd, :f_credits, :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb, :f_task_runs, :f_task_fail
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';
"""
# The three E1 reads: (select expressions, INTO variables, FROM + WHERE). The proc and the PREFLIGHT are both
# built from these tuples. The days are [today-7, today): the 7 complete days ending yesterday.
DAYS_LO = "DATEADD('day', -7, CURRENT_DATE())"
DAYS_HI = "CURRENT_DATE()"
E1_READS = (
    (("SUM(VALUE_USD)", "SUM(VALUE)"), ("f_spend_usd", "f_credits"),
     "FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD\n"
     "    WHERE PANEL = 'DAILY_SPEND' AND METRIC = 'CREDITS' AND COMPANY = 'ALL' AND WINDOW_DAYS = 7\n"
     f"      AND PERIOD_START >= {DAYS_LO} AND PERIOD_START < {DAYS_HI}"),
    (("SUM(QUERY_COUNT)", "SUM(FAILED_COUNT)", "ROUND(SUM(QUEUED_SEC_SUM) / 60, 1)", "ROUND(SUM(SPILL_REMOTE_GB), 2)"),
     ("f_queries", "f_failed_q", "f_queued_min", "f_spill_gb"),
     "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY\n"
     f"    WHERE DAY >= {DAYS_LO} AND DAY < {DAYS_HI}"),
    (("SUM(RUNS)", "SUM(FAILED)"), ("f_task_runs", "f_task_fail"),
     "FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY\n"
     f"    WHERE DAY >= {DAYS_LO} AND DAY < {DAYS_HI}"),
)
NEW_E1 = (
    "    -- E1 >>> V171 R1-228: the 7 COMPLETE days ending yesterday (the still-filling partial day of today left\n"
    "    -- out, the Overview Spend, last N days convention). The board 7-day KPI rows are today-INCLUSIVE (DAY >=\n"
    "    -- today-7: seven full days plus today so far), so they are no longer read. Spend is the board ALL / 7-day\n"
    "    -- DAILY_SPEND rows for those days (warehouse metering at CREDIT_PRICE_USD; one row per day once COMPANY and\n"
    "    -- WINDOW_DAYS are pinned); queries and task runs come from the facts the board aggregates, same days.\n"
    + "".join(f"    SELECT {', '.join(exprs)}\n      INTO {', '.join(':' + v for v in into)}\n    {fw};\n"
              for exprs, into, fw in E1_READS)
    + "    -- <<< E1\n")
assert [v for _e, into, _f in E1_READS for v in into] == [
    "f_spend_usd", "f_credits", "f_queries", "f_failed_q", "f_queued_min", "f_spill_gb", "f_task_runs", "f_task_fail"]

OLD_E2 = """\
    facts := 'WINDOW_DAYS=7; SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
"""
NEW_E2 = """\
    -- E2: V171 R1-228 - the keys say warehouse compute (serverless, AI and storage are not in them)
    facts := 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; WAREHOUSE_CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
"""
OLD_E3A = ("        || 'Use ONLY the FACTS below (the last 7 days, all companies; alert counts are open now or raised in "
           "the last 24 hours). '\n")
NEW_E3A = ("        || 'Use ONLY the FACTS below (the 7 complete days ending yesterday, all companies; alert counts are "
           "open now or raised in the last 24 hours). '\n")
OLD_E3B = """\
        || '*_USD facts and percentages only from *_PCT facts; CREDITS are Snowflake credits, not dollars. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and spend in plain language; '
"""
NEW_E3B = """\
        || '*_USD facts and percentages only from *_PCT facts; WAREHOUSE_CREDITS are Snowflake credits, not dollars. '
        || 'WAREHOUSE_SPEND_USD and WAREHOUSE_CREDITS cover warehouse compute only (serverless, AI and storage are not '
        || 'included): call it warehouse compute spend, never total spend. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and warehouse compute spend in plain language; '
"""
OLD_E4 = """\
             || 'Last 7 days, all companies: spend ' || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a') || ' credits); '
"""
NEW_E4 = """\
             || 'The 7 complete days to yesterday, all companies: warehouse compute spend '
             || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a')
             || ' credits; serverless, AI and storage not included); '
"""

digest = extract_proc(V165, "SP_DAILY_DIGEST()")
assert "\\" not in _body(digest)
digest = _swap(digest, OLD_C1, NEW_C1, "C1 CORTEX_MODEL read")
digest = _swap(digest, OLD_E1, NEW_E1, "E1 complete-day facts")
digest = _swap(digest, OLD_E2, NEW_E2, "E2 warehouse keys")
digest = _swap(digest, OLD_E3A, NEW_E3A, "E3a prompt window")
digest = _swap(digest, OLD_E3B, NEW_E3B, "E3b prompt scope")
digest = _swap(digest, OLD_E4, NEW_E4, "E4 template window + scope")
_db = _body(digest)
assert "\\" not in _db and "$$" not in _db
assert "PANEL = 'KPI'" not in _db and _db.count("PANEL = 'DAILY_SPEND'") == 1
assert _db.count(f">= {DAYS_LO}") == 3 and _db.count(f"< {DAYS_HI}") == 3
assert _db.count("RLIKE(cm, ") == 1 and "COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL'" not in _db
assert "REGEXP_SUBSTR_ALL(:clean," in _db and "grounding_ok := (n_bad = 0);" in _db     # V165 grounding kept
assert _db.count("never total spend") == 1 and _db.count("7 complete days") == 2

# ===================================================================================================
# SP_SCAN_REF_GAPS (from V129) -- R2-019 + R2-104
# ===================================================================================================
OLD_R1 = """\
    enabled_cnt INT;
    scan_sql STRING;
    ins_sql STRING;
BEGIN
"""
NEW_R1 = """\
    enabled_cnt INT;
    ins_sql STRING;
    -- R1 >>> V171 R2-019: one statement per check, each in its own EXCEPTION block
    res RESULTSET;
    nm STRING;
    emsg STRING;
    n_ok INT DEFAULT 0;
    n_failed INT DEFAULT 0;
    all_failed EXCEPTION (-20662, 'ref-gap scan: every configured check failed - see APP_ERROR_LOG ref_gap_check_failed');
    -- <<< R1
BEGIN
"""
OLD_R2 = """\
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
"""
NEW_R2 = """\
    -- family NAME + staging FQN + code column, and build ONE MINUS statement per check (R2 >>> V171: each runs
    -- alone below, R2-019; R2-104: BOTH operands TO_VARCHAR, the shape of the app twin
    -- etl_control_sql._check_sql since v4.527, so a NUMBER or DATE code column compares as text and never
    -- coerces the VARCHAR SRC_IDNTFTN_VAL).
    -- q_name is the apostrophe-escaped name literal; the name allowlist also excludes backslashes and
    -- quotes, so the built literal can never be broken or injected (matches the app's sql_literal).
    res := (
    SELECT nm_clean AS CHECK_NAME,
             'SELECT ' || q_name || ' AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM ( '
             || 'SELECT TO_VARCHAR(s.' || col || ') AS NEW_CODE FROM ' || fqn || ' s WHERE s.' || col
             || ' IS NOT NULL MINUS SELECT TO_VARCHAR(x.SRC_IDNTFTN_VAL) FROM ' || :xlat
             || ' x WHERE x.SRC_IDNTFTN_NM = ' || q_name || ' ) g' AS CHECK_SQL
    FROM (
        SELECT nm_clean, '''' || REPLACE(nm_clean, '''', '''''') || '''' AS q_name, fqn, col
"""
OLD_R3 = """\
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
"""
NEW_R3 = """\
          AND RLIKE(col, '^[A-Za-z0-9_$]+$')
    )
    ORDER BY CHECK_NAME
    );

    -- R3 >>> V171 R2-019: one INSERT per check, each in its own EXCEPTION block. A check that throws (a missing
    -- SELECT grant on one staging table, a renamed table, a code longer than NEW_CODE) logs one
    -- ref_gap_check_failed row naming it, and the other checks still write their gaps. V129 ran every check in
    -- ONE statement after the DELETE, so one bad check blanked the PIPE_REF_GAP page for all of them.
    LET c_checks CURSOR FOR res;
    FOR r IN c_checks DO
        nm := r.CHECK_NAME;
        BEGIN
            ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS (CHECK_NAME, NEW_CODE) '
                       || r.CHECK_SQL;
            EXECUTE IMMEDIATE :ins_sql;
            n_ok := n_ok + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                n_failed := n_failed + 1;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'AlertScan', 'ref_gap_check_failed', LEFT(:emsg, 2000),
                       'PIPE_REF_GAP check ' || LEFT(:nm, 200) || ' - other ref-gap checks unaffected', CURRENT_ROLE();
        END;
    END FOR;

    IF (:n_ok + :n_failed = 0) THEN
        RETURN 'ref-gap scan: no valid checks configured';
    END IF;
    IF (:n_ok = 0) THEN
        RAISE all_failed;   -- every check failed (e.g. no XLAT grant): arm [17] still logs ref_gap_scan_failed
    END IF;

    RETURN 'ref-gap scan complete (' || :n_ok || ' ok, ' || :n_failed || ' failed)';
    -- <<< R3
END;
"""

refgap = extract_proc(V129, "SP_SCAN_REF_GAPS()")
assert "\\" not in refgap
refgap = _swap(refgap, OLD_R1, NEW_R1, "R1 declares")
refgap = _swap(refgap, OLD_R2, NEW_R2, "R2 per-check statements + casts")
refgap = _swap(refgap, OLD_R3, NEW_R3, "R3 per-check loop + tail")
_rb = _body(refgap)
assert "\\" not in _rb and "$$" not in _rb
assert "LISTAGG(" not in _rb and "UNION ALL" not in _rb and "scan_sql" not in _rb
assert _rb.count("EXECUTE IMMEDIATE :ins_sql;") == 1 and _rb.count("RLIKE(nm_clean, ") == 1
assert _rb.count("'SELECT TO_VARCHAR(s.' || col || ') AS NEW_CODE") == 1
assert _rb.count("MINUS SELECT TO_VARCHAR(x.SRC_IDNTFTN_VAL) FROM") == 1
assert _rb.index("DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS;") < _rb.index("FOR r IN c_checks DO")
assert _rb.index("END FOR;") < _rb.index("RAISE all_failed;")

# ===================================================================================================
# The migration file.
# ===================================================================================================
HEADER = f"""-- {NAME}
--
-- Round-2 ops / data-quality fixes: the canary sentinel says what it can see, the morning digest reports the
-- complete days it claims (warehouse compute, labelled), one failing reference-gap check no longer silences the
-- others, and the deploy-gate switch validate.sql reads gets its SETTINGS row.
--
-- WHY:
--   R2-026  SP_CANARY_SENTINEL (V017) probes each source with SELECT 1, which names no column, yet its
--           OPS_CANARY_FAIL detail blamed ACCOUNT_USAGE column drift; and its render-SLA handler always logged
--           'APP_USAGE.RENDER_MS not readable', whatever actually failed.
--   R1-228  SP_DAILY_DIGEST (V165) read the exec board's 7-day KPI rows, which are today-INCLUSIVE (seven full days
--           plus today so far: about 7.25 days at the 07:20 run), and called warehouse metering plain spend in the
--           facts, the prompt and the template.
--   CORTEX  SP_DAILY_DIGEST fell back to the default model only on NULL: a blank, padded, mixed-case or invalid
--           CORTEX_MODEL value went straight into COMPLETE, so the scheduled digest ran a different model from the
--           app (app.core.ai.normalize_model) or failed to the template.
--   R2-019  SP_SCAN_REF_GAPS (V129) ran every configured check in ONE INSERT after its DELETE: one check that
--   R2-104  throws (a missing grant, a renamed table) blanked ETL_REF_GAP_RESULTS for all of them and the HIGH
--           PIPE_REF_GAP page went silent; and its MINUS operands were uncast, so a NUMBER or DATE code column
--           forced a numeric coercion of the VARCHAR XLAT value (the app twin casts both since v4.527).
--   SEED    CREDIT_PRICE_OVERRIDE (read only by snowflake/validate.sql) had no SETTINGS row.
--
--   + SETTINGS row CREDIT_PRICE_OVERRIDE = 'FALSE', WHEN NOT MATCHED only: an existing TRUE override (a contracted
--     non-3.68 rate) is never touched, and FALSE is outside validate's TRUE / Y / YES / 1 list, so the -20013
--     gate stays armed exactly as an absent row did.
--   ~ SP_CANARY_SENTINEL re-derived from V017 (its current definer; V016 has no render-SLA block), byte-identical
--     except K1 the OPS_CANARY_FAIL detail (a missing or renamed object or lost access, never column drift) and
--     K2 the render-SLA handler logs 'render SLA check failed: ' plus the error. Kept: the 24 probes, fails,
--     the 180-day purge and RETURN 'sentinel v2: '.
--   ~ SP_DAILY_DIGEST re-derived from V165, byte-identical except C1 the CORTEX_MODEL read normalized like the
--     app (trimmed, lower-case, a valid name else llama3.1-8b); E1 the facts cover the 7 complete days ending
--     yesterday (spend = the board's ALL / 7-day DAILY_SPEND rows for those days, queries from FACT_QUERY_DAILY,
--     task runs from FACT_TASK_DAILY); E2 the keys WAREHOUSE_SPEND_USD and WAREHOUSE_CREDITS (they still bind
--     through the _USD suffix and the CREDIT substring); E3 / E4 the prompt and the template name the window and
--     say warehouse compute only, never total spend. The grounding check is unchanged.
--   ~ SP_SCAN_REF_GAPS re-derived from V129, byte-identical except R1 the per-check DECLAREs and all_failed
--     (-20662); R2 one MINUS statement per check with both operands TO_VARCHAR; R3 one EXECUTE IMMEDIATE per check
--     in its own EXCEPTION block (ref_gap_check_failed names the check), all_failed raised only when every check
--     failed, RETURN 'ref-gap scan complete (N ok, M failed)'. The name allowlist line is unchanged.
--
-- COST: the digest reads three small day-grain ranges (the board and two app-owned facts, 7 days) instead of one
-- board read: no ACCOUNT_USAGE, same one Cortex call. The ref-gap scan runs one statement per configured check
-- (a handful) instead of one per run. The canary is unchanged.
-- LATENCY: unchanged; TASK_DAILY_DIGEST runs 07:20 America/Chicago, the canary Mondays 05:30, the ref-gap scan
-- inside the daily alert scan.
-- FIRST RUN: nothing runs at apply time (a digest CALL spends Cortex credits and posts to Teams). The next 07:20
-- digest writes the first complete-day row; the next daily alert scan runs the isolated ref-gap checks.
-- ROLLBACK: re-run each base CREATE PROCEDURE: SP_CANARY_SENTINEL from V017__hardening_v7.sql, SP_DAILY_DIGEST
-- from V165__daily_digest_grounding.sql, SP_SCAN_REF_GAPS from V129__pipe_ref_gap_alert.sql. The FALSE SETTINGS
-- row can stay (validate reads it as no override).
-- Apply AFTER V170. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20171, 'V171 requires V170 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 170) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- CREDIT-PRICE-SEED: the deploy-gate switch snowflake/validate.sql reads (an absent row already read FALSE), so
-- Admin > Settings lists it and app/config.py can carry it in DEFAULT_SETTINGS. WHEN NOT MATCHED only: an existing
-- override (TRUE for a contracted non-3.68 rate) is never touched, and FALSE is outside validate's TRUE / Y / YES /
-- 1 list, so this seed can never silence -20013. To run a non-default rate, UPDATE this row (or use Admin): never
-- a second INSERT (SETTINGS.KEY is an unenforced primary key).
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('CREDIT_PRICE_OVERRIDE', 'FALSE')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

"""

MARK_CANARY = ("-- >>> derived:SP_CANARY_SENTINEL  (from V017; OPS_CANARY_FAIL detail no longer blames column drift + "
               "render-SLA handler logs SQLERRM, V171)\n")
MARK_DIGEST = ("-- >>> derived:SP_DAILY_DIGEST  (from V165; 7 complete days to yesterday + warehouse compute spend "
               "keys and wording + CORTEX_MODEL normalized like the app, V171)\n")
MARK_REFGAP = ("-- >>> derived:SP_SCAN_REF_GAPS  (from V129; both MINUS operands TO_VARCHAR + one EXECUTE IMMEDIATE "
               "per check in its own EXCEPTION block, R2-019/R2-104, V171)\n")

DESCRIPTION = (
    "Ops self-watch, digest window, ref-gap isolation, override seed (round 2). SETTINGS CREDIT_PRICE_OVERRIDE "
    "seeded FALSE (WHEN NOT MATCHED; validate.sql reads FALSE as no override). SP_CANARY_SENTINEL re-derived from "
    "V017: the OPS_CANARY_FAIL detail no longer blames column drift (SELECT 1 sees a missing object or lost access "
    "only) and the render-SLA handler logs the real error. SP_DAILY_DIGEST re-derived from V165: facts cover the 7 "
    "complete days ending yesterday (the board ALL 7-day DAILY_SPEND rows for those days, FACT_QUERY_DAILY, "
    "FACT_TASK_DAILY; the today-inclusive KPI rows are no longer read), keys WAREHOUSE_SPEND_USD and "
    "WAREHOUSE_CREDITS, the prompt and template say warehouse compute only, never total spend; CORTEX_MODEL is "
    "normalized like the app (trimmed, lower-case, a valid name else llama3.1-8b). SP_SCAN_REF_GAPS re-derived "
    "from V129: both MINUS operands TO_VARCHAR (parity with the Operations panel) and one statement per check in "
    "its own EXCEPTION block (ref_gap_check_failed names a failing check; the scan raises only when every check "
    "failed). No task change, no new object, no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION and "$" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 171 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 171);
"""

out = (HEADER + MARK_CANARY + canary + "\n\n" + MARK_DIGEST + digest + "\n\n" + MARK_REFGAP + refgap + "\n"
       + VERSION_ROW)

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n")
assert out.count("CREATE OR REPLACE PROCEDURE") == 3 and out.count("$$") == 8
assert out.count("-- >>> derived:") == 3
assert "V171 requires V170 first" in out and "SELECT 171 AS VERSION" in out
assert "\r" not in out and "\\" not in out
assert out.count("RLIKE(nm_clean, ") == 1, "the R2-109 allowlist pin must find exactly one"
assert not re.search(r"^\s*CALL\s", out.replace(
    "            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(", ""), re.M), "no CALL but the digest's route send"
assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+TASK|ALTER\s+TASK|EXECUTE\s+TASK)", out)
assert "SOURCE_FRESHNESS_STATE" not in out
assert "@" not in out, "no address of any kind"

target = Path(os.environ.get("V171_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")


# ===================================================================================================
# PREFLIGHT (read-only; the V171 section). Built from the SAME E1 / C1 text as the proc. The proc runs in the
# task session (account TIMEZONE America/Chicago); the grids pin Central explicitly, so a UTC worksheet previews
# the same days.
# ===================================================================================================
CENTRAL_TODAY = "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE"


def _central(sql: str) -> str:
    n = sql.count("CURRENT_DATE()")
    assert n, sql
    return sql.replace("CURRENT_DATE()", CENTRAL_TODAY)


_OLD_KPI_COLS = re.findall(r"MAX\(IFF\(METRIC = '\w+', VALUE(?:_USD)?, NULL\)\)", OLD_E1)
_OLD_KPI_VARS = re.search(r"INTO (:[^\n]+)", OLD_E1).group(1).replace(":", "").split(", ")
assert len(_OLD_KPI_COLS) == len(_OLD_KPI_VARS) == 8
_OLD_KPI_FROM = OLD_E1[OLD_E1.index("    FROM "):].strip().rstrip(";")
_old_cte = ("old_kpi AS (\n        SELECT " + ",\n               ".join(
    f"{c} AS V165_{v[2:].upper()}" for c, v in zip(_OLD_KPI_COLS, _OLD_KPI_VARS, strict=True))
    + f"\n        {_OLD_KPI_FROM}\n    )")
_new_ctes = ",\n    ".join(
    f"new_{i} AS (\n        SELECT " + ", ".join(f"{e} AS V171_{v[2:].upper()}" for e, v in zip(exprs, into, strict=True))
    + f"\n        {_central(fw)}\n    )"
    for i, (exprs, into, fw) in enumerate(E1_READS, start=1))

PREFLIGHT = f"""-- PREFLIGHT -- V171 section (READ-ONLY). Run before applying V171; changes nothing.
-- P171.1 the digest facts today (V165: the board's today-INCLUSIVE 7-day KPI rows) beside V171's (the 7 complete
--        days ending yesterday). expect V171_SPEND_USD a little below V165_SPEND_USD (today's partial day left
--        out) and never about 3x it (that would be every DAILY_SPEND window counted). Central days, any session.
WITH {_old_cte},
    {_new_ctes}
SELECT *
  FROM old_kpi CROSS JOIN new_1 CROSS JOIN new_2 CROSS JOIN new_3;

-- P171.2 the model the scheduled digest will run (V171 reads CORTEX_MODEL like the app). expect RUNS_AS equal to
--        STORED_VALUE; when they differ, the stored value is blank, padded, mixed-case or invalid (fix it on
--        Admin > Settings or leave it: V171 runs RUNS_AS either way).
SELECT MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)) AS STORED_VALUE,
       LENGTH(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL))) AS LEN,
       (SELECT IFF(RLIKE(cm, '{MODEL_RE_LITERAL}'), cm, '{DEFAULT_MODEL}')
          {C1_SELECT[1]}) AS RUNS_AS
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

-- P171.3 the 30-day history V171 R2-019 / R2-104 changes. ref_gap_scan_failed = the whole scan threw (one bad
--        check, or a coercion like 'Numeric value ... is not recognized', silenced every check). expect none.
SELECT ERROR_TYPE, COUNT(*) AS N, MAX(LOGGED_AT) AS LAST_AT, MAX(LEFT(ERROR_MESSAGE, 200)) AS SAMPLE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE ERROR_TYPE IN ('ref_gap_scan_failed', 'ref_gap_check_failed', 'render_check_unavailable', 'digest_ai_failed')
   AND LOGGED_AT >= DATEADD('day', -30, CURRENT_TIMESTAMP())
 GROUP BY ERROR_TYPE
 ORDER BY N DESC;

-- P171.4 CREDIT_PRICE_OVERRIDE rows before the seed. expect N 0 (the seed inserts FALSE) or 1 (kept as is).
SELECT COUNT(*) AS N, LISTAGG(VALUE, ', ') AS VALUES_SEEN
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY = 'CREDIT_PRICE_OVERRIDE';
"""

# ===================================================================================================
# PART B (read-only verify grids). Every GET_DDL fragment is quote-free and in its proc body.
# ===================================================================================================
PART_B_DDL = (
    ("V171.1", "SP_CANARY_SENTINEL", _cb,
     ("cannot see column drift", "render SLA check failed: ", "sentinel v2: "),
     ("Likely ACCOUNT_USAGE column drift", "APP_USAGE.RENDER_MS not readable")),
    ("V171.2", "SP_DAILY_DIGEST", _db,
     ("WAREHOUSE_SPEND_USD=", "PERIOD_START < CURRENT_DATE()", "FACT_TASK_DAILY", "RLIKE(cm, ",
      "never total spend", "REGEXP_SUBSTR_ALL"),
     ("7; SPEND_USD=", "Last 7 days, all companies")),
    ("V171.3", "SP_SCAN_REF_GAPS", _rb,
     ("TO_VARCHAR(x.SRC_IDNTFTN_VAL)", "ref_gap_check_failed", "LET c_checks CURSOR FOR res", "-20662"),
     ("scan_sql", "LISTAGG(")),
)
_ddl_rows = []
for _label, _proc, _text, _present, _absent in PART_B_DDL:
    for _frag in _present + _absent:
        assert not set(_frag) & {"'", "\\", "\n", "\r"}, _frag
    for _frag in _present:
        assert _frag in _text, (_proc, _frag)
    for _frag in _absent:
        assert _frag not in _text, (_proc, _frag)
    _ddl = f"GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{_proc}()')"
    _cond = (" AND ".join(f"CONTAINS({_ddl}, '{f}')" for f in _present) + "\n           AND "
             + " AND ".join(f"NOT CONTAINS({_ddl}, '{f}')" for f in _absent))
    _ddl_rows.append(f"SELECT '{_label} {_proc} is the V171 proc' AS CHECK_NAME,\n"
                     f"       IFF({_cond},\n           'OK', 'FAIL: {_proc} is not the V171 text') AS RESULT")

PART_B = ("-- PART B -- V171 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.\n"
          "-- V171.1 - V171.4 now.\n"
          + "\nUNION ALL\n".join(_ddl_rows)
          + """
UNION ALL
SELECT 'V171.4 exactly one CREDIT_PRICE_OVERRIDE row',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_OVERRIDE') = 1,
           'OK', 'FAIL: zero or several CREDIT_PRICE_OVERRIDE rows (keep one; UPDATE it, never INSERT a second)');

-- V171.5 the next morning, after the 07:20 CT run. expect FACTS to start 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD='
--        and, on a templated body, 'The 7 complete days to yesterday'.
SELECT DIGEST_DATE, MODEL, BODY_SOURCE, GROUNDING_OK, FIGURES_CHECKED, LEFT(FACTS, 300) AS FACTS_START,
       STARTSWITH(FACTS, 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=') AS V171_FACTS, LEFT(BODY, 200) AS BODY_START
  FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST
 ORDER BY DIGEST_DATE DESC
 LIMIT 1;

-- V171.6 after the next daily alert scan. expect no ref_gap_scan_failed row since the apply (a partial failure
--        now logs ref_gap_check_failed with the check name in CONTEXT, and the healthy checks still alert).
SELECT LOGGED_AT, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE PAGE = 'AlertScan' AND ERROR_TYPE IN ('ref_gap_check_failed', 'ref_gap_scan_failed')
   AND LOGGED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
 ORDER BY LOGGED_AT DESC
 LIMIT 20;
""")

pf = os.environ.get("PREFLIGHT_OUT")
if pf:
    Path(pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {pf}")
pb = os.environ.get("PART_B_OUT")
if pb:
    Path(pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {pb}")

print(f"V171 written: {target} ({len(out)} bytes)")
