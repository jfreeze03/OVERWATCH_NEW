"""Cost Intelligence (Spend & Attribution, Compare, Unmapped entities): honesty fixes.

Each test names the review finding it locks and fails on the code it replaced.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.data import mart_sql
from tests._source import read

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


# ------------------------------------------------------------ R1-145: unmapped worklist window ----
@pytest.mark.parametrize("days", [60, 90, 180, 365])
def test_unmapped_worklist_serves_the_page_window_not_30d(days):
    """The builder clamped to 30 days while the panel's chip, 'in this window' KPI help, 'Est. $ (window)'
    column and green clean state claimed the full window. All three sources are FACT_* tables, so the mart
    window cap applies."""
    sql = mart_sql.unmapped_entities(days)
    assert sql.count(f"DATEADD('day', -{days}, CURRENT_DATE())") == 3
    assert "DATEADD('day', -30," not in sql
    assert "ACCOUNT_USAGE" not in sql                                   # still mart-only


def test_unmapped_worklist_honors_calendar_bounds():
    """Last month read a trailing 30 days (Aug 31 - Sep 30, one day of the labelled month)."""
    sql = mart_sql.unmapped_entities(31, bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    assert "DAY >= '2026-08-01' AND DAY < '2026-09-01'" in sql
    assert "HOUR_TS >= '2026-08-01' AND HOUR_TS < '2026-09-01'" in sql
    assert "DATEADD(" not in sql
    # the default (canary) shape is unchanged
    assert mart_sql.unmapped_entities(7).count("DATEADD('day', -7, CURRENT_DATE())") == 3


def test_cost_page_passes_the_window_bounds_to_the_worklist():
    cost = read("app/ui/pages/cost.py")
    assert 'mart_sql.unmapped_entities(f["days"], bounds=f["bounds"])' in cost
    assert "key=f\"unmapped_{f['days']}{_unm_b}\"" in cost


# ------------------------------------------------------------ R1-146: mapper picks a row + grain ----
_STATEMENTS: list[str] = []


def _mapper_app():
    import pandas as _pd

    from app.ui.pages import cost as _cost

    _cost._unmapped_mapper(_pd.DataFrame({
        "GRAIN": ["DATABASE", "USER", "WAREHOUSE"],
        "ENTITY": ["ANALYTICS", "SVC_ETL", "ANALYTICS"],
        "MEASURE": ["queries", "logins", "credits"],
        "VALUE": [120.0, 4.0, 85.5],
    }), True)


def _drive_mapper(monkeypatch, option_index: int):
    from app.ui.pages import cost

    _STATEMENTS.clear()

    def _record_statement(sql, *a, **k):
        _STATEMENTS.append(str(sql))
        return True, "ok"

    monkeypatch.setattr(cost, "execute_statement", _record_statement)
    at = AppTest.from_function(_mapper_app, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    entity = next(s for s in at.selectbox if s.label == "Entity")
    entity.set_value(option_index)   # the raw row index (AppTest re-formats it via format_func)
    at.run()
    at.button(key="unmap_apply").click()
    at.run()
    assert not at.exception, at.exception
    return at


def test_mapper_offers_each_row_with_its_grain(monkeypatch):
    at = _drive_mapper(monkeypatch, 0)
    entity = next(s for s in at.selectbox if s.label == "Entity")
    assert list(entity.options) == ["ANALYTICS · Database", "SVC_ETL · User", "ANALYTICS · Warehouse"]


def test_mapper_writes_the_warehouse_grain_when_the_warehouse_row_is_picked(monkeypatch):
    """Same-name DATABASE + WAREHOUSE rows: the old name picker could not select the second entry and
    the name lookup took the DATABASE row, so Apply MERGEd a DATABASE scope and the warehouse stayed
    UNKNOWN (billed blind)."""
    at = _drive_mapper(monkeypatch, 2)
    assert len(_STATEMENTS) == 1, _STATEMENTS
    assert "'WAREHOUSE' AS SCOPE_TYPE, 'ANALYTICS' AS PATTERN" in _STATEMENTS[0]
    assert "Classified via OVERWATCH (WAREHOUSE)" in _STATEMENTS[0]
    caps = " ".join(str(c.value) for c in at.caption)
    assert "Maps **ANALYTICS** (WAREHOUSE)" in caps


def test_mapper_user_row_maps_through_user_override(monkeypatch):
    _drive_mapper(monkeypatch, 1)
    assert "'USER_OVERRIDE' AS SCOPE_TYPE, 'SVC_ETL' AS PATTERN" in _STATEMENTS[0]


def test_mapper_latch_key_and_receipt_carry_the_grain():
    cost = read("app/ui/pages/cost.py")
    assert cost.count('f"unmap_apply:{scope_type}:{pick}:{company_choice}"') == 2
    assert 'f"Mapped {pick} ({scope_type}) → {company_choice}."' in cost
