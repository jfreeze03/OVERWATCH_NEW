"""DO-FIRST wave lock tests (v4.82.0).

Covers the trust/actionability fixes on the headline exec KPIs and the morning
surfaces: C1 (score fail-open), N1 (forecast partial-day low bias), C3/N14/N15
(freshness never-loaded + cadence-aware + named), N2 (undelivered criticals),
N3 (triage actionability + linking ids), C4/C7 (uncapped alert counts),
N6 (boss chart account time), N8 (score-driver nav), N9 (small-frame CSV defer).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic.actions import triage_queue
from app.logic.forecast import month_end_projection
from app.logic.scoring import REQUIRED_SIGNAL_SOURCES, platform_score

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C1 — platform score must not fail OPEN (outage suppressing failures != Healthy)
# ---------------------------------------------------------------------------
def test_c1_missing_required_source_is_incomplete_not_healthy():
    # The cardinal sin: every health signal read failed, so nothing adds penalty
    # and a naive score would be 100/Healthy. With coverage gating it is Incomplete.
    res = platform_score(signals={}, available=set())
    assert res.state == "Incomplete"
    assert res.score == 0
    assert res.state != "Healthy"


def test_c1_partial_coverage_still_incomplete():
    # Board loaded but alerts did not — a real critical could be hidden.
    res = platform_score(signals={"critical_alerts": 0}, available={"board"})
    assert res.state == "Incomplete"
    assert set(REQUIRED_SIGNAL_SOURCES) == {"board", "alerts"}


def test_c1_full_coverage_scores_normally():
    healthy = platform_score(signals={}, available={"board", "alerts"})
    assert healthy.state == "Healthy" and healthy.score == 100
    degraded = platform_score(signals={"critical_alerts": 9}, available={"board", "alerts"})
    assert degraded.state != "Incomplete" and degraded.score < 100


def test_c1_available_none_is_backward_compatible():
    # Callers that don't pass coverage (older code / retro history) still score.
    res = platform_score(signals={"critical_alerts": 1}, available=None)
    assert res.state != "Incomplete"


# ---------------------------------------------------------------------------
# N1 — forecasts must exclude today's PARTIAL day from the forward daily rate
# ---------------------------------------------------------------------------
def test_n1_forecast_excludes_partial_today_from_rate():
    days = [date(2026, 7, 9 + i) for i in range(6)]  # six complete days at $100
    rows = [{"DAY": d, "USD": 100.0} for d in days]
    rows.append({"DAY": date(2026, 7, 15), "USD": 10.0})  # today, partial
    f = month_end_projection(pd.DataFrame(rows), date(2026, 7, 15), engine="linear")
    assert f.ok
    # Rate is built from complete days only ($100), NOT dragged down by today's $10
    # (the old inclusive baseline would have averaged to ~$87).
    assert f.daily_rate_usd == 100.0
    # MTD still counts today's actual spend.
    assert f.mtd_usd == 610.0


def test_n1_forecast_source_comment_present():
    assert "N1:" in _src("app/logic/forecast.py")
    assert 'frame["DAY"] < today' in _src("app/logic/forecast.py")
    contract = _src("app/ui/pages/cost_parts/contract.py")
    assert contract.count("< account_today()") >= 1  # burn tail + planner both fixed
    assert 'cydf["DAY"] < today' in contract


# ---------------------------------------------------------------------------
# C3 / N14 / N15 — freshness: never-loaded counts, cadence-aware, named
# ---------------------------------------------------------------------------
def test_c3_stale_count_includes_never_loaded():
    hs = mart_sql.health_strip()
    assert "LAST_LOAD_TS IS NULL OR" in hs  # STALE_SOURCES arm counts NULL ts


def test_n14_stalest_is_cadence_aware_and_n15_named():
    hs = mart_sql.health_strip()
    assert "STALEST_SOURCE_NAME" in hs      # N15: names the source
    assert "MAX_BY" in hs and "URGENCY" in hs
    assert "1e9" in hs                       # never-loaded sentinel outranks all
    # cadence limits come from THRESHOLDS, not a flat 26h/3h pair
    assert "'%DAILY%' OR SOURCE_NAME LIKE '%METERING%'" in hs


def test_c3_board_renders_not_loaded_state():
    cr = _src("app/ui/pages/control_room.py")
    assert '"NOT LOADED"' in cr
    assert 'df["NOT_LOADED"] = df["LAST_LOAD_TS"].isna()' in cr
    assert "STATUS" in cr


def test_n15_brief_stalest_label():
    from app.ui.pages.brief import _stalest_label
    assert _stalest_label({"STALEST_SOURCE_H": "7.0", "STALEST_SOURCE_NAME": "FACT_QUERY_HOURLY"}) == \
        "FACT_QUERY_HOURLY 7.0h"
    assert _stalest_label({"STALEST_SOURCE_H": "-1", "STALEST_SOURCE_NAME": "FACT_STORAGE_DAILY"}) == \
        "FACT_STORAGE_DAILY never loaded"
    assert _stalest_label({"STALEST_SOURCE_H": "-1", "STALEST_SOURCE_NAME": "none"}) == "no data yet"


# ---------------------------------------------------------------------------
# N2 — undelivered criticals surfaced on the morning surfaces
# ---------------------------------------------------------------------------
def test_n2_undelivered_arm_in_strip():
    hs = mart_sql.health_strip()
    assert "'UNDELIVERED_CRITICAL'" in hs
    assert "ALERT_DELIVERIES" in hs and "NOT EXISTS" in hs
    assert "-30, CURRENT_TIMESTAMP()" in hs  # 30+ min old


def test_n2_surfaced_on_shell_brief_and_control_room():
    assert "UNDELIVERED_CRITICAL" in _src("app/main.py")
    assert "reached nobody" in _src("app/main.py")
    assert "UNDELIVERED_CRITICAL" in _src("app/ui/pages/brief.py")
    assert "UNDELIVERED_CRITICAL" in _src("app/ui/pages/control_room.py")


# ---------------------------------------------------------------------------
# N3 — triage queue carries linking ids and is selectable
# ---------------------------------------------------------------------------
def test_n3_triage_carries_event_and_rule_ids():
    alerts = pd.DataFrame([{"SEVERITY": "CRITICAL", "TITLE": "t", "DETAIL": "d",
                            "RAISED_AT": "2026-07-07", "EVENT_ID": "E1", "RULE_ID": "R1"}])
    q = triage_queue(alerts, None, None)
    assert "EVENT_ID" in q.columns and "RULE_ID" in q.columns
    row = q.iloc[0]
    assert row["EVENT_ID"] == "E1" and row["RULE_ID"] == "R1"


def test_n3_triage_non_alert_rows_have_empty_ids():
    tasks = pd.DataFrame([{"TASK_NAME": "T", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S",
                           "FAILED": 2, "LAST_ERROR": "", "DAY": "2026-07-06"}])
    q = triage_queue(None, tasks, [{"label": "WH", "value": 1.0, "z": 6.0}])
    assert set(q["EVENT_ID"]) == {""}  # task + anomaly rows carry no event id


def test_n3_control_room_queue_is_actionable():
    cr = _src("app/ui/pages/control_room.py")
    assert 'selectable_table(queue[' in cr
    assert "request_navigation" in cr


# ---------------------------------------------------------------------------
# C4 / C7 — alert KPI tiles count with one uncapped aggregate
# ---------------------------------------------------------------------------
def test_c4c7_uncapped_severity_counts():
    sql = mart_sql.open_alert_severity_counts("ALFA")
    assert "COUNT_IF(UPPER(SEVERITY) = 'CRITICAL')" in sql
    assert "COUNT(*) AS TOTAL" in sql
    assert "STATUS IN ('OPEN', 'ACK')" in sql
    # company filter keeps account-level rows
    assert "UPPER(COMPANY) = 'ALL'" in sql


def test_c4c7_alerts_page_uses_aggregate():
    al = _src("app/ui/pages/alerts.py")
    assert "open_alert_severity_counts" in al
    assert "total_n" in al


# ---------------------------------------------------------------------------
# N6 / N8 — boss chart account time + score-driver navigation
# ---------------------------------------------------------------------------
def test_n6_boss_chart_uses_account_today():
    ov = _src("app/ui/pages/overview.py")
    assert 'account_today().strftime("%Y-%m")' in ov


def test_n8_score_driver_navigation():
    ov = _src("app/ui/pages/overview.py")
    assert "_SCORE_DRIVER_NAV" in ov
    assert "Investigate →" in ov
    # every live driver name maps to a page
    from app.ui.pages.overview import _SCORE_DRIVER_NAV
    for driver in ("Over budget", "Critical alerts", "Query failures",
                   "Stale telemetry", "Owner queue"):
        assert driver in _SCORE_DRIVER_NAV


# ---------------------------------------------------------------------------
# N9 — small-frame CSV serialized once per content, not every rerun
# ---------------------------------------------------------------------------
def test_n9_small_frame_csv_is_memoized():
    comp = _src("app/ui/components.py")
    assert "_ow_dlsmall" in comp
    assert "N9:" in comp
    # the memoized branch reuses cached bytes when the fingerprint matches
    assert '_slot.get("fp") == _fp' in comp
