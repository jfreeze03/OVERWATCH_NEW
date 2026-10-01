"""PR-1 (cluster c01-ops) locks: Operations-page defects confirmed by the 04fd374e adversarial review.

Each test names its finding id. Render tests drive the real page function with recording fakes (the
tests/test_probe_absence_split.py pattern); source locks cover the sites buried in large functions.
"""

from __future__ import annotations

import ast
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.logic.date_windows import CalendarDayOffset
from tests._source import read

_OPS = "app/ui/pages/operations.py"


def _fn(src: str, name: str) -> str:
    """The source of one top-level function of a module."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"no function {name}")


def _ok(df: pd.DataFrame, **extra):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, error="", error_kind="", truncated=False,
                           usable=lambda: not df.empty, **extra)


def _failed(kind: str):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=f"boom ({kind})", error_kind=kind,
                           truncated=False, usable=lambda: False)


class _Stop(Exception):
    """Raised by a recording fake to end a page function once the value under test is captured."""


# ------------------------------------------------------- R1-049 / R1-013: divide by the bounds SPAN ----

def test_wasted_spend_monthlyizes_by_the_bounds_span():
    # R1-049: Current month passes a day OFFSET (Sep 2 MTD = 1) while the bounded scan covers the
    # span (Sep 1..Sep 3 exclusive = 2 days); the offset divisor doubled "Monthly-ized" on the 2nd.
    body = _fn(read(_OPS), "_queries_tab")
    assert ("_waste_served = ((bounds[1] - bounds[0]).days if bounds is not None\n"
            "                             else min(int(days), MAX_LIVE_WINDOW_DAYS))") in body
    assert "_waste_served = days if bounds is not None" not in body


def test_ops_sizing_divides_by_the_bounds_span(monkeypatch):
    # R1-013 / R1-049: the mart's COVERED_DAYS is 2 on Sep 2 under Current month; dividing by the
    # offset (1) doubled MONTHLY_USD_NOW / IDLE_MONTHLY_USD and gave ACTIVE_DAYS_PER_30D = 60.
    from app.ui.pages import operations as ops
    seen: dict = {}
    prof = _ok(pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "COVERED_DAYS": [2]}))
    prof.df.attrs["_ow_effective_days"] = 1          # the stamp run_mart_first writes for offset 1

    def fake_size(_df, _rate, days_):
        seen["days"] = days_
        raise _Stop

    monkeypatch.setattr(ops, "st", SimpleNamespace(toggle=lambda *_a, **_k: True))
    monkeypatch.setattr(ops, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(ops, "run_mart_first", lambda *_a, **_k: prof)
    monkeypatch.setattr(ops, "guard", lambda *_a, **_k: True)
    monkeypatch.setattr(ops, "run", lambda *_a, **_k: _ok(pd.DataFrame()))
    monkeypatch.setattr(ops, "size_recommendations", fake_size)
    with pytest.raises(_Stop):
        ops._wh_sizing_efficiency("ALL", 3.68, CalendarDayOffset(1),
                                  bounds=(date(2026, 9, 1), date(2026, 9, 3)))
    assert seen["days"] == 2
    body = _fn(read(_OPS), "_wh_sizing_efficiency")
    assert "served_days(_prof, _span)" in body and "served_days(_prof, days)" not in body
