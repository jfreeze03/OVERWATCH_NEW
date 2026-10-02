"""Playbooks + health strip contracts."""

from app.data import mart_sql
from app.logic import navigate, playbooks


def test_every_deep_link_rule_has_a_specific_playbook():
    for rule in navigate._RULE_TARGETS:
        text = playbooks.playbook_for(rule)
        assert ("1." in text and rule not in text.upper()) or "**Means:**" in text
        assert text != playbooks.playbook_for("TOTALLY_UNKNOWN_RULE")


def test_retired_and_weekly_rule_playbooks_say_so():
    # SEC_BREAK_GLASS_USE: rule row deleted at V034 (owner ask 2026-07-10), scan arm [15] gone in V157;
    # its playbook must not read as a live rule in an old event's drawer (V034 closed OPEN/ACK only).
    bg = playbooks.PLAYBOOKS["SEC_BREAK_GLASS_USE"]
    assert bg.startswith("**Retired (V034") and "**Means:**" not in bg and "snoozed" not in bg
    # OPS_CANARY_FAIL: the canary runs weekly (Mondays 05:30 CT; the rule's window is 168h), so the
    # first-step query must look back the week, not 1 day (zero rows by Tuesday read as "cleared").
    canary = playbooks.PLAYBOOKS["OPS_CANARY_FAIL"]
    assert "DATEADD('day', -1," not in canary and "DATEADD('day', -7, CURRENT_TIMESTAMP())" in canary


def test_playbook_family_fallback():
    # v4.49: playbooks name exact pill labels, not the old "Cost > Spend" shorthand
    assert "Cost Intelligence > Spend & Attribution" in playbooks.playbook_for("COST_BRAND_NEW_RULE")
    assert "Security" in playbooks.playbook_for("SEC_SOMETHING")
    assert "add one" in playbooks.playbook_for("XYZ")


def test_health_strip_builder():
    sql = mart_sql.health_strip()
    assert "'OPEN_CRITICAL'" in sql and "'STALEST_SOURCE_H'" in sql and "'MTD_CREDITS'" in sql
    # SOURCE_FRESHNESS_STATE since V040 (r13 #2): the strip reads the 10-min
    # snapshot table, not the 19-aggregate view, every 30s per viewer.
    assert "ALERT_EVENTS" in sql and "SOURCE_FRESHNESS_STATE" in sql and "FACT_METERING_DAILY" in sql
    # rec #3 (v4.159.0): still critical-only, but OPEN_CRITICAL now counts OPEN+ACK
    # to match open_alert_severity_counts / the pages / the score.
    assert "UPPER(e.SEVERITY) = 'CRITICAL'" in sql
    assert "STATUS IN ('OPEN', 'ACK')" in sql
    # rec #2 (v4.159.0): MTD month boundary anchored to the account timezone so it
    # agrees with the account_today()-anchored Overview MTD, not the session clock.
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())" in sql
    assert "DATE_TRUNC('month', CURRENT_DATE())" not in sql


# -- v4.609 (V166-V172 wave): the playbook text tracks what the re-derived scans now do. Each claim is checked
# against the LATEST definer of the proc that raises the rule, so a later re-derivation that drops the
# behaviour turns this red instead of leaving a stale first response in the drawer.

def _latest(proc: str) -> str:
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    return _latest_proc_bodies()[proc]


def test_v168_v169_playbooks_match_the_scans():
    pb = playbooks.PLAYBOOKS
    net = pb["SEC_NEW_ADMIN_NETWORK"]
    assert "login attempts" in net and "failed login attempt(s)" in net and "(0 successful)" in net
    assert "quiet for 90+ days alerts again" in net and "IS_SUCCESS" in net
    assert "logged in from a client IP" not in net                     # the old every-title-says-logged-in claim
    hourly = _latest("SP_ALERT_SCAN")
    assert "' failed login attempt(s) from new network '" in hourly and "' (0 successful)'" in hourly
    assert "'|FAILED'" in hourly and "lo.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'" in hourly   # failed key + supersede
    copy = pb["PIPE_COPY_FAILURES"]
    assert "the Central day the files failed" in copy and "in the last 24h" not in copy
    assert "10 failed files" in copy and "IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY)" in hourly
    assert "' failed file load(s) on ' || TO_VARCHAR(p.FAIL_DAY)" in hourly          # the title ends on the day
    recon = pb["DQ_RECON_ERROR"]
    assert "leaves the next scan" not in recon and "newest error-load date" in recon and "RECON_MTRC_ERROR" in recon
    assert "SAME date folds into that date's event" in recon
    assert "TO_VARCHAR(COALESCE(TO_DATE(r.NEWEST_LOAD), CURRENT_DATE()))" in _latest("SP_ALERT_SCAN_DAILY")
    daily = _latest("SP_ALERT_SCAN_DAILY")
    assert "PARTITION BY DATABASE_ID" in daily and "DELETED IS NULL" in daily
    assert "each live database id" in pb["COST_STORAGE_SURGE"]
    assert "read as 0 since V169" in pb["COST_IDLE_OPPORTUNITY"]
    assert "a threshold below 1 reads as 1 since V169" in pb["SEC_TRUST_REGRESSION"]


def test_v171_ref_gap_and_canary_playbooks_name_the_real_failures():
    ref = playbooks.PLAYBOOKS["PIPE_REF_GAP"]
    assert "ref_gap_check_failed" in ref and "ref_gap_scan_failed" in ref and "`AlertScan`" in ref
    scan = _latest("SP_SCAN_REF_GAPS")
    assert "SELECT 'AlertScan', 'ref_gap_check_failed'" in scan and "RAISE all_failed;" in scan
    canary = playbooks.PLAYBOOKS["OPS_CANARY_FAIL"]
    assert "`SELECT 1`" in canary and "only from the Admin per-builder canary" in canary
    assert "'SELECT 1 FROM ' || :cname" in _latest("SP_CANARY_SENTINEL")


def test_v172_playbooks_match_the_scans():
    perf = playbooks.PLAYBOOKS["PERF_CHANGE_REGRESSION"]
    assert "'fails 0->0'" in perf and "a scheduled run counts once" in perf
    impact = _latest("SP_CHANGE_IMPACT_SCAN")
    assert "' | fails ' || COALESCE(BASELINE_FAILS::VARCHAR, '0') || '->'" in impact   # the detail it quotes
    cs = playbooks.PLAYBOOKS["COST_CLOUD_SVC_ANOMALY"]
    assert "Disabling the rule (Alerts > Rules) stops the scan since V172" in cs
    guard = _latest("SP_SCAN_CLOUD_SVC_ANOMALY")
    assert "WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY' AND ENABLED;" in guard and "IF (:enabled_cnt = 0) THEN" in guard
