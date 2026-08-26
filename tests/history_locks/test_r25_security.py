"""r25 locks — owner picked #6 (new-network logins) + #7 (egress watch).

Both are lazy/click-gated: the new-network read rides the Access tab's
existing batch round-trip; Egress is its own section and renders nothing
until selected. First paint on Security pays zero for this round.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_new_network_logins_contract():
    from app.data import security_sql
    sql = security_sql.new_network_logins(7)
    assert "DATEADD('day', -90," in sql                      # fixed 90d baseline
    assert "'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'" in sql  # same list as admin_role_holders (owner 2026-07-13)
    assert "FIRST_SEEN >= DATEADD('day', -7," in sql         # only window-new pairs surface
    assert "FIRST_AUTHENTICATION_FACTOR" in sql              # password vs SSO visible per row
    assert "COALESCE(L.CLIENT_IP, '(none)')" in sql          # null IPs group honestly
    assert "FIRST_SEEN >= DATEADD('day', -90," in security_sql.new_network_logins(9999)  # clamp


def test_dormant_reawakening_contract():
    import pytest

    from app.data import security_sql
    sql = security_sql.dormant_reawakening(company="ALFA")
    assert "SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.USERS" in sql and "GRANTS_TO_USERS" in sql
    assert "L.IS_SUCCESS = 'YES'" in sql                          # successful logins only
    assert "LAG(L.EVENT_TIMESTAMP)" in sql                         # gap between consecutive logins
    assert "COALESCE(l.PREV_LOGIN, u.CREATED_ON)" in sql           # deep-dormant edge -> since creation
    assert "GAP_DAYS" in sql and "WAKE_LOGIN" in sql and "LAST_ACTIVE_BEFORE" in sql
    assert "w.GAP_DAYS >= 45" in sql                              # default gap threshold
    assert "QUALIFY ROW_NUMBER()" in sql                          # biggest-gap login per user
    # PERF #15: user scope is now the per-distinct-user membership subquery (COMPANY_FOR_USER
    # once per distinct user, not once per scanned row) — leak-safe and byte-exact for non-NULL users.
    assert ("L.USER_NAME IN (SELECT USER_NAME FROM (SELECT DISTINCT USER_NAME FROM "
            "SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY") in sql
    assert "COMPANY_FOR_USER(USER_NAME) = 'ALFA'" in sql          # UDF once per distinct user
    # baseline clamps to LOGIN_HISTORY's 365d retention; the gap clamps to <= 365
    assert "DATEADD('day', -365," in security_sql.dormant_reawakening(baseline_days=9999)
    assert "w.GAP_DAYS >= 365" in security_sql.dormant_reawakening(dormant_gap_days=9999)
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(sql, dialect="snowflake")


def test_egress_builders_contract():
    from app.data import security_sql
    e = security_sql.egress_daily(30)
    assert "DATA_TRANSFER_HISTORY" in e
    assert "TARGET_REGION" in e and "TRANSFER_TYPE" in e
    assert "HAVING SUM(BYTES_TRANSFERRED) > 0" in e           # zero-byte rows stay off the chart
    u = security_sql.unload_activity(30, "ALFA")
    assert "QUERY_TYPE = 'UNLOAD'" in u
    assert "EXECUTION_STATUS = 'SUCCESS'" in u
    assert "GB_OUT" in u and "SAMPLE_TARGET" in u
    # V030 shape law: the company arm applies the UDF to the plain column
    assert "COMPANY_FOR_USER(USER_NAME) = 'ALFA'" in u
    assert "COMPANY_FOR_" not in security_sql.unload_activity(30, "ALL")   # ALL = no arm
    assert security_sql.unload_activity(30, "ALFA") != security_sql.unload_activity(30, "TREXIS")
    assert "DATEADD('day', -90," in security_sql.unload_activity(9999)   # clamp
    # #18: per-event sibling keeps the hour/dow/destination/baseline the score needs
    re_ = security_sql.unload_risk_events(30, "ALFA")
    assert "QUERY_TYPE = 'UNLOAD'" in re_ and "EXECUTION_STATUS = 'SUCCESS'" in re_
    assert "HOUR_OF_DAY" in re_ and "DOW_ISO" in re_          # per-event time-of-day
    assert "CONVERT_TIMEZONE('America/Chicago'" in re_        # account-local, not UTC
    assert "MEDIAN(GB_OUT) OVER (PARTITION BY USER_NAME)" in re_   # per-user baseline
    # per-user fairness cap: one busy loader can't evict everyone else's events
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY USER_NAME ORDER BY GB_OUT DESC)" in re_
    assert "COMPANY_FOR_USER(USER_NAME) = 'ALFA'" in re_      # same scoping as sibling
    assert "COMPANY_FOR_" not in security_sql.unload_risk_events(30, "ALL")


def test_egress_section_and_new_network_panel_wired():
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert '"Changes", "Clients", "Egress",' in sec   # its own lazy section (v4.246: + AI guardrails)
    assert "def _egress_tab(" in sec
    assert '"key": "newnet"' in sec                           # rides the Access batch round-trip
    assert "New networks for privileged users" in sec
    # The $ egress panel (KPI + 'GB by destination region' chart) moved to Cost ▸ Spend
    # (audit consolidation); Security keeps the security lenses + a deep-link to Cost.
    assert "sec_egress_cost_link" in sec
    # honest empty states — silence must read as "checked, clean", never blank
    assert "No break-glass account logged in from a network unseen" in sec
    assert "No unloads to stages in this window" in sec
    # #18: the exfiltration-score subsection is toggle-gated and wired into the egress tab
    assert "sec_exfil_toggle" in sec
    assert "egress_exfil_severity(" in sec
    assert "security_sql.unload_risk_events(" in sec
    assert "No unload events to score in this window" in sec


def test_r25_builders_are_canaried():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for fn in ("new_network_logins", "egress_daily", "unload_activity", "unload_risk_events"):
        assert f"security_sql.{fn}" in canary, f"{fn} has no canary — every reader gets one"
