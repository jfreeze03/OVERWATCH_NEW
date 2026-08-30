"""Security-layer bug-hunt #2 locks (2026-08-30, v4.357.0).

Second, deeper adversarial pass (7 finders). Nine confirmed; two refuted (egress NEW-branch volume
floor = intentional security-conservative choice; a same-region egress duplicate). One (CREATE OR
REPLACE change-risk classification) deferred as a scoped follow-up (needs live validation to avoid
re-flooding the de-noised panel).
  - [HIGH] login fact-coverage span -> day density (a gappy login mart passed COMPLETE and hid an
    attack in the gap window; also fixes the fact_coverage_complete density gap).
  - [MED] takeover 'breakthrough' anchored to the first failure -> a dense pre-success burst.
  - [MED] V104: SEC_CRED_EXPIRY dedupe key drops the ISO-week token (cross-week double-count).
  - [MED] day_ddl replay aligned with recent_ddl_changes (identity/role/policy DDL) + noise dropped.
  - [MED] egress_baseline scores only true egress (same-region internal excluded).
  - [LOW] dormant/reawakening severity tables sort worst-first.
  - [LOW] new_network_logins_fact volume bounded to 90d like its live sibling.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

from app.data import security_sql
from app.logic.insights import dormant_severity, reawakening_severity

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- HIGH (#1/#9): login fact-coverage measures day density, not calendar span -----
def test_login_fact_coverage_uses_distinct_day_density():
    for sql in (security_sql.login_fact_coverage(30), security_sql.security_login_fact_coverage(30)):
        assert "COUNT(DISTINCT DAY) AS COVERAGE_DAYS" in sql
        assert "DATEDIFF('day', MIN(DAY), MAX(DAY)) + 1 AS COVERAGE_DAYS" not in sql
        sqlglot.parse(sql, dialect="snowflake")


# ---- MED (#2): takeover breakthrough anchors to a dense pre-success burst ----------
def test_login_takeover_anchors_breakthrough_to_a_burst():
    sql = security_sql.login_takeover_candidates(7, "ALFA", 5)
    assert "breakthroughs AS (" in sql
    assert "DATEADD('hour', -6, s.EVENT_TIMESTAMP)" in sql           # bounded burst window
    assert "HAVING COUNT(*) >= 5" in sql                             # dense burst before the success
    # the old "any success after the first failure" anchor is gone
    assert "e.EVENT_TIMESTAMP > f.FIRST_FAILURE" not in sql
    assert security_sql._TAKEOVER_BURST_HOURS == 6
    sqlglot.parse(sql, dialect="snowflake")


# ---- MED (#3 / V104): SEC_CRED_EXPIRY dedupe key drops the ISO-week token -----------
def test_v104_drops_week_token_from_cred_expiry_key_and_is_guarded():
    mig = _src("snowflake/migrations/V104__sec_cred_expiry_dedupe_key.sql")
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in mig
    # the cred-expiry key ends at the EXPIRING/EXPIRED state -- no trailing week token
    assert ("c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
            "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')\n") in mig
    # the other weekly-keyed rule (SERVICE_TYPE) is untouched
    assert "s.SERVICE_TYPE || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))" in mig
    # the supersede sweep that now matches is preserved unchanged
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING|', '|EXPIRED|')" in mig
    assert "EXCEPTION (-20104" in mig and "IF (v < 103) THEN" in mig
    assert "SELECT 104 AS VERSION" in mig
    assert "CREATE TABLE" not in mig and "ALTER TABLE" not in mig
    sqlglot.parse(mig, dialect="snowflake")


# ---- MED (#5): day_ddl replay aligned with recent_ddl_changes ----------------------
def test_day_ddl_covers_identity_role_policy_ddl_and_drops_noise():
    sql = security_sql.day_ddl("2026-08-30", "ALFA")
    for qt in ("'DROP_ROLE'", "'ALTER_USER'", "'CREATE_USER'", "'CREATE_ROLE'"):
        assert qt in sql
    assert "QUERY_TYPE ILIKE '%ROLE%'" in sql and "QUERY_TYPE ILIKE '%POLICY%'" in sql
    assert "ALTER_WAREHOUSE_SUSPEND" not in sql and "ALTER_WAREHOUSE_RESUME" not in sql
    assert "ORDER BY START_TIME DESC" in sql and "LIMIT 500" in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- MED (#6): egress_baseline scores only true egress -----------------------------
def test_egress_baseline_excludes_same_region_internal_transfers():
    sql = security_sql.egress_baseline(30)
    assert "AND (TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)" in sql
    sqlglot.parse(sql, dialect="snowflake")


# ---- LOW (#7): dormant / reawakening severity tables sort worst-first ---------------
def test_reawakening_severity_sorts_high_first():
    df = pd.DataFrame([
        {"USER_NAME": "LONGGAP1", "GAP_DAYS": 170, "ROLE_COUNT": 1},   # Medium
        {"USER_NAME": "WHALE", "GAP_DAYS": 100, "ROLE_COUNT": 9},      # High by role count
        {"USER_NAME": "LONGGAP2", "GAP_DAYS": 175, "ROLE_COUNT": 1},   # Medium
    ])
    out = reawakening_severity(df)
    assert out.iloc[0]["USER_NAME"] == "WHALE"
    assert out.iloc[0]["SEVERITY"] == "High"


def test_dormant_severity_sorts_high_first():
    df = pd.DataFrame([
        {"USER_NAME": "LONGDORM", "DAYS_DORMANT": 170, "ROLE_COUNT": 1},  # Medium
        {"USER_NAME": "WHALE", "DAYS_DORMANT": 100, "ROLE_COUNT": 9},     # High by role count
    ])
    out = dormant_severity(df)
    assert out.iloc[0]["USER_NAME"] == "WHALE"
    assert out.iloc[0]["SEVERITY"] == "High"


# ---- LOW (#8): new_network_logins_fact volume bounded to 90d like the live sibling --
def test_new_network_logins_fact_bounds_volume_join_to_90d():
    sql = security_sql.new_network_logins_fact(7, "ALFA")
    assert "AND h.DAY >= DATEADD('day', -90, CURRENT_DATE())" in sql
    sqlglot.parse(sql, dialect="snowflake")
