"""r25 locks — owner picked #6 (new-network logins) + #7 (egress watch).

Both are lazy/click-gated: the new-network read rides the Access tab's
existing batch round-trip; Egress is its own section and renders nothing
until selected. First paint on Security pays zero for this round.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_new_network_logins_contract():
    from app.data import security_sql
    sql = security_sql.new_network_logins(7)
    assert "DATEADD('day', -90," in sql                      # fixed 90d baseline
    assert "'ACCOUNTADMIN', 'SECURITYADMIN', 'ORGADMIN'" in sql  # same list as admin_role_holders
    assert "FIRST_SEEN >= DATEADD('day', -7," in sql         # only window-new pairs surface
    assert "FIRST_AUTHENTICATION_FACTOR" in sql              # password vs SSO visible per row
    assert "COALESCE(L.CLIENT_IP, '(none)')" in sql          # null IPs group honestly
    assert "FIRST_SEEN >= DATEADD('day', -90," in security_sql.new_network_logins(9999)  # clamp


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


def test_egress_section_and_new_network_panel_wired():
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert '"Clients", "Egress", "Trust Center"' in sec       # its own lazy section
    assert "def _egress_tab(" in sec
    assert '"key": "newnet"' in sec                           # rides the Access batch round-trip
    assert "New networks for privileged users" in sec
    assert "GB by destination region" in sec
    # honest empty states — silence must read as "checked, clean", never blank
    assert "No break-glass account logged in from a network unseen" in sec
    assert "No unloads to stages in this window" in sec


def test_r25_builders_are_canaried():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for fn in ("new_network_logins", "egress_daily", "unload_activity"):
        assert f"security_sql.{fn}" in canary, f"{fn} has no canary — every reader gets one"
