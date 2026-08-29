"""UI/UX master list — Wave 2 active-grammar unification (F5).

"This is the current one" was said three (really four) different ways: the section
pills filled with an accent gradient, the sidebar nav used a left rail + wash, the
tabs used BaseWeb's own default (no app accent at all), and the Window pills had no
active fill rule whatsoever. F5 makes them one ACCENT-DRIVEN system: two shared
tokens (`--ow-active-tint` / `--ow-active-bar`) feed every "active" cue, and each
component keeps its shape-appropriate indicator — a filled segment for pills, a
left rail for the nav list, an underline for tabs.
"""

from __future__ import annotations

from pathlib import Path

_THEME = (Path(__file__).resolve().parents[1] / "app" / "theme.py").read_text(encoding="utf-8")


def test_f5_shared_active_tokens_exist():
    assert "--ow-active-tint:" in _THEME
    assert "--ow-active-bar:" in _THEME


def test_f5_nav_uses_the_shared_tokens_left_rail_variant():
    nav = _THEME.split('section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked)', 1)[1][:220]
    assert "var(--ow-active-tint)" in nav        # accent tint wash
    assert "inset 3px 0 0 var(--ow-active-bar)" in nav   # the left accent rail
    assert "font-weight:640" in nav              # heavier ink, consistent


def test_f5_both_section_and_window_pills_get_the_filled_active():
    # the Window pills previously had NO active-fill rule — now they share it
    pill_block = _THEME.split("FILLED variant of the active grammar", 1)[1][:500]
    assert 'div[role="radiogroup"][aria-label="Section"] label:has(input:checked)' in pill_block
    assert 'div[role="radiogroup"][aria-label^="Window"] label:has(input:checked)' in pill_block
    assert "linear-gradient(180deg,var(--ow-accent2),var(--ow-accent))" in pill_block


def test_f5_modern_segmented_control_active_segment_is_filled():
    # review: lazy_sections renders the Section picker as st.segmented_control
    # (stButtonGroup) on modern Streamlit, so the radiogroup rule only styles the
    # old-radio fallback — the REAL active segment must be styled here too.
    assert 'button[data-testid="stBaseButton-segmented_controlActive"]' in _THEME
    assert 'button[aria-checked="true"]' in _THEME
    seg = _THEME.split('stBaseButton-segmented_controlActive"', 1)[1][:200]
    assert "linear-gradient(180deg,var(--ow-accent2),var(--ow-accent))" in seg


def test_f5_active_pill_text_is_dark_ink_not_pale_on_accent():
    # review HIGH: a direct global `p,span { color:ink-soft }` beats the label's
    # inherited dark ink, leaving near-white text on the bright accent fill. The
    # dark ink is forced onto the text node for BOTH the segmented + radio pills.
    assert _THEME.count("color:#0f172a !important") >= 2
    # the radio-pill descendant override targets the option text node
    assert 'label:has(input:checked) span { color:#0f172a !important' in _THEME.replace("\n", " ")


def test_f5_active_tab_joins_the_accent_grammar():
    # tabs used to rely on BaseWeb's default highlight colour — now the app accent
    assert 'button[data-baseweb="tab"][aria-selected="true"]' in _THEME
    tab = _THEME.split('button[data-baseweb="tab"][aria-selected="true"]', 1)[1][:120]
    assert "var(--ow-accent)" in tab
    assert 'div[data-baseweb="tab-highlight"]' in _THEME
    hl = _THEME.split('div[data-baseweb="tab-highlight"]', 1)[1][:100]
    assert "var(--ow-active-bar)" in hl
    # bug-hunt r3: the accent must be forced onto the tab's text NODE (a markdown
    # <p>), not just the button — else line 56's direct ink-soft on <p> wins and the
    # accent ink is a silent no-op, like the pills would have been without the force.
    assert 'button[data-baseweb="tab"][aria-selected="true"] p' in _THEME
    assert 'button[data-baseweb="tab"][aria-selected="true"] span' in _THEME


def test_f5_every_variant_shares_the_one_accent():
    # the whole point: no active cue invents its own colour. The accent bar token
    # and the section-pill accent are the same accent family (#60a5fa / accent).
    assert "--ow-active-bar:#60a5fa" in _THEME.replace(" ", "")
    # and --ow-accent (used by the pill fill + tab ink) is that same hue
    assert "--ow-accent:#60a5fa" in _THEME.replace(" ", "")
