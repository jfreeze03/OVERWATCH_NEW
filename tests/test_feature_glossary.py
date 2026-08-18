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
    for page in ("Brief", "Overview", "Control Room", "Cost & Contract", "Operations",
                 "Decision Studio", "Alerts", "Security", "Admin"):
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
