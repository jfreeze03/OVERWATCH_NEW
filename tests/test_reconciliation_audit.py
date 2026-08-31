"""Cross-surface reconciliation audit locks (2026-08-30, v4.370.0).

Adversarial reconciliation pass (6 finders pairing surfaces that should agree). Eight surfaced, six
confirmed (three distinct), two refuted (the DS "two verified-savings" figures are the deliberate
QTD-vs-all-time pair; the Operations lock-wait mart/live scope is a documented account-wide fallback).
  - [LOW] Brief vs Overview MTD credit spend blended rounded-vs-raw credits -> both blend from raw now.
  - [MED] Overview "Spend, {window}" honors the full window while Cost by-warehouse clamps to 182d;
    the Overview help falsely claimed unconditional reconciliation -> help now states the 182d bound.
  - [MED] Brief "Open incidents" counted len() of a LIMIT-5 feed (saturated at 5); now reads the
    uncapped incident_metrics.OPEN_NOW that Control Room uses.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_health_strip_mtd_partitions_are_raw_for_the_blend() -> None:
    sql = mart_sql.health_strip()
    # the blended partitions are raw sums (rounding deferred to the display edge)
    assert "COALESCE(SUM(IFF({_ai}, CREDITS_BILLED, 0)), 0) AS MTD_AI".replace("{_ai}", "") not in sql
    assert "0) AS MTD_AI" in sql and "0) AS MTD_OTHER" in sql
    # no per-partition ROUND on the blended columns (would zero fractional-cent divergence vs Overview)
    assert "0), 0) AS MTD_AI" not in sql
    assert "0), 0) AS MTD_OTHER" not in sql
    # MTD_ALL (the whole-credit display) stays rounded
    assert "0), 0) AS MTD_ALL" in sql


def test_overview_spend_help_states_the_182d_reconciliation_bound() -> None:
    src = _src("app/ui/pages/overview.py")
    assert "Reconciles with Cost & Contract -> By warehouse for windows up to 182 days" in src
    # the old unconditional claim is gone
    assert "so it reconciles with Cost & Contract -> By warehouse). Serverless" not in src


def test_brief_open_incidents_uses_uncapped_open_now() -> None:
    src = _src("app/ui/pages/brief.py")
    # the uncapped count source is batched and read for the KPI
    assert '"key": "inc_met", "sql": mart_sql.incident_metrics(90, company)' in src
    assert '_n_inc = int(safe_float(_inc_met.df.iloc[0].get("OPEN_NOW")))' in src
    # len(_inc.df) survives only as the fallback when the metrics read is unavailable
    assert "_n_inc = len(_inc.df)" in src and "else:\n            _n_inc = len(_inc.df)" in src
