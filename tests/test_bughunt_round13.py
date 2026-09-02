"""Regression locks for the round-13 bug hunt (v4.428.0).

Five MED cross-surface presentation-integrity fixes:
  CSF-1  admin "Avg GB scanned" byte-humanizes like every other bytes-scanned surface
  HVC-1  operations "Stale" help/caption describe the p90 rule the classifier actually uses
  RD-1   the "Tagged share" KPI reads an account-wide denominator, not the top-30 frame
  DDR-1  control_room verdict warns (not green) when the open-critical read fails
  WLA-1  spend tiles label the bounded "Last month" window honestly, not as trailing "{days}d"
"""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app.data import cost_sql, mart27_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- CSF-1: admin AVG_GB_SCANNED falls through to the byte-humanize convention ----------
def test_admin_avg_gb_scanned_byte_humanizes():
    a = _src("app/ui/pages/admin.py")
    scan_block = a.split("adm_stmt_scan", 1)[1].split("Page adoption", 1)[0]
    # no explicit fixed-decimal GB format on the byte-magnitude column -> it humanizes
    assert 'NumberColumn("Avg GB scanned"' not in scan_block
    assert 'AVG_GB_SCANNED": st.column_config' not in scan_block


# --- HVC-1: the "Stale" help/caption describe the p90 longest-normal-gap rule -----------
def test_task_stale_help_describes_p90_not_median():
    ops = _src("app/ui/pages/operations.py")
    blk = ops.split("Stale (silently stopped)", 1)[1][:1200]
    assert "p90" in blk and "longest NORMAL scheduled gap" in blk
    assert "2x+ its typical cadence" not in blk          # the misleading wording is gone
    # the section caption also reconciles the status basis (p90), not just median
    assert "judged against" in ops and "longest NORMAL gap (p90)" in ops


# --- RD-1: both tag_coverage builders carry account-wide window totals ------------------
def test_tag_coverage_builders_expose_account_wide_totals():
    for sql in (cost_sql.tag_coverage(days=30, company="ALFA"),
                mart27_sql.tag_coverage_daily(days=30, company="ALFA")):
        assert "TOTAL_EXEC_SEC" in sql and "TOTAL_UNTAGGED_EXEC_SEC" in sql
        assert "OVER ()" in sql                            # window over the full population
        assert "LIMIT 30" in sql                           # display frame still capped
        sqlglot.parse_one(sql, read="snowflake")           # window fn is well-formed


def test_tagged_share_kpi_reads_account_wide_totals():
    c = _src("app/ui/pages/cost.py")
    blk = c.split('Tagged share (exec-time)', 1)[0][-900:]
    # KPI reads the account-wide window totals, not a sum over the capped frame
    assert 'tdf_g.iloc[0]["TOTAL_EXEC_SEC"]' in blk
    assert 'tdf_g.iloc[0]["TOTAL_UNTAGGED_EXEC_SEC"]' in blk
    # and the repeated total columns are dropped before the per-user board renders
    assert 'drop(columns=["TOTAL_EXEC_SEC", "TOTAL_UNTAGGED_EXEC_SEC"]' in c


# --- DDR-1: control_room verdict warns when the open-critical read fails ----------------
def test_control_room_verdict_warns_on_unknown_open_criticals():
    cr = _src("app/ui/pages/control_room.py")
    blk = cr.split("_vsig = []", 1)[1].split("page_verdict_line", 1)[0]
    assert "if not _crit_known:" in blk
    assert 'Signal("warn", "open-critical count unavailable")' in blk


# --- WLA-1: spend tiles use the honest window label, not raw {days}d --------------------
def test_spend_tiles_label_last_month_honestly():
    s = _src("app/ui/pages/cost_parts/spend.py")
    tab = s.split("def _spend_tab", 1)[1].split("def _attribution_tab", 1)[0]
    assert '_wlab = "last month" if bounds is not None else f"{days}d"' in tab
    # the flagship + transfer tiles no longer hardcode the trailing "{days}d" descriptor
    assert 'f"Credit spend, {days}d (account)"' not in tab
    assert 'f"Total transferred ({days}d)"' not in tab
    assert 'f"Credit spend, {_wlab} (account)"' in tab
    assert 'f"Total transferred ({_wlab})"' in tab
