"""Locks for the Codex r8 adopt batch (v4.23.0)."""

from __future__ import annotations

from pathlib import Path

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_run_batch_callers_trust_the_contract():
    for page in ("brief.py", "operations.py", "security.py"):
        txt = (_ROOT / "app" / "ui" / "pages" / page).read_text(encoding="utf-8")
        assert ") or {}" not in txt, page          # dict guaranteed since v4.20


def test_new_readers_use_the_shared_literal_helper():
    src = (_ROOT / "app" / "data" / "mart27_sql.py").read_text(encoding="utf-8")
    for fn in ("def lock_wait_daily", "def lock_wait_spikes"):
        body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert "companies.sql_literal(company)" in body, fn
    # output contract unchanged
    assert "(c.COMPANY = 'ALFA' OR UPPER(c.COMPANY) = 'ALL')" in mart27_sql.lock_wait_daily(14, "ALFA")
    assert "''" in mart27_sql.lock_wait_daily(7, "x'y")


def test_spike_reader_is_quiet_conservative_and_canaried():
    sql = mart27_sql.lock_wait_spikes("ALFA")
    assert "MART_LOCK_WAIT_DAILY" in sql and "ACCOUNT_USAGE" not in sql   # mart-only by design
    assert "3 * GREATEST(g.PRIOR_DAILY_AVG, 1)" in sql                    # floor stops div-by-calm
    assert "g.LAST_DAY_WAITS >= 5" in sql                                 # absolute floor too
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "mart27_sql.lock_wait_spikes" in canary
    cr = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")
    assert 'key=f"cr_lockspike_{company}"' in cr                          # triage filter honored
    assert "if _spk.ok and not _spk.empty:" in cr                         # silent pre-V035


def test_admin_drilldown_and_why_stale():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert 'key="adm_tt_sel"' in adm                                      # targets are clickable
    assert "the slow keys behind the pain" in adm
    assert "Why stale?" in adm
    assert "never filled" in adm and "last loader error" in adm           # causes, not raw errors
    assert "tasks suspend if a migration half-applied" in adm


def test_lock_panel_names_its_source():
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    tab = ops.split("def _contention_tab", 1)[1].split("\ndef ", 1)[0]
    assert "result_caption(res)" in tab
