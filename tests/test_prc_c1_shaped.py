"""PR C slice C1 on the RENDERED pages (AppTest, streamlit >= 1.55 like the rest of the shaped harness):

  (a) Operations ▸ Warehouses ▸ Sizing & efficiency with the statement-timeout toggle ON paints the posture
      panel and a review-only script that tightens ONLY the uncapped warehouse (Next-Fifty #33);
  (b) the same lens with the toggle left OFF issues no SHOW PARAMETERS and no runtime-tail read;
  (c) Cost ▸ Optimization & Savings ▸ Idle & sizing with the sizing toggle ON paints the split
      add-a-cluster / size-up caption (#38);
  (d) Cost ▸ Remediation & ledger: an ESTIMATED schedule item paints its measured before/after and the
      verify amount is prefilled from it (#46(d)).

The floor venv skips these (_APPTEST_BUTTONGROUP_OK); tests/test_stmt_timeout.py, tests/test_ledger_measure.py
and tests/test_sizing_scale_split.py lock the same wiring by source and pure behaviour."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_from_sql,
    _shaped_mart_first,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from app.data import mart_sql
from app.logic.stmt_timeout import CAP_LADDER_S

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="t")


def _show(value: str, level: str) -> QueryResult:
    return _ok(pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": value, "default": "172800",
                              "level": level, "description": "", "type": "NUMBER"}]))


def _tail() -> pd.DataFrame:
    rows = []
    for name, runs, p99, mx, over in (("WH_A", 5000, 250.0, 4000.0, {900: 12}),
                                      ("WH_B", 800, 40.0, 290.0, {})):
        r = {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "COMPLETED_RUNS": runs, "P99_ELAPSED_SEC": p99,
             "MAX_ELAPSED_SEC": mx, "TIMEOUT_CANCELLED_RUNS": 1, "TIMEOUT_CANCELLED_TOTAL": 2}
        r.update({f"RUNS_OVER_{s}": over.get(s, 0) for s in CAP_LADDER_S})
        rows.append(r)
    return pd.DataFrame(rows)


def _ops_recorder(monkeypatch) -> list[str]:
    """Record every SQL Operations issues; answer the timeout reads with crafted frames."""
    import app.ui.pages.operations as ops

    seen: list[str] = []

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        seen.append(sql)
        if sql.startswith("SHOW WAREHOUSES"):
            return _ok(pd.DataFrame({"name": ["WH_A", "WH_B"], "size": ["Small", "Small"]}))
        if sql.endswith("IN WAREHOUSE WH_A"):
            return _show("172800", "")
        if sql.endswith("IN WAREHOUSE WH_B"):
            return _show("300", "WAREHOUSE")
        if sql.endswith("IN ACCOUNT"):
            return _show("172800", "")
        if "RUNS_OVER_300" in sql:
            return _ok(_tail())
        return _shaped_run(*args, **kwargs)

    def _batch(specs, **kwargs):
        seen.extend(str(s.get("sql", "")) for s in (specs or []))
        return _shaped_batch(specs, **kwargs)

    def _mart_first(mart, live="", **kwargs):
        seen.extend([str(mart), str(live)])
        return _shaped_mart_first(mart, live, **kwargs)

    monkeypatch.setattr(ops, "run", _run)
    monkeypatch.setattr(ops, "run_batch", _batch)
    monkeypatch.setattr(ops, "run_batch_mixed", _batch)
    monkeypatch.setattr(ops, "run_mart_first", _mart_first)
    return seen


def _sizing_lens(at, *, timeout_on: bool) -> None:
    at.run()
    assert not at.exception
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Warehouses"
    at.session_state["ops_wh_view"] = "Sizing & efficiency"
    if timeout_on:
        at.session_state["ops_wh_timeout_load"] = True
    at.run()
    assert not at.exception, f"warehouses sizing lens (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)


def _blob(at) -> str:
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption)


@_SKIP
def test_statement_timeout_posture_renders_and_scripts_only_the_uncapped_warehouse(monkeypatch):
    seen = _ops_recorder(monkeypatch)
    at = AppTest.from_function(_entry, default_timeout=30)
    _sizing_lens(at, timeout_on=True)
    blob = _blob(at)
    assert "Statement-timeout posture" in blob
    assert 'ow-card__title">Uncapped<' in blob and 'ow-card__title">Account value<' in blob
    code = "\n".join(str(c.value) for c in at.code)
    assert "ALTER WAREHOUSE WH_A SET STATEMENT_TIMEOUT_IN_SECONDS = 900;" in code
    assert "-- undo: ALTER WAREHOUSE WH_A UNSET STATEMENT_TIMEOUT_IN_SECONDS;" in code
    assert "WH_B SET STATEMENT_TIMEOUT_IN_SECONDS" not in code          # capped at 300s: never loosened
    assert "Review only" in blob
    assert any(s.endswith("IN WAREHOUSE WH_A") for s in seen) and any(s.endswith("IN ACCOUNT") for s in seen)


@_SKIP
def test_statement_timeout_reads_wait_for_the_toggle(monkeypatch):
    seen = _ops_recorder(monkeypatch)
    at = AppTest.from_function(_entry, default_timeout=30)
    _sizing_lens(at, timeout_on=False)
    assert "Statement-timeout posture" in _blob(at)                      # the header paints before the toggle
    assert seen, "the recorder saw no reads: the lens did not render"
    assert not any("SHOW PARAMETERS" in s for s in seen)
    assert not any("RUNS_OVER_" in s for s in seen)
    assert not at.code or all("STATEMENT_TIMEOUT_IN_SECONDS" not in str(c.value) for c in at.code)


@_SKIP
def test_cost_optimize_sizing_profile_renders_shaped():
    """Next-Fifty #38: Cost > Optimization & Savings > Idle & sizing with the heavy sizing toggle ON
    paints the split verdict caption (add-a-cluster / size-up) from the shaped profile."""
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_section"] = "Optimization & Savings"
    at.session_state["opt_section"] = "Idle & sizing"
    at.session_state["sizing_load"] = True
    at.run()
    assert not at.exception, f"cost optimize sizing (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    caps = " ".join(str(c.value) for c in at.caption)
    assert "add-a-cluster" in caps and "size-up" in caps, caps[:500]


def _estimated_schedule_row() -> pd.DataFrame:
    df = _shaped_from_sql(mart_sql.savings_ledger()).df.head(1).copy()
    df["ITEM_ID"] = "abcdef12-0000-4000-8000-000000000001"
    df["DESCRIPTION"] = "Suspend schedule on WH_A"
    df["STATE"] = "ESTIMATED"
    df["SOURCE"] = "manual"
    df["FINDING_TYPE"] = "SCHEDULE"
    df["TARGET_OBJECT"] = "WH_A"
    df["CREATED_AT"] = pd.Timestamp("2026-08-01 09:00")
    df["PROOF_SQL"] = "SELECT 1"
    df["SUPERSEDED_BY_CHANGE_ID"] = None
    return df


def _measurement() -> pd.DataFrame:
    return pd.DataFrame([{"BASIS": "WAREHOUSE", "BOOKED_DAY": date(2026, 8, 1), "BEFORE_DAYS": 14,
                          "MAX_AFTER_DAYS": 30, "BEFORE_CREDITS": 1400.0, "AFTER_CREDITS": 1350.0,
                          "BEFORE_QUERIES": 14000.0, "AFTER_QUERIES": 30000.0,
                          "LOADED_THROUGH": date(2026, 9, 27)}])


@_SKIP
def test_ledger_verify_prefills_the_measured_saving(monkeypatch):
    import app.ui.pages.cost_parts.optimize as opt

    def _run(*args, **kwargs):
        key = str(kwargs.get("key") or "")
        if key == "savings_ledger":
            return _ok(_estimated_schedule_row())
        if key.startswith("ledger_measure_"):
            assert "FACT_WAREHOUSE_DAILY" in str(args[0])
            return _ok(_measurement())
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_section"] = "Optimization & Savings"
    at.session_state["opt_section"] = "Remediation & ledger"
    at.run()
    assert not at.exception, f"savings ledger verify (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    blob = _blob(at)
    assert 'ow-card__title">Measured saving / mo<' in blob and "Volume ×" in blob
    # booked Aug 1: after window = Aug 2..Aug 31 (booking + 30), 45 credits/day vs 100 before, at 3.68
    expected = round((100.0 - 1350.0 / 30) * 30 * 3.68, 2)
    assert at.number_input(key="ledger_verified_usd").value == pytest.approx(expected)
    assert at.session_state["_ow_ledger_prefill_sig"] == f"abcdef12-0000-4000-8000-000000000001|{expected:.2f}"
    # an operator's edit survives the next rerun (the sentinel only fires when the measurement changes)
    at.number_input(key="ledger_verified_usd").set_value(1234.0)
    at.run()
    assert at.number_input(key="ledger_verified_usd").value == 1234.0
    code = "\n".join(str(c.value) for c in at.code)
    assert "PROOF_RESULT = '" in code and "PROOF_RUN_AT = CURRENT_TIMESTAMP()" in code
    assert "AND STATE = 'ESTIMATED';" in code
