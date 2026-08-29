"""UI/UX master list — Wave 2 table-layer polish (F32 + F33 + F35 + F36).

Locks: F32 mutes true-zero numeric cells (grey, present-but-quiet) so non-zero
values carry the eye on sparse tables, skipping status/delta/bar columns · F33
sets column width by name convention (id/status/short-ratio -> small, free-text
detail/title/note -> large) so wide tables stop laying out raggedly · F35 keeps
the order/window provenance on SMALL ranked tables (the row count is dropped, the
"by X · last Nd" line is not) · F36 states the CSV-is-raw contract by every
download button.
"""

from __future__ import annotations

from pathlib import Path

from app.ui import components

_COMP = (Path(__file__).resolve().parents[1] / "app" / "ui" / "components.py").read_text(
    encoding="utf-8")


def test_f32_mutes_true_zero_but_not_status_delta_or_bar():
    from app.ui import palette
    # review fix: a LITERAL hex, not a CSS var — st.dataframe's canvas renderer
    # can't resolve var(--ow-ink-mute), so the mute would have been a silent no-op.
    assert components._mute_zero_css(0) == f"color:{palette.INK_MUTE}"
    assert components._mute_zero_css(0.0) == f"color:{palette.INK_MUTE}"
    assert "var(" not in components._mute_zero_css(0)
    assert components._mute_zero_css(3.2) == ""
    assert components._mute_zero_css("x") == ""            # non-numeric -> no style
    assert components._mute_zero_css(float("nan")) == ""   # NaN is already "—"
    body = _COMP.split("def _render_table(", 1)[1].split("\ndef ", 1)[0]
    # applied on the Styler path, skipping status/delta and the progress-bar column
    assert "styler = styler.map(_mute_zero_css, subset=[col])" in body
    assert "_mute_skip |= {c for c in df.columns if is_delta_column(c)}" in body
    assert "_mute_skip.add(_bar_col)" in body


def test_f33_width_intent_by_name():
    w = components._width_for_column
    assert w("STATUS") == "small" and w("SEVERITY") == "small"
    assert w("HIT_PCT") == "small" and w("AGE") == "small"
    assert w("DETAIL") == "large" and w("TITLE") == "large"
    assert w("PREVIEW_ROWS") == "large" and w("RESULT_NOTE") == "large"
    assert w("WAREHOUSE_NAME") is None                     # default width
    # review fix: *_ID is NOT small — QUERY_ID/SESSION_ID are 36-char drill
    # targets that "small" would ellipsis-truncate
    assert w("QUERY_ID") is None and w("SESSION_ID") is None
    # threaded into the prettifier Column so it also configures otherwise-plain columns
    body = _COMP.split("def _render_table(", 1)[1].split("\ndef ", 1)[0]
    assert "_w = _width_for_column(_col)" in body
    assert "not _help and not _w:" in body                 # width alone earns a config
    assert "width=_w" in body


def test_f35_small_ranked_tables_keep_provenance():
    body = _COMP.split("def _render_table(", 1)[1].split("\ndef ", 1)[0]
    assert 'elif size_note and _prov and len(df) >= 4:' in body
    assert 'st.caption(_prov)' in body                     # provenance-only for small tables
    # the large-table caption still leads with the row count
    assert 'st.caption(f"{len(df):,} rows"' in body
    # review fix: decision boards suppress the auto caption (they state ordering
    # in an adjacent caption) so the basis isn't printed twice
    dr = _COMP.split("def decision_rows(", 1)[1].split("\ndef ", 1)[0]
    assert "size_note=False," in dr


def test_f36_csv_raw_contract_on_every_download_button():
    assert "_CSV_RAW_HELP" in _COMP
    assert "CSV is the source, not the on-screen display" in _COMP
    body = _COMP.split("def _render_table(", 1)[1].split("\ndef ", 1)[0]
    # the old bare "Download this table as CSV" help is gone; all three buttons carry the contract
    assert "Download this table as CSV (account time)." not in body
    assert body.count("help=_CSV_RAW_HELP") >= 2
    assert "{_CSV_RAW_HELP}" in body                        # the prep button too
