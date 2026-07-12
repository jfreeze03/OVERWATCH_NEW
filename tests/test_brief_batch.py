"""Locks for the Brief tuning (v4.19.0, live round 10): ten serial reads
became two tier-grouped batches — the exec page must be the fastest page.
Serial fallbacks keep the original keys/tiers; the honesty contract
(telemetry-unreachable warning, company scoping, zero live scans) survives."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BRIEF = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")


def test_brief_reads_go_out_as_two_batches():
    assert _BRIEF.count("run_batch(") == 2                    # live + recent groups
    assert _BRIEF.count(") or run(") == 8                     # every BATCHED read keeps its serial fallback
    # (health_strip left the batch at r15 #14 — it shares the app shell's
    # run() cache entry under key="health_strip" instead of paying twice)
    assert '"key": "strip"' not in _BRIEF
    # v4.23: `or {}` dropped — run_batch guarantees a dict since v4.20
    assert 'tier="live")' in _BRIEF and 'tier="recent")' in _BRIEF
    assert ') or {}' not in _BRIEF


def test_brief_keeps_its_honesty_and_scope():
    assert _BRIEF.count("ACCOUNT_USAGE") == 0                 # budget stays zero
    assert "refuses to invent numbers" in _BRIEF              # telemetry honesty survives
    assert 'company = filters()["company"]' in _BRIEF         # hoisted once
    assert _BRIEF.count('filters()["company"]') == 1
    assert "open_incidents(5, company)" in _BRIEF             # triage-filter law in the batch
    assert "open_alert_events(50, company)" in _BRIEF
