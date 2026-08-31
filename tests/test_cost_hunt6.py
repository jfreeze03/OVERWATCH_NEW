"""Cost-layer bug-hunt #6 locks (2026-08-30, v4.364.0).

Sixth adversarial pass over the less-swept cost slices (storage lifecycle, contract/commitment
pacing, forecast math, cloud-services rebate, reader-scoping matrix, cost-alert rules). Five of six
finders clean; one confirmed, zero refuted.
  - [MED/V108] COST_CONTRACT_BREACH ([16] arm of SP_ALERT_SCAN_DAILY) fired only for DAYS_LEFT
    BETWEEN 0 AND THRESHOLD_NUM, so once the contract was over-consumed DAYS_LEFT went negative and
    the alert went permanently silent in the over-contract (on-demand overage) state. V108 relaxes the
    guard to DAYS_LEFT <= THRESHOLD_NUM and adds a distinct EXHAUSTED title/metric/dedupe band.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v108_contract_breach_fires_when_exhausted() -> None:
    mig = _src("snowflake/migrations/V108__cost_contract_breach_fires_when_exhausted.sql")
    # the guard fires for the over-contract state; the old band-clamped guard is gone
    assert "AND p.DAYS_LEFT <= c.THRESHOLD_NUM" in mig
    assert "BETWEEN 0 AND c.THRESHOLD_NUM" not in mig
    # distinct EXHAUSTED title + metric for the over-contract state
    assert "'Contract EXHAUSTED: '" in mig
    assert "ROUND(p.CONSUMED - p.TOTAL, 0)" in mig
    # the approaching-breach messaging survives (now the else branch of the IFF)
    assert "'Contract projected to exhaust in ' || p.DAYS_LEFT" in mig
    # EXHAUSTED dedupe band so WARN -> CRIT -> EXHAUSTED crossings each re-fire
    assert "IFF(p.DAYS_LEFT <= 0, 'EXH', IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN'))" in mig
    # the unconfigured-contract / no-burn gates are untouched (no new false fires)
    assert "p.TOTAL > 0 AND p.DAILY_BURN > 0" in mig
    # re-derives SP_ALERT_SCAN_DAILY only; no schema change
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" in mig
    assert "CREATE OR REPLACE VIEW" not in mig and "CREATE TABLE " not in mig
    assert "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    # ordered-apply guard + version stamp
    assert "EXCEPTION (-20108" in mig and "IF (v < 107) THEN" in mig
    assert "SELECT 108 AS VERSION" in mig and "WHERE VERSION = 108)" in mig
