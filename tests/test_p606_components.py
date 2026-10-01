"""v4.606.0 PR-1 (cluster c04-components): behaviour locks for the shared UI machinery fixes in
app/ui/components.py, app/ui/charts.py and app/ui/status_colors.py.

Each test drives the real function (with ``st`` stubbed where it renders) and fails on the
pre-fix code at 04fd374e:

  R1-211  delta_css painted a NULL (NaN) delta cell green/red.
  R1-213  run_mart_first stamped a calendar-bounded live read as 90 days (Current year ~3x high).
"""

from __future__ import annotations

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
