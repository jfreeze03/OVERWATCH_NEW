"""Locks for V069 — serverless + AI/Cortex cost drivers on the exec board (audit C5).

SP_REFRESH_EXEC_BOARD built its COST_DRIVER rows from FACT_WAREHOUSE_DAILY and nothing
else, so serverless (auto-clustering, MV refresh, search optimization, snowpipe, serverless
tasks) and AI/Cortex spend could NEVER reach the Overview "Top cost drivers" panel — while
the same page's KPI caption promises "compute, serverless, AI". The proc is re-derived from
its latest def (V054) with a second COST_DRIVER arm over FACT_METERING_DAILY, priced under
the house rate law (AI credits at AI_CREDIT_PRICE_USD, the rest at CREDIT_PRICE_USD).

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V69 = (_MIG / "V069__exec_board_serverless_ai_drivers.sql").read_text(encoding="utf-8")

# The canonical AI/Cortex predicate, spelled as V064/V065's alert blocks and
# app/data/mart_sql.py._AI_SERVICE_PRED spell it.
_AI = "(SERVICE_TYPE ILIKE '%CORTEX%' OR SERVICE_TYPE ILIKE 'AI%' OR SERVICE_TYPE ILIKE '%INTELLIGENCE%')"


def _proc(text: str, name: str) -> str:
    m = re.findall(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S)
    assert m, name
    return m[-1]


def _code(body: str) -> str:
    """`body` with `--` comments stripped — counts must reflect EXECUTED SQL, not prose
    (the V069 comments name the bind variables and the rates they fall back to)."""
    return "\n".join(line.split("--")[0] for line in body.splitlines())


_BOARD = _proc(_V69, "SP_REFRESH_EXEC_BOARD")
_SQL = _code(_BOARD)


def test_v069_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v069.py")],
                       env={**os.environ, "V069_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V69, (
        "V069 drifted from outputs/gen_v069.py — regenerate, never hand-edit.")


def test_v069_guard_version_and_board_reload():
    assert "EXCEPTION (-20069" in _V69 and "IF (v < 68) THEN" in _V69
    assert "SELECT 69 AS VERSION" in _V69 and "WHERE VERSION = 69)" in _V69
    assert _V69.count("CREATE OR REPLACE PROCEDURE") == 1
    # proc re-definition only — no new tables, tasks, views or streams
    for ddl in ("CREATE TABLE", "CREATE TRANSIENT TABLE", "CREATE TASK",
                "CREATE VIEW", "CREATE OR REPLACE VIEW", "CREATE STREAM"):
        assert ddl not in _V69, ddl
    # the tail reloads the board so the new drivers land immediately
    assert _V69.count("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") == 1


def test_new_arm_reads_the_app_fact_not_account_usage():
    assert _SQL.count("FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY") == 1
    assert "SNOWFLAKE.ACCOUNT_USAGE" not in _SQL, "the board must never live-scan ACCOUNT_USAGE"
    # billed credits (adjustment applied) — the same base the page's MTD/Projected KPIs use
    assert "SUM(CREDITS_BILLED) AS CREDITS" in _SQL
    assert "CREDITS_USED" not in _SQL


def test_warehouse_metering_excluded_but_ai_is_not():
    # the canonical exclusion list (V066 COST_SERVERLESS_CREEP) — warehouses are already
    # covered by the existing arm, so double-counting them here would be a lie
    assert "AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER')" in _SQL
    # ...minus V066's AI_SERVICES entry: AI is exactly what this arm exists to surface
    assert "'AI_SERVICES'" not in _SQL


def test_canonical_ai_predicate_drives_both_the_label_and_the_rate():
    # evaluated exactly twice: once as IS_AI, once building the DIMENSION label — one
    # spelling, so the label and the rate can never disagree
    assert _SQL.count(_AI) == 2
    assert f"{_AI} AS IS_AI" in _SQL
    assert f"IFF({_AI}, 'AI/Cortex: ', 'Serverless: ') || SERVICE_TYPE AS DRIVER_LABEL" in _SQL


def test_house_rate_law_two_partition_dollarization():
    # both rates read from SETTINGS in the canonical two-key form (V061..V067)
    assert "ai_credit_price FLOAT;" in _SQL
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)" in _SQL
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)" in _SQL
    assert "INTO :credit_price, :ai_credit_price" in _SQL
    # AI credits x the AI rate, everything else x the compute rate — the V064/V065 shape
    assert ("ROUND(SUM(CASE WHEN IS_AI THEN 0 ELSE CREDITS END) * :credit_price\n"
            "                 + SUM(CASE WHEN IS_AI THEN CREDITS ELSE 0 END) * :ai_credit_price, 2)") in _SQL
    # the rates are NEVER hardcoded into the SQL: 3.68/2.20 appear only as the V001-seed
    # COALESCE fallbacks in the settings read
    assert _SQL.count("3.68") == 1 and _SQL.count("2.20") == 1
    assert _SQL.count(":ai_credit_price") == 2, "read INTO + applied once"
    assert _SQL.count(":credit_price") == 5, "read INTO + KPI + daily spend + both driver arms"


def test_existing_warehouse_driver_arm_is_preserved():
    # #12: the warehouse COST_DRIVER panel is warehouse-only again — the serverless/AI rows
    # moved to their OWN panel (COST_DRIVER_SVC), so the warehouse drivers still reconcile
    # to the warehouse-only headline KPIs and the "% of warehouse compute spend" caption.
    assert _SQL.count("'COST_DRIVER', 'CREDITS'") == 1, "warehouse arm kept, warehouse-only"
    assert _SQL.count("'COST_DRIVER_SVC', 'CREDITS'") == 1, "serverless/AI arm on its own panel"
    assert ("SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER', 'CREDITS', WAREHOUSE_NAME, NULL,\n"
            "           SUM(CREDITS), ROUND(SUM(CREDITS) * :credit_price, 2), 'credits', 10\n"
            "    FROM wh GROUP BY 1, 2, WAREHOUSE_NAME\n") in _BOARD
    # the other panels are untouched
    assert "'DAILY_SPEND', 'CREDITS', NULL, DAY," in _SQL
    assert _SQL.count("'KPI', ") == 7
    # V041-R4 stage + atomic swap + freshness stamp all survive the re-derivation
    assert "SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE;" in _SQL
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" in _SQL
    assert "RETURN 'exec board refreshed (atomic swap)';" in _SQL


def test_column_contract_unchanged_so_the_app_needs_no_change():
    """The app reader is app/data/mart_sql.py exec_board() -> app/ui/pages/overview.py,
    which groups COST_DRIVER by DIMENSION and sums VALUE_USD. The new arm must fill the
    same 10-column INSERT list, with the driver kind carried in DIMENSION (the board has
    no KIND column and the contract has no room for one)."""
    assert _SQL.count(
        "(COMPANY, WINDOW_DAYS, PANEL, METRIC, DIMENSION, PERIOD_START, VALUE, VALUE_USD, "
        "UNIT, SORT_ORDER)") == 1
    # #12: the serverless/AI rows land on a DISTINCT panel so they never poison the
    # warehouse COST_DRIVER denominator / headline KPIs; the app reads them separately.
    assert ("SELECT SCOPE_COMPANY, WINDOW_DAYS, 'COST_DRIVER_SVC', 'CREDITS', DRIVER_LABEL, NULL,"
            in _SQL)
    assert "FROM sv GROUP BY 1, 2, DRIVER_LABEL;" in _SQL
    # a human can tell a serverless/AI driver from a warehouse on the bar chart
    assert "'Serverless: '" in _SQL and "'AI/Cortex: '" in _SQL
    assert "KIND" not in _SQL, "no invented column — the contract has no room for one"

    from app.data import mart_sql
    sql = mart_sql.exec_board("ALL", 30)
    for col in ("PANEL", "METRIC", "DIMENSION", "VALUE", "VALUE_USD", "UNIT", "SORT_ORDER"):
        assert col in sql, col
    assert "MART_EXEC_BOARD" in sql


def test_window_semantics_match_the_existing_arm():
    # same -365d read horizon as wh_daily/qh_daily/tk_daily (= max advertised window)
    assert _SQL.count("WHERE DAY >= DATEADD('day', -365, CURRENT_DATE())") == 4
    # same shared `windows` expansion (7/14/30/60/90/180/365)
    assert _SQL.count("JOIN windows w ON f.DAY >= DATEADD('day', -w.WINDOW_DAYS, CURRENT_DATE())") == 4
    assert "UNION ALL SELECT 180 UNION ALL SELECT 365" in _BOARD


def test_account_level_metering_lands_on_the_all_scope_only():
    """FACT_METERING_DAILY has no COMPANY column. Fanning account-level serverless/AI
    spend across the ALFA/Trexis pills would invent an attribution the source does not
    carry, so the new CTE deliberately does NOT join `scopes`."""
    assert "SELECT 'ALL' AS SCOPE_COMPANY, w.WINDOW_DAYS, f.DRIVER_LABEL" in _SQL
    assert _SQL.count("JOIN scopes s ON (s.COMPANY = 'ALL' OR f.COMPANY = s.COMPANY)") == 3
    # the V044 UNKNOWN pill still means "unmapped database/warehouse", not "unmappable"
    assert "UNION ALL SELECT 'UNKNOWN'" in _BOARD


def test_sv_daily_precedes_sv_in_the_cte_chain():
    assert _BOARD.index("    sv_daily AS (") < _BOARD.index("    sv AS (\n")


def test_v069_in_migration_registry():
    # V069 is registered in the admin contract. (The validate.sql floor is the MOVING tip,
    # owned by tests/test_perf_budgets.py::test_validate_matches_the_repo_tip — pinning it
    # here would fail the moment V070 lands.)
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 69 in _EXPECTED_MIGRATIONS
