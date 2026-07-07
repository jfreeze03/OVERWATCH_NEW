"""V018 delivery-first-class + closed-loop drawer + storm rollup."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_V018 = (_ROOT / "snowflake" / "migrations" / "V018__delivery_first_class.sql").read_text(encoding="utf-8")


def test_v018_notify_in_chain_with_guarded_resume():
    assert "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY" in _V018
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_ALERT_SCAN" in _V018
    assert "SHOW NOTIFICATION INTEGRATIONS LIKE 'OVERWATCH_WEBHOOK'" in _V018
    assert "RESULT_SCAN(LAST_QUERY_ID())" in _V018
    assert "notify task left suspended" in _V018       # honest no-op path
    assert "SCHEMA_VERSION < 17" in _V018 and "RAISE not_ready" in _V018
    assert "SELECT 18 AS VERSION" in _V018


def test_v018_digest_v2_sends_guarded():
    assert "SEND_SNOWFLAKE_NOTIFICATION" in _V018
    assert "CORTEX.COMPLETE" in _V018                  # full digest body carried
    assert "OVERWATCH morning digest" in _V018
    assert "delivery attempted" in _V018
    # send failure never breaks the digest write
    assert _V018.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST") \
        < _V018.index("SEND_SNOWFLAKE_NOTIFICATION")


def test_webhook_file_is_setup_only_now():
    wd = (_ROOT / "snowflake" / "webhook_delivery.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE NOTIFICATION INTEGRATION" in wd
    assert "V018" in wd                                 # points at the chain
    assert "CREATE OR REPLACE PROCEDURE" not in wd      # proc lives in V012 now


def test_inline_fix_targets_warehouse_rules_only():
    from app.logic.navigate import inline_fix_warehouse

    assert inline_fix_warehouse("COST_CLOUD_SVC_RATIO",
                                "WH_TRXS_TRANSFORM ratio 31%") == "WH_TRXS_TRANSFORM"
    assert inline_fix_warehouse("COST_ANOMALY_SWEEP", "WAREHOUSE WH_ETL spent 300") == "WH_ETL"
    assert inline_fix_warehouse("SEC_CRED_EXPIRY", "WH_X something") == ""   # not a lever rule
    assert inline_fix_warehouse("COST_CLOUD_SVC_RATIO", "no warehouse named") == ""


def test_alerts_page_wiring():
    src = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert "_delivery_status()" in src
    assert "Delivery LIVE" in src and "No webhook integration" in src
    assert "Group by rule (storm view)" in src
    assert "ALERT_CLOSED_LOOP" in src                  # audited inline execution
    assert "Respond — closed loop" in src


def test_roi_readers_never_mix_estimates_with_verified():
    import pytest as _pt

    from app.data import mart_sql as _m

    s = _m.savings_summary_quarter()
    assert "VERIFIED_QTD_USD" in s and "ESTIMATED_OPEN_USD" in s
    assert "STATE = 'VERIFIED'" in s and "STATE = 'ESTIMATED'" in s
    assert "DATE_TRUNC('quarter'" in s
    c = _m.app_cost_quarter()
    assert "WH_ALFA_OVERWATCH" in c and "DATE_TRUNC('quarter'" in c
    led = _m.ledger_for_event("ab12cd34")
    assert "'%event ab12cd34%'" in led and "LIMIT 5" in led
    with _pt.raises(ValueError):
        _m.ledger_for_event("not-valid-id!")
    with _pt.raises(ValueError):
        _m.ledger_for_event("ab12cd34ef")   # too long


def test_routing_recipes_documented():
    from pathlib import Path

    wd = (Path(__file__).resolve().parents[1] / "snowflake" / "webhook_delivery.sql").read_text(encoding="utf-8")
    assert "OVERWATCH_WEBHOOK_PAGERDUTY" in wd
    assert "events.pagerduty.com" in wd
    assert "'ALL', 'CRITICAL', 'OVERWATCH_WEBHOOK_PAGERDUTY'" in wd
    assert "OVERWATCH_WEBHOOK_FINOPS" in wd
