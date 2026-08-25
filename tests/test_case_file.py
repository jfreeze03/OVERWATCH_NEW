"""Operator Case File (#10): pure logic — assembly, escaping, dedup, capping.

app/logic/case_file.py is Streamlit-free and deterministic (timestamps passed in),
so the markdown handoff document is fully unit-testable. Session-only, no migration.
"""

from __future__ import annotations

from pathlib import Path

from app.logic import case_file as cf

_ROOT = Path(__file__).resolve().parents[1]


def _item(**over) -> dict:
    base = {"section": "Alerts", "company": "ALFA", "window": "Last 7 days", "days": 7,
            "source": "ACCOUNT_USAGE.QUERY_HISTORY", "fetched_at": "12:30:00", "tier": "recent",
            "summary": "Task LOAD failing", "next_action": "Investigate rule PERF_X",
            "title": "[CRITICAL] LOAD failing"}
    base.update(over)
    return cf.new_case_item(**base)


# ============================================ assemble_markdown ==============

def test_empty_case_is_empty_string():
    assert cf.assemble_markdown([], generated="2026-08-25 12:00") == ""


def test_two_item_assembly_structure():
    items = [_item(), _item(section="Security", title="Impossible travel", company="Trexis",
                            source="ACCOUNT_USAGE.LOGIN_HISTORY")]
    md = cf.assemble_markdown(items, generated="2026-08-25 12:00")
    assert md.startswith("# Operator Case File — 2026-08-25 12:00")
    assert "## 1. [CRITICAL] LOAD failing" in md and "## 2. Impossible travel" in md
    assert "**Scope:** ALFA · Last 7 days · 7d" in md
    assert "**Source:** ACCOUNT_USAGE.QUERY_HISTORY" in md
    assert "2 item(s)" in md and "ALFA, Trexis" in md


def test_preview_table_rendered_and_escaped():
    it = _item(preview_columns=["QUERY_ID", "NOTE"],
               preview_rows=[["q1", "a | b\nc"], ["q2", "ok"]])
    md = cf.assemble_markdown([it], generated="g")
    assert "| QUERY_ID | NOTE |" in md and "| --- | --- |" in md
    # the pipe is escaped and the newline collapsed so the table row never breaks
    assert "a \\| b c" in md and "\n" not in "a \\| b c"


# ============================================ escaping / capping =============

def test_escape_md_cell():
    assert cf.escape_md_cell("a|b") == "a\\|b"
    assert cf.escape_md_cell("line1\nline2") == "line1 line2"
    assert cf.escape_md_cell(None) == ""
    assert cf.escape_md_cell("  x\t y ") == "x  y"


def test_escape_backslash_before_pipe():
    # x\|y : the backslash is escaped FIRST so the pipe stays a literal cell char,
    # not a live column delimiter that would add a phantom column and break the row.
    assert cf.escape_md_cell("x\\|y") == "x\\\\\\|y"
    assert cf.escape_md_cell("C:\\Users\\x") == "C:\\\\Users\\\\x"


def test_preview_is_capped():
    cols = [f"C{i}" for i in range(20)]
    rows = [[f"r{r}c{c}" for c in range(20)] for r in range(50)]
    it = cf.new_case_item(section="Ops", preview_columns=cols, preview_rows=rows)
    assert len(it["preview"]["columns"]) == cf.MAX_PREVIEW_COLS
    assert len(it["preview"]["rows"]) == cf.MAX_PREVIEW_ROWS
    assert all(len(r) == cf.MAX_PREVIEW_COLS for r in it["preview"]["rows"])
    assert it["truncated"] is True


# ============================================ dedup / remove / clear =========

def test_add_dedups_identical_evidence():
    items: list = []
    items = cf.add_item(items, _item())
    items = cf.add_item(items, _item())                 # same fingerprint -> no-op
    assert len(items) == 1
    items = cf.add_item(items, _item(company="Trexis"))  # different scope -> added
    assert len(items) == 2


def test_dedup_ignores_timestamps():
    a = _item(added_at="10:00", fetched_at="10:00:00")
    b = _item(added_at="11:00", fetched_at="11:00:00")
    assert a["id"] == b["id"]                            # id excludes timestamps
    assert len(cf.add_item([a], b)) == 1


def test_remove_and_clear():
    items = cf.add_item(cf.add_item([], _item()), _item(company="Trexis"))
    assert len(items) == 2
    items = cf.remove_item(items, items[0]["id"])
    assert len(items) == 1 and items[0]["company"] == "Trexis"
    assert cf.clear_items(items) == []


# ============================================ wiring ==========================

def test_component_and_wiring_present():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "def add_to_case_button(" in comp and "case_file.add_item" in comp
    brief = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert "assemble_markdown(" in brief and "Operator Case File" in brief
    # at least the v1 high-value sites adopt the control
    hits = sum(
        "add_to_case_button(" in (_ROOT / "app" / "ui" / "pages" / p).read_text(encoding="utf-8")
        for p in ("alerts.py", "operations.py", "security.py", "overview.py")
    )
    assert hits >= 3
