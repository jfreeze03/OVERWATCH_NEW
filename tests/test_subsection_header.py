"""v4.155 UI pass: subsection_header is the ONE sub-panel heading.

The long Cost and Admin pages used to title their sub-panels with bare
``st.markdown("**Title**")`` — plain bold text with no visual grammar, the #1
inconsistency vs the rest of the app's section/card system. This pins the fix:

  1. ``subsection_header`` exists and emits the ``.ow-subsection`` shell.
  2. ``app/theme.py`` styles ``.ow-subsection`` (so the shell is not naked HTML).
  3. Admin and every cost_parts module no longer carry a bare ``st.markdown("**…")``
     panel title — they call ``subsection_header`` instead.

A regression that reintroduces a bare bold subhead (or drops the helper) fails
here, so the grammar can't silently drift back.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.ui import components

_ROOT = Path(__file__).resolve().parents[1]
_CONVERTED = [
    "app/ui/pages/admin.py",
    "app/ui/pages/cost_parts/spend.py",
    "app/ui/pages/cost_parts/optimize.py",
    "app/ui/pages/cost_parts/unit_costs.py",
    "app/ui/pages/cost_parts/contract.py",
    "app/ui/pages/cost_parts/compare.py",
    "app/ui/pages/cost_parts/ai_chargeback.py",
]

# A bare bold panel title: st.markdown("**…") (NOT an f-string, which carries a
# dynamic value, and NOT inline emphasis inside a longer markdown paragraph).
_BARE_BOLD_SUBHEAD = re.compile(r'st\.markdown\(\s*"\*\*')


def test_subsection_header_emits_the_subsection_shell(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(components.st, "markdown",
                        lambda body, *a, **k: captured.append(body))
    components.subsection_header("Storage by database", "billed basis")
    assert len(captured) == 1
    html = captured[0]
    assert 'class="ow-subsection"' in html
    assert "ow-subsection__title" in html and "Storage by database" in html
    assert "ow-subsection__desc" in html and "billed basis" in html


def test_subsection_header_escapes_and_omits_empty_desc(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(components.st, "markdown",
                        lambda body, *a, **k: captured.append(body))
    components.subsection_header("A & B <x>")
    html = captured[0]
    assert "A &amp; B &lt;x&gt;" in html          # escaped, never raw
    assert "ow-subsection__desc" not in html       # no empty trailing clause


def test_theme_styles_the_subsection_shell():
    theme = (_ROOT / "app" / "theme.py").read_text(encoding="utf-8")
    assert ".ow-subsection {" in theme
    assert ".ow-subsection__title" in theme
    assert ".ow-subsection__desc" in theme


def test_converted_pages_use_the_helper_not_bare_bold():
    offenders = []
    for rel in _CONVERTED:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "subsection_header(" in src, f"{rel} should use subsection_header"
        if _BARE_BOLD_SUBHEAD.search(src):
            offenders.append(rel)
    assert not offenders, (
        "bare st.markdown(\"**…\") panel titles reintroduced in: "
        + ", ".join(offenders) + " — use subsection_header instead")
