"""Decision Studio Wave-2 SLO trust fixes: #9 P95-basis disclosure, #10 latency
burn -> n/a, #11 staleness gate on AS_OF.
"""

from pathlib import Path

import pandas as pd

from app.data import workbench_sql
from app.logic.decision import slo_summary

_SRC = (Path(__file__).resolve().parents[2] / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")


def _frame(rows):
    return pd.DataFrame(rows)


# --- #11 staleness ---------------------------------------------------------

def test_slo_summary_counts_stale_separately():
    out = slo_summary(_frame([
        {"STATUS": "MET", "BURN_MULTIPLE": 0.5},
        {"STATUS": "BREACH", "BURN_MULTIPLE": 2.0},
        {"STATUS": "STALE", "BURN_MULTIPLE": None},
        {"STATUS": "NO_DATA", "BURN_MULTIPLE": None},
    ]))
    assert out["total"] == 4
    assert out["met"] == 1 and out["breach"] == 1 and out["no_data"] == 1
    assert out["stale"] == 1     # a stale objective is neither met nor breached


def test_slo_cockpit_gates_status_on_staleness():
    sql = workbench_sql.slo_cockpit()
    assert "'STALE'" in sql
    assert "m.AS_OF < DATEADD('day', -2, CURRENT_DATE())" in sql
    # STALE ranks as a warning tier (after BREACH, before/at NO_DATA), not silently MET
    assert "WHEN 'STALE' THEN 1" in sql


# --- #10 latency burn -> n/a ----------------------------------------------

def test_has_burn_distinguishes_success_from_latency_only():
    # a success objective carries a burn (even 0.0) -> has_burn true -> KPI shows Nx
    assert slo_summary(_frame([{"STATUS": "MET", "BURN_MULTIPLE": 0.0}]))["has_burn"] == 1.0
    # latency/P95 objectives have NULL burn -> has_burn false -> UI shows n/a, not 0.00x
    latency = slo_summary(_frame([{"STATUS": "MET", "BURN_MULTIPLE": None}]))
    assert latency["has_burn"] == 0.0 and latency["worst_burn"] == 0.0
    # mixed: at least one burn-applicable objective -> has_burn true
    mixed = slo_summary(_frame([{"STATUS": "MET", "BURN_MULTIPLE": None},
                                {"STATUS": "BREACH", "BURN_MULTIPLE": 3.0}]))
    assert mixed["has_burn"] == 1.0 and mixed["worst_burn"] == 3.0


def test_slo_summary_empty_carries_the_new_keys():
    out = slo_summary(pd.DataFrame())
    assert out["stale"] == 0.0 and out["has_burn"] == 0.0 and out["worst_burn"] == 0.0


# --- UI wiring (#9 caption, #10 n/a, #11 stale surfaced) -------------------

def test_slo_board_surfaces_the_trio():
    assert 'summary["stale"]' in _SRC              # #11 stale KPI + exception
    assert 'summary["has_burn"]' in _SRC           # #10 n/a gate
    assert '"n/a"' in _SRC
    assert "worst daily P95" in _SRC               # #9 P95-basis disclosure caption
