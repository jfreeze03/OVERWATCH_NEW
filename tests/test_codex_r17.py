"""Codex r17 lock — the one new item in a fourth convergent round.

Expander bodies execute even when collapsed, so an optional live scan
inside one runs on every page paint. The scan must wait for an explicit
toggle (the deep-scan forensics pattern)."""

from __future__ import annotations

from pathlib import Path

_AI = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages"
       / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")


def test_ai_functions_scan_waits_for_the_toggle():
    block = _AI.split('AI Functions usage (optional view)', 1)[1]
    toggle_at = block.find("st.toggle(")
    run_at = block.find('key=f"cortex_fn_')
    assert toggle_at != -1 and run_at != -1
    assert toggle_at < run_at  # gate BEFORE the query, inside the expander
