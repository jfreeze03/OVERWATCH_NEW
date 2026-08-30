"""Incident-management bug-hunt locks (2026-08-30).

App-side fixes shipped in v4.351.0 alongside the owner-gated V099 migration:
  F2  manual declare guards against duplicating an already-open family (returns a
      statement LIST so it is never string-split on ';' — F9)
  F5  RCA no longer advertises the (production-inert) entity-match factor
  F7  incident_gantt lanes by unique INCIDENT_ID, not the possibly-shared TITLE
  F8  declare confirm/latch keys scoped by the selected proposal
  F10 the Incidents exception-summary won't show a clean all-clear when a feed is unknown
  F11 incident_gantt marks a 'now' reference line (open bars reach it; C38)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.logic.rca import rank_root_causes
from app.ui import charts
from app.ui.pages.control_room import _incident_declare_sql

_ROOT = Path(__file__).resolve().parents[1]
_CR = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")


# --- F2 / F9: declare returns a statement list, guarded against family duplication ------

def test_declare_returns_statement_list_not_a_semicolon_string():
    stmts = _incident_declare_sql("Deploy failed; rollback pending", "CRITICAL", "ALFA",
                                  "COST_WH_DAILY_CREDITS|x|ACCOUNT|")
    # F9: a LIST of complete statements — the caller runs each verbatim, so a ';' in the
    # alert-derived title can never split the INSERT mid-literal.
    assert isinstance(stmts, list) and len(stmts) == 2
    assert stmts[0].startswith("INSERT INTO") and "INCIDENTS" in stmts[0]
    assert "INCIDENT_MEMBERS" in stmts[1]


def test_declare_incidents_insert_guards_family_already_open_by_company():
    stmts = _incident_declare_sql("t", "HIGH", "ALFA", "FAM|x|ACCOUNT|")
    incidents_sql = stmts[0]
    # F2: the parent INSERT is a no-op when a (family, company) already has an OPEN/MITIGATED
    # incident holding a member alert of that family — mirroring SP_INCIDENT_AUTODECLARE.
    assert "WHERE NOT EXISTS" in incidents_sql
    assert "i.STATUS IN ('OPEN', 'MITIGATED')" in incidents_sql
    assert "i.COMPANY = 'ALFA'" in incidents_sql
    assert "= 'FAM'" in incidents_sql
    # the members INSERT only fires if the incident row was actually created (no orphans).
    assert "AND EXISTS (SELECT 1 FROM" in stmts[1] and "i2.INCIDENT_ID =" in stmts[1]


def test_declare_caller_iterates_the_list_and_scopes_keys_by_proposal():
    body = _CR.split('elif section == "Incidents & triage":', 1)[1]
    # F9: no split-on-';'
    assert '_dec.split(";")' not in body
    assert "for _stmt in _dec:" in body
    # F8: confirm/latch keys carry the selected proposal so a typed DECLARE authorizes
    # only that proposal (mirrors the close flow's per-incident key scoping)
    assert '_exec_key = f"inc_prop_exec_{_pick}"' in body
    assert 'key="inc_prop_exec"' not in body


# --- F5: RCA stops advertising the inert entity-match factor ----------------------------

def test_rca_why_omits_entity_match_without_context():
    cands = [{"kind": "WH_CHANGE", "title": "resize", "when": None, "magnitude": 0.5,
              "entity": "WH_ALFA_ETL", "magnitude_text": "", "changed_by": ""}]
    # production call path passes no entity_name/families -> the why must not claim entity match
    no_ctx = rank_root_causes(cands, None, top=5)
    assert no_ctx and "entity match" not in no_ctx[0]["why"]
    # when a caller DOES supply context, the factor is advertised again
    with_ctx = rank_root_causes(cands, None, entity_name="WH_ALFA_ETL", top=5)
    assert "entity match" in with_ctx[0]["why"]


def test_control_room_caption_drops_the_inert_entity_match_claim():
    assert "0.20·entity-match" not in _CR
    assert "Ranked by timing" in _CR


# --- F7 / F11: gantt lanes by unique incident + a 'now' reference line ------------------

def test_incident_gantt_lanes_distinct_same_title_incidents(monkeypatch):
    rendered = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda c, **k: rendered.append(c))
    monkeypatch.setattr(charts, "_empty_note", lambda *a, **k: None)
    df = pd.DataFrame({
        "INCIDENT_ID": ["aaaaaaaa-1", "bbbbbbbb-2"],
        "TITLE": ["Auto: COST_WH_DAILY_CREDITS", "Auto: COST_WH_DAILY_CREDITS"],  # SAME title
        "SEVERITY": ["CRITICAL", "CRITICAL"],
        "STATUS": ["RESOLVED", "OPEN"],
        "STARTED": pd.to_datetime(["2026-08-14 01:00", "2026-08-15 09:00"]),
        "ENDED": pd.to_datetime(["2026-08-14 03:30", "2026-08-15 12:00"]),
        "DURATION_MIN": [150, 180],
    })
    charts.incident_gantt(df)
    spec = json.dumps(rendered[0].to_dict())
    # F7: two same-title incidents get two DISTINCT lanes (title + short id), not one
    assert "aaaaaaaa" in spec and "bbbbbbbb" in spec
    assert '"field": "LANE"' in spec
    # F11: a 'now' reference rule is layered in
    assert '"layer"' in spec and '"NOW"' in spec


# --- F10: exception-summary won't false-all-clear when a feed is unknown ----------------

def test_incidents_section_flags_partial_telemetry_before_all_clear():
    body = _CR.split('elif section == "Incidents & triage":', 1)[1]
    assert "if not (inc_met.usable() and _crit_known and _sv):" in body
    # the partial-telemetry signal is appended to the same exception list the all-clear reads
    assert '"value": "partial"' in body
