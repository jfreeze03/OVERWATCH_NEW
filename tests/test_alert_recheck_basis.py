"""Review R1-040: the drawer's "Re-check condition now" must measure the SAME basis the alert fired on.

PERF_QUEUED_MINUTES / PERF_SPILL_GB are raised (SP_ALERT_SCAN arms [04]/[05]) and kept firing (the V091
auto-clear sweep) on a TRAILING 24h of FACT_QUERY_HOURLY per warehouse. The re-check summed raw QUERY_HISTORY
since account-midnight -- a strict subset of that window -- so every morning it could read "Condition clear"
and offer the one-click ACTIONED resolve while the scan still held the event over its threshold.

COST_WH_DAILY_CREDITS is one DAY's total (arm [02]); its since-midnight re-check of an event raised for an
earlier day measures a different, partial day, so it may never present a clear verdict for that event.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.data import recheck_sql
from tests.test_alert_rule_consistency import _latest_proc_bodies

_ROOT = Path(__file__).resolve().parents[1]
# The CURRENT SP_ALERT_SCAN definer (last definition wins, as on the live account): V168 re-derived it from V162
# (R2-034 dropped the sweep's 48h age bound), so the recheck-vs-scan basis lock tracks the live body, never a
# pinned file a later re-derivation leaves behind.
_SCAN = _latest_proc_bodies()["SP_ALERT_SCAN"]
_ALERTS = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")


def _raise_arm(rule: str) -> str:
    """The raise arm's candidate subquery for ``rule`` in the current SP_ALERT_SCAN definer."""
    block = _SCAN.split(f"-- [0{'4' if rule == 'PERF_QUEUED_MINUTES' else '5'}] {rule}", 1)[1]
    return block.split("EXCEPTION", 1)[0]


@pytest.mark.parametrize(("rule", "value", "coalesced"), [
    ("PERF_QUEUED_MINUTES", "SUM(QUEUED_SEC_SUM) / 60", "COALESCE(SUM(QUEUED_SEC_SUM), 0) / 60"),
    ("PERF_SPILL_GB", "SUM(SPILL_REMOTE_GB)", "COALESCE(SUM(SPILL_REMOTE_GB), 0)"),
])
def test_queued_and_spill_recheck_read_the_alerts_trailing_24h_fact_basis(rule: str, value: str,
                                                                          coalesced: str) -> None:
    sql = recheck_sql.recheck_sql(rule, "WH_ALFA_BI_PRD")
    assert sql is not None
    arm = _raise_arm(rule)
    window = "HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())"
    # the alert's own value expression, source and window (so the two can't drift apart silently)
    assert value in arm and window in arm and "FACT_QUERY_HOURLY" in arm
    # same expression; an idle warehouse (no hourly rows) reads 0, which the sweep also treats as cleared
    assert f"SELECT {coalesced} AS CURRENT_VALUE" in sql and window in sql
    assert "DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY" in sql and "WAREHOUSE_NAME IS NOT NULL" in sql
    assert "UPPER(WAREHOUSE_NAME) = 'WH_ALFA_BI_PRD'" in sql
    # never the since-midnight raw-QUERY_HISTORY basis again, and no company filter (the sweep is per warehouse)
    assert "CURRENT_DATE()" not in sql and "QUERY_HISTORY" not in sql and "COMPANY" not in sql
    # the help phrase and the label follow the basis
    assert recheck_sql.recheck_window_phrase(rule) == "the last 24h of data"
    assert "(24h)" in recheck_sql.recheck_label(rule) and "today" not in recheck_sql.recheck_label(rule)


def test_queued_and_spill_recheck_still_need_a_safe_warehouse() -> None:
    for rule in ("PERF_QUEUED_MINUTES", "PERF_SPILL_GB"):
        assert recheck_sql.recheck_sql(rule, "") is None
        assert recheck_sql.recheck_sql(rule, "WH; DROP TABLE X") is None


def test_the_auto_clear_sweep_uses_the_same_basis_the_recheck_reads() -> None:
    sweep = _SCAN.split("V091 auto-clear", 1)[0].rsplit("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev", 1)[1]
    for value in ("SUM(QUEUED_SEC_SUM) / 60", "SUM(SPILL_REMOTE_GB)"):
        assert value in sweep
    assert sweep.count("HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())") == 3


@pytest.mark.parametrize(("title", "today", "want"), [
    ("WH_ALFA_ETL_PRD used 412.5 credits on 2026-09-29", "2026-09-30", "2026-09-29"),   # yesterday's event
    ("WH_ALFA_ETL_PRD used 120.0 credits on 2026-09-30", "2026-09-30", ""),             # today's: comparable
    ("WH_ALFA_ETL_PRD used 120.0 credits", "2026-09-30", ""),                           # no day: no claim
    ("", "2026-09-30", ""),
])
def test_closed_day_is_only_an_earlier_day_of_a_full_day_rule(title: str, today: str, want: str) -> None:
    assert recheck_sql.recheck_closed_day("COST_WH_DAILY_CREDITS", title, today) == want
    assert recheck_sql.recheck_closed_day(" cost_wh_daily_credits ", title, today) == want


def test_closed_day_never_applies_to_rolling_window_rules() -> None:
    title = "WH_ALFA_BI_PRD queued 45.0 min in 24h on 2026-09-29"
    for rule in ("PERF_QUEUED_MINUTES", "PERF_SPILL_GB", "PERF_QUERY_FAIL_PCT", "COST_CLOUD_SVC_RATIO", ""):
        assert recheck_sql.recheck_closed_day(rule, title, "2026-09-30") == ""


def test_arm_02_titles_carry_the_day_the_helper_reads() -> None:
    arm = _SCAN.split("-- [02] COST_WH_DAILY_CREDITS", 1)[1].split("EXCEPTION", 1)[0]
    assert "' credits on ' || f.DAY" in arm
    assert "f.DAY >= DATEADD('day', -1, CURRENT_DATE())" in arm      # yesterday or today


def test_drawer_never_offers_a_clear_for_a_closed_day_event() -> None:
    # the verdict carries the closed day, stamped at re-check time in the account (Central) timezone
    assert ('"closed_day": recheck_sql.recheck_closed_day(\n'
            '                                        _rid, str(row["TITLE"]), account_now().date().isoformat()),'
            in _ALERTS)
    assert '_rc_day = str(_rc_state.get("closed_day") or "")' in _ALERTS
    # a closed-day verdict takes the no-clear branch, which sits BEFORE the clear success + ACTIONED prefill
    gate = _ALERTS.index("elif not _rc_fresh or _rc_day:")
    assert gate < _ALERTS.index('st.success(f"Condition clear: {_rcl} = "')
    assert gate < _ALERTS.index('"Resolve as ACTIONED with this evidence"')
    branch = _ALERTS[gate:_ALERTS.index('st.success(f"Condition clear: {_rcl} = "')]
    assert "a closed day" in branch and "this is not a clear" in branch
    assert re.search(r"else:\s+st\.success\(f\"Condition clear", _ALERTS)
