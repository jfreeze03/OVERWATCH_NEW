"""Regression locks for the round-12 bug hunt (v4.427.0)."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- FC-1 (MED): Heaviest-queries spill uses the byte-humanize convention, not raw GB ----
def test_heaviest_queries_spill_is_not_raw_gb():
    ops = _src("app/ui/pages/operations.py")
    top_block = ops.split('key="ops_top_sel"', 1)[1].split("methodology_note", 1)[0]
    # no explicit raw-GB NumberColumn on SPILL_REMOTE_GB -> it falls through to _auto_formats
    # (humanize_bytes -> "30.7 MB"), matching the triage table, KPI card, and drill card.
    assert 'st.column_config.NumberColumn("Spill GB"' not in top_block
    assert 'format="%.2f"' not in top_block                # the raw-GB format is gone


# --- EXEC-1 (MED): the driver caption/comment no longer falsely claim reconciliation -----
def test_cost_driver_reconciliation_claim_is_honest():
    ov = _src("app/ui/pages/overview.py")
    # the stale "drivers reconcile to the KPI total" claim is gone
    assert "the drivers reconcile to the KPI total" not in ov
    # the caption discloses the driver window basis (through today vs last month)
    assert '_drv_thru = "through today" if _ov_bounds is None else "last month"' in ov
    assert "can slightly EXCEED the headline" in ov       # honest note in the comment


# --- FRESH-1 (MED): admin stale-diagnose uses the cadence-aware 3h/30h split -------------
def test_admin_stale_diagnose_is_cadence_aware():
    a = _src("app/ui/pages/admin.py")
    blk = a.split("Diagnose stale sources", 1)[1][:2600]
    assert 'THRESHOLDS["stale_daily_fact_hours"]' in blk and 'THRESHOLDS["stale_fact_hours"]' in blk
    assert '"DAILY" in n or "METERING" in n' in blk        # same split as health_strip / control_room
    assert "stale = fresh.df[(_hrs > _lim_hrs) | _hrs.isna()]" in blk
    assert "> 26" not in blk                                # the flat 26h threshold is gone
    assert "from app.config import (" in a and "THRESHOLDS," in a
