"""v4.606.0 PR-1 (cluster c04-components): behaviour locks for the shared UI machinery fixes in
app/ui/components.py, app/ui/charts.py and app/ui/status_colors.py.

Each test drives the real function (with ``st`` stubbed where it renders) and fails on the
pre-fix code at 04fd374e:

  R1-211  delta_css painted a NULL (NaN) delta cell green/red.
  R1-213  run_mart_first stamped a calendar-bounded live read as 90 days (Current year ~3x high).
  R1-214  confirm_gate returned a click from a run whose typed text no longer matched.
  R1-215  the row-click seen-guards never re-armed, so a return-then-re-click did nothing.
  R1-216  spend_trend averaged/paced ROWS, not calendar days, and always dimmed the newest row.
"""

from __future__ import annotations

import contextlib
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core.result import QueryResult

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R1-211: a NULL delta has no direction
# ---------------------------------------------------------------------------

def test_delta_css_leaves_a_null_or_infinite_delta_uncolored():
    from app.ui.status_colors import delta_css
    # Styler.map hands a NULL delta in as float NaN (_coerce_object_numerics); a missing
    # prior (pct_delta -> None) is not "better" or "worse".
    assert delta_css(float("nan"), "DELTA_USD") == ""
    assert delta_css(float("nan"), "DELTA_HIT_PCT") == ""
    assert delta_css(math.inf, "DELTA_USD") == "" and delta_css(-math.inf, "DELTA_USD") == ""
    assert delta_css(pd.NA, "DELTA_USD") == "" and delta_css(None, "DELTA_USD") == ""
    # real movements keep their polarity colors
    assert delta_css(5.0, "DELTA_USD") != "" and delta_css(-5.0, "DELTA_USD") != ""
    assert delta_css(5.0, "DELTA_USD") != delta_css(-5.0, "DELTA_USD")


def test_delta_css_nan_cell_is_uncolored_in_the_rendered_styler():
    from app.ui.status_colors import delta_css
    df = pd.DataFrame({"A_USD": [500.0, 100.0], "DELTA_PCT": [float("nan"), 25.0]})
    ctx = df.style.map(lambda v: delta_css(v, "DELTA_PCT"), subset=["DELTA_PCT"])._compute().ctx
    assert not ctx.get((0, 1))           # the new-spend row's empty Δ% carries no color
    assert ctx.get((1, 1))               # a real +25% still does


# ---------------------------------------------------------------------------
# R1-213: a calendar-bounded read is stamped with its day SPAN on both legs
# ---------------------------------------------------------------------------

@pytest.fixture
def _mart_down_live_up(monkeypatch):
    """app.core.query.run stub: the mart probe ('<key>_fact') FAILS, the live twin answers."""
    from app.ui import components
    components._MART_FAIL_BACKOFF.clear()

    def _run(sql, *, page, key, tier, source, **kwargs):
        if key.endswith("_fact"):
            return QueryResult(df=pd.DataFrame(), ok=False, error="mart down")
        return QueryResult(df=pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "IDLE_CREDITS": [273.0]}), ok=True)

    monkeypatch.setattr("app.core.query.run", _run)
    yield
    components._MART_FAIL_BACKOFF.clear()


def _bounded(window: str, today: date) -> tuple[int, tuple[date, date], int]:
    from app.logic.date_windows import resolve_window_days, window_bounds
    days = resolve_window_days(window, today=today)
    bounds = window_bounds(window, today=today)
    assert bounds is not None
    return days, bounds, (bounds[1] - bounds[0]).days


@pytest.mark.parametrize(("window", "today", "span"), [
    ("CURRENT_YEAR", date(2026, 9, 30), 273),     # offset 272, the live SQL covers Jan 1 .. Sep 30
    ("CURRENT_MONTH", date(2026, 9, 2), 2),       # offset 1, the SQL covers Sep 1 .. Sep 2
    ("CURRENT_MONTH", date(2026, 9, 30), 30),
    ("LAST_MONTH", date(2026, 9, 30), 31),        # offset == span (unchanged)
])
def test_live_fallback_of_a_bounded_read_reports_the_bounds_span(_mart_down_live_up, window, today, span):
    from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW, LAST_MONTH_WINDOW
    from app.ui.components import run_mart_first, served_days
    sel = {"CURRENT_YEAR": CURRENT_YEAR_WINDOW, "CURRENT_MONTH": CURRENT_MONTH_WINDOW,
           "LAST_MONTH": LAST_MONTH_WINDOW}[window]
    days, bounds, got_span = _bounded(sel, today)
    assert got_span == span
    res = run_mart_first("MART", "LIVE", page="P", key=f"idle_{window}", days=days, bounds=bounds,
                         mart_source="m", live_source="l")
    assert res.df.attrs["_ow_served_live"] is True
    # the bounded builders never apply bounded_days' 90 cap, so the served window is the span --
    # not clamp_days(offset) (90 for Current year, 1 for Sep 2 MTD)
    assert served_days(res, span) == span


def test_bounded_idle_projection_is_not_inflated_on_the_live_leg(_mart_down_live_up):
    # The owner-visible number: $1/day of idle over Current year must project ~$30/mo, not ~$91.
    from app.config import CURRENT_YEAR_WINDOW
    from app.logic.insights import idle_waste_summary
    from app.ui.components import run_mart_first, served_days
    days, bounds, span = _bounded(CURRENT_YEAR_WINDOW, date(2026, 9, 30))
    res = run_mart_first("MART", "LIVE", page="P", key="idle_proj", days=days, bounds=bounds,
                         mart_source="m", live_source="l")
    frame = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "TOTAL_CREDITS": [float(span)],
                          "IDLE_CREDITS": [float(span)]})
    out = idle_waste_summary(frame, 1.0, served_days(res, span))
    assert out["PROJECTED_MONTHLY_USD"] == pytest.approx(30.0, rel=0.01)


def test_trailing_live_fallback_still_clamps_to_the_live_horizon(_mart_down_live_up):
    from app.ui.components import run_mart_first, served_days
    res = run_mart_first("MART", "LIVE", page="P", key="idle_365", days=365,
                         mart_source="m", live_source="l")
    assert served_days(res, 365) == 90           # K1 unchanged: no bounds -> the trailing live clamp


def test_idle_and_sizing_call_sites_pass_their_bounds():
    # The stamp can only be right where the caller says both builders honor the calendar bounds.
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    for key in ('key=f"idle_{company}_{days}{_lm}"', 'key=f"sizing_{company}_{days}{_lm}"',
                'key=f"remed_idle_{company}_{days}{_lm}"'):
        lines = [ln for ln in opt.splitlines() if f"page=_PAGE, {key}" in ln]   # the run_mart_first calls
        assert lines and all("days=days, bounds=bounds," in ln for ln in lines), key
    ops = _src("app/ui/pages/operations.py")
    assert 'key=f"ops_sizing_{company}_{days}{_lm}", days=days, bounds=bounds,' in ops


# ---------------------------------------------------------------------------
# R1-214: confirm_gate re-checks the typed text on the click's own run
# ---------------------------------------------------------------------------

class _GateSt:
    """Just enough of ``st`` for confirm_gate: the typed text, the click, and any warning."""

    def __init__(self, typed: str, clicked: bool):
        self.typed, self.clicked = typed, clicked
        self.disabled: bool | None = None
        self.warnings: list[str] = []

    def text_input(self, *_a, **_k):
        return self.typed

    def button(self, *_a, disabled=False, **_k):
        self.disabled = disabled
        return self.clicked          # Streamlit hands back the trigger even when rendered disabled

    def warning(self, msg, *_a, **_k):
        self.warnings.append(str(msg))


@pytest.mark.parametrize(("typed", "expected", "object_name", "enabled"), [
    ("WH_A", "WH_B", True, True),        # target changed under a still-typed old name
    ("WH_X", "WH_A", True, True),        # typed text edited, then clicked
    ("cancel", "CANCEL", False, True),   # an action verb stays exact-case
    ("WH_A", "WH_A", True, False),       # matched, but the entitlement gate is closed
])
def test_confirm_gate_never_fires_a_click_the_gate_did_not_authorize(monkeypatch, typed, expected,
                                                                      object_name, enabled):
    from app.ui import components
    fake = _GateSt(typed, clicked=True)
    monkeypatch.setattr(components, "st", fake)
    assert components.confirm_gate(expected, "Execute", key="remed", object_name=object_name,
                                   enabled=enabled) is False
    assert fake.disabled is True
    assert len(fake.warnings) == 1 and fake.warnings[0].startswith("Nothing ran")   # never a silent drop


def test_confirm_gate_fires_on_a_matching_click_and_stays_quiet_without_one(monkeypatch):
    from app.ui import components
    fake = _GateSt("wh_alfa_admin", clicked=True)
    monkeypatch.setattr(components, "st", fake)
    assert components.confirm_gate("WH_ALFA_ADMIN", "Execute", key="k", object_name=True) is True
    assert fake.warnings == []
    idle = _GateSt("WH_X", clicked=False)
    monkeypatch.setattr(components, "st", idle)
    assert components.confirm_gate("WH_A", "Execute", key="k", object_name=True) is False
    assert idle.warnings == []                    # no click -> no receipt


def _remed_gate_script():
    import streamlit as st

    from app.ui.components import confirm_gate
    wh = st.selectbox("Warehouse", ["WH_A", "WH_B"], key="remed_wh")
    if confirm_gate(wh, "Execute", key="remed", object_name=True):
        st.session_state["_p606_fired"] = [*st.session_state.get("_p606_fired", []), wh]


def test_confirm_gate_apptest_target_switch_and_click_in_one_rerun_runs_nothing():
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_function(_remed_gate_script, default_timeout=15)
    at.run()
    at.text_input(key="remed_confirm").input("WH_A")
    at.run()
    assert at.button(key="remed_btn").disabled is False         # WH_A typed: Execute enabled
    # the operator switches the target and clicks the still-enabled Execute before the redraw
    at.selectbox(key="remed_wh").select("WH_B")
    at.button(key="remed_btn").click()
    at.run()
    assert not at.exception
    assert "_p606_fired" not in at.session_state                # nothing ran against WH_B
    assert any("Nothing ran" in str(w.value) for w in at.warning)
    # control: typing the new target and clicking runs it exactly once
    at.text_input(key="remed_confirm").input("WH_B")
    at.run()
    at.button(key="remed_btn").click()
    at.run()
    assert at.session_state["_p606_fired"] == ["WH_B"]


# ---------------------------------------------------------------------------
# R1-215: an unselected render re-arms every row-click seen-guard
# ---------------------------------------------------------------------------

class _NavSt:
    """``st`` stand-in for the selection primitives: a plain-dict session_state and no-op layout."""

    def __init__(self):
        self.session_state: dict = {}

    def caption(self, *_a, **_k):
        return None

    def container(self, *_a, **_k):
        return contextlib.nullcontext()

    def columns(self, *_a, **_k):
        return contextlib.nullcontext(), contextlib.nullcontext()


def test_selectable_nav_table_fires_again_after_a_return_to_the_page(monkeypatch):
    from app.ui import components
    monkeypatch.setattr(components, "st", _NavSt())
    # click row 0 -> drill away (the table unmounts, Streamlit drops its selection) -> back, which
    # mounts unselected -> click row 0 again
    emitted = iter([0, None, 0])
    monkeypatch.setattr(components, "selectable_table", lambda *_a, **_k: next(emitted))
    fired: list[int] = []
    for _ in range(3):
        components.selectable_nav_table(pd.DataFrame({"A": [1, 2]}), key="ov_actions_sel",
                                        on_select=fired.append, hint="")
    assert fired == [0, 0]


def test_selectable_nav_table_still_ignores_a_sticky_reemitted_selection(monkeypatch):
    from app.ui import components
    monkeypatch.setattr(components, "st", _NavSt())
    emitted = iter([1, 1, 1])               # the same sticky index re-emitted on every rerun
    monkeypatch.setattr(components, "selectable_table", lambda *_a, **_k: next(emitted))
    fired: list[int] = []
    for _ in range(3):
        components.selectable_nav_table(pd.DataFrame({"A": [1, 2]}), key="k", on_select=fired.append, hint="")
    assert fired == [1]                     # the rerun-loop guard is intact


def test_master_detail_binds_the_row_clicked_after_a_return(monkeypatch):
    from app.ui import components
    monkeypatch.setattr(components, "st", _NavSt())
    frames = iter([pd.DataFrame({"FP": ["X", "Y"]}),     # click row 0 (X)
                   pd.DataFrame({"FP": ["Y", "X"]}),     # return: re-ranked, nothing selected
                   pd.DataFrame({"FP": ["Y", "X"]})])    # click row 0 again -- now Y
    emitted = iter([0, None, 0])
    shown: list[str] = []
    for _ in range(3):
        components.master_detail(next(frames), key="ac", id_col="FP",
                                 list_render_fn=lambda _df, _k: next(emitted),
                                 detail_render_fn=lambda row: shown.append(str(row["FP"])))
    # the return keeps the last bound item (identity persistence); the new click rebinds to Y
    assert shown == ["X", "X", "Y"]


def test_decision_rows_on_select_re_arms_after_an_unselected_render(monkeypatch):
    from app.ui import components
    monkeypatch.setattr(components, "st", _NavSt())
    emitted = iter([0, None, 0])
    monkeypatch.setattr(components, "selectable_table", lambda *_a, **_k: next(emitted))
    fired: list[int] = []
    frame = pd.DataFrame({"TITLE": ["a", "b"], "DETAIL": ["x", "y"]})
    for _ in range(3):
        components.decision_rows(frame, key="d", decision_col="TITLE", why_col="DETAIL",
                                 on_select=fired.append)
    assert fired == [0, 0]


def test_every_seen_guard_re_arms_before_it_checks():
    # pin the order the way test_v4153_watchlist_guard does for the watchlist
    comp = _src("app/ui/components.py")
    for fn, rearm, guard in (
        ("def selectable_nav_table", "st.session_state.pop(seen_key, None)",
         "sel != st.session_state.get(seen_key)"),
        ("def master_detail", "st.session_state.pop(seen_sel, None)\n        if sel is not None",
         "sel != st.session_state.get(seen_sel)"),
        ("def decision_rows", "st.session_state.pop(seen_key, None)",
         "selection != st.session_state.get(seen_key)"),
    ):
        body = comp.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert rearm in body and guard in body, fn
        assert body.index(rearm) < body.index(guard), fn


# ---------------------------------------------------------------------------
# R1-216: spend_trend's 7-day average / pace are calendar days; only today is partial
# ---------------------------------------------------------------------------

_TODAY = date(2026, 9, 30)


def _render_trend(monkeypatch, df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Run the real charts.spend_trend with st stubbed; return the frame it charted and its caption."""
    from app.ui import charts
    seen: dict = {}
    caps: list[str] = []
    real_layer = charts.alt.layer

    def _layer(*layers, **kw):
        if "data" in kw:
            seen["data"] = kw["data"].copy()
        return real_layer(*layers, **kw)

    monkeypatch.setattr(charts, "account_today", lambda: _TODAY, raising=False)
    monkeypatch.setattr(charts.alt, "layer", _layer)
    monkeypatch.setattr(charts.st, "altair_chart", lambda *a, **k: None)
    monkeypatch.setattr(charts.st, "caption", lambda msg, *a, **k: caps.append(str(msg)))
    charts.spend_trend(df)
    return seen["data"], caps[-1]


def _mwf_proc() -> pd.DataFrame:
    """A Mon/Wed/Fri proc (no row on other days): $100 per call-day, $200 from Mon Sep 21; last run Fri Sep 25."""
    days = [d for d in pd.date_range("2026-08-31", "2026-09-25") if d.dayofweek in (0, 2, 4)]
    return pd.DataFrame({"DAY": days, "USD": [200.0 if d >= pd.Timestamp("2026-09-21") else 100.0 for d in days]})


def test_spend_trend_paces_a_sparse_proc_by_calendar_week(monkeypatch):
    data, cap = _render_trend(monkeypatch, _mwf_proc())
    # calendar weeks anchored on Fri Sep 25: Sep 19-25 = $600 vs Sep 12-18 = $300 -> +100%
    # (the row-based pace read +29%: 7 rows of a Mon/Wed/Fri series span 2+ weeks)
    assert "pace +100% vs the prior week" in cap
    last = data.sort_values("Day").iloc[-1]
    assert last["AVG7"] == pytest.approx(600 / 7, abs=0.01)      # $85.71/day over 7 CALENDAR days
    # a Friday five days old is complete: nothing dimmed, no partial-day disclaimer
    assert not data["PROVISIONAL"].any()
    assert "Newest day is dimmed" not in cap


def test_spend_trend_last_month_window_has_no_partial_day(monkeypatch):
    days = pd.date_range("2026-08-01", "2026-08-31")
    df = pd.DataFrame({"DAY": days, "USD": [150.0 if d >= pd.Timestamp("2026-08-25") else 100.0 for d in days]})
    data, cap = _render_trend(monkeypatch, df)
    assert not data["PROVISIONAL"].any()                  # Aug 31 is a closed day on Sep 30
    assert "Newest day is dimmed" not in cap
    assert "pace +50% vs the prior week" in cap           # Aug 25-31 vs Aug 18-24


def test_spend_trend_dense_window_ending_today_still_dims_only_today(monkeypatch):
    days = pd.date_range("2026-09-01", "2026-09-30")
    df = pd.DataFrame({"DAY": days, "USD": [20.0 if d == pd.Timestamp("2026-09-30") else 100.0 for d in days]})
    data, cap = _render_trend(monkeypatch, df)
    assert data.loc[data["PROVISIONAL"], "Day"].dt.date.tolist() == [_TODAY]
    assert "pace +0% vs the prior week" in cap            # today's partial $20 stays out of the pace
    assert "Newest day is dimmed" in cap

