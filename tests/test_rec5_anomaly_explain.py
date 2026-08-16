"""rec#5: anomaly root-cause auto-explain — decompose a flagged day's spend delta
across warehouses. Contributions must sum to the total delta (nothing hides in a
residual), and the narrative must name the true driver.
"""

from datetime import date, timedelta

import pandas as pd

from app.logic.anomaly_explain import explain_by_warehouse

_FLAG = date(2026, 7, 20)


def _frame(spike_wh: str | None = None, spike_usd: float = 0.0) -> pd.DataFrame:
    rows = []
    for i in range(1, 21):                       # 20 complete days before the flagged day
        day = _FLAG - timedelta(days=i)
        for wh, base in (("WH_A", 100.0), ("WH_B", 50.0)):
            rows.append({"DAY": day, "WAREHOUSE_NAME": wh, "USD": base})
    for wh, base in (("WH_A", 100.0), ("WH_B", 50.0)):   # the flagged day itself
        rows.append({"DAY": _FLAG, "WAREHOUSE_NAME": wh,
                     "USD": base + (spike_usd if wh == spike_wh else 0.0)})
    return pd.DataFrame(rows)


def _driver(exp, name):
    return next(d for d in exp.drivers if d.name == name)


def test_contributions_sum_to_the_total_delta():
    exp = explain_by_warehouse(_frame("WH_A", 400.0), _FLAG)
    assert exp.ok
    assert abs(sum(d.delta_usd for d in exp.drivers) - exp.total_delta_usd) < 0.01
    assert abs(exp.total_delta_usd - 400.0) < 0.01              # only WH_A spiked +400
    # total_actual - total_baseline reconciles too
    assert abs((exp.total_actual_usd - exp.total_baseline_usd) - exp.total_delta_usd) < 0.01


def test_top_driver_is_the_spiking_warehouse_with_full_share():
    exp = explain_by_warehouse(_frame("WH_A", 400.0), _FLAG)
    assert exp.drivers[0].name == "WH_A"
    assert exp.drivers[0].delta_usd == 400.0
    assert exp.drivers[0].share_pct == 100.0
    assert "WH_A" in exp.narrative and "above" in exp.narrative


def test_new_warehouse_contributes_its_whole_spend():
    frame = pd.concat([_frame(), pd.DataFrame(
        [{"DAY": _FLAG, "WAREHOUSE_NAME": "WH_NEW", "USD": 200.0}])], ignore_index=True)
    exp = explain_by_warehouse(frame, _FLAG)
    new = _driver(exp, "WH_NEW")
    assert new.baseline_usd == 0.0 and new.delta_usd == 200.0   # no history -> all of it is new


def test_a_silent_warehouse_is_a_negative_driver():
    frame = _frame()
    frame = frame[~((frame["DAY"] == _FLAG) & (frame["WAREHOUSE_NAME"] == "WH_A"))]
    exp = explain_by_warehouse(frame, _FLAG)
    quiet = _driver(exp, "WH_A")
    assert quiet.actual_usd == 0.0 and quiet.delta_usd == -100.0
    assert "below" in exp.narrative                            # net move is down


def test_an_inline_day_reports_no_material_move():
    exp = explain_by_warehouse(_frame(), _FLAG)                # no spike
    assert exp.ok and not exp.drivers
    assert "in line" in exp.narrative


def test_empty_or_missing_inputs_return_not_ok():
    assert not explain_by_warehouse(pd.DataFrame(), _FLAG).ok
    assert not explain_by_warehouse(_frame(), None).ok
    assert not explain_by_warehouse(_frame().drop(columns=["WAREHOUSE_NAME"]), _FLAG).ok


def test_drill_is_wired_into_the_spend_anomaly_panel():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "cost_parts"
           / "spend.py").read_text(encoding="utf-8")
    assert "explain_by_warehouse(" in src
    assert "root-cause waterfall" in src
