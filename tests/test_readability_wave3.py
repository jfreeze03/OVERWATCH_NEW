"""Regression locks for the v4.133.0 readability follow-through."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.core.result import QueryResult

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_rec33_totals_caption_precedes_table(monkeypatch):
    from app.ui import components

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(components.st, "caption", lambda value: calls.append(("caption", value)))
    monkeypatch.setattr(
        components.st,
        "dataframe",
        lambda *args, **kwargs: calls.append(("table", "")),
    )

    components._render_table(
        pd.DataFrame({"USD": [1.0]}),
        height=None,
        column_config=None,
        totals=(("Current spend", "$1.00"), ("Prior spend", "$2.00")),
    )

    assert calls == [
        ("caption", "Σ Current spend: $1.00 · Prior spend: $2.00"),
        ("table", ""),
    ]


def test_rec33_cost_tables_wire_additive_totals():
    components = _src("app/ui/components.py")
    assert components.count("totals: tuple = ()") >= 2
    assert "sort_label=sort_label, totals=totals" in components

    chargeback = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert 'totals=(("Total spend", format_usd(total_usd)),),' in chargeback

    spend = _src("app/ui/pages/cost_parts/spend.py")
    current_total = 'window_usd = float(view["USD_CURRENT"].sum())'
    prior_total = 'prior_window_usd = float(view["USD_PRIOR"].sum())'
    table = "styled_table(  # rec21"
    assert spend.index(current_total) < spend.index(table)
    assert spend.index(prior_total) < spend.index(table)
    assert '(("Current spend", format_usd(window_usd)),' in spend
    assert '("Prior spend", format_usd(prior_window_usd)))' in spend


def test_rec28_mtd_card_has_credits_on_every_data_outcome(monkeypatch):
    from app.ui.pages import overview

    monkeypatch.setattr(overview, "account_today", lambda: date(2026, 8, 3))
    frame = pd.DataFrame({
        "DAY": [date(2026, 7, 1), date(2026, 7, 2),
                date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)],
        "CREDITS_BILLED": [1.0, 1.0, 2.0, 2.0, 2.0],
    })

    paced = overview._mtd_pace_kpi(0.0, QueryResult(df=frame), 4.0, 2.0, 0.0)
    assert paced["sub"] == "6.00 cr"

    no_prior = overview._mtd_pace_kpi(
        0.0,
        QueryResult(df=frame[frame["DAY"] >= date(2026, 8, 1)]),
        4.0,
        2.0,
        0.0,
    )
    assert no_prior["sub"] == "6.00 cr"

    fallback = overview._mtd_pace_kpi(
        40.0,
        QueryResult(ok=False),
        4.0,
        2.0,
        0.0,
    )
    assert fallback["sub"] == "10.00 cr"


def test_rec28_mtd_credits_are_column_independent():
    overview = _src("app/ui/pages/overview.py")
    body = overview.split("def _mtd_pace_kpi", 1)[1].split("@safe_page", 1)[0]
    assert "_mtd_credits = safe_float(mtd_spend) / rate" in body
    assert "_mtd_credits = safe_float(mtd) / rate" in body
    assert 'frame["CREDITS' not in body


def test_rec30_task_sort_label_is_assigned_after_sort():
    operations = _src("app/ui/pages/operations.py")
    body = operations.split("def _task_health_view", 1)[1].split("\ndef ", 1)[0]
    default_pos = body.index('task_sort_label = ""')
    sort_pos = body.index("df = df.sort_values(failed_col, ascending=False)")
    label_pos = body.index('task_sort_label = "failed runs desc"')
    table_pos = body.index("styled_table(df, sort_label=task_sort_label)")
    assert default_pos < sort_pos < label_pos < table_pos
