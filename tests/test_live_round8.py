"""Locks for live round 8 (v4.17.0): incidents honor the triage filter,
delivery routes gain company scoping (Teams = ALFA-only for now), and
SEC_BREAK_GLASS_USE retires for good.

The triage-filter law: every new metric/panel takes the page filters
(company at minimum) at birth. The incident surfaces shipped without it and
production caught it same-day — these locks make the omission impossible
to repeat on THESE surfaces, and the discipline note covers the next ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG26 = (_ROOT / "snowflake" / "migrations" / "V026__teams_safe_delivery.sql").read_text(encoding="utf-8")
_MIG34 = (_ROOT / "snowflake" / "migrations" / "V034__route_company_filter.sql").read_text(encoding="utf-8")
_CR = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Incidents honor the triage filter (readers + call sites + declare flow)
# ---------------------------------------------------------------------------

def test_incident_readers_take_company_with_account_rows():
    oi = mart_sql.open_incidents(50, "ALFA")
    assert "(i.COMPANY = 'ALFA' OR UPPER(i.COMPANY) = 'ALL')" in oi
    assert "COMPANY = '" not in mart_sql.open_incidents(50)          # ALL stays account-wide
    pr = mart_sql.incident_proposals(20, "ALFA")
    assert "(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')" in pr
    met = mart_sql.incident_metrics(90, "ALFA")
    assert met.count("(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')") == 2   # window CTE + OPEN_NOW


def test_control_room_and_brief_pass_the_scope():
    for call in ("incident_metrics(90, company)", "open_incidents(50, company)",
                 "incident_proposals(20, company)"):
        assert call in _CR, call
    assert 'key=f"inc_metrics_{company}"' in _CR                     # cache keys scoped too
    assert 'key=f"open_incidents_{company}"' in _CR
    assert 'key=f"inc_props_{company}"' in _CR
    brief = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert "open_incidents(5, _inc_company)" in brief


def test_declare_links_members_by_company_and_family():
    body = _CR.split("def _incident_declare_sql", 1)[1].split("\ndef ", 1)[0]
    # both companies share rule families — family alone could link the other
    # company's alerts as members (live round 8)
    assert "e.COMPANY = {sql_literal(str(company))}" in body
    assert "UPPER(e.COMPANY) = 'ALL'" in body                        # account rows ride along


# ---------------------------------------------------------------------------
# V034 — route company filter (sender v4) + break-glass retirement
# ---------------------------------------------------------------------------

def _sender(text: str) -> str:
    start = text.find("CREATE OR REPLACE PROCEDURE")
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def test_v034_guard_column_and_owner_decision():
    assert "EXCEPTION (-20034" in _MIG34
    assert "IF (v < 33) THEN" in _MIG34
    assert "SELECT 34 AS VERSION" in _MIG34
    assert "ADD COLUMN IF NOT EXISTS COMPANY_FILTER VARCHAR(40) DEFAULT 'ALL'" in _MIG34
    assert "SET COMPANY_FILTER = 'ALFA'" in _MIG34                   # Teams = ALFA-only for now


def test_sender_v4_is_v3_with_the_five_edits():
    s34 = _sender(_MIG34)
    assert s34.count("AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')") == 2
    assert "COALESCE(r.COMPANY_FILTER, 'ALL') AS COMPANY_FILTER" in s34
    assert "r_compfilter := rec.COMPANY_FILTER;" in s34
    # revert the five edits and recover V026's sender exactly
    rev = s34.replace(",\n               COALESCE(r.COMPANY_FILTER, 'ALL') AS COMPANY_FILTER", "")
    rev = rev.replace("\n    r_compfilter VARCHAR;   -- v4: per-route company scope (owner: Teams = ALFA-only for now)", "")
    rev = rev.replace("\n        r_compfilter := rec.COMPANY_FILTER;", "")
    rev = re.sub(r"\n\s+AND \(:r_compfilter = 'ALL' OR e\.COMPANY = :r_compfilter OR UPPER\(e\.COMPANY\) = 'ALL'\)",
                 "", rev)
    rev = rev.replace("""
      -- v4: an event NO route will ever carry (company-filtered out) is out
      -- of delivery scope by policy, not undelivered — no hourly noise.
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r2
                  WHERE r2.ENABLED
                    AND (COALESCE(r2.COMPANY_FILTER, 'ALL') = 'ALL'
                         OR e.COMPANY = r2.COMPANY_FILTER
                         OR UPPER(e.COMPANY) = 'ALL'));""", ";")
    assert rev == _sender(_MIG26)                                    # zero silent drift


def test_expiry_watchdog_respects_delivery_scope():
    s34 = _sender(_MIG34)
    tail = s34.split("undelivered_expired", 1)[0]
    assert "out\n      -- of delivery scope by policy" in tail or "out of delivery scope" in _MIG34
    assert "r2.ENABLED" in tail                                      # eligibility needs a live route


def test_break_glass_rule_is_gone_events_closed_evidence_stays():
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG" in _MIG34
    assert "WHERE RULE_ID = 'SEC_BREAK_GLASS_USE'" in _MIG34
    assert "RESOLUTION_KIND = 'EXPECTED'" in _MIG34                  # lingering opens close tagged
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "retired at V034" in sec                                  # panel says so
    assert "admin_role_activity" in sec                              # evidence panel survives
    rb = (_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "~~SEC_BREAK_GLASS_USE~~" in rb                           # catalogue marks it retired


def test_triage_filter_law_is_written_down():
    log = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "triage-filter law" in log
