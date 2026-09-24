"""Next-Fifty #4 (v4.589.0): the opt-in EMAIL template carries three out-of-band dead-man watchers besides
new-event mail — stale telemetry OR a loader failure, a lost alert-scan / notify heartbeat, and failing
Teams/webhook delivery — so OVERWATCH going quiet is itself reported through an independent channel.
These locks keep the template honest: the app's own cadence rule, INFORMATION_SCHEMA (no lag) for the
heartbeat, the warm-window CRON, placeholder-only recipients, and teardown coverage."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import THRESHOLDS
from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_TPL = (_ROOT / "snowflake" / "native_alert_templates.sql").read_text(encoding="utf-8")
_LIVE = "\n".join(ln for ln in _TPL.splitlines() if not ln.lstrip().startswith("--"))


def _block(name: str) -> str:
    start = _LIVE.index(f"CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.{name}")
    nxt = _LIVE.find("CREATE OR REPLACE ALERT", start + 10)
    return _LIVE[start: nxt if nxt > 0 else len(_LIVE)]


def test_template_defines_exactly_the_email_alerts():
    found = re.findall(r"CREATE OR REPLACE ALERT DBA_MAINT_DB\.OVERWATCH\.(\w+)", _LIVE)
    assert found == list(mart_sql.EMAIL_ALERT_NAMES)


def test_stale_facts_reads_loader_owned_state_with_the_app_cadence():
    b = _block("NATIVE_ALERT_STALE_FACTS")
    assert "SOURCE_FRESHNESS_STATE" in b and "MART_SOURCE_FRESHNESS" not in b
    assert (f"IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', "
            f"{THRESHOLDS['stale_daily_fact_hours']}, {THRESHOLDS['stale_fact_hours']})") in b
    for err in ("mart_load_failed", "fact_load_failed", "extract_load_failed",
                "cloud_svc_mart_failed", "object_cost_load_failed"):
        assert f"'{err}'" in b, err
    assert "LAST_SUCCESSFUL_SCHEDULED_TIME" in b          # one failure emails once, not every hour


def test_scan_heartbeat_uses_information_schema_task_history():
    b = _block("NATIVE_ALERT_SCAN_HEARTBEAT")
    for part in ("INFORMATION_SCHEMA.TASK_HISTORY", "TASK_ALERT_SCAN", "TASK_ALERT_NOTIFY",
                 "DATEADD('hour', -3", "ALERT_ROUTES WHERE ENABLED"):
        assert part in b, part
    assert "ACCOUNT_USAGE" not in b


def test_delivery_failing_covers_send_expiry_and_undelivered_critical():
    b = _block("NATIVE_ALERT_DELIVERY_FAILING")
    for part in ("route_send_failed", "undelivered_expired", "webhook_run_failed", "PAGE = 'NotifyWebhook'",
                 "ALERT_DELIVERIES", "DATEADD('minute', -60"):
        assert part in b, part


def test_template_only_carries_the_placeholder_recipient():
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", _TPL)
    assert emails and set(emails) == {"dba-team@example.com"}
    assert _LIVE.count("SYSTEM$SEND_EMAIL(") == 4


def test_dead_man_alerts_share_the_warm_window_cron():
    for name in ("NATIVE_ALERT_STALE_FACTS", "NATIVE_ALERT_SCAN_HEARTBEAT", "NATIVE_ALERT_DELIVERY_FAILING"):
        assert "SCHEDULE = 'USING CRON 10 * * * * America/Chicago'" in _block(name), name


def test_teardown_suspends_every_email_alert():
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    for name in mart_sql.EMAIL_ALERT_NAMES:
        assert f"ALTER ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.{name}" in td, name
        assert f"DROP ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.{name}" in td, name
