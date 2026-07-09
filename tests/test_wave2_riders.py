"""Locks for the wave-2 rider panels + small fixes (v4.12.0, Codex r5/r6).

Delivery SLOs and alert fatigue land on Alerts -> History; acceptance and
per-page telemetry (cache-hit %, batch, truncation) and usage events land on
Admin -> Performance; the forecast-quality readout rides the Overview page;
bulk RESOLVE now requires a resolution kind; fixes show how to reverse and
log remediation_exec/alert_ack/alert_resolve usage events.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql
from app.logic.remediation import reverse_hint

_ROOT = Path(__file__).resolve().parents[1]
_ALERTS = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
_ADMIN = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
_OV = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
_OPT = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Builders — shapes and honesty
# ---------------------------------------------------------------------------

def test_delivery_slo_reads_ledger_events_and_error_log():
    sql = mart_sql.delivery_slo_summary(30)
    for col in ("EVENTS_RAISED", "EVENTS_DELIVERED", "MEDIAN_MIN", "P95_MIN",
                "UNDELIVERED_CRITICALS_30M", "ROUTE_FAILURES"):
        assert col in sql, col
    assert "route_send_failed" in sql and "LOGGED_AT" in sql   # APP_ERROR_LOG's real ts col
    assert "DATEADD('minute', -30" in sql                      # criticals get 30m grace
    assert "ROUTE_ID" in mart_sql.delivery_by_route(30)


def test_alert_fatigue_counts_kinds_untagged_and_repeats():
    sql = mart_sql.alert_fatigue(30)
    for col in ("PER_WEEK", "ACTIONED", "NOISE", "EXPECTED", "UNTAGGED", "REPEAT_EVENTS"):
        assert col in sql, col
    assert "COALESCE(DEDUPE_KEY, EVENT_ID)" in sql             # NULL dedupe keys never inflate


def test_acceptance_funnel_is_the_honest_subset():
    sql = mart_sql.acceptance_funnel(90)
    for col in ("FIXES_EXECUTED", "FIXES_COPIED", "FIXES_FAILED",
                "SAVINGS_ESTIMATED", "SAVINGS_VERIFIED", "SAVINGS_REJECTED", "VERIFIED_USD"):
        assert col in sql, col
    assert "REMEDIATION_LOG" in sql and "SAVINGS_LEDGER" in sql
    assert "impression" not in sql.lower()                     # audit rows only, by decision


def test_telemetry_by_page_uses_the_v027_rider_null_safely():
    sql = mart_sql.telemetry_by_page(7)
    for col in ("P95_S", "CACHE_HIT_PCT", "AVG_BATCH", "TRUNCATED_N", "SLOW_2S"):
        assert col in sql, col
    assert "IFF(CACHE_HIT IS NULL, NULL," in sql               # pre-V027 rows excluded from %
    assert "EVENT_KIND" in mart_sql.usage_event_summary(30)


def test_rider_builders_are_canaried():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("delivery_slo_summary", "delivery_by_route", "alert_fatigue",
                 "acceptance_funnel", "telemetry_by_page", "usage_event_summary"):
        assert f"mart_sql.{name}" in canary, name


# ---------------------------------------------------------------------------
# Panel wiring
# ---------------------------------------------------------------------------

def test_alerts_history_gains_slo_and_fatigue():
    assert "mart_sql.delivery_slo_summary(30)" in _ALERTS
    assert "mart_sql.delivery_by_route(30)" in _ALERTS
    assert "mart_sql.alert_fatigue(30)" in _ALERTS
    assert "Undelivered criticals (30m+)" in _ALERTS
    assert "100% is not the target" in _ALERTS                 # routes filter by severity


def test_admin_performance_gains_rider_panels():
    assert "def _perf_rider_panels" in _ADMIN
    assert "_perf_rider_panels()" in _ADMIN                    # actually called
    assert "mart_sql.telemetry_by_page(7)" in _ADMIN
    assert "mart_sql.usage_event_summary(30)" in _ADMIN
    assert "mart_sql.acceptance_funnel(90)" in _ADMIN
    assert "a floor, not a census" in _ADMIN                   # cache-hit % honesty


def test_overview_promotes_forecast_quality():
    assert "Forecast quality (3-month backtest):" in _OV       # rides the page
    assert "with st.expander(\"Forecast accuracy" in _OV       # evidence stays available
    assert "backtest_forecasts(_bt_daily[[\"DAY\", \"USD\"]])" in _OV  # computed once, used twice


# ---------------------------------------------------------------------------
# Small fixes: bulk kind, reverse guidance, usage events
# ---------------------------------------------------------------------------

def test_bulk_resolve_requires_a_kind():
    body = _ALERTS.split("Bulk acknowledge / resolve", 1)[1]
    assert 'if b_action == "RESOLVE":' in body
    assert "alert_bulk_kind" in body and "RESOLUTION_KINDS" in body
    assert "_lifecycle_sql(options[label], b_action, b_note, b_kind)" in body


def test_reverse_hint_names_the_evidence_not_a_guess():
    hint = reverse_hint("RESIZE", "WH_ALFA_ETL")
    assert "WAREHOUSE_CHANGE_REGISTRY" in hint                 # where the old value lives
    assert "REMEDIATION_LOG.STATEMENT_SQL" in hint             # what actually ran
    assert "WH_ALFA_ETL" in hint and "<previous>" in hint      # never invents the prior value
    assert "AUTO_SUSPEND" in reverse_hint("AUTO_SUSPEND", "X")
    assert "re-apply the previous setting" in reverse_hint("SOMETHING_NEW", "X")


def test_exec_sites_show_reverse_and_log_the_event():
    assert _OPT.count('log_ui_event("remediation_exec"') == 1
    assert 'reverse_hint("RESIZE"' in _OPT
    assert _ALERTS.count('log_ui_event("remediation_exec"') == 1
    assert "remediation.reverse_hint(" in _ALERTS
    # lifecycle actions log too — single + bulk paths
    assert _ALERTS.count('log_ui_event("alert_resolve" if') == 2
