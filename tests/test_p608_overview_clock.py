"""v4.608 holistic review, Overview on the account clock.

  #9   The budget burndown under the 'Pace vs budget calendar' card cuts its source at the metering fact's own
       first incomplete day (formulas.metering_complete_before), the cut the card already uses (R2-050). Before
       the 06:45 Central load the newest FACT_METERING_DAILY row is yesterday's partial snapshot; the burndown
       counted it as a whole day, so on the same render the card read 'on straight-line' while the chart and its
       'Complete days only' caption read $550 under pace.
  #10  The platform score's queue / spill per-day divisor comes from the score read's OWN clock (the window
       SQL returns WIN_START_AT and READ_AT on the account clock), not the render clock. A window read at 23:50
       Central and served from the hourly cache after midnight was divided by the new day's ~1.0 divisor,
       reading a steady 9 min/day of queueing as ~17.7 min/day (a queue deduction) until the entry expired.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import sqlglot

st = pytest.importorskip("streamlit")

from app.core.result import QueryResult  # noqa: E402
from app.data import mart_sql  # noqa: E402
from app.logic import formulas  # noqa: E402
from tests.test_pages_shaped import _stub_shaped  # noqa: E402, F401  (the harness's autouse stub fixture)
from tests.test_r2_e3_overview_ui import _freeze_clock, _render_overview  # noqa: E402

# ============================================================================ #9 burndown ====


def _render_with_budget(monkeypatch, wide: QueryResult, budget: float) -> dict:
    """Render Overview with the shared 150d metering frame = ``wide`` and MONTHLY_BUDGET_USD = ``budget`` at
    $1/credit; capture the burndown frames, the KPI cards and the md_dollars captions."""
    from app.ui.pages import overview as ov

    got: dict = {"burn": [], "kpis": [], "captions": []}
    real_settings, real_burn, real_kpi, real_md = ov.load_settings, ov.budget_burndown, ov.kpi_row, ov.md_dollars

    def _settings(page):
        return {**real_settings(page), "MONTHLY_BUDGET_USD": budget, "CREDIT_PRICE_USD": 1.0}

    def _burn(*a, **k):
        out = real_burn(*a, **k)
        got["burn"].append(out)
        return out

    def _kpis(items, *a, **k):
        got["kpis"].extend(items)
        return real_kpi(items, *a, **k)

    def _md(text, *a, **k):
        got["captions"].append(str(text))
        return real_md(text, *a, **k)

    monkeypatch.setattr(ov, "daily_spend_wide", lambda _page: wide)
    monkeypatch.setattr(ov, "load_settings", _settings)
    monkeypatch.setattr(ov, "budget_burndown", _burn)
    monkeypatch.setattr(ov, "kpi_row", _kpis)
    monkeypatch.setattr(ov, "md_dollars", _md)
    _render_overview(monkeypatch)
    return got


def _wide(newest: str, partial_usd: float) -> QueryResult:
    days = pd.date_range("2026-08-01", newest).date
    credits = [1000.0] * (len(days) - 1) + [partial_usd]
    return QueryResult(ok=True, source="stub", df=pd.DataFrame({"DAY": days, "CREDITS_BILLED": credits}))


def test_burndown_drops_the_partial_metering_row_before_the_load_and_agrees_with_the_pace_card(monkeypatch):
    """03:00 CDT Sep 15 (before the 06:45 load): the newest metering row, Sep 14, holds only $450 of a flat
    $1,000/day against a $30,000 budget. The card reads on straight-line after 13 complete days; the burndown
    must end at Sep 13 too, with no gap -- not $13,450 vs $14,000, '$550 under pace'."""
    _freeze_clock(monkeypatch, "2026-09-15 08:00")
    wide = _wide("2026-09-14", 450.0)
    got = _render_with_budget(monkeypatch, wide, 30000.0)

    burn = got["burn"][-1]
    cut = formulas.metering_complete_before(wide.df, date(2026, 9, 15))
    assert cut == date(2026, 9, 14)
    assert pd.Timestamp(burn["DAY"].max()).date() < cut
    assert pd.Timestamp(burn["DAY"].max()).date() == date(2026, 9, 13)
    last = burn.iloc[-1]
    gap = float(last["CUM_ACTUAL_USD"]) - float(last["BUDGET_LINE_USD"])

    pace = [k for k in got["kpis"] if k.get("label") == "Pace vs budget calendar"][-1]
    assert "after 13 complete day(s) of 30" in pace["delta"]
    pace_var, expected = formulas.budget_pace_variance(13000.0, 30000.0, date(2026, 9, 15), complete_before=cut)
    assert float(last["BUDGET_LINE_USD"]) == pytest.approx(expected)
    assert gap == pytest.approx(pace_var) == pytest.approx(0.0)
    assert pace["delta"].startswith("on straight-line")

    cap = [c for c in got["captions"] if c.startswith("Cumulative ")][-1]
    assert "$0 over pace" in cap or "$0.00 over pace" in cap, cap
    assert "until the 06:45 Central load lands so is yesterday's" in cap


def test_burndown_after_the_load_keeps_yesterday_and_drops_only_today(monkeypatch):
    """12:00 CDT Sep 15 (after the load): the newest row is today's partial (Sep 15), so the burndown runs
    through Sep 14 -- the complete-day count the card uses (14)."""
    _freeze_clock(monkeypatch, "2026-09-15 17:00")
    got = _render_with_budget(monkeypatch, _wide("2026-09-15", 300.0), 30000.0)
    burn = got["burn"][-1]
    assert pd.Timestamp(burn["DAY"].max()).date() == date(2026, 9, 14)
    last = burn.iloc[-1]
    assert float(last["CUM_ACTUAL_USD"]) == pytest.approx(14000.0)
    assert float(last["BUDGET_LINE_USD"]) == pytest.approx(14000.0)
    pace = [k for k in got["kpis"] if k.get("label") == "Pace vs budget calendar"][-1]
    assert "after 14 complete day(s) of 30" in pace["delta"]


def test_burndown_source_is_cut_with_the_projection_cut():
    from tests._source import read

    ov = read("app/ui/pages/overview.py")
    assert "_burn_cut = _proj_cut or account_today()" in ov
    assert '.dt.date < _burn_cut]' in ov
    assert "budget_burndown(_burn_src, budget, account_today())" in ov


# ============================================================================== #10 score ====

def test_score_window_sql_returns_its_own_account_clock():
    sql = mart_sql.fact_query_window_summary(1, "ALFA", read_clock=True)
    # the window start the SQL reports is byte-for-byte the anchor its WHERE filters on
    assert "HOUR_TS >= DATEADD('day', -1, CURRENT_DATE())" in sql
    assert "DATEADD('day', -1, CURRENT_DATE())::TIMESTAMP_NTZ AS WIN_START_AT" in sql
    assert "CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS READ_AT" in sql
    selects = sqlglot.parse_one(sql, read="snowflake").named_selects
    assert {"QUERY_COUNT", "QUEUED_SEC", "SPILL_REMOTE_GB", "WIN_START_AT", "READ_AT"} <= set(selects)


def test_other_window_summary_callers_keep_their_sql():
    """Control Room's pulse and the Operations Queries summary never asked for the clock: their SQL (and so
    their cache identity and result-cache eligibility) is unchanged."""
    from app.logic.date_windows import window_bounds

    aug = window_bounds("LAST_MONTH", date(2026, 9, 17))
    for sql in (mart_sql.fact_query_window_summary(1, "ALFA"),
                mart_sql.fact_query_window_summary(7, "ALL", "WH_", "KEB", "ALFA_DW"),
                mart_sql.fact_query_window_summary(31, "ALL", bounds=aug)):
        assert "READ_AT" not in sql and "WIN_START_AT" not in sql and "CURRENT_TIMESTAMP" not in sql
    with pytest.raises(ValueError):
        mart_sql.fact_query_window_summary(31, "ALL", bounds=aug, read_clock=True)


def test_overview_asks_for_the_read_clock_and_divides_by_it():
    from tests._source import read

    ov = read("app/ui/pages/overview.py")
    assert "fact_query_window_summary(_SCORE_HEALTH_WINDOW_DAYS, company, read_clock=True)" in ov
    assert "_elapsed_days = _score_read_elapsed_days(_tr)" in ov


def test_score_divisor_reads_the_row_clock(monkeypatch):
    from app.ui.pages import overview as ov

    _freeze_clock(monkeypatch, "2026-10-01 05:20")          # 00:20 CDT Oct 1
    row = pd.Series({"READ_AT": pd.Timestamp("2026-09-30 23:50"), "WIN_START_AT": pd.Timestamp("2026-09-29")})
    assert ov._score_read_elapsed_days(row) == pytest.approx((47 + 50 / 60) / 24.0)
    # no clock columns (a read from before this change, a stub) -> the account render clock, as before
    assert ov._score_read_elapsed_days(pd.Series({"QUEUED_SEC": 1.0})) == pytest.approx((24 + 20 / 60) / 24.0)
    assert ov._score_read_elapsed_days(None) == pytest.approx((24 + 20 / 60) / 24.0)
    # READ_AT alone: the window start is its own Central midnight minus the window
    assert ov._score_read_elapsed_days(pd.Series({"READ_AT": "2026-09-30 12:00"})) == pytest.approx(36 / 24.0)
    # a tz-aware stamp is read on its own wall clock
    aware = pd.Timestamp("2026-09-30 18:00", tz="America/Chicago")
    assert ov._score_read_elapsed_days(pd.Series({"READ_AT": aware})) == pytest.approx(42 / 24.0)
    # a number or garbage is not a clock -> render clock
    for junk in (1.0, "not a time", None, pd.NaT):
        assert ov._score_read_elapsed_days(pd.Series({"READ_AT": junk, "WIN_START_AT": junk})) \
            == pytest.approx((24 + 20 / 60) / 24.0)


def test_cached_window_served_after_central_midnight_keeps_its_per_day_rate(monkeypatch):
    """The window was read at 23:50 CDT Sep 30 (it opened Sep 29 00:00, 47h50m earlier) and is served from the
    hourly cache at 00:20 CDT Oct 1. A steady 9 min/day of queueing and 4 GB/day of spill still read 9 and 4
    -- the render-clock divisor (24h20m) read them as ~17.7 min/day and ~7.9 GB/day, both over threshold."""
    _freeze_clock(monkeypatch, "2026-10-01 05:20")
    read_at, win_start = pd.Timestamp("2026-09-30 23:50"), pd.Timestamp("2026-09-29 00:00")
    span_days = (read_at - win_start).total_seconds() / 86400.0
    window = QueryResult(ok=True, source="stub", df=pd.DataFrame({
        "QUERY_COUNT": [1000.0], "FAILED_COUNT": [0.0],
        "QUEUED_SEC": [9.0 * 60.0 * span_days], "SPILL_REMOTE_GB": [4.0 * span_days],
        "WIN_START_AT": [win_start], "READ_AT": [read_at]}))
    got = _render_overview(monkeypatch, frames={"score_throughput_": window})
    sig = got["signals"][-1]
    assert sig["queue_minutes"] == pytest.approx(9.0)
    assert sig["spill_gb"] == pytest.approx(4.0)
