"""Locks for the second-tier batch (v4.21.0): lock waits carry their
environment (owner ask), KPI cards carry source badges, Admin ranks the
next tuning targets from telemetry — not opinions."""

from __future__ import annotations

from pathlib import Path

from app.data import ops_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_lock_waits_carry_database_and_schema():
    sql = ops_sql.lock_contention(7)
    assert "DATABASE_NAME" in sql and "SCHEMA_NAME" in sql        # which environment
    assert "GROUP BY 1, 2, 3, 4" in sql                           # grain widened to match
    assert "OBJECT_NAME" in sql and "NEVER_ACQUIRED" in sql       # original contract kept


def test_kpi_cards_carry_source_badges():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = comp.split("def metric_card_html", 1)[1].split("\ndef ", 1)[0]
    assert 'item.get("badge"' in body
    assert '"mart": "#34d399"' in body and '"stale": "#fbbf24"' in body
    brief = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert '"badge": "mart" if strip_up else "stale"' in brief    # money KPI says its source
    assert '"badge": "live" if strip_up else "stale"' in brief


def test_admin_ranks_next_tuning_targets_from_telemetry():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "Next tuning targets" in adm
    assert '_tt["PAIN"]' in adm                                   # pain = p95 x slow count
    assert "the telemetry picks, not opinions" in adm             # no speculative fix text
