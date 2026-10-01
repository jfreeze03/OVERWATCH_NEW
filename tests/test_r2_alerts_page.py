"""Round-2 review fixes on the Alerts page (cluster e4): the Native delivery banner's integration read tier
(R2-098) and the event drawer's rule-history / prior-resolution reads failing silently (merge-notes lead, c03).
"""

from __future__ import annotations

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tests.test_alerts_failed_reads import (  # noqa: E402
    _ERRORS,
    _FAILED_KINDS,
    _drawer,
    _fail,
    _ok,
    _St,
    _stub,
    _texts,
)


def test_r2_098_delivery_reads_both_probes_on_the_five_minute_tier(monkeypatch):
    """The integration probe sat on the shared 4 h metadata entry (no app write invalidates it), so a dropped or
    recreated integration read as up / missing for hours under 'who gets paged right now'."""
    from app.ui.pages import alerts
    by_key = {"delivery_integ": _ok(pd.DataFrame({"name": ["OVERWATCH_TEAMS"]})),
              "delivery_task": _ok(pd.DataFrame({"name": ["TASK_ALERT_NOTIFY"], "state": ["started"]})),
              "delivery_last": _ok(pd.DataFrame({"LAST_SEND": ["2026-09-30 01:00:00"]})),
              "delivery_routes_integ": _ok(pd.DataFrame({"N": ["OVERWATCH_TEAMS"]}))}
    tiers: dict[str, str] = {}

    def _run(*_a, **kw):
        tiers[str(kw.get("key"))] = str(kw.get("tier"))
        return by_key[str(kw.get("key"))]

    fake = _St()
    monkeypatch.setattr(alerts, "run", _run)
    monkeypatch.setattr(alerts, "st", fake)
    alerts._delivery_status()
    assert tiers["delivery_integ"] == "recent" and tiers["delivery_task"] == "recent"
    assert "metadata" not in tiers.values()
    assert fake.out[0][0] == "success" and fake.out[0][1].startswith("Delivery LIVE")


def _open_drawer(monkeypatch, results) -> AppTest:
    _stub(monkeypatch, results)
    at = AppTest.from_function(_drawer, default_timeout=20)
    at.session_state["_ow_nav_context"] = {"event_id": "E1EVENT0"}
    at.run()
    assert not at.exception, at.exception
    assert any(b.label == "Assemble the evidence" for b in at.button), "the drawer did not open"
    return at


@pytest.mark.parametrize("kind", _FAILED_KINDS)
def test_lead_c03_failed_drawer_history_reads_are_unavailable(monkeypatch, kind):
    at = _open_drawer(monkeypatch, {"hist": _fail(kind), "res": _fail(kind)})
    t = _texts(at)
    assert ("This rule's recent events could not be read, so its history is missing from this drawer."
            in t["error"])
    assert ("How this rule was resolved before could not be read, so its past resolutions are missing from "
            "this drawer." in t["error"])
    assert any(_ERRORS[kind] in c.value for c in at.code)            # the error is one click away


def test_lead_c03_ok_drawer_reads_show_no_error(monkeypatch):
    at = _open_drawer(monkeypatch, {})
    assert not any("could not be read" in e for e in _texts(at)["error"])


def test_r2_050_overview_mtd_and_pace_stop_at_the_metering_fact_newest_day(monkeypatch):
    """03:00 Central on Sep 15: the newest FACT_METERING_DAILY row (Sep 14) is the partial 06:45 snapshot, so the
    complete-days MTD (the budget card's numerator) and the pace window end at Sep 13 -- an on-pace month reads
    0%, not the -4% the partial row used to produce."""
    from datetime import date

    from app.core.result import QueryResult
    from app.ui.pages import overview as ov
    monkeypatch.setattr(ov, "account_today", lambda: date(2026, 9, 15))
    days = pd.date_range("2026-08-01", "2026-09-14").date
    res = QueryResult(df=pd.DataFrame({"DAY": days, "CREDITS_BILLED": [100.0] * (len(days) - 1) + [45.0]}), ok=True)
    complete = ov._mtd_spend_usd(3.68, 2.20, preloaded=res, exclude_today=True)[0]
    assert complete == pytest.approx(13 * 100.0 * 3.68)          # Sep 1-13; the Sep 14 snapshot is not a whole day
    kpi = ov._mtd_pace_kpi(0.0, res, 3.68, 2.20, 0.0)
    assert kpi["delta"].startswith("+0% vs ") or kpi["delta"].startswith("-0% vs "), kpi["delta"]
