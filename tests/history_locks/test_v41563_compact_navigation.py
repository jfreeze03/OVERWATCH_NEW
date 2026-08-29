"""v4.158.0 locks: compact triage toolbar and reliable jump controls."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_triage_filters_render_as_one_command_strip() -> None:
    main = _read("app/main.py")
    body = main.split("def _topbar_scope", 1)[1].split("\ndef ", 1)[0]
    assert 'st.container(border=True, key="ow_triage_toolbar")' in body
    assert body.count("st.columns(") == 1
    assert "c_scope, c_company, c_days, c_db, c_more, c_reset" in body
    assert '"Date range", options=list(TRIAGE_WINDOW_OPTIONS)' in body
    assert 'label_visibility="collapsed"' in body
    assert "with st.popover(_more_label" in body
    assert "st.expander" not in body


def test_toolbar_has_dedicated_compact_css() -> None:
    theme = _read("app/theme.py")
    assert ".st-key-ow_triage_toolbar" in theme
    assert ".ow-triage-title" in theme
    assert 'div[data-baseweb="select"]>div{min-height:2.28rem;}' in theme


def test_sidebar_jump_is_enter_to_go_with_recents() -> None:
    # C3 (v4.313): the command palette navigates on SELECTION — no separate
    # "Open destination" button — guarded so the retained selectbox value can't
    # re-fire on the post-navigation rerun; a recents strip re-dispatches the
    # last destinations; the ID lookup is folded in as an enter-to-go mode.
    main = _read("app/main.py")
    body = main.split("def _global_jump", 1)[1].split("\ndef ", 1)[0]
    assert '"Open destination"' not in body            # the extra button is gone
    assert 'key=f"_ow_jump_{_jump_nonce}"' in body      # box remounts empty per jump
    assert "def _go(dest: str)" in body
    assert "_ow_jump_recents" in body                  # recents strip
    # the ID lookup navigates on Enter (value change), no Open-investigation button
    assert '"Open investigation"' not in body
    assert '_iv != st.session_state.get("_ow_investigate_last")' in body
    dispatch = main.split("def _dispatch_jump", 1)[1].split("\ndef ", 1)[0]
    assert dispatch.index('if kind == "Page":') < dispatch.index('elif kind == "DB":')


def test_in_page_jump_chips_are_removed() -> None:
    # v4.158.0: the in-page "JUMP TO" chips could never scroll in
    # Streamlit-in-Snowflake — section_toc rendered its scroll JS inside a
    # cross-origin components.v1.html iframe, so window.parent.document was a
    # SecurityError and every fallback dead-ended. The chips are gone; the old
    # test asserted the dead markup existed (source-grep theater). Sidebar
    # "Open destination" remains the working jump (test above).
    assert "def section_toc" not in _read("app/ui/components.py")
    for page in ("app/ui/pages/cost.py", "app/ui/pages/operations.py",
                 "app/ui/pages/security.py"):
        assert "section_toc" not in _read(page)


def test_notebook_subset_adds_exact_name_parts() -> None:
    spend = _read("app/ui/pages/cost_parts/spend.py")
    # the whole "Compute pools & notebooks" branch (v4.265 added a pool→user drill
    # above the notebook subset, so don't truncate at the first st.caption)
    notebook = spend.split('detail == "Compute pools & notebooks"', 1)[1].split(
        "elif detail ==", 1
    )[0]
    assert "with_user_name_parts(notebooks.df, _PAGE)" in notebook
    directory = _read("app/data/directory_sql.py")
    assert "FIRST_NAME" in directory and "LAST_NAME" in directory
