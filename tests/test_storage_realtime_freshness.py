"""Real-time current-setting + mart-freshness hardening pass (round-37).

Bundles the two deferred storage cousins:

A) Retention remediation now reads the CURRENT DATA_RETENTION_TIME_IN_DAYS LIVE (INFORMATION_SCHEMA.TABLES)
   at decision time instead of the ~1-2h-lagged storage-scan value — so a retention lowered elsewhere
   can't be silently raised back (the A3 class the auto-suspend guard already avoids). Fail-closed: an
   unavailable / exotic-identifier read -> no ALTER (insights_sql.table_retention_live raises on unsafe db).
B) The per-table storage mart readers expose SNAPSHOT_DAY, and the three mart-first call sites gate on
   components.storage_snapshot_fresh so a multi-day-stale MART_TABLE_STORAGE_DAILY snapshot yields to the
   live TABLE_STORAGE_METRICS scan instead of serving out-of-date bytes as current.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.data import insights_sql, mart_sql
from app.logic.formulas import account_today
from app.ui.components import storage_snapshot_fresh

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- A) live retention read -------------------------------------------------
def test_table_retention_live_reads_information_schema_realtime():
    sql = insights_sql.table_retention_live("DB1", "PUBLIC", "EVENTS")
    assert "RETENTION_TIME AS RETENTION_DAYS" in sql
    assert "DB1.INFORMATION_SCHEMA.TABLES" in sql          # the DB's live metadata view, not ACCOUNT_USAGE
    assert "TABLE_SCHEMA = 'PUBLIC'" in sql                # schema/table are literals (injection-safe)
    assert "TABLE_NAME = 'EVENTS'" in sql
    assert "ACCOUNT_USAGE" not in sql                      # not the ~1-2h-lagged source


def test_table_retention_live_fails_closed_on_unsafe_database():
    # an exotic / injection-shaped database name raises (caller catches -> no ALTER)
    with pytest.raises(ValueError):
        insights_sql.table_retention_live("bad; DROP TABLE x", "PUBLIC", "EVENTS")


def test_retention_panel_wires_the_live_read_and_fails_closed():
    optimize = _read("app/ui/pages/cost_parts/optimize.py")
    assert "insights_sql.table_retention_live(" in optimize
    # fail-closed default: unknown until the live read succeeds
    assert "_ret_known, _cur_ret = False, 0.0" in optimize
    # the ALTER/savings still gate on _ret_known + keep_days < _cur_ret (existing guard, now on live data)
    assert "_can_reduce = _ret_known and _cur_ret > 0 and int(keep_days) < _cur_ret" in optimize
    # an exotic (quote-requiring) schema/table identifier fails closed at the gate too, so the ALTER
    # build (remediation.retention_fix) can never crash the render on an unscriptable name
    assert '_safe_ident(str(wrow["SCHEMA_NAME"]))' in optimize
    assert '_safe_ident(str(wrow["TABLE_NAME"]))' in optimize


# --- B) storage-mart snapshot freshness ------------------------------------
def test_storage_marts_expose_snapshot_day():
    for sql in (mart_sql.table_storage_waste_mart("ALL"),
                mart_sql.table_storage_breakdown_mart("ALL")):
        assert "(SELECT MAX(DAY) FROM" in sql and "AS SNAPSHOT_DAY" in sql


def test_storage_snapshot_fresh_accepts_fresh_rejects_stale():
    today = account_today()
    fresh = pd.DataFrame({"TABLE_NAME": ["T"], "SNAPSHOT_DAY": [today]})
    assert storage_snapshot_fresh(fresh) is True
    # yesterday is still within the 2-day tolerance (a daily loader can lag a day)
    y = pd.DataFrame({"TABLE_NAME": ["T"], "SNAPSHOT_DAY": [today - timedelta(days=1)]})
    assert storage_snapshot_fresh(y) is True
    # a multi-day-stale snapshot (stalled loader) is rejected -> caller falls to the live scan
    stale = pd.DataFrame({"TABLE_NAME": ["T"], "SNAPSHOT_DAY": [today - timedelta(days=5)]})
    assert storage_snapshot_fresh(stale) is False


def test_storage_snapshot_fresh_fails_closed_without_a_verifiable_day():
    today = account_today()
    # missing column, empty frame, and NaT all -> not fresh (never silently assume freshness)
    assert storage_snapshot_fresh(pd.DataFrame({"TABLE_NAME": ["T"]})) is False
    assert storage_snapshot_fresh(pd.DataFrame({"SNAPSHOT_DAY": []})) is False
    assert storage_snapshot_fresh(pd.DataFrame({"SNAPSHOT_DAY": [pd.NaT]})) is False
    assert storage_snapshot_fresh(None) is False
    # the live twin (no SNAPSHOT_DAY) is never gated by this — it isn't the mart leg
    assert storage_snapshot_fresh(pd.DataFrame({"SNAPSHOT_DAY": [today]}), max_age_days=2) is True


def test_storage_mart_first_sites_gate_on_freshness():
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/pages/cost_parts/spend.py"):
        src = _read(rel)
        assert "mart_accept=storage_snapshot_fresh" in src
        assert 'drop(columns=["SNAPSHOT_DAY"], errors="ignore")' in src
