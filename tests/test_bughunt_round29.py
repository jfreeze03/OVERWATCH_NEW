"""Bug-hunt round 29: adversarial sweep of the read-execution + formatting core.

4 confirmed defects (cache-key finder errored = known gap; batch_wall refuted). The two
row-cap findings share one root: a builder's trailing ``LIMIT n`` equal to the transport
cap defeated the ``cap+1`` truncation canary.

#1 (MED, row-cap): the task-graph "Pipeline spend (window)" KPI silently undercounted. Both
   task_graphs (mart) and graph_daily_costs (live) ended ``ORDER BY DAY ... LIMIT 5000`` ==
   DEFAULT_MAX_ROWS, and unit_costs._graphs_tab sums the WHOLE frame in pandas. With the cap
   equal to the builder LIMIT, >5000 day-rows were truncated (ORDER BY DAY asc -> newest days
   dropped) with truncated=False (no banner). Fixed: _graphs_tab passes max_rows=0 and both
   builders raise LIMIT to a 50000 safety ceiling, so the window sum sees the full frame.
#2 (LOW, row-cap root): _with_row_cap kept a trailing ``LIMIT n`` whenever n <= cap+1, so a
   builder ``LIMIT 5000`` with the default cap 5000 was kept -> at most cap rows -> len>cap
   never True -> no truncation banner. Fixed: keep only a STRICTLY smaller LIMIT (n < cap);
   n >= cap is rewritten to cap+1 so the canary always arms.
#3 (LOW, registry-contract): control_pulse declared summary_reads=2 ("up to 2 reads") but the
   mart-miss path does 3 (empty fact + live fallback + activity spark). Fixed: 3.
#4 (LOW, telemetry): the 60/session healthy-persist cap was checked BEFORE the slow-row check,
   so slow (>=2s, SAMPLE_PROB=1.0) rows were starved by the 2% healthy sample in a busy
   session -> the 1/SAMPLE_PROB fleet re-weight under-counted load. Fixed: slow rows get their
   own reserved budget (like failures), short-circuited ahead of the healthy cap.
"""

from __future__ import annotations

from pathlib import Path

from app.core.query import (
    _TELEMETRY_PERSIST_CAP,
    _TELEMETRY_SLOW_CAP,
    _with_row_cap,
    should_persist_telemetry,
)
from app.logic.read_models import get_contract

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- #2: the row-cap canary arms even when the builder LIMIT equals the cap -----------

def test_with_row_cap_arms_canary_when_builder_limit_equals_cap():
    # builder LIMIT == cap -> rewritten to cap+1 so truncation is detectable
    assert "LIMIT 5001" in _with_row_cap("SELECT 1\nLIMIT 5000", 5000)
    # builder LIMIT > cap -> rewritten to cap+1 (unchanged behavior)
    assert "LIMIT 5001" in _with_row_cap("SELECT 1\nLIMIT 20000", 5000)
    # no trailing LIMIT -> append cap+1 (unchanged)
    assert "LIMIT 5001" in _with_row_cap("SELECT 1", 5000)
    # a STRICTLY smaller builder LIMIT is a genuine within-budget answer -> kept
    assert _with_row_cap("SELECT 1\nLIMIT 100", 5000).endswith("LIMIT 100")
    assert _with_row_cap("SELECT 1\nLIMIT 4999", 5000).endswith("LIMIT 4999")
    # cap<=0 short-circuits (no cap applied)
    assert _with_row_cap("SELECT 1\nLIMIT 5000", 0) == "SELECT 1\nLIMIT 5000"


# --- #1: the task-graph pandas-sum readers are not capped at DEFAULT_MAX_ROWS ---------

def test_task_graph_readers_uncapped_for_the_window_sum():
    mart = _read("app/data/mart27_sql.py").split("def task_graphs", 1)[1].split("\ndef ", 1)[0]
    live = _read("app/data/graph_sql.py").split("def graph_daily_costs", 1)[1].split("\ndef ", 1)[0]
    assert "LIMIT 50000" in mart and "LIMIT 5000\n" not in mart
    assert "LIMIT 50000" in live and "LIMIT 5000\n" not in live
    # the caller sums the whole frame in pandas, so it must disable the transport cap
    graphs = _read("app/ui/pages/cost_parts/unit_costs.py").split("def _graphs_tab", 1)[1].split("\ndef ", 1)[0]
    assert "max_rows=0" in graphs


# --- #3: the control_pulse read-model contract states its honest max reads ------------

def test_control_pulse_contract_counts_the_mart_miss_fallback():
    assert get_contract("control_pulse").summary_reads == 3


# --- #4: slow telemetry rows have a reserved budget, never starved by the healthy cap --

def test_slow_telemetry_row_survives_a_full_healthy_cap():
    cap = _TELEMETRY_PERSIST_CAP
    # a slow (>=2s) ok row is admitted even when the healthy cap is full...
    assert should_persist_telemetry(5000.0, True, persisted=cap, cap=cap, slow_persisted=0) is True
    # ...but is bounded by its OWN reserve (can't spam forever)
    assert should_persist_telemetry(
        5000.0, True, persisted=0, slow_persisted=_TELEMETRY_SLOW_CAP) is False
    # a healthy row is still governed by the healthy cap
    assert should_persist_telemetry(
        100.0, True, persisted=cap, cap=cap, sample_roll=0.0, sample_rate=0.02) is False
    # a healthy sampled row under the cap persists
    assert should_persist_telemetry(
        100.0, True, persisted=0, sample_roll=0.001, sample_rate=0.02) is True
    # a failure still draws from its own reserved budget regardless of the healthy cap
    assert should_persist_telemetry(
        100.0, False, persisted=cap, cap=cap, failed_persisted=0, fail_cap=20) is True
