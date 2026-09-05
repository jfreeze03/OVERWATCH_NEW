"""UI/UX master list — Wave 2 visual-grammar cluster (F17 + F24).

Both are pure theme CSS:

* F17 — KPI cards in a row equalize height. Streamlit columns already stretch to
  the tallest sibling, but a shorter card sat top-aligned inside its stretched
  column with dead space below, so a row where one card carries a sparkline read
  as bottom-ragged. The cards now fill their column (via :has()-scoped flex, so
  no other column layout is touched).

* F24 — a gated action must READ as locked. A disabled Execute (type-to-confirm
  not yet matched) or a disabled Save (nothing dirty) now dims, goes dashed-edge,
  and takes a not-allowed cursor with no hover lift, instead of looking like a
  live button that simply ignores the click.
"""

from __future__ import annotations

from pathlib import Path

_THEME = (Path(__file__).resolve().parents[1] / "app" / "theme.py").read_text(encoding="utf-8")


def test_f24_disabled_buttons_read_as_locked():
    # both :disabled and [disabled] are covered (SiS markup varies), and the
    # lock signals are all present.
    assert ".stButton > button:disabled" in _THEME
    assert ".stButton > button[disabled]" in _THEME
    disabled_block = _THEME.split(".stButton > button:disabled, .stButton > button[disabled]", 1)[1][:400]
    assert "opacity:0.55" in disabled_block
    assert "border-style:dashed" in disabled_block
    # review fix (finding #3): the dashed edge takes ink-mute, not the 0.28-alpha
    # hairline, so it stays legible after the 0.55 element fade (0.28x0.55 ~= 0.15
    # was near-invisible).
    assert "border-color:var(--ow-ink-mute)" in disabled_block
    # the hover lift is explicitly cancelled so a locked button never animates
    assert ".stButton > button:disabled:hover" in _THEME
    hover_block = _THEME.split(".stButton > button:disabled:hover", 1)[1][:200]
    assert "transform:none" in hover_block


def test_f24_cursor_is_on_the_wrapper_not_the_disabled_button():
    # review fix (finding #2): a disabled <button> is not a pointer-event target,
    # so cursor:not-allowed set only on it is ignored (browsers show the arrow).
    # It must ALSO sit on the enabled .stButton wrapper, which does receive hover.
    assert ".stButton:has(> button:disabled)" in _THEME
    wrap_block = _THEME.split(".stButton:has(> button:disabled)", 1)[1][:160]
    assert "cursor:not-allowed" in wrap_block
    # we deliberately do NOT kill pointer-events on the button — that would
    # suppress the help= tooltip explaining why it's locked. Guard that intent:
    # the disabled button declaration block must not carry pointer-events:none.
    disabled_decl = _THEME.split(
        ".stDownloadButton > button:disabled {", 1)[1].split("}", 1)[0]
    assert "opacity:0.55" in disabled_decl   # anchor: this is the right block
    assert "pointer-events:none" not in disabled_decl


def test_f24_primary_gradient_is_excluded_when_disabled():
    # review fix (finding #1, high): the primary rule set border:none + a bright
    # gradient with !important, which (same specificity, later source order) beat
    # F24's dashed edge on a DISABLED PRIMARY button and left it looking live-but-
    # faded. Excluding :disabled makes it fall through to the locked treatment.
    assert '.stButton > button[kind="primary"]:not(:disabled)' in _THEME
    assert 'button[data-testid="stBaseButton-primary"]:not(:disabled)' in _THEME
    # the un-suffixed primary selector must no longer directly precede the
    # gradient declaration (that was the bug).
    assert '.stButton > button[kind="primary"],\n.stButton' not in _THEME
    # bughunt r1: the primary TEXT-INK rule must ALSO exclude :disabled, else a
    # disabled primary keeps dark ink after its accent gradient was removed above
    # -> dark-ink-on-dark-disabled-surface, illegible. Both rules gate on :disabled.
    assert '.stButton > button[kind="primary"]:not(:disabled) p' in _THEME
    assert 'button[data-testid="stBaseButton-primary"]:not(:disabled) span' in _THEME
    # the un-guarded primary p/span ink rule must be gone
    assert '.stButton > button[kind="primary"] p,' not in _THEME


def test_f17_kpi_cards_equalize_via_min_height_floor():
    # review fix (finding #3, confirmed): the earlier :has() flex approach was
    # INERT — .ow-card{height:100%} resolves against the auto-height stMarkdown
    # wrapper chain and does nothing. F17 now rides on a min-height FLOOR on the
    # base .ow-card rule, which actually renders and evens the common ragged case.
    card_rule = _THEME.split(".ow-card {", 1)[1].split("}", 1)[0]
    # v4.461 P0 flatten: the floor dropped 96px -> 72px for the dense expert
    # audience (a ragged bottom costs less than wasted fold space); the FLOOR
    # mechanism (F17) stays — a short label+value+delta card still evens up.
    assert "min-height:72px" in card_rule
    # box-sizing:border-box so the floor includes the padding rather than
    # adding to it (a card is not silently taller than the declared floor).
    assert "box-sizing:border-box" in card_rule


def test_f17_did_not_reintroduce_the_inert_or_leaky_flex():
    # the inert + scope-leak-prone rules must be gone: no height:100% on .ow-card,
    # and no :has()-scoped column flex that a nested kpi_row could get caught by.
    assert ".ow-card { height:100%" not in _THEME.replace("\n", " ")
    assert ':has(> [data-testid="stColumn"] .ow-card) [data-testid="stColumn"] {' not in _THEME
    # and never a bare/global column flex, which would restack master-detail etc.
    for line in _THEME.splitlines():
        stripped = line.strip()
        if stripped.startswith('[data-testid="stColumn"]') and "display:flex" in stripped:
            raise AssertionError("unscoped column flex would break other layouts: " + stripped)
