"""Guards for FEATURE_GLOSSARY.md — the granular section/metric/formula reference.

Not a per-feature contract (it's generated from an audit map, not hand-maintained per
ship); this just keeps it from rotting to empty/truncated and ensures it still covers
every page and states the cross-cutting conventions a reader needs.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_GLOSSARY = _ROOT / "FEATURE_GLOSSARY.md"


def _text() -> str:
    return _GLOSSARY.read_text(encoding="utf-8")


def test_glossary_exists_and_is_substantial():
    assert _GLOSSARY.exists(), "FEATURE_GLOSSARY.md is missing"
    text = _text()
    # It is a big reference (74 sections / 358 metrics). Guard against a truncation
    # that would silently gut it.
    assert len(text) > 120_000, "glossary looks truncated"
    assert text.count("\n| ") > 250, "glossary metric tables look gutted"


def test_glossary_covers_every_page():
    text = _text()
    for page in ("Brief", "Overview", "Control Room", "Cost Intelligence", "Operations",
                 "Proof", "Alerts", "Security", "Admin"):
        assert f"## {page}" in text, f"glossary missing page: {page}"


def test_glossary_states_the_conventions():
    # The cross-cutting rules that answer most "what does this mean" questions.
    text = _text()
    assert "## Conventions" in text
    for concept in ("Measured", "allocated", "Mart vs live", "account", "p95",
                    "Humanization", "robust-z"):
        assert concept in text, f"conventions missing: {concept}"


def test_features_index_links_to_the_glossary():
    feat = (_ROOT / "FEATURES.md").read_text(encoding="utf-8")
    assert "FEATURE_GLOSSARY.md" in feat


# -- v4.609 (V166-V172 wave): the rows the wave changed say what the merged code does --------------------------

def _row(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


def test_glossary_v4609_rows_track_the_wave():
    text = _text()
    assert "'—' until fact loads." not in text                                    # R1-016: the stamped gate
    assert "SESSION_PAD_DAYS" in text                                             # C10
    from app.data.app_cost_sql import SESSION_PAD_DAYS
    assert f"sessions are resolved {SESSION_PAD_DAYS} days before the window" in text
    contract = _row(text, "| **Contract balance exhausts / Credit commitment exhausts** |")
    assert "Outlasts the term" in contract and "[CONTRACT_START_DATE, CONTRACT_END_DATE)" in contract
    assert "since CONTRACT_START_DATE." not in contract
    assert '"value": "Outlasts the term"' in (_ROOT / "app/ui/pages/brief.py").read_text(encoding="utf-8")
    # detection review r1: the shim writes the scans' ASCII arrow, and the glossary quotes it
    assert "30m → 40m" not in text and "'p95 30m -> 40m'" in text
    assert "→" not in (_ROOT / "app/logic/wh_change.py").read_text(encoding="utf-8")
    # R1-071 shipped in V169: the NULL-timer note no longer promises a later migration
    assert "the daily idle alert follows in a later migration" not in text
    # HEAL-CS-MART / HEAL-OBJ-COST: backfill_365.sql fills the statement mart and offers the object-cost year
    assert "MART_CLOUD_SVC_DAILY is never backfilled" not in text
    assert "the ledger is not backfilled by default" in text
    backfill = (_ROOT / "snowflake/backfill_365.sql").read_text(encoding="utf-8")
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY" in backfill
    assert "--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(365);" in backfill
    # R2-033 caption half: met = touched
    assert "met = touched, not rows loaded" in _row(text, "| **SLA compliance** |")
    # CREDIT-PRICE-SEED (V171)
    assert "seeded FALSE by V171" in _row(text, "| **Settings rows the app no longer reads (safe to delete)** |")
    # V170: the declarer column and proposal confidence
    assert "the DBA who typed DECLARE from V170" in text and "EXH / ALL band tokens are account-level" in text
