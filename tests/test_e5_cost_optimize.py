"""v4.608 (PR-3, cluster e5-cost): Cost ▸ Optimization & Savings ▸ Storage & waste.

  R2-013  the object-cost ledger (FACT_OBJECT_COST_DAILY, never backfilled: first fill 14 days) summed ~3 months
          under a 365-day / Current-year label with no word of it; the panel now names the ledger's first day
          and how many of the window's days it covers (LEDGER_START_DAY, ledger-wide);
  R2-097  the retention control's "read live" current retention came from the 4 h process-wide metadata cache,
          so a retention lowered in a worksheet since an earlier selection still read as the old value and the
          can't-raise guard let the statement RAISE it; it reads on the 30 s live tier now.

Each test fails on e504d089. AppTests reuse the shaped page harness (tests/test_pages_shaped.py)."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _entry,
    _nav_to,
    _shaped_mart_first,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from app.data import cost_sql
from app.logic.formulas import account_today


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="t")


def _cost_page(monkeypatch, section: str, *, run_hook=None, operator: bool = False,
               state: dict | None = None) -> AppTest:
    """Cost ▸ Optimization & Savings ▸ ``section`` on shaped data; ``run_hook(sql, kwargs)`` overrides one read."""
    import app.ui.pages.cost as cost
    import app.ui.pages.cost_parts.optimize as opt

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        got = run_hook(sql, kwargs) if run_hook else None
        return got if got is not None else _shaped_run(*args, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    monkeypatch.setattr(opt, "run_mart_first", lambda mart, live="", **kw: _shaped_mart_first(mart, live, **kw))
    monkeypatch.setattr(opt, "execute_statement", lambda *_a, **_k: (True, "stubbed"))
    if operator:
        monkeypatch.setattr(cost, "_is_operator", lambda: True)
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_section"] = "Optimization & Savings"
    at.session_state["opt_section"] = section
    for k, v in (state or {}).items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"cost optimize {section} (shaped): {at.exception}"
    return at


# ---- R2-013: the object ledger says how much of a long window it covers --------------------------------------

def test_object_cost_by_arm_carries_a_ledger_wide_first_day():
    sql = cost_sql.object_cost_by_arm(365, "ALFA", database="ALFA_EDW")
    assert "(SELECT MIN(l0.DAY) FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY l0) AS LEDGER_START_DAY" in sql
    sub = sql.split("(SELECT MIN(l0.DAY)", 1)[1].split(") AS LEDGER_START_DAY", 1)[0]
    assert "WHERE" not in sub                     # never the company / Database / window scope (the R1-016 trap)


def test_ledger_coverage_note_math():
    from app.ui.pages.cost_parts.optimize import _object_ledger_coverage_note as note
    lm = (date(2026, 8, 1), date(2026, 9, 1))
    assert note(date(2026, 6, 30), 31, lm) == ""                     # ledger older than the window: covered
    cy = (date(2026, 1, 1), date(2026, 10, 1))
    assert note("2026-06-30", 272, cy) == ("The object ledger starts 2026-06-30 (it is not backfilled), so these "
                                           "totals cover 93 of the window's 273 days.")
    assert note(None, 365, None) == "" and note("not a date", 365, None) == ""
    today = account_today()
    first = today - timedelta(days=92)
    assert note(first, 365, None).endswith("cover 92 of the window's 365 days.")
    assert note(today - timedelta(days=400), 365, None) == ""


def test_a_365_day_window_older_than_the_ledger_says_so(monkeypatch):
    first = account_today() - timedelta(days=93)
    arms = pd.DataFrame({"COST_ARM": ["QUERY_COMPUTE_READ", "CLUSTERING", "QUERY_COMPUTE_RESIDUAL"],
                         "OBJECTS": [10, 2, 1], "CREDITS": [600.0, 100.0, 200.0],
                         "LEDGER_START_DAY": [first] * 3})

    def hook(_sql, kw):
        return _ok(arms) if str(kw.get("key", "")).startswith("objcost_arm_") else None

    at = _cost_page(monkeypatch, "Storage & waste", run_hook=hook, state={"flt_days": 365})
    caps = " ".join(str(c.value) for c in at.caption)
    assert (f"The object ledger starts {first:%Y-%m-%d} (it is not backfilled), so these totals cover 93 of the "
            "window's 365 days.") in caps


def test_a_window_inside_the_ledger_has_no_coverage_note(monkeypatch):
    arms = pd.DataFrame({"COST_ARM": ["CLUSTERING"], "OBJECTS": [2], "CREDITS": [100.0],
                         "LEDGER_START_DAY": [account_today() - timedelta(days=120)]})

    def hook(_sql, kw):
        return _ok(arms) if str(kw.get("key", "")).startswith("objcost_arm_") else None

    at = _cost_page(monkeypatch, "Storage & waste", run_hook=hook, state={"flt_days": 90})
    assert "The object ledger starts" not in " ".join(str(c.value) for c in at.caption)


# ---- R2-097: the current retention is read on the live tier ---------------------------------------------------

def _waste_selected(monkeypatch):
    """Select row 0 of the storage-waste table (AppTest cannot click a grid row)."""
    import app.ui.components as components

    real = components._st_dataframe

    class _Event:
        def __init__(self, rows):
            self.selection = type("S", (), {"rows": rows})()

    def _st_dataframe(data, **kwargs):
        out = real(data, **kwargs)
        if kwargs.get("key") == "waste_sel":
            return _Event([0])
        return out

    monkeypatch.setattr(components, "_st_dataframe", _st_dataframe)


def test_the_retention_guard_reads_the_current_setting_on_the_live_tier(monkeypatch):
    _waste_selected(monkeypatch)
    seen: list[dict] = []

    def hook(_sql, kw):
        if str(kw.get("key", "")).startswith("retlive_"):
            seen.append(dict(kw))
            return _ok(pd.DataFrame({"RETENTION_DAYS": [30]}))
        return None

    at = _cost_page(monkeypatch, "Storage & waste", run_hook=hook, operator=True,
                    state={"cost_waste_toggle": True, "waste_days": 7})
    assert seen, "the retention control did not read the current retention (no row selected?)"
    assert {kw.get("tier") for kw in seen} == {"live"}          # pre-fix: "metadata" (cached 4 h, process-wide)
    assert not any("metadata view" in str(kw.get("source", "")) for kw in seen)
    caps = " ".join(str(c.value) for c in at.caption)
    assert "30d -> 7d retention releases about" in caps          # the reduction is offered off the live value ...
    assert re.search(r"read live from the table's metadata on each render \(at most ~30 s old\)", caps)
