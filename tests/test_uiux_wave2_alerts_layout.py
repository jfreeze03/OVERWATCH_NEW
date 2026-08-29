"""UI/UX master list — C42: desktop master-detail layout for Alerts.

Locks: the open-events @st.fragment renders the feed LEFT / drawer RIGHT via
st.columns inside the shared ow_md_* keyed container (so it restacks on narrow
viewports with every other master-detail surface); the bulk panel and snoozed
tray stay full-width outside the columns; the drawer placeholder is single-mode
only (bulk mode has no drawer) and the drawer pane owns the "select an event"
prompt (the feed no longer duplicates it). A pure render-location split — every
F51/C44/C48 guard is unchanged session state.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "alerts.py").read_text(
    encoding="utf-8")


def test_feed_and_drawer_are_columns_in_the_shared_container():
    frag = _SRC.split("def _open_events_section", 1)[1]
    assert 'with st.container(key="ow_md_alerts"):' in frag   # shared restack key
    assert "col_feed, col_drawer = st.columns(" in frag
    assert 'vertical_alignment="top"' in frag
    assert "with col_feed:" in frag and "with col_drawer:" in frag
    # the drawer renders under col_drawer, gated on a live single-row selection
    assert "with col_drawer:" in frag
    d = frag.split("with col_drawer:", 1)[1]
    assert "if sel is not None and 0 <= int(sel) < len(edf):" in d
    assert 'row = edf.iloc[sel]' in d


def test_bulk_and_snoozed_stay_full_width():
    frag = _SRC.split("def _open_events_section", 1)[1]
    # the bulk panel is an 8-space sibling, OUTSIDE the columns container (full width)
    assert "\n        if is_operator and _bulk_rows:" in frag
    # the snoozed tray renders at 4-space — OUTSIDE the `if guard(events, ...)` block —
    # so an empty open feed still surfaces pending snoozed events rather than a false
    # green all-clear while a snoozed CRITICAL is invisible until its auto-wake.
    assert "\n    _snz = run(mart_sql.snoozed_alert_events" in frag
    assert "\n        _snz = run(mart_sql.snoozed_alert_events" not in frag  # not back inside the guard


def test_drawer_placeholder_is_single_mode_and_not_duplicated():
    frag = _SRC.split("def _open_events_section", 1)[1]
    # the drawer pane owns the "select an event" prompt, only when NOT bulk mode
    assert "elif not _bulk_mode:" in frag
    assert "Select an event on the left to open its drawer" in frag
    # the feed no longer duplicates that prompt — it keeps only the bulk tip
    assert "Click a row to open its drawer" not in frag
    assert 'elif is_operator:\n                        st.caption("Flip Bulk select' in frag
    # no orphaned leading divider capping the drawer pane
    assert "event_id = str(row[\"EVENT_ID\"])\n                    st.divider()" not in frag
