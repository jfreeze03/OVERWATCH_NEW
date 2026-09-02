"""Perf: run_mart_first skips re-probing a recently-FAILED mart (a failed read is never
cached, so an outage/absent mart would re-pay the probe round-trip every render before the
inevitable live fallback). The backoff clears the instant the mart recovers and never
touches the coverage-gate path (a mart that SUCCEEDS but doesn't cover keeps its partial
fallback). See app/ui/components.py::run_mart_first. (perf audit 2026-09-02)
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.ui import components


@pytest.fixture(autouse=True)
def _clear_backoff():
    components._MART_FAIL_BACKOFF.clear()
    yield
    components._MART_FAIL_BACKOFF.clear()


def _usable(rows: int = 2) -> QueryResult:
    return QueryResult(df=pd.DataFrame({"DAY": ["2026-08-15"] * rows, "X": [1.0] * rows}), ok=True)


def _failed() -> QueryResult:
    return QueryResult(df=pd.DataFrame(), ok=False, error="mart down")


class _Runner:
    """Stub for app.core.query.run: mart probes use key '<key>_fact', live uses '<key>'."""

    def __init__(self, mart, live):
        self.mart, self.live = mart, live
        self.mart_calls = self.live_calls = 0

    def __call__(self, sql, *, page, key, tier, source, **kwargs):
        if key.endswith("_fact"):
            self.mart_calls += 1
            return self.mart() if callable(self.mart) else self.mart
        self.live_calls += 1
        return self.live() if callable(self.live) else self.live


def _call(**over):
    kw = {"page": "P", "key": "k", "mart_source": "m", "live_source": "l"}
    kw.update(over)
    return components.run_mart_first("MART SQL", "LIVE SQL", **kw)


def test_failed_mart_backs_off_and_serves_live_without_reprobing(monkeypatch):
    r = _Runner(mart=_failed, live=lambda: _usable())
    monkeypatch.setattr("app.core.query.run", r)
    out1 = _call()                                        # mart fails -> live; backoff set
    assert out1.usable() and r.mart_calls == 1 and r.live_calls == 1
    assert components._mart_backoff_active("P", "k")
    out2 = _call()                                        # within backoff: mart NOT re-probed
    assert out2.usable() and r.mart_calls == 1 and r.live_calls == 2


def test_backoff_expires_then_reprobes_and_recovers(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(components.time, "monotonic", lambda: clock["t"])
    r = _Runner(mart=_failed, live=lambda: _usable())
    monkeypatch.setattr("app.core.query.run", r)
    _call()
    assert r.mart_calls == 1 and components._mart_backoff_active("P", "k")
    _call()
    assert r.mart_calls == 1                              # skipped within backoff
    clock["t"] += components._MART_FAIL_BACKOFF_SEC + 1   # advance past the backoff
    r.mart = lambda: _usable()                            # mart recovered
    out = _call()
    assert r.mart_calls == 2 and out.usable()
    assert not components._mart_backoff_active("P", "k")  # success cleared the backoff


def test_healthy_preloaded_clears_a_stale_backoff_without_probing(monkeypatch):
    components._MART_FAIL_BACKOFF[("P", "k")] = components.time.monotonic() + 999
    r = _Runner(mart=_failed, live=lambda: _usable())
    monkeypatch.setattr("app.core.query.run", r)
    out = _call(preloaded=_usable())
    assert out.usable() and r.mart_calls == 0            # preloaded used, no probe
    assert not components._mart_backoff_active("P", "k")  # healthy prefetch cleared the backoff


def test_coverage_gate_miss_keeps_probing_and_serves_partial(monkeypatch):
    # mart is USABLE but fails the coverage gate; live also fails -> the partial mart is served,
    # and because the mart SUCCEEDED there is NO backoff (the partial fallback must stay live).
    r = _Runner(mart=lambda: _usable(), live=_failed)
    monkeypatch.setattr("app.core.query.run", r)
    out1 = _call(mart_accept=lambda df: False)
    assert out1.usable() and r.mart_calls == 1 and r.live_calls == 1   # partial mart served
    assert not components._mart_backoff_active("P", "k")               # successful mart -> no backoff
    _call(mart_accept=lambda df: False)
    assert r.mart_calls == 2                                            # still probing (fallback kept)


def test_backoff_skip_ignores_empty_is_answer_and_goes_live(monkeypatch):
    # empty_is_answer short-circuits only a SUCCESSFUL empty mart; on a backoff-skip there is no
    # mart result, so the panel must fall through to live rather than render a vacuous empty.
    r = _Runner(mart=_failed, live=lambda: _usable())
    monkeypatch.setattr("app.core.query.run", r)
    _call(empty_is_answer=True)
    assert components._mart_backoff_active("P", "k")
    out = _call(empty_is_answer=True)
    assert out.usable() and r.mart_calls == 1 and r.live_calls == 2
