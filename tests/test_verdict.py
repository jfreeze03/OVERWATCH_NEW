"""Locks for the page-verdict primitive (CoCo do-first #1)."""
from pathlib import Path

import pandas as pd

from app.logic.verdict import (
    Signal,
    decision_studio_signals,
    oldest_open_hours,
    operations_signals,
    page_verdict,
)

_ROOT = Path(__file__).resolve().parents[1]


# Wave 1 #7 — the Operations page verdict signals
def test_operations_signals_from_score_inputs():
    # two days of per-day aggregates: 10% query fails (bad), 6% task fails, ~40 min/day
    # queueing (2400s/day = 40 min/day, bad); spill 15 GB/day (30 GB / 2 days) -> warn
    # (r28: spill is a PER-DAY onset of 5 GB/day, normalized by ndays like queue, not a
    # window total); plus 2 stale sources.
    df = pd.DataFrame({
        "QUERY_COUNT": [500, 500], "FAILED_COUNT": [50, 50],
        "TASK_RUNS": [100, 100], "TASK_FAILED": [6, 6],
        "QUEUED_SEC": [2400, 2400], "SPILL_GB": [15, 15],
    })
    sigs = operations_signals(df, stale_sources=2)
    phrases = " | ".join(s.phrase for s in sigs)
    assert any(s.level == "bad" and "query failures" in s.phrase for s in sigs)
    assert any("task failures" in s.phrase for s in sigs)
    assert any("warehouse queueing" in s.phrase for s in sigs)
    # r28: spill reads per-day (15 GB/day -> warn), phrased "GB/day", not a window total
    assert any("remote spill" in s.phrase and "GB/day" in s.phrase for s in sigs)
    assert "2 stale sources" in phrases
    # it composes into an Attention verdict
    assert page_verdict(sigs, healthy="ok")["level"] == "bad"


def test_operations_signals_healthy_and_empty_safe():
    clean = pd.DataFrame({
        "QUERY_COUNT": [1000], "FAILED_COUNT": [1], "TASK_RUNS": [200],
        "TASK_FAILED": [0], "QUEUED_SEC": [60], "SPILL_GB": [0],
    })
    assert operations_signals(clean, stale_sources=0) == []      # nothing over threshold
    assert operations_signals(None, stale_sources=0) == []       # missing frame is safe
    assert operations_signals(pd.DataFrame(), stale_sources=0) == []


def test_all_clear_is_healthy():
    v = page_verdict([], healthy="all systems nominal")
    assert v["level"] == "ok" and v["severity"] == "ok" and v["label"] == "Healthy"
    assert v["sentence"] == "Healthy — all systems nominal"


def test_warn_only_is_watch():
    v = page_verdict([Signal("warn", "1 source stale")], healthy="x")
    assert v["level"] == "warn" and v["label"] == "Watch"
    assert v["sentence"] == "Watch — 1 source stale"


def test_worst_level_wins_and_leads():
    v = page_verdict(
        [Signal("warn", "spend 18% over pace"), Signal("bad", "2 open criticals")],
        healthy="x",
    )
    assert v["level"] == "bad" and v["label"] == "Attention needed"
    # the bad concern leads; the warn follows
    assert v["body"].startswith("2 open criticals")
    assert "spend 18% over pace" in v["body"]


def test_multiple_bads_join_worst_first_stable():
    v = page_verdict(
        [Signal("warn", "w"), Signal("bad", "b1"), Signal("bad", "b2")], healthy="x"
    )
    assert v["body"] == "b1; b2; w"


def test_ok_blank_and_none_signals_are_ignored():
    v = page_verdict([Signal("", ""), Signal("ok", "fine"), None], healthy="clear")
    assert v["level"] == "ok" and v["body"] == "clear"


def test_verdict_line_is_wired_across_the_do_first_surfaces():
    # the primitive leads the Brief, Overview, Control Room, and Cost pages
    for rel in ("brief.py", "overview.py", "control_room.py", "cost.py"):
        src = (_ROOT / "app" / "ui" / "pages" / rel).read_text(encoding="utf-8")
        assert "page_verdict(" in src and "page_verdict_line(" in src, rel
        assert "from app.logic.verdict import" in src, rel


# Wave 2 #8 — the Decision Studio page verdict, derived from proof_verdict so it can
# never disagree with the scorecard banner it sits above.
def test_decision_studio_signals_map_proof_levels():
    # good -> no concern -> Healthy
    assert decision_studio_signals({"level": "good", "reasons": []}) == []
    assert page_verdict(decision_studio_signals({"level": "good"}), healthy="earning its keep")["level"] == "ok"
    # unproven -> a single warn (must NOT read as a green all-clear)
    uns = decision_studio_signals({"level": "unproven", "reasons": []})
    assert len(uns) == 1 and uns[0].level == "warn"
    assert page_verdict(uns, healthy="x")["level"] == "warn"
    # watch -> one warn per worst-first reason, composed worst-first
    watch = decision_studio_signals(
        {"level": "watch", "reasons": ["run cost not yet covered (0.4x)", "low realization (40%)"]})
    assert [s.level for s in watch] == ["warn", "warn"]
    v = page_verdict(watch, healthy="x")
    assert v["level"] == "warn" and v["body"].startswith("run cost not yet covered")
    # empty / None proof -> unproven-safe warn (never a false green)
    assert decision_studio_signals(None)[0].level == "warn"
    assert decision_studio_signals({})[0].level == "warn"


def test_decision_studio_verdict_is_wired():
    shell = (_ROOT / "app" / "ui" / "pages" / "decision_studio.py").read_text(encoding="utf-8")
    body = (_ROOT / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")
    # the shell renders the hoisted verdict line above the section bar
    assert "page_verdict_line(" in shell and "decision_verdict(" in shell
    # the body composes it from the same proof_verdict the scorecard banner uses, and
    # the scorecard + verdict read/compute through one shared helper
    assert "decision_studio_signals(" in body and "page_verdict(" in body
    assert "_proof_signals(" in body


def test_oldest_open_hours_filters_severity_and_handles_empty():
    now = pd.Timestamp("2026-08-16 12:00:00")
    frame = pd.DataFrame({
        "SEVERITY": ["CRITICAL", "HIGH", "CRITICAL"],
        "RAISED_AT": ["2026-08-15 12:00:00", "2026-08-14 00:00:00", "2026-08-16 06:00:00"],
    })
    assert oldest_open_hours(frame, now=now, severity="CRITICAL") == 24.0  # oldest CRITICAL
    assert oldest_open_hours(frame, now=now) == 60.0                       # oldest overall
    assert oldest_open_hours(None, now=now, severity="CRITICAL") is None
    assert oldest_open_hours(pd.DataFrame(), now=now) is None


def test_brief_surfaces_oldest_open_critical():
    # Label says "open" not "unacked": the feed is STATUS IN ('OPEN','ACK'), so an
    # ACK'd critical still drives this tile — "unacked" was a mislabel (audit fix).
    brief = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert "oldest_open_hours(" in brief and "Oldest open critical" in brief
