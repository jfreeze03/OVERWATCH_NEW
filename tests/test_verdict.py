"""Locks for the page-verdict primitive (CoCo do-first #1)."""
from pathlib import Path

from app.logic.verdict import Signal, page_verdict

_ROOT = Path(__file__).resolve().parents[1]


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
