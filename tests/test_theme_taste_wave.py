"""Codex visual taste wave (v4.587.0) — theme.py: rec12 (bump small metadata toward 12px)
and rec13 (sentence-case the metric/data labels; keep chips uppercase). Owner taste calls."""

from __future__ import annotations

import re
from pathlib import Path

_THEME = (Path(__file__).resolve().parents[1] / "app" / "theme.py").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """The declaration block for a selector (up to the first closing brace)."""
    return _THEME.split(selector, 1)[1].split("}", 1)[0]


# --- rec12: no tiny metadata below the ~11.5px/0.72rem floor --------------------

def test_rec12_no_metadata_below_the_12px_floor() -> None:
    # the sub-12px outliers the adjudication named (~10.5-11px) are gone
    assert not re.search(r"font-size:\s*0\.6[0-9]rem", _THEME)      # 0.66rem/0.68rem bumped
    assert "font-size:11px" not in _THEME                          # the 11px chip bumped
    # the specific bumps
    assert ".ow-breadcrumb { font-size:0.75rem" in _THEME
    assert ".ow-src-badge { font-size:12px" in _THEME
    assert "font-size:0.72rem" in _rule(".ow-hero__c-label")
    assert "font-size:0.72rem" in _rule(".ow-triage-sub")


# --- rec13: metric/data labels are sentence-case; chips stay uppercase ----------

_LABEL_SELECTORS = (
    '[data-testid="stMetricLabel"] p',
    ".ow-card__title",
    ".ow-hero__label",
    ".ow-hero__c-label",
    ".ow-stat__k",
    ".ow-triage-label",
)


def test_rec13_metric_labels_are_sentence_case() -> None:
    for sel in _LABEL_SELECTORS:
        block = _rule(sel)
        assert "text-transform:uppercase" not in block, f"{sel} should not be uppercase"
        assert "letter-spacing:0.06em" not in block and "letter-spacing:0.05em" not in block, (
            f"{sel} should not be tracked")
        assert "letter-spacing:0" in block, f"{sel} should zero its tracking"
    # tracking is fully gone from the label vocabulary
    assert "letter-spacing:0.06em" not in _THEME


def test_rec13_chips_stay_uppercase() -> None:
    # a chip reads as a chip — the provenance + section badges keep their uppercase tracking
    assert "text-transform:uppercase" in _rule(".ow-src-badge")
    assert "text-transform:uppercase" in _rule(".ow-section__badge")
    # exactly the two chips keep uppercase, nothing else
    assert _THEME.count("text-transform:uppercase") == 2
