"""Control Room database filter (Joe 2026-07-11: "i should be able to
filter in Control room using database").

Grain law: panels whose source carries DATABASE_NAME follow the filter
(pulse, activity spark, tasks, lock spikes); panels without the grain say
so instead of silently ignoring it (timeline, spend movers, triage note)."""

from __future__ import annotations

from pathlib import Path

from app.data import mart27_sql, mart_sql

_CR = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages"
       / "control_room.py").read_text(encoding="utf-8")


def test_activity_spark_takes_company_and_database():
    plain = mart_sql.fact_daily_activity(14)
    assert "COMPANY" not in plain and "DATABASE_NAME" not in plain  # old shape
    scoped = mart_sql.fact_daily_activity(14, "ALFA", "ALFA_EDW_PRD")
    assert "COMPANY = 'ALFA'" in scoped
    assert "ALFA_EDW_PRD" in scoped


def test_lock_spikes_take_database():
    plain = mart27_sql.lock_wait_spikes("ALFA")
    assert "c.DATABASE_NAME" in plain and "ALFA_EDW_PRD" not in plain
    scoped = mart27_sql.lock_wait_spikes("ALFA", "ALFA_EDW_PRD")
    assert "ALFA_EDW_PRD" in scoped


def test_control_room_wires_the_filter_and_labels_the_rest():
    assert 'fact_daily_activity(14, company, f["database"])' in _CR
    assert 'lock_wait_spikes(company, f["database"])' in _CR
    # honest labels where the grain doesn't exist
    assert "the database filter doesn't apply here" in _CR      # timeline
    assert "the database filter doesn't narrow this" in _CR     # movers
    assert "don't have database grain" in _CR                   # triage note
    # the page states its scope when the filter is on
    assert '''f" · {f['database']}" if f["database"] else ""''' in _CR
