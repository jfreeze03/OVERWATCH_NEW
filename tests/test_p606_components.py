"""v4.606.0 PR-1 (cluster c04-components): behaviour locks for the shared UI machinery fixes in
app/ui/components.py, app/ui/charts.py and app/ui/status_colors.py.

Each test drives the real function (with ``st`` stubbed where it renders) and fails on the
pre-fix code at 04fd374e:

  R1-211  delta_css painted a NULL (NaN) delta cell green/red.
  R1-213  run_mart_first stamped a calendar-bounded live read as 90 days (Current year ~3x high);
          review r1: the Spend CS statement-type caption said "Scanned 90d of the 272d window".
  R1-214  confirm_gate returned a click from a run whose typed text no longer matched.
  R1-215  the row-click seen-guards never re-armed, so a return-then-re-click did nothing.
  R1-216  spend_trend averaged/paced ROWS, not calendar days, and always dimmed the newest row.
  R1-217  bar_count's takeaway printed a share of a SUM of per-warehouse averages.
  R1-218  section-count badges keyed on the day count only, so Last month reused the 30d count.
  R1-219  the display-timezone pass blanked NEVER_READ (bool) and turned raw *_TIME numbers into 1970.
  R1-220  Add to Case exported NULL cells as the literal text nan / None / NaT / <NA>.
  R1-221  a numeric cell of +/-inf crashed the page from inside the table's lazy Styler render.
  R1-222  a >400-row table's Hr/Min/Sec duration column sorted as text with no word of it.
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
    # review r1 (R1-213 sibling): both Spend cloud-services statement-type reads -- the
    # account-wide one and the per-warehouse drill -- pass their bounds too.
    spend = _src("app/ui/pages/cost_parts/spend.py")
    for key in ('key=f"cs_types_{company}_{days}_{_sel_wh}"', 'key=f"cs_types_{company}_{days}"'):
        assert f"page=_PAGE, {key}, bounds=bounds," in spend, key


def _render_cs_types(monkeypatch, *, days: int, bounds, mart_up: bool):
    """Render the real Spend tab (AppTest) with one ELEVATED warehouse, so the cloud-services
    statement-type panel paints. The cs_types mart leg fails (or answers, ``mart_up``); the live
    twin answers and its SQL is recorded. Every other read is an empty-but-ok stub."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui import components
    from app.ui.pages.cost_parts import spend

    components._MART_FAIL_BACKOFF.clear()
    empty = QueryResult(df=pd.DataFrame(), ok=True, source="stub")
    types = pd.DataFrame({"QUERY_TYPE": ["SHOW", "DESCRIBE"], "QUERIES": [900, 400],
                          "CS_CREDITS": [3.0, 1.0], "CS_CREDITS_PER_1K": [3.33, 2.5]})
    live_sql: list[str] = []

    def _run(sql, *_a, **kwargs):
        key = str(kwargs.get("key", ""))
        if key.startswith("cs_types_") and key.endswith("_fact"):
            if mart_up:
                return QueryResult(df=types.copy(), ok=True, source="mart stub")
            return QueryResult(df=pd.DataFrame(), ok=False, error="mart down")
        if key.startswith("cs_types_"):
            live_sql.append(str(sql))
            return QueryResult(df=types.copy(), ok=True, source="live stub")
        return empty

    monkeypatch.setattr("app.core.query.run", _run)        # run_mart_first's own reads
    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "run_batch", lambda specs, **_k: {s["key"]: empty for s in specs})
    monkeypatch.setattr(spend, "load_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(spend, "can_open", lambda _page: True)
    monkeypatch.setattr(spend, "request_navigation", lambda *_a, **_k: None)
    # the tab returns early on an empty metering read, so hand it one warehouse-metering day
    metering = QueryResult(df=pd.DataFrame({
        "DAY": [date(2026, 9, 29)], "SERVICE_TYPE": ["WAREHOUSE_METERING"], "CREDITS_USED": [100.0],
        "CREDITS_BILLED": [100.0], "CREDITS_ADJUSTMENT": [0.0]}), ok=True, source="metering stub")
    csr = QueryResult(df=pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"], "COMPUTE_CREDITS": [70.0],
        "CLOUD_SVC_CREDITS": [30.0], "TOTAL_CREDITS": [100.0], "CLOUD_SVC_PCT": [30.0],
        "STATUS": ["ELEVATED"]}), ok=True, source="csr stub")
    # AppTest runs the function's SOURCE as a script (no closures): hand the inputs over through a
    # module attribute the script imports (the tests/test_spend_grain_coverage.py pattern).
    monkeypatch.setattr(spend, "_CS_TYPES_TEST_ARGS", {
        "days": days, "bounds": bounds,
        "pre": {"metering_res": metering, "csr_res": csr, "coco_res": empty, "allin_res": empty,
                "napp_res": empty, "csfam_res": empty}}, raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _a = _spend._CS_TYPES_TEST_ARGS
        _spend._spend_tab("ALL", _a["days"], 3.0, 3.0, bounds=_a["bounds"], **_a["pre"])

    try:
        at = AppTest.from_function(_app, default_timeout=60)
        at.run()
    finally:
        components._MART_FAIL_BACKOFF.clear()
    assert not at.exception, at.exception
    cs_caps = [str(c.value) for c in at.caption if "Metadata storms show up here" in str(c.value)]
    assert len(cs_caps) == 1, "the statement-type panel did not render"
    return cs_caps[0], live_sql


@pytest.mark.parametrize("mart_up", [False, True])
def test_cs_statement_types_caption_reports_no_clamp_for_a_whole_year_scan(monkeypatch, mart_up):
    # Current year on 2026-09-30: day offset 272, bounds Jan 1 .. Oct 1 (a 273-day range). Both legs
    # scan the whole range; the old caption said "Scanned 90d of the 272d window" on the live leg.
    from app.config import CURRENT_YEAR_WINDOW
    days, bounds, span = _bounded(CURRENT_YEAR_WINDOW, date(2026, 9, 30))
    assert (days, span) == (272, 273)
    cap, live_sql = _render_cs_types(monkeypatch, days=days, bounds=bounds, mart_up=mart_up)
    if not mart_up:
        assert live_sql and "START_TIME >= '2026-01-01' AND START_TIME < '2026-10-01'" in live_sql[0]
    # no false clamp disclosure, and no "273d of the 272d window" from comparing the span to the offset
    assert "Scanned" not in cap, cap


def test_cs_statement_types_caption_still_discloses_the_trailing_live_clamp(monkeypatch):
    # K1 unchanged: a trailing 365d window served by the 90d-clamped live scan says so.
    cap, live_sql = _render_cs_types(monkeypatch, days=365, bounds=None, mart_up=False)
    assert live_sql
    assert "Scanned 90d of the 365d window (the live fallback caps its scan)." in cap


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
    # Streamlit <= 1.58 still delivers the click to a button that this run renders disabled, so
    # confirm_gate must refuse it and say so; newer Streamlit (1.64, CI's lint-and-test leg) drops
    # that click itself. Either way nothing runs, and a click that DID arrive always gets the receipt.
    if at.button(key="remed_btn").value:
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


# ---------------------------------------------------------------------------
# R1-217: a rate metric's takeaway names the top bar without a share of a sum of rates
# ---------------------------------------------------------------------------

def _contention_frame() -> pd.DataFrame:
    """Operations > Warehouses contention's derivation (operations.py: AVG_QUEUE_SEC = QUEUED_SEC / QUERY_COUNT)."""
    pdf = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A", "WH_B", "WH_C"], "QUEUED_SEC": [1200.0, 50.0, 880.0],
                        "QUERY_COUNT": [100, 5, 100]})
    pdf["AVG_QUEUE_SEC"] = pdf["QUEUED_SEC"] / pdf["QUERY_COUNT"].replace(0, pd.NA)
    return pdf.sort_values("AVG_QUEUE_SEC", ascending=False)


def _bar_caps(monkeypatch, *args, **kwargs) -> list[str]:
    from app.ui import charts
    caps: list[str] = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda *a, **k: None)
    monkeypatch.setattr(charts.st, "caption", lambda msg, *a, **k: caps.append(str(msg)))
    charts.bar_count(*args, **kwargs)
    return caps


def test_bar_count_rate_takeaway_has_no_share_of_a_sum_of_averages(monkeypatch):
    caps = _bar_caps(monkeypatch, _contention_frame(), "WAREHOUSE_NAME", "AVG_QUEUE_SEC",
                     title="Average queue per query", takeaway=True, unit="sec", additive=False,
                     value_fmt=",.1f")
    assert caps == ["Top: WH_A 12s."]                 # was 'Top: WH_A 12s (39% of 31s).'


def test_bar_count_additive_takeaway_keeps_its_share(monkeypatch):
    df = pd.DataFrame({"TASK": ["T1", "T2"], "FAILURES": [3, 1]})
    caps = _bar_caps(monkeypatch, df, "TASK", "FAILURES", takeaway=True)
    assert caps == ["Top: T1 3 (75% of 4)."]            # a count still sums: unchanged default


def test_contention_chart_declares_its_average_non_additive():
    ops = _src("app/ui/pages/operations.py")
    seg = ops.split('"WAREHOUSE_NAME", _chart_metric, title=_chart_title,', 1)[1].split(")\n", 1)[0]
    assert 'additive=_chart_metric != "AVG_QUEUE_SEC"' in seg


# ---------------------------------------------------------------------------
# R1-218: a section badge is keyed on the window's bounds, not just its day count
# ---------------------------------------------------------------------------

def _window_filters(window, today: date) -> dict:
    from app.logic.date_windows import resolve_window_days, window_bounds
    return {"company": "ALFA", "days": resolve_window_days(window, today=today),
            "bounds": window_bounds(window, today=today)}


def test_badge_stashed_under_30d_does_not_show_under_last_month_of_the_same_length(monkeypatch):
    import app.core.state as state
    from app.config import CURRENT_MONTH_WINDOW, LAST_MONTH_WINDOW
    from app.ui import components
    monkeypatch.setattr(components, "st", _NavSt())
    today = date(2026, 10, 15)                       # Sep has 30 days: Last month resolves to 30
    current = {"f": _window_filters(30, today)}
    monkeypatch.setattr(state, "filters", lambda: current["f"])
    components.stash_section_count("Operations", "Optimize", 12, dims=("company", "days"))
    assert components.stashed_counts("Operations") == {"Optimize": 12}
    current["f"] = _window_filters(LAST_MONTH_WINDOW, today)
    assert current["f"]["days"] == 30                # the collision this guards
    assert components.stashed_counts("Operations") == {}          # unbadged, never the 30d count
    current["f"] = _window_filters(CURRENT_MONTH_WINDOW, today)   # resolves to 14 on the 15th
    components.stash_section_count("Operations", "Optimize", 3, dims=("company", "days"))
    current["f"] = _window_filters(14, today)
    assert components.stashed_counts("Operations") == {}
    current["f"] = _window_filters(CURRENT_MONTH_WINDOW, today)
    assert components.stashed_counts("Operations") == {"Optimize": 3}   # same window: still badged


# ---------------------------------------------------------------------------
# R1-219: the display-timezone pass converts only columns that ARE timestamps
# ---------------------------------------------------------------------------

def test_localize_timestamps_leaves_name_lookalike_columns_alone(monkeypatch):
    from app.ui import components
    fake = _NavSt()
    fake.session_state["_ow_display_tz"] = "America/New_York"
    monkeypatch.setattr(components, "st", fake)
    df = pd.DataFrame({
        "NEVER_READ": [True, False],                                    # bool, name ends in _READ
        "TOTAL_ELAPSED_TIME": [125000, 3400],                           # raw ms, name ends in _TIME
        "STATUS_AT": ["n/a", "2026-09-29 10:00:00"],                    # text that is not all timestamps
        "LAST_READ": pd.to_datetime(["2026-09-29 10:00:00", None]),     # a real timestamp (+ a NULL)
        "CREATED_AT": ["2026-09-29 10:00:00", "2026-09-29 11:30:00"],   # timestamp text
    })
    out, note = components.localize_timestamps(df, components.timestampish_columns(df.columns))
    assert out["NEVER_READ"].tolist() == [True, False]
    assert out["TOTAL_ELAPSED_TIME"].tolist() == [125000, 3400]
    assert out["STATUS_AT"].tolist() == ["n/a", "2026-09-29 10:00:00"]
    # the real timestamps still convert (Central -> Eastern, +1h) and the note still shows
    assert str(out["LAST_READ"].iloc[0]) == "2026-09-29 11:00:00" and pd.isna(out["LAST_READ"].iloc[1])
    assert str(out["CREATED_AT"].iloc[1]) == "2026-09-29 12:30:00"
    assert "America/New_York" in note


# ---------------------------------------------------------------------------
# R1-220: Add to Case hands NULL cells to the Case File as NULLs, not 'nan' / 'NaT'
# ---------------------------------------------------------------------------

class _CaseSt(_NavSt):
    def button(self, *_a, **_k):
        return True

    def toast(self, *_a, **_k):
        return None


def test_add_to_case_preview_exports_nulls_blank_not_as_nan_text(monkeypatch):
    import app.core.state as state
    from app.logic import case_file
    from app.ui import components
    fake = _CaseSt()
    monkeypatch.setattr(components, "st", fake)
    monkeypatch.setattr(state, "filters", lambda: {"company": "ALFA", "window_label": "30d", "days": 30})
    # a Security account-takeover row for a terminal lock-out: FIRST_SUCCESS_AFTER is NULL
    df = pd.DataFrame({
        "USER_NAME": ["SVC_X", "SVC_Y"],
        "FAILS": pd.array([7, None], dtype="Int64"),
        "SCORE": [1.5, float("nan")],
        "NOTE": ["ok", None],
        "FIRST_SUCCESS_AFTER": pd.to_datetime([None, "2026-09-29 10:05:00"]),
        "LOCKED": [False, True],
    })
    assert components.add_to_case_button("Security", QueryResult(df=df, ok=True, source="t"),
                                         summary="ATO", key="case_ato") is True
    item = fake.session_state[case_file.CASE_STATE_KEY][0]
    md = case_file.assemble_markdown([item], generated="2026-09-30")
    for junk in ("nan", "NaT", "None", "<NA>"):
        assert junk not in md, junk
    # non-null cells still render exactly as before
    assert "SVC_X" in md and "| 7 |" in md and "1.5" in md and "2026-09-29 10:05:00" in md and "True" in md


# ---------------------------------------------------------------------------
# R1-221: an infinite numeric cell renders the no-value glyph instead of killing the page
# ---------------------------------------------------------------------------

def test_clean_numeric_cell_renders_infinity_as_the_em_dash():
    from app.ui.components import _clean_numeric_cell
    assert _clean_numeric_cell(math.inf) == "—" and _clean_numeric_cell(-math.inf) == "—"
    assert _clean_numeric_cell(float("nan")) == "—" and _clean_numeric_cell(2.5) == "2.5"


def test_styled_table_with_an_infinite_ratio_still_renders_the_rest_of_the_page():
    from app.ui.components import _clean_numeric_cell
    df = pd.DataFrame({"RATIO": [1.5, math.inf], "SCORE": [2.0, -math.inf]})
    html = df.style.format(_clean_numeric_cell, na_rep="—", subset=["RATIO", "SCORE"]).to_html()
    assert "1.5" in html and "—" in html                  # the lazy format pass completes


# ---------------------------------------------------------------------------
# R1-222: a large table's text duration column says its header sort is textual
# ---------------------------------------------------------------------------

def _rendered_table_config(monkeypatch, df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    from app.ui import components
    seen: dict = {}

    def _capture(data, **kwargs):
        seen["data"], seen["cfg"] = data, kwargs.get("column_config") or {}

    monkeypatch.setattr(components.st, "dataframe", _capture)
    monkeypatch.setattr(components.st, "download_button", lambda *a, **k: False)
    monkeypatch.setattr(components.st, "caption", lambda *a, **k: None)
    components.styled_table(df, size_note=False)
    return seen["data"], seen["cfg"]


def test_large_table_duration_header_says_its_sort_is_textual(monkeypatch):
    from app.ui.components import STYLER_MAX_ROWS
    n = STYLER_MAX_ROWS + 1
    df = pd.DataFrame({"QUERY_ID": [f"q{i}" for i in range(n)],
                       "ELAPSED_SEC": [float((i * 37) % 4000) + 0.85 for i in range(n)]})
    data, cfg = _rendered_table_config(monkeypatch, df)
    assert data["ELAPSED_SEC"].dtype == object                  # Hr/Min/Sec text cells (owner rule) stay
    help_text = str((cfg.get("ELAPSED_SEC") or {}).get("help") or "")
    assert "textual" in help_text and "CSV use the real value" in help_text
    assert df["ELAPSED_SEC"].dtype == float                     # the caller's frame / CSV stays numeric


def test_small_table_duration_keeps_numeric_sort_and_no_textual_note(monkeypatch):
    df = pd.DataFrame({"QUERY_ID": ["a", "b", "c"], "ELAPSED_SEC": [850.0, 10.0, 3780.0]})
    data, cfg = _rendered_table_config(monkeypatch, df)
    assert data.data["ELAPSED_SEC"].dtype == float              # a Styler over numeric cells: real sort
    assert "textual" not in str((cfg.get("ELAPSED_SEC") or {}).get("help") or "")
