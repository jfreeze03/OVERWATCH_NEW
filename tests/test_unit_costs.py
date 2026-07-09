"""Locks for v4.8.0 — Unit costs: measured $ per query, per CALL, per AI model.

The panel's contract: query/proc dollars are MEASURED (attribution credits,
idle excluded), the proc leaderboard covers EVERY procedure via ROOT_QUERY_ID
roll-up, and the database filter the user asked for actually reaches the SQL.
"""

from __future__ import annotations

from pathlib import Path

from app.data import cortex_sql, insights_sql

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Measured query costs
# ---------------------------------------------------------------------------

def test_measured_queries_use_attribution_not_allocation():
    sql = insights_sql.measured_query_costs(30)
    assert "QUERY_ATTRIBUTION_HISTORY" in sql
    assert "CREDITS_ATTRIBUTED_COMPUTE" in sql
    assert "WAREHOUSE_METERING_HISTORY" not in sql     # that's the allocated lens
    assert "att.CREDITS > 0" in sql                    # unpriced rows omitted, not zeroed


def test_measured_queries_honor_every_filter():
    sql = insights_sql.measured_query_costs(
        30, "ALFA", "ALFA_EDW_PRD", "EDW", "WH_ALFA", "JFREEZE")
    assert "ALFA_EDW_PRD" in sql                       # database filter (the user's ask)
    # contains-filters are LIKE-escaped: '_' arrives as '~_'
    assert "WH~_ALFA" in sql and "JFREEZE" in sql and "SCHEMA_NAME ILIKE" in sql
    trexis = insights_sql.measured_query_costs(30, "Trexis")
    assert "TRXS" in trexis                            # company via warehouse scope


def test_measured_queries_clamps():
    assert "-90," in insights_sql.measured_query_costs(999999)
    assert "LIMIT 200" in insights_sql.measured_query_costs(30, limit=99999)


# ---------------------------------------------------------------------------
# Procedure $/call leaderboard
# ---------------------------------------------------------------------------

def test_procedure_costs_roll_children_to_the_call():
    sql = insights_sql.procedure_costs_usd(30)
    assert "COALESCE(ROOT_QUERY_ID, QUERY_ID)" in sql  # children roll up to the CALL
    assert "QUERY_TYPE = 'CALL'" in sql
    assert "CALL[[:space:]]+" in sql                   # POSIX class: NO backslashes at any
    assert chr(92) not in sql                          # layer (the 'CALLs+' live bug, r3)
    assert "CREDITS_PER_CALL" in sql
    assert "ATTRIBUTED_CALLS" in sql                   # $0 rows stay visible + diagnosable
    assert "HAVING" not in sql


def test_procedure_costs_honor_db_and_schema_filters():
    sql = insights_sql.procedure_costs_usd(30, "ALFA", "ALFA_EDW_PRD", "OVERWATCH")
    assert "ALFA_EDW_PRD" in sql and "OVERWATCH" in sql
    unfiltered = insights_sql.procedure_costs_usd(30, "ALFA")
    assert "ALFA_EDW_PRD" not in unfiltered


def test_procedure_costs_reports_reliability_too():
    sql = insights_sql.procedure_costs_usd(30)
    assert "FAIL_PCT" in sql and "P95_S" in sql        # $ without reliability is half a story


# ---------------------------------------------------------------------------
# AI by function/model
# ---------------------------------------------------------------------------

def test_cortex_model_costs_shape():
    sql = cortex_sql.cortex_model_costs(30)
    assert "CORTEX_FUNCTIONS_USAGE_HISTORY" in sql
    assert "MODEL_NAME" in sql and "FUNCTION_NAME" in sql
    assert "CREDITS_PER_1M_TOKENS" in sql              # unit rate, not just totals
    assert "-30," in sql and "-90," in cortex_sql.cortex_model_costs(999999)


def test_cortex_source_costs_covers_code_billing():
    # This account's AI spend arrives via Cortex CODE (Snowsight/CLI) —
    # the fallback grain must read those views and price per 1M tokens.
    sql = cortex_sql.cortex_source_costs(30)
    assert "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY" in sql
    assert "CORTEX_CODE_CLI_USAGE_HISTORY" in sql
    assert "CREDITS_PER_1M_TOKENS" in sql


# ---------------------------------------------------------------------------
# Wiring: the section exists and dispatches
# ---------------------------------------------------------------------------

def test_cost_page_has_the_unit_costs_section():
    src = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    assert '"Unit costs"' in src.split("lazy_sections(")[1].split("key=")[0]
    assert 'elif section == "Unit costs":' in src
    assert "_unit_costs_tab(f, rate, ai_rate)" in src


def test_unit_costs_panel_declares_measurement_honesty():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert "idle" in src.lower()                       # measured-vs-allocated distinction
    assert "credits_to_usd" in src                     # dollarized through the one tested path
    assert "USD_PER_1M_TOKENS" in src
