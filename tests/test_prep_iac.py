"""Locks for the in-the-meantime batch (v4.16.0): change attribution (V033),
Flyway-readiness, incidents SOP + Brief visibility.

Attribution is evidence, not lineage; MANAGED/MANUAL derives at read time
from DEPLOY_ACTORS (empty today = honestly MANUAL/UNKNOWN); the unmanaged-
change alert deliberately does not ship as decorative config; the Flyway
panel lights up on its own when the ledger appears.
"""

from __future__ import annotations

from pathlib import Path

from app.data import change_impact_sql, mart_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V033__change_attribution.sql").read_text(encoding="utf-8")
_ADMIN = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# V033 — attribution
# ---------------------------------------------------------------------------

def test_v033_guard_version_and_pieces():
    assert "EXCEPTION (-20033" in _MIG
    assert "IF (v < 32) THEN" in _MIG
    assert "SELECT 33 AS VERSION" in _MIG
    assert "ADD COLUMN IF NOT EXISTS CHANGED_BY VARCHAR(200)" in _MIG
    assert "'DEPLOY_ACTORS' AS KEY, '' AS VALUE" in _MIG          # empty until IaC lands
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION();" in _MIG


def test_v033_attribution_is_evidence_not_lineage():
    body = _MIG.split("SP_CHANGE_ATTRIBUTION", 1)[1]
    assert "r.CHANGED_BY IS NULL" in body                          # attribute once
    assert "DATEADD('minute', -65, r.CHANGE_SEEN_AT)" in body      # hourly snapshot window
    assert "MAX_BY(q.USER_NAME, q.START_TIME)" in body             # latest match wins
    assert "q.QUERY_TEXT ILIKE '%' || r.WAREHOUSE_NAME || '%'" in body
    assert "evidence, not lineage" in _MIG                         # the V010 rule, stated


def test_v033_task_chain_and_no_decorative_rule():
    assert "TASK_LOAD_HOURLY SUSPEND" in _MIG
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY" in _MIG
    tail = _MIG.split("TASK_CHANGE_ATTRIBUTION RESUME", 1)[1]
    assert "TASK_INCIDENT_AUTODECLARE RESUME" in tail              # siblings resume
    assert "TASK_LOAD_HOURLY RESUME" in tail
    assert "OPS_UNMANAGED_CHANGE" not in _MIG.split("--", 1)[0]    # never as live config
    assert "decorative config" in _MIG                             # the why, recorded
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "SP_CHANGE_ATTRIBUTION();" in teardown
    assert "TASK_CHANGE_ATTRIBUTION;" in teardown


def test_registry_reader_derives_source_at_read_time():
    sql = change_impact_sql.warehouse_change_registry(90, "ALFA")
    assert "CHANGED_BY" in sql and "CHANGE_SOURCE" in sql
    for word in ("'MANAGED'", "'MANUAL'", "'UNKNOWN'"):
        assert word in sql, word
    assert "'DEPLOY_ACTORS'" in sql                                # setting drives the split
    assert "CROSS JOIN" in sql and "da.ACTORS" in sql              # uncorrelated by construction
    # the correlated-aggregate shape that failed live (round 9) can't return:
    assert "SELECT POSITION" not in sql
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "CHANGE_SOURCE: MANAGED" in ops                         # semantics on the panel


# ---------------------------------------------------------------------------
# Flyway-readiness
# ---------------------------------------------------------------------------

def test_flyway_reader_is_quoted_and_uncanaried():
    sql = mart_sql.flyway_history()
    assert '"flyway_schema_history"' in sql                        # case-sensitive table
    assert '"installed_rank"' in sql and '"success"' in sql
    src = (_ROOT / "app" / "data" / "mart_sql.py").read_text(encoding="utf-8")
    assert "NOT canaried" in src.split("def flyway_history", 1)[1].split('"""', 2)[1]
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "flyway_history" not in canary                          # absence is legitimate


def test_admin_panel_degrades_honestly_without_flyway():
    assert "mart_sql.flyway_history()" in _ADMIN
    assert "Flyway deploy history" in _ADMIN                       # present branch
    assert "Flyway not detected" in _ADMIN                         # absent branch
    assert "docs/FLYWAY_ADOPTION.md" in _ADMIN


def test_adoption_docs_carry_the_load_bearing_steps():
    doc = (_ROOT / "docs" / "FLYWAY_ADOPTION.md").read_text(encoding="utf-8")
    for marker in ("baseline -baselineVersion", "DEPLOY_ACTORS", "R__roles.sql",
                   "afterMigrate", "Do NOT run Flyway as ACCOUNTADMIN",
                   "scratch database"):
        assert marker in doc, marker
    toml = (_ROOT / "snowflake" / "flyway.toml.example").read_text(encoding="utf-8")
    assert "SNOWFLAKE_JWT" in toml                                 # key-pair, never a password
    assert "cleanDisabled = true" in toml                          # operator history is sacred
    assert "OVERWATCH_DEPLOY" in toml


# ---------------------------------------------------------------------------
# Incidents SOP + Brief visibility
# ---------------------------------------------------------------------------

def test_runbook_gains_the_incidents_sop():
    rb = (_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    sec = rb.split("Incidents — the operator SOP", 1)[1]
    for marker in ("INCIDENT_AUTO_DECLARE_CRITICAL", "REOPENED_FROM",
                   "INCIDENT_REOPEN_DAYS", "change-correlated %", "DEPLOY_ACTORS"):
        assert marker in sec, marker


def test_brief_shows_open_incidents_guarded():
    brief = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert "open_incidents(5, company)" in brief            # triage filter honored (batched)
    assert "if _inc.ok:" in brief                                  # silent pre-V032
    assert brief.count("ACCOUNT_USAGE") == 0                       # budget stays zero
