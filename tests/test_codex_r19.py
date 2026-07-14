"""Codex r19 locks — verified page-level ships (2026-07-12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import cost_sql, mart_sql
from app.logic import actions as actions_logic

# skip cleanly on the CI floor-compat job, which installs no sqlglot
sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]
_CR = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
_OPS = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
_CMP = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")


def test_day_replay_loads_on_demand():
    gate = _CR.split('key="cr_replay_on"', 1)[0]
    assert "st.toggle(" in gate.rsplit("st.divider()", 1)[1]  # gated after the divider
    assert "_day_replay()" in _CR.split('key="cr_replay_on"', 1)[1][:400]


def test_action_queue_filters_open_in_sql_and_mirrors_logic():
    sql = mart_sql.action_queue(200)
    for status in actions_logic.OPEN_STATUSES:            # cross-lock: same set
        assert f"'{status}'" in sql
    assert "UPPER(STATUS) IN" in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_failure_timeline_skips_scan_when_summary_says_zero():
    assert "known_failures: float | None = None" in _OPS
    assert "if known_failures is not None and known_failures <= 0:" in _OPS
    assert "if days >= 7 else None" in _OPS               # 7d detail needs >=7d summary


def test_storage_builders_use_monthly_average_billing_basis():
    # F1 (2026-07-14): Snowflake bills storage on the monthly average of daily
    # bytes, not a latest-day snapshot. Both builders now AVG the window per
    # database (superseding the r19 QUALIFY-latest-day snapshot).
    for sql in (cost_sql.storage_by_database(90, "ALFA"),
                cost_sql.storage_by_database_live(90, "ALFA")):
        assert "AVG(COALESCE(" in sql
        assert "QUALIFY DAY = MAX(DAY) OVER ()" not in sql
        assert "DAYS_AVERAGED" in sql
        sqlglot.parse(sql, dialect="snowflake")


def test_contention_uses_plain_run_not_a_one_member_batch():
    tab = _OPS.split("def _contention_tab", 1)[1].split("\ndef ", 1)[0]
    assert "run_batch" not in tab


def test_exports_cache_bytes_and_never_rerun_on_download():
    assert _CMP.count('on_click="ignore"') == 3           # two table paths + text
    for pg in ("overview.py", "security.py"):
        _p = (_ROOT / "app" / "ui" / "pages" / pg).read_text(encoding="utf-8")
      