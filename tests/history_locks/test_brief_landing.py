"""Landing default: OVERWATCH opens on Brief for every profile.

The default landing page is pages[0] of the viewer's PAGES_BY_PROFILE tuple
(app/main.py: `current = st.session_state.get("_ow_page") or pages[0]`), used when
there is no saved DEFAULT_VIEW and no ?page= deep link. The DBA tuple used to list
"Ask" first, so DBAs opened on Ask OVERWATCH instead of Brief (owner ask 2026-08-30).
"""

from __future__ import annotations

from app.config import PAGES_BY_PROFILE


def test_every_profile_lands_on_brief_first():
    for profile, pages in PAGES_BY_PROFILE.items():
        assert pages[0] == "Brief", f"{profile} lands on {pages[0]!r}, not Brief"


def test_dba_still_sees_ask_but_it_trails_last():
    dba = PAGES_BY_PROFILE["DBA"]
    assert "Ask" in dba                 # DBA-only grounded Q&A still available
    assert dba[-1] == "Ask"             # ...but last, matching the nav display order
    assert dba[0] == "Brief"
