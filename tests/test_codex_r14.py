"""Codex r14 fix-batch locks (v4.32.0). The fleet board picked these."""

from __future__ import annotations

from pathlib import Path

from app.data import chargeback_sql, mart27_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_contract_coverage_is_computed_without_the_contract_filter():
    """r14 #8: MIN(DAY) inside WHERE DAY >= start made a quiet contract-start
    day read as no-coverage forever."""
    sql = mart_sql.fact_contract_consumed("2026-01-01")
    assert "FACT_FIRST_DAY" in sql
    assert "WHERE" not in sql.upper().split("FROM", 1)[1]  # coverage sees the whole fact
    assert "SUM(IFF(DAY >= '2026-01-01'" in sql            # window sum via IFF, not WHERE
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "FACT_FIRST_DAY" in ct and 'get("FIRST_DAY")' not in ct


def test_metering_surfaces_moved_to_the_fact():
    """r14 #5: the 365d fact backfill makes the live WMH scans avoidable."""
    for sql in (chargeback_sql.department_window_credits(30, "ALFA"),
                chargeback_sql.department_month_credits("2026-06", "ALFA"),
                mart_sql.app_cost_quarter(),
                mart27_sql.fact_monthly_spend_by_warehouse(12, "ALFA")):
        assert "FACT_WAREHOUSE_DAILY" in sql
        assert "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY" not in sql
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "fact_monthly_spend_by_warehouse" in ov          # boss-chart fallback = fact
    br = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert "FACT_WAREHOUSE_DAILY (WH_ALFA_OVERWATCH" in br  # label tells the truth


def test_cache_cardinality_is_bounded():
    """r14 #17: refresh salts + filters mint keys forever; max_entries caps
    process memory."""
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    assert q.count("max_entries=") >= 10                    # every tier fetcher, run + batch


def test_freshness_boards_poll_at_snapshot_cadence():
    """r14 #13: the state table moves every 10 minutes — a 30s live tier
    re-read bought nothing."""
    for page in ("control_room", "admin"):
        src = (_ROOT / "app" / "ui" / "pages" / f"{page}.py").read_text(encoding="utf-8")
        seg = src.split("source_freshness_state", 1)[1][:400]
        assert 'mart_tier="recent"' in seg


def test_security_header_reads_posture_once():
    """r14 #18: the 3d + 90d posture double-read collapsed to one shared
    90d frame."""
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert sec.count("mart27_sql.security_posture(") == 1
    assert "security_posture(90)" in sec
    assert "_posture_trend_panel(_post90)" in sec
