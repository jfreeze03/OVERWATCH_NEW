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


def _tail(*, v4603: bool = False) -> pd.DataFrame:
    """The runtime tail. ``v4603``: WH_A's cancel fired at 600 s (below its 48h effective cap) and a
    Snowflake-managed serverless-task pool SHOW never lists ran statements too (#33 D4/D5)."""
    rows = []
    specs = [("WH_A", 5000, 250.0, 4000.0, {900: 12}, (600.0, 600.0) if v4603 else (None, None)),
             ("WH_B", 800, 40.0, 290.0, {}, (None, None))]
    if v4603:
        specs.append(("COMPUTE_SERVICE_WH_USER_TASKS_POOL_STANDARD_GEN1_XSMALL", 149986, 25.0, 930.0, {},
                      (None, None)))
    for name, runs, p99, mx, over, (lo, hi) in specs:
        r = {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "COMPLETED_RUNS": runs, "P99_ELAPSED_SEC": p99,
             "MAX_ELAPSED_SEC": mx, "TIMEOUT_CANCELLED_RUNS": 1, "TIMEOUT_FIRED_MIN_SEC": lo,
             "TIMEOUT_FIRED_MAX_SEC": hi, "TIMEOUT_CANCELLED_TOTAL": 2}
        r.update({f"RUNS_OVER_{s}": over.get(s, 0) for s in CAP_LADDER_S})
        rows.append(r)
    return pd.DataFrame(rows)


def _ops_recorder(monkeypatch, *, account: tuple[str, str] = ("172800", ""), tail_fails: bool = False,
                  v4603: bool = False) -> list[str]:
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
            return _show(*account)
        if "RUNS_OVER_300" in sql:
            if tail_fails:
                return QueryResult(df=pd.DataFrame(), ok=False, error="Statement reached its timeout",
                                   error_kind="timeout", source="t")
            return _ok(_tail(v4603=v4603))
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


def _sizing_lens(at, *, timeout_on: bool, company: str = "") -> None:
    at.run()
    assert not at.exception
    _nav_to(at, "Operations")
    if company:
        at.session_state["flt_company"] = company
    at.session_state["ops_section"] = "Warehouses"
    at.session_state["ops_wh_view"] = "Sizing & efficiency"
    if timeout_on:
        at.session_state["ops_wh_timeout_load"] = True
    at.run()
    assert not at.exception, f"warehouses sizing lens (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)


def _blob(at) -> str:
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption)


def _amount_input(at):
    """The verify form's amount widget -- keyed per item since review r3 (ledger_verified_usd_<ITEM_ID>)."""
    found = [n for n in at.number_input if str(n.key).startswith("ledger_verified_usd_")]
    assert len(found) == 1, [str(n.key) for n in at.number_input]
    return found[0]


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


def _card(blob: str, title: str) -> str:
    """One KPI card's HTML: from its title to the next card's title (or 3000 chars)."""
    at_title = blob.index(f'ow-card__title">{title}<')
    nxt = blob.find('ow-card__title">', at_title + 20)
    return blob[at_title:nxt if nxt > 0 else at_title + 3000]


@_SKIP
def test_statement_timeout_account_zero_reads_as_the_seven_day_max(monkeypatch):
    """Review C15/C20: ALTER ACCOUNT SET STATEMENT_TIMEOUT_IN_SECONDS = 0 enforces the 7-day maximum; the
    'Account value' tile shows 168h (and says why), never '0s' beside the uncapped caption."""
    _ops_recorder(monkeypatch, account=("0", "ACCOUNT"))
    at = AppTest.from_function(_entry, default_timeout=30)
    _sizing_lens(at, timeout_on=True)
    blob = _blob(at)
    card = _card(blob, "Account value")
    assert "168h" in card and "0 = 7-day max" in card and ">0s<" not in card, card
    assert "The account value is also 48 hours or more" in blob


@_SKIP
def test_statement_timeout_company_scope_with_a_failed_tail_is_unavailable(monkeypatch):
    """Review C16: under a company scope the warehouse list IS the runtime tail; when that read fails the
    panel says the read failed (the red 'unavailable' state), not that the scope returned none."""
    _ops_recorder(monkeypatch, tail_fails=True)
    at = AppTest.from_function(_entry, default_timeout=30)
    _sizing_lens(at, timeout_on=True, company="ALFA")
    errors = " ".join(str(e.value) for e in at.error)
    assert "completed-runtime tail could not be read, so this company's warehouses are unknown" in errors
    assert "returned none" not in _blob(at)


@_SKIP
def test_statement_timeout_managed_compute_and_fired_below_cap_name_their_cause(monkeypatch):
    """#33 D4/D5 on the rendered panel: a COMPUTE_SERVICE_WH* pool SHOW never lists is Managed compute (its own
    caption, not the 'dropped, renamed, or not visible' one), and a cancel that fired below the effective cap
    names every lower ceiling that can fire (a user, session, client or task value, or an earlier, lower
    warehouse or account value); the Timed-out help no longer calls cancels 'caps that
    already fired'."""
    _ops_recorder(monkeypatch, v4603=True)
    at = AppTest.from_function(_entry, default_timeout=30)
    _sizing_lens(at, timeout_on=True)
    blob = _blob(at)
    assert "1 Snowflake-managed compute pool(s) (COMPUTE_SERVICE_WH*: serverless-task and upgrade pools)" in blob
    assert "USER_TASK_TIMEOUT_MS. Shown as Managed compute." in blob
    assert "ran statements in the window but SHOW WAREHOUSES does not list them" not in blob
    assert "Timed out below the effective cap on WH_A:" in blob
    assert "a user, session, client or task value, or an earlier, lower warehouse or account value" in blob
    assert "caps that already fired" not in blob
    assert "at whichever ceiling was lowest for that statement" in _card(blob, "Timed out (30d)")


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
    assert _amount_input(at).value == pytest.approx(expected)
    # review C12/C17: the sentinel records the item and the amount OVERWATCH left in the widget
    assert at.session_state["_ow_ledger_prefill"] == {"item": "abcdef12-0000-4000-8000-000000000001",
                                                      "val": expected}
    # an operator's edit survives the next rerun (and a moved measurement: see the C17 tests below)
    _amount_input(at).set_value(1234.0)
    at.run()
    assert _amount_input(at).value == 1234.0
    code = "\n".join(str(c.value) for c in at.code)
    assert "PROOF_RESULT = '" in code and "PROOF_RUN_AT = CURRENT_TIMESTAMP()" in code
    assert "AND STATE = 'ESTIMATED';" in code


# ---------------------------------------------------------------------------
# PR C review r1 (C12 / C17 / C22): the measured-verify prefill on the rendered page
# ---------------------------------------------------------------------------

class _LedgerPage:
    """Cost ▸ Remediation & ledger with a crafted ledger + a MUTABLE before/after frame (so a test can move
    the measurement between reruns, as the daily FACT_WAREHOUSE_DAILY load does), an operator viewer and a
    recording statement runner."""

    def __init__(self, monkeypatch, ledger: pd.DataFrame | None = None) -> None:
        import app.ui.pages.cost_parts.optimize as opt

        self.ledger = _estimated_schedule_row() if ledger is None else ledger
        self.measure = _measurement()
        self.written: list[str] = []

        def _run(*args, **kwargs):
            key = str(kwargs.get("key") or "")
            if key == "savings_ledger":
                return _ok(self.ledger)
            if key.startswith("ledger_measure_"):
                return _ok(self.measure.copy())
            return _shaped_run(*args, **kwargs)

        def _record(sql, **_kwargs):
            self.written.append(str(sql))
            return True, "ok"

        monkeypatch.setattr(opt, "run", _run)
        monkeypatch.setattr(opt, "_is_operator", lambda: True)
        monkeypatch.setattr(opt, "write_gate_open", lambda *_a, **_k: True)
        monkeypatch.setattr(opt, "execute_statement", _record)
        self.at = AppTest.from_function(_entry, default_timeout=30)

    def open(self):
        at = self.at
        at.run()
        assert not at.exception
        _nav_to(at, "Cost Intelligence")
        at.session_state["cost_section"] = "Optimization & Savings"
        at.session_state["opt_section"] = "Remediation & ledger"
        at.run()
        self.check()
        return at

    def check(self) -> None:
        assert not self.at.exception, f"savings ledger verify (shaped): {self.at.exception}"
        assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in self.at.error)

    def amount(self) -> float:
        return float(_amount_input(self.at).value)

    def update_sql(self) -> str:
        shown = [str(c.value) for c in self.at.code if "SET STATE = 'VERIFIED'" in str(c.value)]
        assert len(shown) == 1, shown
        return shown[0]

    def verify_updates(self) -> list[str]:
        return [s for s in self.written if "SET STATE = 'VERIFIED'" in s]


def _prefill_for(after_credits: float) -> float:
    # booked Aug 1: 14 days before at 100 credits/day; after = Aug 2..Aug 31 (booking + 30), at 3.68
    return round((1400.0 / 14 - after_credits / 30) * 30 * 3.68, 2)


@_SKIP
def test_ledger_prefill_rearms_after_leaving_and_returning(monkeypatch):
    """C12: leaving Remediation & ledger drops the number_input's widget state; the prefill must re-arm on
    the way back instead of rendering 0 under a 'Measured saving / mo' KPI that says it was prefilled."""
    page = _LedgerPage(monkeypatch)
    at = page.open()
    expected = _prefill_for(1350.0)
    assert page.amount() == pytest.approx(expected)
    at.session_state["opt_section"] = "Idle & sizing"
    at.run()
    page.check()
    at.session_state["opt_section"] = "Remediation & ledger"
    at.run()
    page.check()
    assert page.amount() == pytest.approx(expected)
    assert f"VERIFIED_USD = {expected}" in page.update_sql()


@_SKIP
def test_ledger_prefill_never_overwrites_an_edit_when_the_measurement_moves(monkeypatch):
    """C17: an operator's typed amount survives the measurement moving (the daily load landing), both on
    the next render and in the UPDATE the Verify click executes."""
    page = _LedgerPage(monkeypatch)
    at = page.open()
    _amount_input(at).set_value(1234.0)
    at.run()
    page.check()
    assert page.amount() == 1234.0
    page.measure.loc[0, "AFTER_CREDITS"] = 1200.0                 # the load lands: a new after-window
    at.run()
    page.check()
    assert page.amount() == 1234.0
    shown = page.update_sql()
    assert "VERIFIED_USD = 1234.0" in shown
    caps = " ".join(str(c.value) for c in at.caption)
    assert "your entry is kept" in caps, caps[-800:]
    at.button(key="ledger_verify_exec").click()
    at.run()
    page.check()
    assert page.verify_updates() == [shown]


@_SKIP
def test_ledger_verify_click_writes_exactly_what_was_shown_when_the_measurement_moves(monkeypatch):
    """C17: the measurement moves between the render and the Verify click (untouched prefill). The click's
    rerun must write the UPDATE st.code showed -- the same VERIFIED_USD and the same PROOF_RESULT --
    never a silently re-prefilled figure."""
    page = _LedgerPage(monkeypatch)
    at = page.open()
    shown = page.update_sql()
    assert f"VERIFIED_USD = {_prefill_for(1350.0)}" in shown
    page.measure.loc[0, "AFTER_CREDITS"] = 1200.0                 # moves after the render, before the click
    at.button(key="ledger_verify_exec").click()
    at.run()
    page.check()
    assert page.verify_updates() == [shown]
    # the click is spent: the next render re-measures and moves the untouched prefill to the new figure
    at.run()
    page.check()
    assert page.amount() == pytest.approx(_prefill_for(1200.0))


def _other_row(item_id: str, *, finding: str, source: str, state: str, created: str,
               target: str | None = "WH_A", change_wh: str | None = None, superseded: str | None = None,
               description: str = "Other change") -> pd.DataFrame:
    df = _estimated_schedule_row()
    df["ITEM_ID"] = item_id
    df["DESCRIPTION"] = description
    df["STATE"] = state
    df["SOURCE"] = source
    df["FINDING_TYPE"] = finding
    df["TARGET_OBJECT"] = target
    df["CREATED_AT"] = pd.Timestamp(created)
    df["SUPERSEDED_BY_CHANGE_ID"] = superseded
    if "CHANGE_WAREHOUSE" in df.columns:
        df["CHANGE_WAREHOUSE"] = change_wh
    return df


@_SKIP
def test_ledger_verify_is_not_prefilled_when_another_change_on_the_warehouse_overlaps(monkeypatch):
    """C22: a settled AUTO_SUSPEND change on WH_A three days after this SCHEDULE booking sits inside its
    measured window: the warehouse delta is not this item's alone, so the amount is not prefilled and the
    overlapping item is named."""
    ledger = pd.concat([
        _other_row("feedface-0000-4000-8000-00000000000a", finding="AUTO_SUSPEND", source="auto",
                   state="VERIFIED", created="2026-08-04 07:00", target=None, change_wh="WH_A",
                   description="Detected AUTO_SUSPEND change on WH_A: 600 -> 60"),
        _estimated_schedule_row(),
    ], ignore_index=True)
    page = _LedgerPage(monkeypatch, ledger)
    at = page.open()
    assert page.amount() == 0.0
    assert "VERIFIED_USD = 0.0" in page.update_sql()
    warns = " ".join(str(w.value) for w in at.warning)
    assert "feedface" in warns and "split" in warns, warns[:800]
    # the overlap is recorded in the proof a hand-entered verify stamps
    assert "overlapping_items" in page.update_sql()
