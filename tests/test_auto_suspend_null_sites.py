"""R1-071, the two app sites the first fix missed: a SHOW-listed NULL auto_suspend never suspends.

SHOW WAREHOUSES reports a warehouse with AUTO_SUSPEND = NULL ("never automatically suspends") as a NULL
``auto_suspend`` cell. insights.with_auto_suspend_settings already reads it as the KNOWN never-suspend 0
(show_auto_suspend), so Optimize ▸ Idle calls such a warehouse ACTIONABLE. Two other readers of the same
SHOW cell did not:

* the Alerts closed-loop tighten set known only for a non-NaN cell, so the same warehouse got no ALTER and a
  false "Current AUTO_SUSPEND could not be verified... until SHOW WAREHOUSES returns this setting";
* the Cost ▸ Optimize what-if ran safe_float(cell, 600), so a never-suspend warehouse was modelled as a
  10-minute timer and its auto-suspend saving under-priced.

Both now go through show_auto_suspend; only a failed read, an unlisted warehouse, a missing column or an
unparseable cell stays unknown (Alerts: no ALTER; what-if: the 600 s stand-in).
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.logic import remediation
from app.ui.pages import alerts
from app.ui.pages.cost_parts import optimize


def _show(rows: list[dict]) -> QueryResult:
    return QueryResult(df=pd.DataFrame(rows), ok=True, source="SHOW WAREHOUSES")


_FLEET = [{"name": "WH_NEVER", "size": "X-Small", "auto_suspend": None},
          {"name": "WH_ZERO", "size": "X-Small", "auto_suspend": 0},
          {"name": "WH_600", "size": "X-Small", "auto_suspend": 600},
          {"name": "WH_BAD", "size": "X-Small", "auto_suspend": "abc"}]


# ------------------------------------------------------------------------ Alerts closed-loop tighten ----
# v4.606 integration: the closed-loop guard reads ONE warehouse live (R1-170) and parses it with
# _auto_suspend_in_force, which now reads the cell through show_auto_suspend (R1-071). These tests pin the
# R1-071 half on that merged helper; tests/test_alert_suspend_guard_live.py pins the live-read half.

def test_alerts_tighten_reads_a_listed_null_as_never_suspend_and_generates_the_alter():
    known, cur = alerts._auto_suspend_in_force(_show(_FLEET).df, "wh_never")   # case-insensitive match
    assert (known, cur) == (True, 0.0)
    plan = remediation.tighten_suspend_plan("WH_NEVER", cur, known)
    assert plan["stmt"] == remediation.auto_suspend_fix("WH_NEVER", 60) and plan["level"] == "none"
    assert "could not be verified" not in plan["message"]
    # the ledger note the drawer writes for it says the never-suspend row is not auto-booked
    assert "not auto-booked" in remediation.closed_loop_note_suffix("AUTO_SUSPEND", cur)


@pytest.mark.parametrize(("wh", "want"), [("WH_ZERO", (True, 0.0)), ("WH_600", (True, 600.0)),
                                          ("WH_BAD", (False, None)), ("WH_GONE", (False, None))])
def test_alerts_tighten_keeps_real_values_and_only_an_unreadable_setting_is_unknown(wh, want):
    assert alerts._auto_suspend_in_force(_show(_FLEET).df, wh) == want


@pytest.mark.parametrize("df", [None, pd.DataFrame(), pd.DataFrame([{"name": "WH_NEVER", "size": "X-Small"}])])
def test_alerts_tighten_failed_empty_or_columnless_read_is_unknown(df):
    assert alerts._auto_suspend_in_force(df, "WH_NEVER") == (False, None)


def test_alerts_closed_loop_uses_the_helper():
    from tests._source import read
    src = read("app/ui/pages/alerts.py")
    assert "_cl_known, _cl_cur = _auto_suspend_in_force(" in src
    assert "_cl_plan = remediation.tighten_suspend_plan(wh_inline, _cl_cur, _cl_known)" in src
    helper = src.split("def _auto_suspend_in_force(", 1)[1].split("\ndef ", 1)[0]
    assert "show_auto_suspend(" in helper and "pd.to_numeric" not in helper


# ----------------------------------------------------------------------------- Cost ▸ Optimize what-if ----

class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _whatif(monkeypatch, res: QueryResult, pick: str) -> tuple[dict, list]:
    seen: dict = {}
    kpis: list = []
    fake = SimpleNamespace(
        expander=lambda *_a, **_k: _Ctx(), caption=lambda *_a, **_k: None, warning=lambda *_a, **_k: None,
        info=lambda *_a, **_k: None, toggle=lambda *_a, **_k: True,
        selectbox=lambda _l, options, *_a, **_k: pick, columns=lambda n: [_Ctx() for _ in range(n)],
        select_slider=lambda _l, *_a, value=None, **_k: value)
    monkeypatch.setattr(optimize, "st", fake)
    monkeypatch.setattr(optimize, "run", lambda *_a, **_k: res)
    monkeypatch.setattr(optimize, "kpi_row", lambda items, *_a, **_k: kpis.append(items))
    real = optimize.simulate_scenario

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(optimize, "simulate_scenario", spy)
    sized = pd.DataFrame([{"WAREHOUSE_NAME": r["name"], "CREDITS_TOTAL": 100.0, "IDLE_PCT": 50.0}
                          for r in [*_FLEET, {"name": "WH_GONE"}]])
    optimize._whatif_panel.__wrapped__(sized, 30, 3.68)          # the fragment body, outside a script run
    return seen, kpis


def test_whatif_models_a_listed_null_as_never_suspend_not_a_ten_minute_timer(monkeypatch):
    seen, kpis = _whatif(monkeypatch, _show(_FLEET), "WH_NEVER")
    assert seen["autosuspend_now_s"] == 0 and seen["size"] == "X-Small"
    ((now, _scenario),) = kpis
    assert now["label"] == "Now (XSMALL, never suspends)"
    # the saving matches a real-0 warehouse's, and is larger than the 600 s stand-in the old read produced
    zero, _ = _whatif(monkeypatch, _show(_FLEET), "WH_ZERO")
    assert zero["autosuspend_now_s"] == 0
    from app.logic.sizing import simulate_scenario
    base = {k: v for k, v in seen.items() if k != "autosuspend_now_s"}
    never = simulate_scenario(**base, autosuspend_now_s=0)
    assumed = simulate_scenario(**base, autosuspend_now_s=600)
    assert never["monthly_high_usd"] < assumed["monthly_high_usd"]


@pytest.mark.parametrize(("pick", "want"), [("WH_600", 600), ("WH_BAD", 600), ("WH_GONE", 600)])
def test_whatif_keeps_real_timers_and_the_600_stand_in_only_for_an_unreadable_cell(monkeypatch, pick, want):
    seen, _ = _whatif(monkeypatch, _show(_FLEET), pick)
    assert seen["autosuspend_now_s"] == want


def test_whatif_a_show_without_the_auto_suspend_column_is_not_never_suspend(monkeypatch):
    seen, _ = _whatif(monkeypatch, _show([{"name": "WH_NEVER", "size": "X-Small"}]), "WH_NEVER")
    assert seen["autosuspend_now_s"] == 600
