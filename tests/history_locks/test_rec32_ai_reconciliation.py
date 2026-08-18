"""rec#32: the billing-truth-vs-app-model reconciliation now models the AI/Cortex
bucket (AI credits x the AI rate) against org AI_USD, not just the compute bucket.
"""

from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2] / "app" / "ui" / "pages" / "cost_parts"
        / "contract.py").read_text(encoding="utf-8")


def test_recon_models_the_ai_bucket_and_reconciles_to_org_ai_usd():
    # new modeled-AI column + AI drift alongside the compute columns
    assert "APP_MODEL_AI_USD" in _SRC
    assert "AI_DELTA_PCT" in _SRC
    # modeled from AI credits x the AI rate (never the compute rate)
    assert "CREDITS_BILLED_AI" in _SRC
    assert 'settings.get("AI_CREDIT_PRICE_USD")' in _SRC
    # reconciled against the org AI_USD bucket
    assert 'orow.get("AI_USD")' in _SRC


def test_ai_recon_uses_the_same_monthly_grain_as_compute():
    # aligned by month like the compute side (get(month, 0.0)) -> no 30d/Nd window mismatch
    assert "ai_by_month.get(month" in _SRC
    assert 'dt.to_period("M")' in _SRC
