"""Bug-hunt round 28: data-truth hunt over app/logic (calc) + app/data (SQL builders).
2 confirmed defects (1 refuted candidate); the twin-divergence finder hit a transient
API error, so that dimension is a known gap (re-runnable).

#1 (MED, rate/unit): Operations-page verdict compared WINDOW-TOTAL remote spill against
a PER-DAY threshold. verdict.operations_signals normalized queue by ndays but not spill,
so a benign steady 0.3 GB/day over a 30-day window (= 9 GB total) fired a false
"Watch — remote spill", and the false-alarm magnitude scaled with window length —
contradicting the platform score's 5-GB/DAY onset on the same page. Fixed: spill is now
/ndays like queue, phrased "GB/day".

#2 (MED, bare-aggregate empty-vs-zero): Decision Studio ▸ Cost Truth "Billed credits"
fabricated "$0.00" on an empty window. mart_sql.billed_split is a bare aggregate (SUM,
no GROUP BY) so .usable() is True with an all-NULL row; `_billed_present = present["BILLED"]
or split.usable()` therefore went True even when the correct present["BILLED"] gate
(pd.notna on the key column) was False. Fixed: gate on the split's CREDITS_BILLED key
column being non-NULL (the same guard spend.py uses for TOTAL_USD / DAYS_AVERAGED).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.verdict import operations_signals

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- #1: spill is a per-day rate, window-length-independent -------------------

def test_operations_signals_spill_is_per_day_not_window_total():
    # 0.3 GB/day sustained for 30 days = 9 GB TOTAL — benign, well under the 5 GB/DAY
    # onset. The pre-fix window-total compare fired a false "warn" (9 >= 5); per-day it
    # must be silent.
    steady = pd.DataFrame({
        "QUERY_COUNT": [1000] * 30, "FAILED_COUNT": [0] * 30,
        "TASK_RUNS": [100] * 30, "TASK_FAILED": [0] * 30,
        "QUEUED_SEC": [0] * 30, "SPILL_GB": [0.3] * 30,
    })
    assert not any("remote spill" in s.phrase for s in operations_signals(steady)), \
        "0.3 GB/day (9 GB/30d total) must NOT fire a spill signal — spill is a per-day rate"

    # a genuinely high per-day spill (6 GB/day) DOES warn, phrased per-day
    hot = pd.DataFrame({
        "QUERY_COUNT": [1000] * 30, "FAILED_COUNT": [0] * 30,
        "TASK_RUNS": [100] * 30, "TASK_FAILED": [0] * 30,
        "QUEUED_SEC": [0] * 30, "SPILL_GB": [6.0] * 30,
    })
    spill = [s for s in operations_signals(hot) if "remote spill" in s.phrase]
    assert spill and spill[0].level == "warn" and "GB/day" in spill[0].phrase

    # window length no longer changes the verdict for the SAME per-day rate
    hot_short = pd.DataFrame({k: v[:3] for k, v in hot.items()})
    assert (len([s for s in operations_signals(hot) if "remote spill" in s.phrase])
            == len([s for s in operations_signals(hot_short) if "remote spill" in s.phrase]))


# --- #2: billed presence gates on the key column, not a bare-aggregate .usable() ---

def test_cost_truth_billed_gates_on_key_column_not_bare_aggregate_usable():
    src = _read("app/ui/decision_studio.py")
    body = src.split("def _cost_truth", 1)[1].split("\ndef ", 1)[0]
    # the presence flag no longer ORs in split.usable() (always True for the 1-row
    # bare aggregate); it uses a key-column non-NULL gate instead
    assert 'or split.usable()' not in body
    assert '_split_has = split.usable() and pd.notna(split.df.iloc[0].get("CREDITS_BILLED"))' in body
    assert '_billed_present = present.get("BILLED", False) or _split_has' in body
    # billed_split really is a bare aggregate (SUM, no GROUP BY) — the reason .usable()
    # can't be trusted as a presence signal
    bs = _read("app/data/mart_sql.py").split("def billed_split", 1)[1].split("\ndef ", 1)[0]
    assert "GROUP BY" not in bs and "SUM(" in _read("app/data/mart_sql.py").split(
        "def _billed_split_cols", 1)[1].split("\ndef ", 1)[0]
