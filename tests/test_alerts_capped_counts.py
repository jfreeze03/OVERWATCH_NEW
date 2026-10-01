"""Reviews R1-177 / R1-179: an Alerts count read off a LIMITed frame never stops silently at the cap.

R1-177: when the uncapped open_alert_severity_counts read fails, the KPI tiles fall back to the open feed, which
is LIMIT 500 -- in a 1,200-event storm 'Open total' read 500 with the plain "across all severities" help. Now a
full feed reads '500+', CRITICAL / HIGH get a '+' when the severity-first feed could hide more of them past the
cap, and a caption names the fallback path.

R1-179: the drawer's "This rule recently (N events)" read len() of events_for_rule's LIMIT 20 (a rule with 200
events in 90 days read "(20 events)"), and the snoozed tray's "Snoozed (N)" read len() of a LIMIT 100 read.
"""

from __future__ import annotations

import pandas as pd

from app.data import mart_sql
from tests._source import read


def _feed(crit: int, high: int, medium: int) -> pd.DataFrame:
    return pd.DataFrame({"SEVERITY": ["CRITICAL"] * crit + ["HIGH"] * high + ["MEDIUM"] * medium})


def test_caps_match_the_reads_they_describe() -> None:
    from app.ui.pages import alerts
    src = read("app/ui/pages/alerts.py")
    assert f"mart_sql.open_alert_events({alerts._OPEN_FEED_CAP}, company)" in src
    assert "mart_sql.snoozed_alert_events(_SNOOZED_CAP, company)" in src
    assert f"LIMIT {alerts._RULE_HISTORY_CAP}\n" in mart_sql.events_for_rule("COST_X", 90)
    assert f"LIMIT {alerts._OPEN_FEED_CAP}\n" in mart_sql.open_alert_events(alerts._OPEN_FEED_CAP)
    # the feed's order is what makes the per-severity floors below sound
    assert ("ORDER BY CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1"
            in mart_sql.open_alert_events(alerts._OPEN_FEED_CAP))


def test_feed_fallback_under_the_cap_is_exact() -> None:
    from app.ui.pages.alerts import _feed_fallback_counts
    assert _feed_fallback_counts(_feed(3, 4, 5), 500) == (3, 4, 12, "3", "4", "12", False)
    assert _feed_fallback_counts(pd.DataFrame({"SEVERITY": ["critical", "High"]}), 500)[:3] == (1, 1, 2)


def test_feed_fallback_at_the_cap_is_a_floor() -> None:
    from app.ui.pages.alerts import _feed_fallback_counts
    # a storm: the feed is full of CRITICAL + HIGH -> total and HIGH are floors, CRITICAL is exact (HIGH rows follow)
    assert _feed_fallback_counts(_feed(300, 200, 0), 500) == (300, 200, 500, "300", "200+", "500+", True)
    # the feed reaches MEDIUM rows -> both CRITICAL and HIGH are exact, only the total is a floor
    assert _feed_fallback_counts(_feed(10, 20, 470), 500) == (10, 20, 500, "10", "20", "500+", True)
    # all CRITICAL -> CRITICAL itself may continue past the cap
    assert _feed_fallback_counts(_feed(500, 0, 0), 500) == (500, 0, 500, "500+", "0+", "500+", True)


def test_render_labels_the_fallback_tiles() -> None:
    src = read("app/ui/pages/alerts.py")
    block = src.split('if section == "Open events":', 1)[1].split('elif section == "Rules":', 1)[0]
    assert "_feed_fallback_counts(\n                    events.df, _OPEN_FEED_CAP)" in block
    assert '{"label": "Open total", "value": total_s,' in block
    assert "at least this many are open." in block and "if _feed_capped" in block
    assert "Tiles counted from the open-events feed" in block
    assert "total_n = len(events.df)" not in block                     # the uncapped-looking count is gone


def test_capped_count_labels() -> None:
    from app.ui.pages.alerts import _capped_count
    assert _capped_count(99, 100) == "99"
    assert _capped_count(100, 100) == "100+"
    assert _capped_count(1500, 1000) == "1,500+"


def test_drawer_and_tray_labels_read_the_cap() -> None:
    src = read("app/ui/pages/alerts.py")
    assert 'f"This rule recently ({len(hist.df)} events)"' not in src
    assert '(f"latest {_hn}" if _hn >= _RULE_HISTORY_CAP else f"{_hn}")' in src
    assert "Snoozed ({len(_snz.df)})" not in src
    assert "Snoozed ({_capped_count(len(_snz.df), _SNOOZED_CAP)})" in src
    assert "soonest to wake — more may be snoozed; any others" in src
    assert "more events are snoozed" not in src     # a full frame is a possibility, never a fact


def test_rendered_storm_tiles_say_500_plus_when_the_uncapped_count_fails(monkeypatch) -> None:
    import pytest
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from tests.test_alerts_failed_reads import _fail, _ok, _render_section, _stub
    storm = _feed(300, 200, 0).assign(
        EVENT_ID=[f"E{i:07d}" for i in range(500)], RULE_ID="COST_X", RAISED_AT=pd.Timestamp("2026-09-30"),
        COMPANY="ALFA", TITLE="x", DETAIL="", METRIC_VALUE=1.0, STATUS="OPEN", ACK_BY=None, ACK_AT=None)
    from app.companies import COMPANIES
    stubs = {}
    for comp in COMPANIES:                     # whatever the default scope is
        stubs[f"alert_counts_{comp}"] = _fail("timeout")
        stubs[f"alert_events_{comp}"] = _ok(storm)
    _stub(monkeypatch, stubs)
    at = AppTest.from_function(_render_section, default_timeout=30)
    at.session_state["alerts_section"] = "Open events"
    at.run()
    assert not at.exception, at.exception
    html = " ".join(md.value for md in at.markdown)
    assert "500+" in html and "200+" in html
    assert any("Tiles counted from the open-events feed" in c.value for c in at.caption)


def test_rendered_snoozed_tray_at_exactly_the_cap_never_claims_more(monkeypatch) -> None:
    """Exactly 100 snoozed events fill the LIMIT 100 frame with nothing hidden: the tray says '100+' and
    that more MAY be snoozed -- never that more ARE (the pre-fix caption stated it as fact)."""
    import pytest
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.companies import COMPANIES
    from app.ui.pages import alerts
    from tests.test_alerts_failed_reads import _ok, _render_section, _stub
    n = alerts._SNOOZED_CAP
    snz = pd.DataFrame({
        "EVENT_ID": [f"S{i:05d}" for i in range(n)], "RULE_ID": "COST_X",
        "RAISED_AT": pd.Timestamp("2026-09-30"), "COMPANY": "ALFA", "SEVERITY": "HIGH", "TITLE": "snoozed",
        "SNOOZED_UNTIL": pd.date_range("2099-01-01", periods=n, freq="min"),
        "SNOOZE_BY": "op", "SNOOZE_REASON": "noise"})
    _stub(monkeypatch, {f"alert_snoozed_{comp}": _ok(snz) for comp in COMPANIES})
    monkeypatch.setattr(alerts, "lazy_sections", lambda *_a, **_k: "Open events")
    at = AppTest.from_function(_render_section, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert any(f"Snoozed ({n}+)" in e.label for e in at.expander)
    caps = [c.value for c in at.caption]
    assert any(f"Showing the {n} soonest to wake — more may be snoozed" in c for c in caps), caps
    assert not any("more events are snoozed" in c for c in caps)
