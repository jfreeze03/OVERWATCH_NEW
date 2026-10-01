"""R2-057 behaviour lock: Control Room > Timeline & movers, with a timeline row selected, renders the
+/-30 min drill. render() used to hold a branch-local `from datetime import timedelta` in the Pulse
section, which made the name local to the whole function, so this drill (a different section) raised
UnboundLocalError on every selection. AppTest over the shaped harness; the floor leg skips it like the
other section-switching AppTests (the AST ratchet in tests/test_local_import_scope.py runs there)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_run,
    _stub_shaped,
)

from app.core.result import QueryResult


def _timeline_frame() -> pd.DataFrame:
    base = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=3)
    return pd.DataFrame({
        "AT": pd.to_datetime([base, base + timedelta(minutes=10), base + timedelta(hours=2)]),
        "EVENT_TYPE": ["ALERT", "TASK_FAILURE", "DDL"],
        "SEVERITY": ["HIGH", "MEDIUM", "LOW"],
        "LABEL": ["WH_PROD queued", "LOAD_TASK failed", "ALTER WAREHOUSE"],
        "COMPANY": ["ALFA", "ALFA", "ALFA"],
        "REF_ID": ["E1", "T1", "Q1"],
    })


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_selecting_a_timeline_row_opens_the_30_minute_drill(monkeypatch):
    """The Pulse branch (the one that held the local import) never runs in this section, which is
    the exact path that raised UnboundLocalError: `_activity_ready` is never even computed here."""
    from streamlit.testing.v1 import AppTest

    from app.ui.pages import control_room

    def _run(*args, **kwargs):
        if str(kwargs.get("key") or "").startswith("incident_tl_mart_"):
            return QueryResult(df=_timeline_frame(), ok=True, source="MART_INCIDENT_TIMELINE")
        return _shaped_run(*args, **kwargs)

    real_selectable = control_room.selectable_table

    def _select_first_timeline_row(df, key, **kwargs):
        picked = real_selectable(df, key, **kwargs)
        return 0 if key == "cr_timeline_sel" else picked

    monkeypatch.setattr(control_room, "run", _run)
    monkeypatch.setattr(control_room, "selectable_table", _select_first_timeline_row)

    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Control Room")
    at.session_state["cr_section"] = "Timeline & movers"
    at.session_state["cr_tl_win"] = "48h (fresh)"
    at.run()
    assert not at.exception, at.exception
    errors = [str(e.value) for e in at.error]
    assert not any("could not finish rendering" in e for e in errors), errors
    md = " ".join(str(m.value) for m in at.markdown)
    # the first two events are 10 minutes apart; the third is 2h later, outside the +/-30 min window
    assert "**±30 minutes around** `WH_PROD queued` — 2 event(s)" in md, md[-2000:]
