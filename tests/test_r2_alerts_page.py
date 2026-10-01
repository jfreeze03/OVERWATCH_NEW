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


@pytest.mark.parametrize(("today", "newest"), [
    ("2026-10-02", "2026-10-01"),     # 03:00 Central on the 2nd: Oct 1 is the 06:45 partial snapshot
    ("2026-10-01", "2026-10-01"),     # all day on the 1st, after its load
    ("2026-10-02", "2026-09-30"),     # the 2nd after a failed Oct 1 load
])
def test_r2_050_no_complete_day_yet_is_not_missing_history(monkeypatch, today, newest):
    """With no complete metering day this month the MTD card has no pace, and its help blamed missing prior-month
    facts and told the reader to run backfill_365.sql -- with September fully loaded."""
    from datetime import date

    from app.core.result import QueryResult
    from app.ui.pages import overview as ov
    monkeypatch.setattr(ov, "account_today", lambda: date.fromisoformat(today))
    days = pd.date_range("2026-08-01", newest).date
    res = QueryResult(df=pd.DataFrame({"DAY": days, "CREDITS_BILLED": [100.0] * (len(days) - 1) + [45.0]}), ok=True)
    kpi = ov._mtd_pace_kpi(0.0, res, 3.68, 2.20, 0.0)
    assert kpi["label"] == "MTD credit spend" and "delta" not in kpi
    assert "backfill" not in kpi["help"] and "prior month has daily facts" not in kpi["help"]
    assert kpi["help"].startswith("Pace vs last month appears after this month's first complete metering day")


def test_r2_050_backfill_hint_only_when_the_prior_month_has_no_rows(monkeypatch):
    from datetime import date

    from app.core.result import QueryResult
    from app.ui.pages import overview as ov
    monkeypatch.setattr(ov, "account_today", lambda: date(2026, 10, 15))
    days = pd.date_range("2026-10-01", "2026-10-14").date
    res = QueryResult(df=pd.DataFrame({"DAY": days, "CREDITS_BILLED": [100.0] * len(days)}), ok=True)
    kpi = ov._mtd_pace_kpi(0.0, res, 3.68, 2.20, 0.0)
    assert "delta" not in kpi and "backfill_365.sql" in kpi["help"]
    # September loaded but billed nothing on the same days: no pace, and no backfill instruction
    days = pd.date_range("2026-09-01", "2026-10-14").date
    zero = QueryResult(df=pd.DataFrame({"DAY": days, "CREDITS_BILLED": [0.0 if d.month == 9 else 100.0
                                                                         for d in days]}), ok=True)
    kpi = ov._mtd_pace_kpi(0.0, zero, 3.68, 2.20, 0.0)
    assert "delta" not in kpi and "backfill" not in kpi["help"] and "no billed spend" in kpi["help"]


def _sweep_drawer():
    # AppTest runs this body as its own script: everything it needs is built here, not read from module globals
    import pandas as _pd

    from app.core.result import QueryResult as _QR
    from app.ui.pages import alerts
    event = _pd.DataFrame({
        "EVENT_ID": ["e1e2e3e4-0000-4000-8000-000000000001"], "RULE_ID": ["COST_ANOMALY_SWEEP"],
        "RAISED_AT": [_pd.Timestamp("2026-09-30 06:40:12")], "COMPANY": ["ALL"], "SEVERITY": ["HIGH"],
        "TITLE": ["SERVICE AUTO_CLUSTERING spiked to 140.0 credits on 2026-09-29 (z=8.1)"],
        "DETAIL": ["Median 12.0 credits/day over the prior 28d. | AI: The top family 'USE WAREHOUSE WH_ALFA_ETL; "
                   "MERGE ...' ran 4.1h vs 0.3h."],
        "METRIC_VALUE": [8.1], "STATUS": ["OPEN"], "ACK_BY": [None], "ACK_AT": [None],
    })
    alerts._open_events_section(_QR(df=event, ok=True), True, "ALL")


def test_r2_092_drawer_offers_no_closed_loop_on_a_warehouse_the_ai_text_named(monkeypatch):
    """An AUTO_CLUSTERING spike whose pre-explain mentions WH_ALFA_ETL offered 'Respond -- closed loop on
    WH_ALFA_ETL' (an ALTER WAREHOUSE booked against this event) and a re-check scoped to it."""
    _stub(monkeypatch, {})
    at = AppTest.from_function(_sweep_drawer, default_timeout=20)
    # a real (hex) event id: the closed-loop expander's ledger read validates it
    at.session_state["_ow_nav_context"] = {"event_id": "e1e2e3e4-0000-4000-8000-000000000001"}
    at.run()
    assert not at.exception, at.exception
    assert any(b.label == "Investigate →" for b in at.button), "the drawer did not open"
    assert not any("WH_ALFA_ETL" in str(e.label) for e in at.expander), [e.label for e in at.expander]
    assert not any("WH_ALFA_ETL" in str(getattr(b, "help", "") or "") for b in at.button)
