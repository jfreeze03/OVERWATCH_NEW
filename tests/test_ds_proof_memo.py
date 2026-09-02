"""Perf (simulator finding): the Decision Studio page-open verdict and the Scorecard
section both call _proof_signals in the same render. The run() cache already deduped the 5
Snowflake reads, but each duplicate call still re-did the Python compute AND emitted a
cache-hit telemetry row — double-counting those 5 keys in APP_QUERY_TELEMETRY (the perf
effort's own slow-query oracle). A per-render memo (reset_proof_memo at the page top; safe
because Decision Studio has no fragments) makes the two callers share ONE computation.
See app/ui/decision_studio.py::_proof_signals / reset_proof_memo. (perf audit 2026-09-02)
"""

from __future__ import annotations

from collections import Counter

import pytest
from test_pages_shaped import _shaped_from_sql

from app.ui import decision_studio as ds

_PROOF_KEYS = ("decision_roi_ledger_full", "sc_quarter", "sc_appcost", "sc_accept", "sc_precision")


@pytest.fixture(autouse=True)
def _clear_memo():
    ds.reset_proof_memo()
    yield
    ds.reset_proof_memo()


def _counting_run(counter: Counter):
    def _run(sql, *, page, key, tier, source="", **_kw):
        counter[key] += 1
        return _shaped_from_sql(sql)          # shaped frame so the downstream aggregation runs
    return _run


def test_proof_signals_shared_once_per_render(monkeypatch):
    counter: Counter = Counter()
    monkeypatch.setattr(ds, "run", _counting_run(counter))
    ds.reset_proof_memo()
    a = ds._proof_signals(3.68)
    b = ds._proof_signals(3.68)               # same render (no reset) -> memo hit, no new reads
    assert a is b                             # identical object => one computation, not two
    for k in _PROOF_KEYS:
        assert counter[k] == 1, f"{k} read {counter[k]}x within one render (expected 1)"


def test_reset_re_enables_reads_next_render(monkeypatch):
    counter: Counter = Counter()
    monkeypatch.setattr(ds, "run", _counting_run(counter))
    ds._proof_signals(3.68)
    ds._proof_signals(3.68)                   # memo hit
    assert counter["decision_roi_ledger_full"] == 1
    ds.reset_proof_memo()                     # next render
    ds._proof_signals(3.68)
    assert counter["decision_roi_ledger_full"] == 2
    assert ds._PROOF_MEMO.get("rate") == 3.68


def test_reset_clears_the_memo(monkeypatch):
    monkeypatch.setattr(ds, "run", _counting_run(Counter()))
    ds._proof_signals(3.68)
    assert ds._PROOF_MEMO                       # populated
    ds.reset_proof_memo()
    assert ds._PROOF_MEMO == {}
