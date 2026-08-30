"""V105 lock: CREATE OR REPLACE TABLE is scored DESTRUCTIVE change-risk (2026-08-30, v4.358.0).

Completes the finding deferred from the security-layer hunt #2. A CREATE OR REPLACE TABLE that
wipes a live table was classified benign CREATE (RISK ~40) and so entered neither the
destructive-events breakdown nor the RISK>=70 change-risk queue -- a false all-clear. Both the mart
loader (SP_LOAD_SECURITY_FACTS, V105) and the live recent_ddl_changes builder now promote it to
base 55 (ALTER band), so only a PROD replace by an admin role reaches the >=70 queue while routine
service-role replaces (never ACCOUNTADMIN/SNOW_ACCOUNTADMINS, <=65) stay out of it.
"""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app.data import security_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_live_recent_ddl_changes_promotes_create_or_replace_table():
    sql = security_sql.recent_ddl_changes(7, "ALFA", "", "")
    # a table create whose text has OR REPLACE gets base 55 (via a group-level MAX flag)
    assert ("WHEN q.QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')\n"
            "                    AND MAX(IFF(q.QUERY_TEXT ILIKE '%OR REPLACE%', 1, 0)) = 1 THEN 55") in sql
    # the existing DROP/TRUNCATE=90 classification is untouched
    assert "WHEN q.QUERY_TYPE ILIKE 'DROP%' OR q.QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90" in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_v105_loader_scores_create_or_replace_destructive_both_arms():
    mig = (_ROOT / "snowflake/migrations/V105__change_risk_create_or_replace_destructive.sql").read_text(
        encoding="utf-8")
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS" in mig
    # both reload arms (d<=3 + d>3 backfill) promote CHANGE_KIND + base 55
    assert mig.count("AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 'DESTRUCTIVE'") == 2
    assert mig.count("AND QUERY_TEXT ILIKE '%OR REPLACE%' THEN 55") == 2
    # base 55 is the ALTER band, NOT 90 -- so a service-role replace (no admin bump) stays < 70
    assert "THEN 90\n" in mig  # DROP/TRUNCATE still 90
    assert mig.count("WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE'") == 2
    # the V100 reload-gap fix survives
    assert "IF (d <= 3)" in mig
    assert "EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)" in mig
    # guarded + versioned, no schema change
    assert "EXCEPTION (-20105" in mig and "IF (v < 104) THEN" in mig
    assert "SELECT 105 AS VERSION" in mig
    assert "CREATE TABLE " not in mig and "ALTER TABLE " not in mig
    sqlglot.parse(mig, dialect="snowflake")


def test_v105_only_promotes_table_creates_not_views():
    # CREATE OR REPLACE VIEW must NOT be promoted (definition churn, no data loss); the branch is
    # gated on QUERY_TYPE IN the table-create types, which excludes CREATE_VIEW.
    mig = (_ROOT / "snowflake/migrations/V105__change_risk_create_or_replace_destructive.sql").read_text(
        encoding="utf-8")
    assert "QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT')\n" in mig
    assert "'CREATE_VIEW'" not in mig.split("AND QUERY_TEXT ILIKE '%OR REPLACE%'")[0][-200:]
