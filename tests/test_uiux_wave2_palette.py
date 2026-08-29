"""UI/UX master list — C3: finish the command palette.

Locks: the "Jump to" box navigates on selection (enter-to-go — no separate
Open button), guarded against re-firing the retained selectbox value after the
navigation rerun; a session recents strip re-dispatches the last destinations;
the Investigate ID-lookup is folded into the same palette as an enter-to-go
mode (type + field, no Open-investigation button); dispatch is one shared
helper both the box and the recents buttons call.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MAIN = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")


def _jump() -> str:
    return _MAIN.split("def _global_jump", 1)[1].split("\ndef ", 1)[0]


def test_selection_navigates_without_an_open_button():
    body = _jump()
    assert '"Open destination"' not in body
    assert "open_jump" not in body
    # review fix: the box remounts empty under a bumped nonce after every jump,
    # so a retained selection can't re-fire and a page-to-self no-op can't
    # strand a stale selection; the explicit rerun covers the no-op path.
    assert 'key=f"_ow_jump_{_jump_nonce}"' in body
    assert 'st.session_state["_ow_jump_nonce"] = _jump_nonce + 1' in body
    assert "def _go(dest: str)" in body and "st.rerun()" in body
    assert "if pick:\n        _go(pick)" in body


def test_recents_strip_redispatches():
    body = _jump()
    assert '_recents = [r for r in (st.session_state.get("_ow_jump_recents") or []) if r in options]' in body
    assert "_go(_r)" in body                            # recents ride the same path
    rec = _MAIN.split("def _record_recent", 1)[1].split("\ndef ", 1)[0]
    assert "recents[:6]" in rec                         # capped, session-only


def test_id_lookup_is_an_enter_to_go_mode():
    body = _jump()
    assert '"Open investigation"' not in body
    # review fix: navigate on a VALUE change only (a kind change alone must not
    # fire against leftover text), under the current kind
    assert '_iv != st.session_state.get("_ow_investigate_last")' in body
    assert "investigation_target(target_kind, _iv)" in body


def test_dispatch_is_one_shared_helper():
    disp = _MAIN.split("def _dispatch_jump", 1)[1].split("\ndef ", 1)[0]
    for kind in ('"Page"', '"Section"', '"DB"', '"WH"', '"Rule"'):
        assert f'kind == {kind}' in disp, kind
    assert "request_navigation" in disp
