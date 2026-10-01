"""Bug-hunt c09 R1-191 / R1-192 / R1-193 / R1-195: Overview never turns a failed read (or an empty denominator)
into a number.

Rendered through the shaped AppTest harness (its autouse stub fixture is imported below); individual reads are
then failed by cache key and the hero, the KPI tiles, the executive-export view and the movers table are
captured as the page hands them over.

  R1-191  both spend reads fail      -> the flagship 'Spend' hero, the case summary and the export say
                                        unavailable, never $0.00 (a successful empty read keeps $0.00);
  R1-192  the alert-count read fails -> the export's 'Open alerts' card says unavailable, never 0 | 0;
  R1-193  ACTION_QUEUE / the daily metering fact time out -> 'unavailable', not 'isn't installed yet' /
                                        'Needs daily facts' (a true absence keeps the setup wording);
  R1-195  a warehouse with no prior-month spend -> its Δ % is blank (None), never a fabricated +0.0%.
  R1-229  the ML engine reads TODAY's forecast row too, so the #24 prorated today-remainder is never 0.
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402
from tests.test_pages_shaped import (  # noqa: E402, F401  (_stub_shaped: the harness's autouse stub fixture)
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_mart_first,
    _shaped_run,
    _stub_shaped,
)

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")


def _failed(kind: str = "timeout") -> QueryResult:
    return QueryResult(ok=False, error=f"stub {kind}", error_kind=kind)


def _render(monkeypatch, *, fail: dict | None = None, frames: dict | None = None,
            daily_wide: QueryResult | None = None) -> dict:
    """Render Overview. ``fail`` / ``frames`` map a cache-key PREFIX to the result that read returns."""
    from app.ui.pages import overview as ov

    fail, frames = dict(fail or {}), dict(frames or {})

    def _override(key: str):
        for table in (fail, frames):
            for prefix, res in table.items():
                if key.startswith(prefix):
                    return res
        return None

    def _run(*a, **k):
        return _override(str(k.get("key", ""))) or _shaped_run(*a, **k)

    def _mart_first(*a, **k):
        return _override(str(k.get("key", ""))) or _shaped_mart_first(*a, **k)

    def _batch(specs, **k):
        out = _shaped_batch(specs, **k)
        for s in specs:
            hit = _override(str(s.get("key", "")))
            if hit is not None:
                out[s["key"]] = hit
        return out

    got: dict = {"hero": [], "kpis": [], "views": [], "movers": []}
    real_view, real_nav = ov.ExecutiveSummaryView, ov.entity_nav_table

    def _view(**kw):
        got["views"].append(kw)
        return real_view(**kw)

    def _nav(df, *a, **k):
        if str(k.get("key", "")).startswith("ov_wh_movers_"):
            got["movers"].append(df.copy())
        return real_nav(df, *a, **k)

    def _kpis(items, *a, **k):
        got["kpis"].extend(items)

    monkeypatch.setattr(ov, "run", _run)
    monkeypatch.setattr(ov, "run_mart_first", _mart_first)
    monkeypatch.setattr(ov, "run_batch", _batch)
    monkeypatch.setattr(ov, "hero_metric", lambda item, companions=None: got["hero"].append(item))
    monkeypatch.setattr(ov, "kpi_row", _kpis)
    monkeypatch.setattr(ov, "ExecutiveSummaryView", _view)
    monkeypatch.setattr(ov, "entity_nav_table", _nav)
    monkeypatch.setattr(ov, "daily_spend_wide",
                        lambda _page: daily_wide if daily_wide is not None else QueryResult(ok=True))
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Overview")
    at.run()
    assert not at.exception, at.exception
    got["at"] = at
    return got


def _card(view: dict, label: str) -> str:
    return dict(view["cards"])[label]


# ------------------------------------------------------------------------------------- R1-191 ----

@_SKIP
def test_failed_spend_reads_never_render_a_zero_hero(monkeypatch):
    got = _render(monkeypatch, fail={"exec_board_": _failed(), "live_wh_daily_": _failed()})
    hero = got["hero"][0]
    assert hero["label"].startswith("Spend, ")
    assert hero["value"] == "Unavailable" and hero["severity"] == "warn"
    assert "could not be read" in hero["help"] and "stub timeout" in hero["help"]
    assert "sub" not in hero and "delta" not in hero and "spark" not in hero
    assert _card(got["views"][-1], "Window spend").startswith("unavailable (spend read failed)")
    assert "$0.00" not in _card(got["views"][-1], "Window spend")


@_SKIP
def test_a_successful_empty_spend_read_still_reads_zero(monkeypatch):
    empty = QueryResult(ok=True, df=pd.DataFrame())
    got = _render(monkeypatch, fail={"exec_board_": empty, "live_wh_daily_": empty})
    assert got["hero"][0]["value"] == "$0.00"                      # rec1/rec36: no complete day yet
    assert _card(got["views"][-1], "Window spend").startswith("$0.00")


_LIVE_SRC = "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (bounded fallback)"


def test_spend_failure_help_names_only_the_reads_that_ran_and_failed():
    """R1-191 follow-up (house law 8): a failed trend_source is the fallback's LAST (live metering) leg; the
    exec board was skipped ('Last month'), failed, or answered empty, and the help says which."""
    from app.ui.pages import overview as ov

    live = QueryResult(ok=False, error="stub timeout", error_kind="timeout", source=_LIVE_SRC)
    skipped = ov._spend_failure_help(None, live)
    failed = ov._spend_failure_help(_failed(), live)
    empty = ov._spend_failure_help(QueryResult(ok=True, df=pd.DataFrame()), live)
    for text in (skipped, failed, empty):
        assert text.startswith("Warehouse spend could not be read, so no total is shown.")
        assert f"the last fallback, {_LIVE_SRC}, failed: stub timeout." in text
        assert "all failed" not in text
    assert "does not cover a calendar-month window, so it was not read" in skipped
    assert "The exec board read failed" in failed
    assert "The exec board returned no rows" in empty
    sourceless = ov._spend_failure_help(None, QueryResult(ok=False, error=""))
    assert "the last fallback, the warehouse metering read, failed: read failed." in sourceless


@_SKIP
def test_an_empty_board_is_not_reported_as_a_failed_read(monkeypatch):
    got = _render(monkeypatch, fail={
        "exec_board_": QueryResult(ok=True, df=pd.DataFrame(), source="MART_EXEC_BOARD"),
        "live_wh_daily_": QueryResult(ok=False, error="stub timeout", error_kind="timeout", source=_LIVE_SRC)})
    hero = got["hero"][0]
    assert hero["value"] == "Unavailable"
    assert "The exec board returned no rows" in hero["help"] and "all failed" not in hero["help"]
    assert f"{_LIVE_SRC}, failed: stub timeout" in hero["help"]


# ------------------------------------------------------------------------------------- R1-192 ----

@_SKIP
def test_failed_alert_counts_never_export_an_all_clear(monkeypatch):
    got = _render(monkeypatch, fail={"alert_counts_": _failed()})
    card = _card(got["views"][-1], "Open alerts")
    assert card.startswith("Unavailable") and "0 critical" not in card
    tile = next(k for k in got["kpis"] if k.get("label") == "Open critical / high alerts")
    assert tile["value"] == "Unavailable"                          # the screen and the export agree


# ------------------------------------------------------------------------------------- R1-193 ----

@_SKIP
@pytest.mark.parametrize("kind", ["timeout", "other", "missing_column"])
def test_failed_reads_are_unavailable_not_not_installed(monkeypatch, kind):
    got = _render(monkeypatch, fail={"action_queue_": _failed(kind)}, daily_wide=_failed(kind))
    at = got["at"]
    assert "Action queue isn't installed yet." not in [str(i.value) for i in at.info]
    assert any("The action queue couldn't be read." in str(e.value) for e in at.error)
    mtd = got["kpis"][0]
    assert mtd["label"] == "MTD credit spend" and mtd["value"] == "Unavailable" and mtd["severity"] == "warn"
    assert _card(got["views"][-1], "Month to date").startswith("unavailable (daily facts could not be read)")


@_SKIP
def test_absent_queue_and_facts_keep_the_setup_wording(monkeypatch):
    got = _render(monkeypatch, fail={"action_queue_": _failed("absent")}, daily_wide=_failed("absent"))
    assert "Action queue isn't installed yet." in [str(i.value) for i in got["at"].info]
    assert got["kpis"][0]["value"] == "Needs daily facts"
    assert _card(got["views"][-1], "Month to date").startswith("n/a (daily facts not deployed)")


# ------------------------------------------------------------------------------------- R1-195 ----

@_SKIP
def test_new_warehouse_mover_has_no_fabricated_zero_percent(monkeypatch):
    from app.ui.pages import overview as ov

    monkeypatch.setattr(ov, "account_today", lambda: datetime.date(2026, 9, 15))
    rows = [("2026-07", "WH_OLD", 1000.0), ("2026-08", "WH_OLD", 1100.0),
            ("2026-07", "WH_X", 50.0), ("2026-08", "WH_X", 0.0),
            ("2026-08", "WH_NEW", 5000.0), ("2026-09", "WH_NEW", 10.0)]
    monthly = QueryResult(ok=True, source="stub", df=pd.DataFrame(
        [{"MONTH": m, "WAREHOUSE_NAME": w, "CREDITS": c} for m, w, c in rows]))
    got = _render(monkeypatch, frames={"ov_monthly_": monthly})
    mv = got["movers"][-1].set_index("WAREHOUSE")
    assert pd.isna(mv.loc["WH_NEW", "DELTA_PCT"])                  # no prior spend -> no percentage
    assert mv.loc["WH_OLD", "DELTA_PCT"] == pytest.approx(10.0)
    assert mv.loc["WH_X", "DELTA_PCT"] == pytest.approx(-100.0)
    assert mv.index[0] == "WH_NEW"                                 # still the biggest mover by |Δ$|


# ------------------------------------------------------------------------------------- R1-229 ----

def _ml_frame(first: datetime.date, n: int) -> QueryResult:
    days = [first + datetime.timedelta(days=i) for i in range(n)]
    return QueryResult(ok=True, source="stub", df=pd.DataFrame({
        "DAY": days, "FORECAST_CREDITS": [100.0] * n, "LOWER_BOUND": [90.0] * n, "UPPER_BOUND": [110.0] * n}))


def _render_ml(monkeypatch, ml: QueryResult) -> dict:
    from app.config import DEFAULT_SETTINGS
    from app.ui.pages import overview as ov

    monkeypatch.setattr(ov, "account_today", lambda: datetime.date(2026, 10, 10))
    settings = {**DEFAULT_SETTINGS, "_source": "stub", "FORECAST_ENGINE": "ml_forecast"}
    monkeypatch.setattr(ov, "load_settings", lambda _p: dict(settings))
    got = _render(monkeypatch, frames={"ml_forecast": ml})
    return next(k for k in got["kpis"] if k.get("label") == "Projected month-end credit spend")


@_SKIP
def test_ml_horizon_short_of_month_end_falls_back_to_seasonal(monkeypatch):
    """A model trained once (last TS 08-31) forecasts 09-01..10-15; on 10-10 only 10-11..10-15 remain. That
    5-day partial sum used to be the month-end projection; it now falls back to the disclosed seasonal engine
    and names the stale horizon."""
    kpi = _render_ml(monkeypatch, _ml_frame(datetime.date(2026, 10, 11), 5))
    assert "SNOWFLAKE.ML.FORECAST via FORECAST_ML_DAILY" not in kpi["help"]
    assert "The ML forecast table ends 2026-10-15, before month-end (2026-10-31)" in kpi["help"]


@_SKIP
def test_ml_horizon_covering_month_end_is_used(monkeypatch):
    kpi = _render_ml(monkeypatch, _ml_frame(datetime.date(2026, 10, 11), 40))
    assert "SNOWFLAKE.ML.FORECAST via FORECAST_ML_DAILY" in kpi["help"]
    assert "ML forecast table ends" not in kpi["help"]


def _ml_table_as_read(today: datetime.date) -> QueryResult:
    """FORECAST_ML_DAILY holds today's row and the 40 days after it. The SQL never runs here, so the stub
    answers with the rows the REAL reader's date predicate keeps."""
    import re

    from app.data import mart_sql

    m = re.search(r"WHERE TS::DATE (>=|>) CURRENT_DATE\(\)", mart_sql.ml_forecast_daily())
    assert m, "the reader's date predicate moved"
    table = _ml_frame(today, 41).df
    keep = table["DAY"] >= today if m.group(1) == ">=" else table["DAY"] > today
    return QueryResult(ok=True, source="stub", df=table[keep].reset_index(drop=True))


@_SKIP
def test_ml_projection_adds_todays_prorated_remainder(monkeypatch):
    """R1-229 follow-up: #24's today-remainder term reads TODAY's forecast row. A strictly-future reader never
    returned it, so the term was 0 every day; at 06:00 three quarters of today's 100 credits are still ahead."""
    from app.config import DEFAULT_SETTINGS
    from app.logic.formulas import credits_to_usd, safe_float
    from app.ui.pages import overview as ov

    made: list[dict] = []
    real = ov.MonthEndForecast

    def _capture(**kw):
        made.append(kw)
        return real(**kw)

    monkeypatch.setattr(ov, "MonthEndForecast", _capture)
    monkeypatch.setattr(ov, "account_now", lambda: datetime.datetime(2026, 10, 10, 6, 0, 0))
    kpi = _render_ml(monkeypatch, _ml_table_as_read(datetime.date(2026, 10, 10)))
    assert "SNOWFLAKE.ML.FORECAST via FORECAST_ML_DAILY" in kpi["help"]
    fc = made[-1]
    rate = safe_float(DEFAULT_SETTINGS.get("CREDIT_PRICE_USD"), 3.68)
    # 10-11..10-31 whole (21 x 100 cr) + today's remainder (100 cr x 0.75)
    assert fc["projected_usd"] - fc["mtd_usd"] == pytest.approx(
        credits_to_usd(21 * 100.0 + 75.0, rate, round_cents=False), abs=0.02)
    assert fc["days_remaining"] == 21                                  # today is prorated, never a whole day


def test_ml_option_procedure_retrains_before_it_refreshes():
    """The weekly task CALLs SP_REFRESH_ML_FORECAST; a trained ML.FORECAST model is immutable and forecasts from
    its last training day, so the procedure itself must re-create the model before re-materializing."""
    from tests._source import read

    script = read("snowflake/ml_forecast_option.sql")
    body = script.split("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST()", 1)[1]
    body = body.split("$$", 2)[1]
    train = body.index("CREATE OR REPLACE SNOWFLAKE.ML.FORECAST DBA_MAINT_DB.OVERWATCH.OVERWATCH_SPEND_FORECAST(")
    assert train < body.index("CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.FORECAST_ML_DAILY")
    assert "WHERE DAY < CURRENT_DATE()" in body                      # trains through every complete day
    task = script.split("CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_ML_FORECAST", 1)[1]
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST();" in task
