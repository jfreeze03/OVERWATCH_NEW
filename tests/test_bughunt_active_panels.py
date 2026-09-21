"""Locks for the active-panel bug-hunt round (wf_9eabfa90 / v4.564): window-label consistency,
dynamic-table scope + current-condition status, object-ledger KPI reconciliation, QAS/SP captions.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW, LAST_MONTH_WINDOW
from app.data import ops_sql
from app.logic.date_windows import window_bounds, window_label, window_phrase

_ROOT = Path(__file__).resolve().parents[1]
_TODAY = datetime.date(2026, 9, 21)


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #3 window label
def test_window_label_distinguishes_the_three_calendar_presets():
    # the old `"last month" if bounds is not None` idiom labeled Current-month AND Current-year
    # "last month" too, because window_bounds returns non-None for ALL THREE presets.
    for w, exp in ((LAST_MONTH_WINDOW, "last month"),
                   (CURRENT_MONTH_WINDOW, "current month"),
                   (CURRENT_YEAR_WINDOW, "current year")):
        assert window_label(window_bounds(w, _TODAY), 30, _TODAY) == exp
    assert window_label(None, 30) == "30d"                 # trailing keeps the short served label
    assert window_label(None, 7) == "7d"


def test_window_phrase_forms():
    assert window_phrase(window_bounds(LAST_MONTH_WINDOW, _TODAY), 30, _TODAY) == "last month"
    assert window_phrase(window_bounds(CURRENT_MONTH_WINDOW, _TODAY), 30, _TODAY) == "the current month"
    assert window_phrase(None, 14) == "the last 14 days"


def test_no_hardcoded_last_month_idiom_remains_in_the_panels():
    # every panel must go through window_label/window_phrase, not the mislabeling idiom.
    for rel in ("app/ui/pages/operations.py", "app/ui/pages/cost_parts/optimize.py",
                "app/ui/pages/cost_parts/spend.py", "app/ui/pages/cost_parts/unit_costs.py",
                "app/ui/pages/cost_parts/ai_chargeback.py"):
        src = _read(rel)
        assert '"last month" if bounds is not None' not in src, rel
        assert "'last month' if bounds is not None" not in src, rel


# --------------------------------------------------------------------------- #4 + #7 dynamic tables
def test_dynamic_table_health_honors_scope():
    all_sql = ops_sql.dynamic_table_health(7)
    scoped = ops_sql.dynamic_table_health(7, "ALFA", "ALFA_EDW_PRD", "PUBLIC")
    assert "COMPANY_FOR_DATABASE" not in all_sql                    # ALL -> account-wide
    assert "COMPANY_FOR_DATABASE(DATABASE_NAME)" in scoped
    assert "ALFA_EDW_PRD" in scoped and "SCHEMA_NAME ILIKE" in scoped
    pytest.importorskip("sqlglot").parse_one(scoped, dialect="snowflake")


def test_dynamic_table_status_is_current_condition_not_any_failure():
    sql = ops_sql.dynamic_table_health(7)
    # current-condition status from the newest refresh, not "any failure in the window"
    assert "'STALE NOW'" in sql and "'RECOVERED'" in sql and "'HEALTHY'" in sql
    assert "'FAILED', 'SUCCEEDED'" not in sql                       # old overclaiming 2-state gone
    # the upstream/cancelled failure definition is preserved (test_ops_hunt invariant)
    assert "STATE IN ('FAILED', 'UPSTREAM_FAILED', 'CANCELLED')" in sql


# --------------------------------------------------------------------------- #2/#5/#6/#8 wiring
def test_object_attributed_kpi_excludes_residual_arm():
    src = _read("app/ui/pages/cost_parts/optimize.py")
    # the KPI value must exclude QUERY_COMPUTE_RESIDUAL so it matches its label + the footer
    assert '_obj_attr = float(_adf[_adf["COST_ARM"] != "QUERY_COMPUTE_RESIDUAL"]["USD"].sum())' in src
    assert 'format_usd(_obj_attr)' in src


def test_sp_breakdown_child_steps_excludes_call_overhead():
    src = _read("app/ui/pages/cost_parts/unit_costs.py")
    assert '_n_children = int((_bdf["STEP_TYPE"] != "CALL (own overhead)").sum())' in src


def test_qas_caption_is_conditional_on_qas_already_on():
    src = _read("app/ui/pages/cost_parts/optimize.py")
    assert "_qas_on = safe_float(_qrow.get(\"QAS_USD\")) > 0" in src
    assert "QAS is already enabled here" in src and "QAS is off here" in src


def test_pipeline_contract_reflects_scoped_volume_and_dt():
    src = _read("app/ui/pages/operations.py")
    # the stale "remain account-wide — a follow-up will scope them" claim is gone
    assert "remain account-wide — a follow-up will scope them" not in src
    assert "Dynamic-table refresh health honor Company/Database/Schema" in src
