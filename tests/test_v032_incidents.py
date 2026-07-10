"""Locks for V032 — the incident object (v4.15.0, design doc built to spec).

Owner decisions baked in: auto-declare CRITICALs ON (per dedupe family per
24h, SETTINGS toggle), reopen window 14 days, declare/close DBA-only.
Births are declared, auto-critical, or proposed — never silent. Lineage is
joins, not notes text. History never rewrites.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V032__incident_object.sql").read_text(encoding="utf-8")
_CR = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_v032_guard_version_and_tables():
    assert "EXCEPTION (-20032" in _MIG
    assert "IF (v < 31) THEN" in _MIG
    assert "SELECT 32 AS VERSION" in _MIG
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.INCIDENTS" in _MIG
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS" in _MIG
    assert "REOPENED_FROM   VARCHAR(80)" in _MIG          # reopen = new incident, linked


def test_v032_lineage_is_additive_joins():
    assert "REMEDIATION_LOG ADD COLUMN IF NOT EXISTS EVENT_ID VARCHAR(80)" in _MIG
    assert "REMEDIATION_LOG ADD COLUMN IF NOT EXISTS INCIDENT_ID VARCHAR(80)" in _MIG
    assert "SAVINGS_LEDGER  ADD COLUMN IF NOT EXISTS REMEDIATION_ID VARCHAR(80)" in _MIG


def test_v032_owner_decisions_are_data():
    assert "'INCIDENT_AUTO_DECLARE_CRITICAL' AS KEY, 'TRUE' AS VALUE" in _MIG
    assert "'INCIDENT_REOPEN_DAYS' AS KEY, '14' AS VALUE" in _MIG


def test_v032_autodeclare_compresses_and_never_doubles():
    body = _MIG.split("SP_INCIDENT_AUTODECLARE", 1)[1]
    assert "INCIDENT_AUTO_DECLARE_CRITICAL" in body        # toggle read at runtime
    assert "UPPER(e.SEVERITY) = 'CRITICAL'" in body
    assert "DATEADD('hour', -24" in body                   # one per family per 24h
    assert "SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1)" in body
    assert "i.STATUS IN ('OPEN', 'MITIGATED')" in body     # open-incident family guard
    assert "AUTO_LINKED" in body and "'SP_INCIDENT_AUTODECLARE'" in body


def test_v032_proposals_require_a_human():
    view = _MIG.split("INCIDENT_PROPOSALS AS", 1)[1].split("CREATE OR REPLACE PROCEDURE", 1)[0]
    assert "NOT EXISTS" in view                            # already-member alerts excluded
    assert "NEARBY_WH_CHANGES" in view                     # ±30min change correlation
    assert "suggestions ONLY" in _MIG


def test_v032_task_chain_and_bookkeeping():
    assert "TASK_LOAD_HOURLY SUSPEND" in _MIG
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY" in _MIG
    tail = _MIG.split("TASK_INCIDENT_AUTODECLARE RESUME", 1)[1]
    assert "TASK_LOAD_MARTS_V27_HOURLY RESUME" in tail     # siblings resume too
    assert "TASK_LOAD_HOURLY RESUME" in tail
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "PRESERVED — no DROP here" in teardown          # operator history survives teardown
    assert "DROP VIEW IF EXISTS DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS;" in teardown
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_INCIDENT_AUTODECLARE;" in teardown
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert "INCIDENTS       TO ROLE OVERWATCH_OPERATOR" in roles
    assert "INCIDENT_MEMBERS TO ROLE OVERWATCH_OPERATOR" in roles


def test_is_rerun_rider_closed_as_already_safe():
    assert "ALREADY-SAFE" in _MIG or "already-safe" in _MIG
    doc = (_ROOT / "docs" / "design" / "V029_INCIDENT_OBJECT.md").read_text(encoding="utf-8")
    assert "BUILT as V032" in doc


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_incident_readers_shapes():
    oi = mart_sql.open_incidents(50)
    assert "STATUS IN ('OPEN', 'MITIGATED')" in oi and "MEMBERS" in oi
    mem = mart_sql.incident_members_detail("abc-123")
    assert "ALERT_TITLE" in mem and "'abc-123'" in mem
    assert "''" in mart_sql.incident_members_detail("x'y")    # injection-safe
    met = mart_sql.incident_metrics(90)
    for col in ("OPEN_NOW", "TTD_MIN", "MTTA_MIN", "MTTR_MIN",
                "REOPEN_PCT", "COMPRESSION", "CHANGE_PCT"):
        assert col in met, col
    assert "('WH_CHANGE', 'DEPLOY')" in met                   # the IaC payoff metric
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("open_incidents", "incident_members_detail", "incident_proposals",
                 "incident_metrics"):
        assert f"mart_sql.{name}" in canary, name


# ---------------------------------------------------------------------------
# Control Room wiring — DBA-gated, generate-then-run, forward-only
# ---------------------------------------------------------------------------

def test_control_room_incidents_section():
    assert 'st.subheader("Incidents")' in _CR
    assert "resolve_role_profile(current_role()) in OPERATOR_PROFILES" in _CR
    assert "mart_sql.incident_metrics(90)" in _CR
    assert "mart_sql.open_incidents(50)" in _CR
    assert "nothing groups silently" in _CR                   # proposals expander says so


def test_declare_sql_links_the_family_without_doubling():
    body = _CR.split("def _incident_declare_sql", 1)[1].split("\ndef ", 1)[0]
    assert "SET OW_INC_ID = UUID_STRING();" in body           # three statements, one id
    assert "SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1)" in body
    assert "NOT EXISTS" in body                               # already-member alerts skipped


def test_close_sql_is_forward_only_and_events_log():
    body = _CR.split("def _incident_close_sql", 1)[1].split("\ndef ", 1)[0]
    assert "STATUS IN ('OPEN', 'MITIGATED')" in body          # resolved rows never move
    assert "REOPENED_FROM" in body                            # doc'd in the docstring
    assert 'log_ui_event("incident_declare"' in _CR
    assert 'log_ui_event("incident_close"' in _CR
    assert 'type="primary"' in _CR.split("inc_prop_exec", 1)[1][:120]
