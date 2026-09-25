"""snowflake/resettle_autobook_14d.sql: the owner opt-in that re-settles pre-V153 autobook rows (O-8).

V153 fixes the autobook going forward; this script restates rows settled before it. Its plan must mirror
V153's settle logic exactly, and grid 2 (the only write) must stay commented until the owner decides.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SQL = (_ROOT / "snowflake" / "resettle_autobook_14d.sql").read_text(encoding="utf-8")
_V153 = (_ROOT / "snowflake" / "migrations" / "V153__ledger_autobook_full_window_settle.sql").read_text(
    encoding="utf-8")
_CLOSED = "r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')"


def _grid2() -> str:
    lines = _SQL.split("-- GRID 2 (WRITES -- commented)", 1)[1].splitlines()[2:]
    body = [ln[3:] if ln.startswith("-- ") else ln[2:] for ln in lines if ln.startswith("--")]
    return "\n".join(body).strip()


def test_grid2_is_commented_and_the_only_write():
    live = "\n".join(ln for ln in _SQL.splitlines() if not ln.lstrip().startswith("--"))
    assert "UPDATE " not in live and "DELETE " not in live and "INSERT " not in live
    g2 = _grid2()
    assert g2.startswith("UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l") and "WHERE l.ITEM_ID = p.ITEM_ID" in g2


def test_plan_mirrors_the_v153_settle_logic():
    for frag in (_CLOSED, "CURRENT_DATE() > r.TRACKING_UNTIL", "r.AFTER_CREDITS_PER_DAY IS NOT NULL",
                 "r.AFTER_CREDITS_PER_DAY IS NULL", "TRY_TO_DOUBLE(", "WH_USD >= 5"):
        assert frag in _SQL, frag
    live = "\n".join(ln for ln in _SQL.splitlines() if not ln.lstrip().startswith("--"))
    assert "TRY_TO_NUMBER" not in live and "TRY_TO_NUMBER" not in _grid2()   # the pre-V153 rate defect
    for frag in ("CURRENT_DATE() > r.TRACKING_UNTIL", "AFTER_CREDITS_PER_DAY IS NOT NULL", "TRY_TO_DOUBLE("):
        assert frag in _V153, frag                                   # ...the same gates V153 settles on
    # the LBA-1 row number (co-attributed rows book $0) partitions exactly like the proc
    rn = re.search(r"PARTITION BY r\.WAREHOUSE_NAME, r\.BASELINE_CREDITS_PER_DAY,\s+r\.AFTER_CREDITS_PER_DAY, r\.AFTER_DAYS",
                   _SQL)
    assert rn


def test_scope_is_auto_settled_rows_only_and_idempotent():
    assert "l2.VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK'" in _SQL      # never a hand-verified row
    assert "NOT LIKE '%re-settled on the full 14-day window%'" in _SQL  # a re-run is a no-op
    assert "re-settled on the full 14-day window (owner opt-in" in _grid2()
    assert "VERIFIED_AT" not in _grid2().split(" SET ", 1)[1].split("FROM (", 1)[0]   # never moves a quarter


def test_every_grid_parses():
    sqlglot = pytest.importorskip("sqlglot")
    live = "\n".join(ln for ln in _SQL.splitlines() if not ln.lstrip().startswith("--"))
    stmts = [s for s in sqlglot.parse(live, read="snowflake") if s is not None]
    assert len(stmts) >= 4                                          # USE, ALTER SESSION, grid 1, grid 1b
    upd = sqlglot.parse_one(_grid2(), read="snowflake")
    assert type(upd).__name__ == "Update"
