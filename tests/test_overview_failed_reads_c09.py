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
