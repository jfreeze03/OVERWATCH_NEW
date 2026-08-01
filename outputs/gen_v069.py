#!/usr/bin/env python3
r"""Forward-generate V069__exec_board_serverless_ai_drivers.sql.

Audit finding C5 (verified 2026-07-31): the Overview "Top cost drivers" panel reads the
COST_DRIVER rows of MART_EXEC_BOARD, which SP_REFRESH_EXEC_BOARD builds from
FACT_WAREHOUSE_DAILY and NOTHING else (V054 lines ~159-161). Serverless (auto-clustering,
MV refresh, search optimization, snowpipe, serverless tasks, SPCS...) and AI/Cortex spend
can therefore NEVER appear as a cost driver -- while the same page's KPI caption promises
"credit-billed services (compute, serverless, AI)". A Cortex or auto-clustering line could
be the account's fastest-growing cost and be structurally invisible on the driver panel.

Fix -- SP_REFRESH_EXEC_BOARD re-derived from its LATEST def (V054; grep confirms V003 /
V041 / V042 / V043 / V044 / V045 / V052 / V054 define it and nothing after V054 redefines
it) with a second COST_DRIVER arm sourced from FACT_METERING_DAILY -- the app's OWN daily
fact (SP_LOAD_DAILY_FACTS), never a live ACCOUNT_USAGE scan:

  * Warehouse metering is excluded (the existing arm already covers it) using the canonical
    exclusion list COST_SERVERLESS_CREEP spells in V066 -- minus its 'AI_SERVICES' entry,
    because AI is exactly what this arm exists to surface.
  * HOUSE RATE LAW: AI/Cortex credits price at :ai_credit_price, everything else at
    :credit_price, via the same two-partition dollarization V064/V065's alert blocks use
    over the canonical AI predicate. The proc only had :credit_price in scope, so the
    single-key SETTINGS read becomes the canonical two-key IFF read (V061..V067 form) --
    the rates are NEVER hardcoded in the SQL; the only 3.68/2.20 in the proc are the
    COALESCE fallbacks that mirror the V001 seeds.
  * CREDITS_BILLED (adjustment applied) is the base -- the same one the page's MTD /
    Projected KPIs dollarize (app/data/mart_sql.py _billed_split_cols), so the driver panel
    and the KPI row agree.
  * Same window semantics as the existing arm: a -365d read horizon aggregated once, then
    the shared `windows` join (7/14/30/60/90/180/365).
  * Column contract unchanged (PANEL/METRIC/DIMENSION/VALUE/VALUE_USD/UNIT/SORT_ORDER), so
    the mart reader (app/data/mart_sql.py exec_board) needs NO change -- it selects every
    panel generically. The rows go under a DISTINCT panel value PANEL='COST_DRIVER_SVC' (#12:
    NOT 'COST_DRIVER'), so the warehouse COST_DRIVER panel stays warehouse-only and keeps
    reconciling to the warehouse-only headline KPIs / "% of warehouse compute spend" caption;
    app/ui/pages/overview.py reads COST_DRIVER_SVC and renders it as a separate table beneath
    the warehouse drivers. There is no KIND column and the contract has no room for one, so
    the kind is carried in the DIMENSION label: 'Serverless: <SVC>' / 'AI/Cortex: <SVC>'.
  * FACT_METERING_DAILY carries NO company dimension (account-level metering), so the new
    rows are emitted for the 'ALL' pill only. Fanning them across ALFA/Trexis would invent
    an attribution the source does not carry; parking them in the V044 UNKNOWN pill would
    poison that pill's "go map this" signal with spend that can never be mapped.

Derivation law (see gen_v061..68): extract_proc takes the LAST matching definition,
_apply asserts every needle's occurrence count, GUARDS assert nothing load-bearing
vanished. tests/test_v069_exec_board_drivers.py byte-compares (regenerate via V069_OUT).
Idempotent; apply AFTER V068. No new objects -- a proc re-definition plus one board reload.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

_V054 = "V054__exec_board_window_history.sql"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def _apply(body: str, old: str, new: str, count: int, name: str) -> str:
    n = body.count(old)
    assert n == count, f"{name}: needle x{n} (want {count}): {old[:70]!r}"
    return body.replace(old, new)


def derive(name: str) -> str:
    base, edits = EDITS[name]
    body = extract_proc(base, name)
    for old, new, cnt in edits:
        body = _apply(body, old, new, cnt, name)
    for g in GUARDS.get(name, []):
        assert g in body, f"{name}: guardrail vanished: {g[:70]!r}"
    return body


def sql_only(body: str) -> str:
    """The proc with `--` comments stripped, for counting EXECUTED constructs.

    The V069 comments name the bind variables (:credit_price / :ai_credit_price) and the
    rates they default to, which would otherwise inflate the counts below and let a real
    hardcoded rate hide behind a comment mention. No string literal in this proc contains
    '--', so a line-wise split is exact here."""
    return "\n".join(line.split("--")[0] for line in body.splitlines())


# The canonical AI/Cortex predicate, spelled EXACTLY as V064/V065's alert blocks and
# app/data/mart_sql.py._AI_SERVICE_PRED spell it. Kept as ONE string and evaluated ONCE in
# the proc (sv_daily.IS_AI) so the label and the dollarization can never drift apart.
_AI = "(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')"

EDITS = {
    'SP_REFRESH_EXEC_BOARD': (_V054, [
        # ---- 1. the AI rate must be in scope. The proc read ONE setting with a
        # single-key WHERE; swap in the canonical two-key IFF read used by every alert
        # scan since V061 so :ai_credit_price is available to the new arm. The COALESCE
        # fallbacks mirror the V001 seeds -- the rates are never written into the SQL.
        ("""DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(VALUE)), 3.68) INTO :credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'CREDIT_PRICE_USD';""",
         """DECLARE
    credit_price FLOAT;
    ai_credit_price FLOAT;   -- V069: AI/Cortex credits bill at their OWN rate (house rate law)
BEGIN
    -- V069: both rates in ONE read, the canonical house form (V061..V067 alert scans).
    -- The COALESCE fallbacks mirror the V001 SETTINGS seeds; no rate is ever written
    -- into the SQL below.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)
      INTO :credit_price, :ai_credit_price
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;""", 1),
        # ---- 2. the serverless/AI daily aggregate, next to the other -365d "aggregate
        # each fact ONCE" CTEs. Same read horizon (= max advertised window, V054).
        ("""    tk_daily AS (
        SELECT COMPANY, DAY, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
        GROUP BY 1, 2
    ),""",
         f"""    tk_daily AS (
        SELECT COMPANY, DAY, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
        GROUP BY 1, 2
    ),
    -- V069 (audit C5): serverless + AI/Cortex spend, so the driver panel can show what
    -- the KPI row already counts. Source is FACT_METERING_DAILY -- the app's own daily
    -- fact (SP_LOAD_DAILY_FACTS), never a live ACCOUNT_USAGE scan -- on the SAME -365d
    -- horizon as the three arms above. Warehouse metering is excluded because wh_daily
    -- already carries it: the canonical exclusion list COST_SERVERLESS_CREEP spells in
    -- V066, minus its AI_SERVICES entry (AI is what this arm exists to surface).
    -- CREDITS_BILLED (adjustment applied) is the same base the page's MTD/Projected KPIs
    -- dollarize, so the driver panel and the KPI row agree. IS_AI evaluates the canonical
    -- AI predicate ONCE, here, so the label and the rate can never disagree.
    sv_daily AS (
        SELECT DAY, SERVICE_TYPE,
               {_AI} AS IS_AI,
               IFF({_AI}, 'AI/Cortex: ', 'Serverless: ') || SERVICE_TYPE AS DRIVER_LABEL,
               SUM(CREDITS_BILLED) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())
          AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')
        GROUP BY 1, 2, 3, 4
    ),""", 1),
        # ---- 3. window expansion, mirroring wh/qh/tk. No scopes join: see the comment.
        ("""    tk AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.RUNS, f.FAILED
        FROM tk_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),""",
         """    tk AS (
        SELECT s.COMPANY AS SCOPE_COMPANY, w.WINDOW_DAYS, f.RUNS, f.FAILED
        FROM tk_daily f
        JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),
    -- V069: the SAME windows expansion the three arms above use. There is deliberately NO
    -- scopes join -- FACT_METERING_DAILY carries no company dimension (account-level
    -- metering), so these rows are emitted for the 'ALL' pill ONLY. Fanning them across
    -- ALFA/Trexis would invent an attribution the source does not carry, and parking them
    -- in the V044 UNKNOWN pill would poison that pill's "go map this" signal with spend
    -- that can never be mapped.
    sv AS (
        SELECT 'ALL' AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DRIVER_LABEL, f.IS_AI, f.CREDITS
        FROM sv_daily f
        JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())
    ),""", 1),
        # ---- 4. the new COST_DRIVER arm, appended to the warehouse arm that closes the
        # INSERT. Identical column contract, so the app reader needs no change.
        ("    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME;",
         """    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME
    -- V069 (audit C5): serverless + AI/Cortex cost drivers on their OWN panel. The
    -- warehouse arm above reads FACT_WAREHOUSE_DAILY ONLY, so a Cortex or auto-clustering
    -- line could be the account's fastest-growing cost and never reach the driver panel,
    -- while this page's KPI caption promises compute + serverless + AI. These rows go under
    -- PANEL='COST_DRIVER_SVC' -- a DISTINCT panel from the warehouse 'COST_DRIVER' -- so
    -- the warehouse drivers keep summing to the warehouse-only headline KPIs and the page's
    -- "% of warehouse compute spend" caption stays true; the app renders this as a separate
    -- table beneath the warehouse drivers. Same column contract; the kind rides in the
    -- DIMENSION label because the board has no KIND column.
    -- BASIS: this panel is BILLED $ -- CREDITS_BILLED (adjustment applied), AI/Cortex
    -- credits x :ai_credit_price and everything else x :credit_price (the two-partition
    -- dollarization of V064/V065's alert blocks, over the canonical AI predicate resolved
    -- once as sv_daily.IS_AI). The warehouse panel is operational CREDITS_TOTAL at the
    -- compute rate -- the two panels never mix bases.
    UNION ALL
    SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER_SVC', 'CREDITS', DRIVER_LABEL, NULL,
           SUM(CREDITS),
           ROUND(SUM(CASE WHEN IS_AI THEN 0 ELSE CREDITS END) * :credit_price
                 + SUM(CASE WHEN IS_AI THEN CREDITS ELSE 0 END) * :ai_credit_price, 2),
           'credits', 20
    FROM sv GROUP BY 1, 2, DRIVER_LABEL;""", 1),
    ]),
}

GUARDS = {
    'SP_REFRESH_EXEC_BOARD': [
        # the V041-R4 build-stage + atomic swap must survive
        "DELETE FROM DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;",
        "SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;",
        "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE",
        "RETURN 'exec board refreshed (atomic swap)';",
        # V044 (#18) UNKNOWN pill + V052/V054 long windows must survive
        "UNION ALL SELECT 'UNKNOWN'",
        "UNION ALL SELECT 180 UNION ALL SELECT 365",
        # the existing warehouse-fed arms must survive untouched
        "FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY",
        "'COST_DRIVER', 'CREDITS', WAREHOUSE_NAME, NULL,",
        "'DAILY_SPEND', 'CREDITS', NULL, DAY,",
    ],
}

board = derive("SP_REFRESH_EXEC_BOARD")
code = sql_only(board)   # comment-free view: counts below must reflect EXECUTED SQL

# ---- correctness assertions on the generated proc ----
# the new arm exists, sourced from the app's own fact (not ACCOUNT_USAGE)
assert code.count("FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY") == 1, "one metering read"
assert "SNOWFLAKE.ACCOUNT_USAGE" not in code, "the board never live-scans ACCOUNT_USAGE"
assert code.count("SUM(CREDITS_BILLED) AS CREDITS") == 1, "billed credits (adjustment applied) is the base"
assert "CREDITS_USED" not in code, "billed, not used — matches the page's MTD/Projected base"
# warehouse metering excluded (the existing arm already covers it)
assert "AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')" in code, \
    "warehouse metering excluded — the canonical V066 list"
assert "'AI_SERVICES'" not in code, "AI is NOT excluded here — it is the point of the arm"
# the canonical AI predicate, once for IS_AI and once for the label
assert code.count(_AI) == 2, "canonical AI predicate: IS_AI + the DIMENSION label"
assert "'AI/Cortex: '" in code and "'Serverless: '" in code, "labelled drivers"
assert "|| SERVICE_TYPE AS DRIVER_LABEL" in code, "the label carries the service type"
# house rate law: two-partition dollarization, both rates from SETTINGS, never hardcoded
assert "ai_credit_price FLOAT;" in code, "the AI rate is in scope"
assert "IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL)" in code, "read from SETTINGS, house form"
assert ("ROUND(SUM(CASE WHEN IS_AI THEN 0 ELSE CREDITS END) * :credit_price\n"
        "                 + SUM(CASE WHEN IS_AI THEN CREDITS ELSE 0 END) * :ai_credit_price, 2)") in code, \
    "two-partition dollarization mirroring V064/V065"
assert code.count("3.68") == 1 and code.count("2.20") == 1, "rates appear only as the COALESCE seeds"
assert code.count(":ai_credit_price") == 2, "AI rate: read INTO + applied once"
assert code.count(":credit_price") == 5, "compute rate: read INTO + KPI + daily spend + 2 driver arms"
# the existing warehouse driver arm is preserved (warehouse-only), and the serverless/AI
# arm lands on its OWN panel so the warehouse drivers still reconcile to the headline KPIs
assert code.count("'COST_DRIVER', 'CREDITS'") == 1, "warehouse arm preserved, warehouse-only"
assert code.count("'COST_DRIVER_SVC', 'CREDITS'") == 1, "serverless/AI arm on its own panel"
assert "FROM wh GROUP BY 1, 2, WAREHOUSE_NAME\n" in board, "warehouse driver arm intact"
assert "FROM sv GROUP BY 1, 2, DRIVER_LABEL;" in code, "new arm closes the INSERT"
assert code.count("(COMPANY, WINDOW_DAYS, PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, UNIT, SORT_ORDER)") == 1, \
    "column contract unchanged — the app reader needs no change"
# same window semantics as the existing arms (365d read horizon + the shared windows join)
assert code.count("WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())") == 4, "wh/qh/tk/sv same horizon"
assert code.count("JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())") == 4, \
    "wh/qh/tk/sv share the windows expansion"
# account-level metering lands on the ALL pill only — no invented company attribution
assert "SELECT 'ALL' AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DRIVER_LABEL" in code, "ALL scope only"
assert code.count("JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)") == 3, \
    "the serverless arm does NOT join scopes"
# CTE order: sv_daily is defined before sv consumes it
assert board.index("    sv_daily AS (") < board.index("    sv AS (\n"), "sv_daily precedes sv"
assert board.count("CREATE OR REPLACE PROCEDURE") == 1

out = f"""-- V069__exec_board_serverless_ai_drivers.sql
--
-- Audit finding C5: the Overview "Top cost drivers" panel reads the COST_DRIVER rows of
-- MART_EXEC_BOARD, and SP_REFRESH_EXEC_BOARD built those from FACT_WAREHOUSE_DAILY and
-- nothing else -- so serverless (auto-clustering, MV refresh, search optimization,
-- snowpipe, serverless tasks, SPCS...) and AI/Cortex spend could NEVER appear as a cost
-- driver, while the same page's KPI caption promises "compute, serverless, AI". A Cortex
-- or auto-clustering line could be the account's fastest-growing cost and be structurally
-- invisible on the driver panel.
--
-- SP_REFRESH_EXEC_BOARD is re-derived from its LATEST def (V054) via outputs/gen_v069.py +
-- count-asserted needle edits, gaining a second COST_DRIVER arm over FACT_METERING_DAILY --
-- the app's own daily fact, not a live ACCOUNT_USAGE scan:
--
--   * warehouse metering excluded (the existing arm covers it) with the canonical
--     COST_SERVERLESS_CREEP list, minus AI_SERVICES -- AI is the point of the arm;
--   * HOUSE RATE LAW -- AI/Cortex credits x :ai_credit_price, everything else x
--     :credit_price, the two-partition dollarization of V064/V065's alert blocks. The proc
--     had only :credit_price in scope, so its single-key SETTINGS read becomes the
--     canonical two-key IFF read (V061..V067 form); no rate is hardcoded in the SQL;
--   * CREDITS_BILLED (adjustment applied) is the base -- the same one this page's MTD /
--     Projected KPIs dollarize, so panel and KPI row agree;
--   * same window semantics as the existing arm (-365d read horizon, shared windows join);
--   * unchanged column contract, so the mart reader (mart_sql.py exec_board, which selects
--     every panel generically) needs NO change. The rows land on a DISTINCT panel value
--     PANEL='COST_DRIVER_SVC' (#12: NOT 'COST_DRIVER'), so the warehouse COST_DRIVER panel
--     stays warehouse-only and keeps reconciling to the warehouse-only headline KPIs;
--     overview.py reads COST_DRIVER_SVC and renders it as a separate table beneath the
--     warehouse drivers. The board has no KIND column, so the kind rides in the DIMENSION
--     label: 'Serverless: <SERVICE_TYPE>' / 'AI/Cortex: <SERVICE_TYPE>';
--   * FACT_METERING_DAILY has no company dimension (account-level metering), so the new
--     rows are emitted for the 'ALL' pill ONLY -- fanning them across ALFA/Trexis would
--     invent an attribution the source does not carry.
--
-- The tail reloads the board so the new drivers appear immediately (the hourly task keeps
-- them current thereafter). Idempotent; apply AFTER V068. No new objects -- one proc
-- re-definition plus one board reload. Byte-verified by
-- tests/test_v069_exec_board_drivers.py; no owner smoke test required.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20069, 'V069 requires V068 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 68) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_REFRESH_EXEC_BOARD  (serverless + AI/Cortex COST_DRIVER arm, house rate law)
{board}
-- Rebuild the board so the serverless/AI drivers are on the panel now; the hourly
-- TASK_REFRESH_EXEC_BOARD chain keeps them fresh thereafter.
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 69 AS VERSION,
       'Exec-board serverless + AI cost drivers (audit C5): SP_REFRESH_EXEC_BOARD built COST_DRIVER rows from FACT_WAREHOUSE_DAILY alone, so serverless (auto-clustering, MV refresh, search optimization, snowpipe, serverless tasks) and AI/Cortex spend could never appear on the Overview driver panel even as the fastest-growing line - while the same page KPIs cover compute + serverless + AI. A second driver arm over FACT_METERING_DAILY (the app fact, not ACCOUNT_USAGE) excludes warehouse metering, prices AI/Cortex credits at AI_CREDIT_PRICE_USD and the rest at CREDIT_PRICE_USD (both read from SETTINGS - the proc gains the two-key read), keeps the same -365d/windows semantics and column contract, and labels drivers Serverless:/AI/Cortex:. The rows go under a DISTINCT panel PANEL=COST_DRIVER_SVC (not COST_DRIVER) so the warehouse driver panel stays warehouse-only and keeps reconciling to the warehouse-only KPIs; overview.py renders COST_DRIVER_SVC as a separate table. Account-level metering has no company dimension, so the rows land on the ALL scope only. Re-derived from V054; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 69);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1, "one proc re-defined"
assert "CREATE TABLE" not in out and "CREATE TASK" not in out, "no new objects"
assert "EXCEPTION (-20069" in out and "IF (v < 68) THEN" in out, "version guard"
assert out.count("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") == 1, "one board reload"
assert "SELECT 69 AS VERSION" in out and "WHERE VERSION = 69)" in out, "registry insert"

target = Path(os.environ.get("V069_OUT") or (MIG / "V069__exec_board_serverless_ai_drivers.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
