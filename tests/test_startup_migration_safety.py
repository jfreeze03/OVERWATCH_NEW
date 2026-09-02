"""Startup compatibility gate (#4) + impact-annotated migration punch list (#5), v4.431.0.

Both address the migration-lag problem: a build that outruns the applied schema should
fail with ONE actionable state, and pending fixes should be visible as what-runs-wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

import app.config as config
import app.main as m
from app.core.result import QueryResult

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _fixed_run(qr: QueryResult):
    return lambda *a, **k: qr


# --- #4: the floor gate blocks only below the floor ------------------------------------
def test_schema_floor_gate_blocks_only_below_floor(monkeypatch):
    floor = config.REQUIRED_SCHEMA_FLOOR
    # exactly at the floor -> proceed (None)
    monkeypatch.setattr(m, "run", _fixed_run(
        QueryResult(df=pd.DataFrame({"VERSION": [1, 50, floor]}), ok=True)))
    assert m._schema_floor_breach() is None
    # above the floor -> proceed
    monkeypatch.setattr(m, "run", _fixed_run(
        QueryResult(df=pd.DataFrame({"VERSION": [floor + 30]}), ok=True)))
    assert m._schema_floor_breach() is None
    # below the floor -> returns the live max so main() can name it in the blocked state
    monkeypatch.setattr(m, "run", _fixed_run(
        QueryResult(df=pd.DataFrame({"VERSION": [1, 10, 20]}), ok=True)))
    assert m._schema_floor_breach() == 20


# --- #4: the gate fails OPEN, never bricking a working install --------------------------
def test_schema_floor_gate_fails_open(monkeypatch):
    monkeypatch.setattr(m, "run", _fixed_run(QueryResult(ok=False, error="boom")))
    assert m._schema_floor_breach() is None                      # read error -> proceed
    monkeypatch.setattr(m, "run", _fixed_run(QueryResult(ok=True)))
    assert m._schema_floor_breach() is None                      # empty -> proceed
    monkeypatch.setattr(m, "run", _fixed_run(
        QueryResult(df=pd.DataFrame({"VERSION": ["x", None]}), ok=True)))
    assert m._schema_floor_breach() is None                      # junk -> proceed


# --- #4: the app floor and the deploy validate teeth are one number, and it is wired ----
def test_floor_matches_validate_teeth_and_is_wired():
    v = _src("snowflake/validate.sql")
    teeth = int(re.search(r"n_versions < (\d+)\)", v).group(1))
    assert teeth == config.REQUIRED_SCHEMA_FLOOR
    mainsrc = _src("app/main.py")
    assert '_schema_floor_breach() if page != "Admin" else None' in mainsrc
    assert "def _schema_floor_breach()" in mainsrc


# --- #5: missing migrations render as a per-row impact punch list -----------------------
def test_missing_migrations_render_as_per_row_punch_list():
    a = _src("app/ui/pages/admin.py")
    blk = a.split("def _migrations_tab", 1)[1][:3000]
    assert "for n, name in missing:" in blk
    assert 'st.markdown(md_dollars(f"- **V{n:03d}** — {name}"))' in blk
    # the old single comma-joined blob is gone
    assert 'Missing migrations: " + ", ".join(' not in a
