"""Landing default: OVERWATCH opens on Brief for every profile that offers it.

The default landing page is pages[0] of the viewer's PAGES_BY_PROFILE tuple
(app/main.py: `current = st.session_state.get("_ow_page") or pages[0]`), used when
there is no saved DEFAULT_VIEW and no ?page= deep link. The DBA tuple used to list
"Ask" first, so DBAs opened on Ask OVERWATCH instead of Brief (owner ask 2026-08-30).
v4.610.0 (owner decision 2026-10-05): the view-only MONITOR tier has no Brief (only
Cost Intelligence and Operations), so it lands on Cost Intelligence.
"""

from __future__ import annotations

from app.config import PAGES_BY_PROFILE


def test_every_profile_lands_on_brief_first():
    for profile, pages in PAGES_BY_PROFILE.items():
        if "Brief" not in pages:
            continue
        assert pages[0] == "Brief", f"{profile} lands on {pages[0]!r}, not Brief"
    # the one profile without Brief is MONITOR, and it opens on Cost Intelligence
    assert [p for p, pages in PAGES_BY_PROFILE.items() if "Brief" not in pages] == ["MONITOR"]
    assert PAGES_BY_PROFILE["MONITOR"][0] == "Cost Intelligence"


def test_dba_still_sees_ask_but_it_trails_last():
    dba = PAGES_BY_PROFILE["DBA"]
    assert "Ask" in dba                 # DBA-only grounded Q&A still available
    assert dba[-1] == "Ask"             # ...but last, matching the nav display order
    assert dba[0] == "Brief"
