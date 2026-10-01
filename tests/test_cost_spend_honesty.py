"""Cost Intelligence (Spend & Attribution, Compare, Unmapped entities): honesty fixes.

Each test names the review finding it locks and fails on the code it replaced.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.result import QueryResult
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


# --------------------------------------------- R1-150 / R1-151 / R1-152: Compare honesty ----
_KPIS: list[list[dict]] = []
_TABLES: list[pd.DataFrame] = []


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="stub")


def _fail(kind: str) -> QueryResult:
    return QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind, source="stub")


_EMPTY = _ok(pd.DataFrame())


def _wh(b_days: float = 30.0, b_credits: float = 80.0) -> QueryResult:
    return _ok(pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A"], "A_CREDITS": [100.0], "B_CREDITS": [b_credits],
        "A_DAYS": [30.0], "B_DAYS": [b_days], "TOTAL_A_CREDITS": [100.0], "TOTAL_B_CREDITS": [b_credits],
        "LOADED_THROUGH": ["2099-01-01"]}))


def _act(*sides: tuple) -> QueryResult:
    return _ok(pd.DataFrame([{"SIDE": sd, "QUERIES": q, "FAILS": f, "QUEUED_SEC": qu, "SPILL_REMOTE_GB": 0.0}
                             for sd, q, f, qu in sides]))


def _render_compare(monkeypatch, *, wh=None, act=None, bill=None, pat=None):
    from app.ui.pages.cost_parts import compare

    batch = {"wh": wh or _wh(),
             "act": act if act is not None else _act(("A", 1000.0, 25.0, 300.0), ("B", 900.0, 10.0, 200.0)),
             "bill": bill if bill is not None else _EMPTY, "pat": pat if pat is not None else _EMPTY}
    _KPIS.clear()
    _TABLES.clear()

    def _no_serial_read(*_a, **_k):
        raise AssertionError("no serial read expected: run_batch returns every key")

    monkeypatch.setattr(compare, "run_batch", lambda *a, **k: dict(batch))
    monkeypatch.setattr(compare, "run", _no_serial_read)
    monkeypatch.setattr(compare, "kpi_row", lambda items, *a, **k: _KPIS.append(list(items)))
    monkeypatch.setattr(compare, "styled_table", lambda df, *a, **k: _TABLES.append(df.copy()))

    def _app():
        from app.ui.pages.cost_parts import compare as _compare
        _compare._compare_tab("ALL", 3.0, 3.0)

    at = AppTest.from_function(_app, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def _kpi(label_start: str) -> dict:
    return next(k for k in (_KPIS[-1] if _KPIS else []) if k["label"].startswith(label_start))


def test_compare_fail_rate_never_fabricates_a_zero_b_side(monkeypatch):
    """R1-150: an A-only activity frame drew '+2.50 pts vs B' (red) against a B with no queries, beside a
    Queries card that said 'no B-side data'."""
    _render_compare(monkeypatch, act=_act(("A", 1000.0, 25.0, 300.0)))
    fr = _kpi("Fail rate")
    assert fr["value"] == "2.50%"
    assert fr["delta"] == "no B-side data" and fr["delta_color"] == "off"
    assert "0.00%" not in fr["help"] and "no queries" in fr["help"]
    assert _kpi("Queries")["delta"] == "no B-side data"
    assert _kpi("Queued")["delta"] == "no B-side data" and _kpi("Queued")["delta_color"] == "off"
    # the Volume shape table shows the absent side as a dash, never a fabricated 0
    vol = _TABLES[-1]
    assert list(vol["B"]) == ["—"] * 4 and vol["DELTA_PCT"].isna().all()


def test_compare_fail_rate_with_no_a_side_is_a_dash(monkeypatch):
    _render_compare(monkeypatch, act=_act(("B", 900.0, 10.0, 200.0)))
    fr = _kpi("Fail rate")
    assert fr["value"] == "—" and fr["delta"] == "no A-side queries" and fr["delta_color"] == "off"


def test_compare_a_loaded_zero_b_is_not_missing_data(monkeypatch):
    """R1-150: B loaded with 0s queued read 'no B-side data'."""
    _render_compare(monkeypatch, act=_act(("A", 1000.0, 25.0, 300.0), ("B", 900.0, 10.0, 0.0)))
    q = _kpi("Queued")
    assert q["delta"] == "up from 0 vs B" and q["delta_color"] == "inverse"
    fr = _kpi("Fail rate")
    assert fr["delta"] == f"{2.5 - 10 / 9:+.2f} pts vs B" and fr["delta_color"] == "inverse"


def test_compare_warehouse_spend_b_presence_comes_from_coverage(monkeypatch):
    _render_compare(monkeypatch, wh=_wh(b_days=0.0, b_credits=0.0))
    w = _kpi("Warehouse spend")
    assert w["delta"] == "no B-side data" and w["delta_color"] == "off"


def _errors(at) -> str:
    return " | ".join(str(e.value) for e in at.error)


def _infos(at) -> str:
    return " | ".join(str(e.value) for e in at.info)


@pytest.mark.parametrize("kind", ["timeout", "missing_column", "other"])
def test_compare_failed_pattern_read_is_unavailable_not_v037(monkeypatch, kind):
    """R1-151: any pattern-movers failure said 'need migration V037' (installed since v4.37) and hid the error."""
    at = _render_compare(monkeypatch, pat=_fail(kind))
    assert "V037" not in _infos(at) and "MART_PATTERN_COST_DAILY" not in _infos(at)
    assert f"boom ({kind})" in _errors(at)


def test_compare_absent_pattern_mart_is_needs_setup(monkeypatch):
    at = _render_compare(monkeypatch, pat=_fail("absent"))
    assert "MART_PATTERN_COST_DAILY" in _infos(at) and "boom" not in _errors(at)


def test_compare_failed_activity_and_billed_reads_are_named(monkeypatch):
    """R1-152: a failed act / bill read silently dropped its KPIs (and the Volume shape) with no message."""
    at = _render_compare(monkeypatch, act=_fail("timeout"), bill=_fail("timeout"))
    errs = _errors(at)
    assert "Query activity (FACT_QUERY_HOURLY) could not be read" in errs
    assert "Account billed credits (FACT_METERING_DAILY) could not be read" in errs
    assert [k["label"].split(" — ", 1)[0] for k in _KPIS[-1]] == ["Warehouse spend"]


def test_compare_empty_activity_says_so_under_volume_shape(monkeypatch):
    at = _render_compare(monkeypatch, act=_EMPTY)
    assert any("Volume shape" in str(m.value) for m in at.markdown)
    assert any("No query activity in either window yet" in str(c.value) for c in at.caption)


# ------------------------------------------------- R1-151 (optimize half): savings ledger ----
@pytest.mark.parametrize("kind", ["timeout", "missing_column", "other"])
def test_savings_ledger_failure_is_unavailable(monkeypatch, kind):
    from app.ui.pages.cost_parts import optimize
    from tests.test_probe_absence_split import _failed, _patch

    _fake, seen = _patch(monkeypatch, optimize, {"savings_ledger": _failed(kind)})
    optimize._savings_tab(3.0, {})
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "not installed" not in msg
    assert seen["detail"] == [f"boom ({kind})"]


@pytest.mark.parametrize("kind", ["absent", "privilege", "unknown_function"])
def test_savings_ledger_absence_is_needs_setup(monkeypatch, kind):
    from app.ui.pages.cost_parts import optimize
    from tests.test_probe_absence_split import _failed, _patch

    _fake, seen = _patch(monkeypatch, optimize, {"savings_ledger": _failed(kind)})
    optimize._savings_tab(3.0, {})
    assert [s for s, _m in seen["empty"]] == ["needs_setup"]
