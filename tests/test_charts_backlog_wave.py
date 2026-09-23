"""Codex visual backlog wave (v4.586.0) — charts.py: rec38 (sparkline threads the real
measure + unit into its tooltip) and rec41 (genuine takeaway captions render ABOVE their
chart; caveats/legends stay below). Presentation-only; these lock the intended behavior."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.ui import charts

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "charts.py").read_text(encoding="utf-8")


class _FakeCol:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeSt:
    """Records caption/altair_chart call ORDER and captures chart specs; every other st.*
    call is a no-op. st.columns yields context-manager stand-ins (sparkline_row needs them)."""

    def __init__(self) -> None:
        self.order: list[tuple] = []
        self.specs: list[dict] = []
        self.captions: list[str] = []

    def columns(self, spec, **_k):
        n = spec if isinstance(spec, int) else len(spec)
        return [_FakeCol() for _ in range(n)]

    def caption(self, msg: str = "", **_k) -> None:
        self.order.append(("cap", str(msg)))
        self.captions.append(str(msg))

    def altair_chart(self, chart, **_k):
        self.order.append(("chart",))
        try:
            self.specs.append(chart.to_dict())
        except Exception:  # noqa: BLE001
            self.specs.append({})
        return None

    def __getattr__(self, _name):
        return lambda *a, **k: None


# --- rec38: sparkline units/tooltips -----------------------------------------

def test_rec38_sparkline_threads_measure_and_unit_into_the_tooltip(monkeypatch) -> None:
    fake = _FakeSt()
    monkeypatch.setattr(charts, "st", fake)
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-11", "2026-08-12"]), "QUERIES": [5000, 5432]})
    charts.sparkline_row([("Queries, 14d", df, "DAY", "QUERIES", "count")])
    tips = fake.specs[-1]["encoding"]["tooltip"]
    day_tip = next(t for t in tips if t.get("field") == "Day")
    val_tip = next(t for t in tips if t.get("field") == "ValueText")
    assert day_tip.get("format") == "%b %d, %Y" and day_tip.get("title") == "Day"   # real date, not midnight ts
    assert val_tip.get("title") == "Queries, 14d"                                    # the measure, not "Value"
    # count formatting flows through the shared _fmt_metric_value (5,432 not 5432.0)
    assert charts._fmt_metric_value(5432, "count") == "5,432"
    assert charts._fmt_metric_value(1240, "usd").startswith("$")


def test_rec38_backward_compatible_four_tuple_and_source_guard(monkeypatch) -> None:
    fake = _FakeSt()
    monkeypatch.setattr(charts, "st", fake)
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-11", "2026-08-12"]), "V": [1, 2]})
    charts.sparkline_row([("Legacy", df, "DAY", "V")])   # 4-tuple, no unit -> must not raise
    # the generic tooltip regression is locked out
    assert 'alt.Tooltip("ValueText:N", title=label)' in _SRC
    assert 'tooltip=["Day:T", "Value:Q"]' not in _SRC


# --- rec41: takeaway above / footnote below ----------------------------------

def _order(monkeypatch, fn, *args, **kwargs) -> list:
    fake = _FakeSt()
    monkeypatch.setattr(charts, "st", fake)
    fn(*args, **kwargs)
    return fake.order


def test_rec41_bar_usd_takeaway_precedes_chart(monkeypatch) -> None:
    df = pd.DataFrame({"L": ["A", "B"], "USD": [75.0, 25.0]})
    tags = [o[0] for o in _order(monkeypatch, charts.bar_usd, df, "L", "USD", takeaway=True)]
    assert "cap" in tags and "chart" in tags and tags.index("cap") < tags.index("chart")


def test_rec41_daily_metric_line_takeaway_precedes_chart(monkeypatch) -> None:
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-11", "2026-08-12"]), "V": [3.0, 9.0]})
    tags = [o[0] for o in _order(monkeypatch, charts.daily_metric_line, df, "DAY", "V")]
    assert tags.index("cap") < tags.index("chart")


def test_rec41_hour_heatmap_takeaway_above_but_capped_caveat_below(monkeypatch) -> None:
    rows = [{"R": f"WH_{i}", "H": 3, "V": float(i + 1)} for i in range(charts.HEATMAP_MAX_ROWS + 3)]
    order = _order(monkeypatch, charts.hour_heatmap, pd.DataFrame(rows), "R", "H", "V")
    tags = [o[0] for o in order]
    assert tags == ["cap", "chart", "cap"]           # Hottest takeaway, chart, then the cap caveat
    assert "Hottest" in order[0][1]
    assert f"Top {charts.HEATMAP_MAX_ROWS} of" in order[2][1]


def test_rec41_lag_footnote_stays_below_the_chart() -> None:
    # spend_trend's legend + "metering lags" caveat is a FOOTNOTE — it must stay AFTER the chart
    # (rec41 promotes conclusions, never caveats).
    body = _SRC.split("def spend_trend", 1)[1].split("\ndef ", 1)[0]
    assert body.index("st.altair_chart") < body.index("Newest day is dimmed")
