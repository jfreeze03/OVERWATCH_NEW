"""v4.604.0 review r1 R1-6: the heavy-toggle cost hint humanizes its runtime.

components.toggle_cost_hint rendered f"~{ms / 1000:.1f}s", so the 35-90-day cluster-cap scan (and every other
heavy toggle, ~12 call sites) read "Last run took ~145.0s" — the raw-seconds format the duration rule bans. It
now goes through the shared formulas.humanize_duration ("2m 25s"), and a cached rerun (a few ms, cache_hit
True) no longer masks the real scan's time once one was recorded this session."""

from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("streamlit")


def _hint(monkeypatch, rows: list[dict], key: str = "cluster_use") -> str:
    import app.core.query as query
    from app.ui.components import toggle_cost_hint

    monkeypatch.setattr(query, "query_telemetry", lambda: pd.DataFrame(rows))
    return toggle_cost_hint(key)


def _row(key: str, ms: float, cache_hit: object = False) -> dict:
    return {"key": key, "elapsed_ms": ms, "cache_hit": cache_hit, "ok": True}


def test_a_multi_minute_scan_is_humanized(monkeypatch):
    hint = _hint(monkeypatch, [_row("cluster_use_ALL_35", 145_000.0)])
    assert hint == "Last run took ~2m 25s this session (cached repeats are instant)."
    assert "145.0s" not in hint
    assert "~1h 5m" in _hint(monkeypatch, [_row("cluster_use_ALL_90", 3_900_000.0)])
    assert "~8.4s" in _hint(monkeypatch, [_row("cluster_use_ALL_35", 8_400.0)])      # sub-10s keeps a decimal


def test_a_cached_rerun_does_not_mask_the_scan(monkeypatch):
    rows = [_row("cluster_use_ALL_35", 145_000.0, cache_hit=False),
            _row("cluster_use_ALL_35", 4.0, cache_hit=True),
            _row("other_key", 9_000.0, cache_hit=False)]
    assert "~2m 25s" in _hint(monkeypatch, rows)
    # None (a failed read's telemetry) is not a cache hit; an all-bool column (numpy bools) filters the same
    assert "~3m" in _hint(monkeypatch, [_row("cluster_use_ALL_35", 180_000.0, cache_hit=None),
                                        _row("cluster_use_ALL_35", 3.0, cache_hit=True)])
    only_cached = [_row("cluster_use_ALL_35", 4.0, cache_hit=True)]
    assert "~4ms" in _hint(monkeypatch, only_cached)                    # nothing else recorded: say what ran
    assert "~2m 25s" in _hint(monkeypatch, rows[:2])


def test_unknown_stays_first_run(monkeypatch):
    assert _hint(monkeypatch, []) == "First run this session — expect a live scan."
    assert _hint(monkeypatch, [_row("other_key", 9_000.0)]) == "First run this session — expect a live scan."
