"""Locks for the 2026-07-10 live findings, round 6 (v4.13.0).

V029's MAX() fix broke differently (UDF correlates its argument — aggregate
landed in the inlined WHERE); V030 uses the bulletproof shape: aggregate in
a derived table, UDF outside. Posture snapshot gains the governance score's
last two live inputs; gov panel and unused-roles go mart-first; CALL/session
pricing answers the 'three procs, no graph id' question; primary buttons
can never render pale-on-pale again.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql, mart27_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG29 = (_ROOT / "snowflake" / "migrations" / "V029__loader_fix.sql").read_text(encoding="utf-8")
_MIG30 = (_ROOT / "snowflake" / "migrations" / "V030__loader_fix2.sql").read_text(encoding="utf-8")
_SEC = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
_UC = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# V030 — loader fix 2 (the correct shape) + posture rider
# ---------------------------------------------------------------------------

def _proc(text: str) -> str:
    start = text.find("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    assert start > 0
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


_OLD_ROLE = """                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                       COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                       COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(MAX(COALESCE(WAREHOUSE_NAME, ''))) AS COMPANY,
                       COUNT(*) AS QUERIES,
                       COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                       ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3"""
_NEW_ROLE = """                SELECT g.HOUR_TS, g.ROLE_NAME, g.WAREHOUSE_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.EXEC_SEC
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(ROLE_NAME, 'UNKNOWN') AS ROLE_NAME,
                           COALESCE(WAREHOUSE_NAME, 'NONE') AS WAREHOUSE_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           ROUND(SUM(COALESCE(EXECUTION_TIME, 0)) / 1000, 1) AS EXEC_SEC
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2, 3
                ) g"""
_OLD_SCH = """                SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                       COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                       COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(MAX(COALESCE(DATABASE_NAME, ''))) AS COMPANY,
                       COUNT(*) AS QUERIES,
                       COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                       ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                       ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                       ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3"""
_NEW_SCH = """                SELECT g.HOUR_TS, g.DATABASE_NAME, g.SCHEMA_NAME,
                       DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME) AS COMPANY,
                       g.QUERIES, g.FAILS, g.QUEUED_SEC, g.SPILL_GB, g.P95_S
                FROM (
                    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS,
                           COALESCE(DATABASE_NAME, 'NONE') AS DATABASE_NAME,
                           COALESCE(SCHEMA_NAME, 'NONE') AS SCHEMA_NAME,
                           COUNT(*) AS QUERIES,
                           COUNT_IF(EXECUTION_STATUS = 'FAILED') AS FAILS,
                           ROUND(SUM(COALESCE(QUEUED_OVERLOAD_TIME, 0)) / 1000, 1) AS QUEUED_SEC,
                           ROUND(SUM(COALESCE(BYTES_SPILLED_TO_REMOTE_STORAGE, 0)) / POWER(1024, 3), 3) AS SPILL_GB,
                           ROUND(APPROX_PERCENTILE(TOTAL_ELAPSED_TIME, 0.95) / 1000, 1) AS P95_S
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                    GROUP BY 1, 2, 3
                ) g"""

_POSTURE_RIDER = """
                UNION ALL
                SELECT CURRENT_DATE(), 'MFA_GAP_USERS', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.USERS U
                WHERE U.DELETED_ON IS NULL AND U.DISABLED = FALSE
                  AND U.HAS_PASSWORD = TRUE AND COALESCE(U.HAS_MFA, FALSE) = FALSE
                  AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY L
                              WHERE L.USER_NAME = U.NAME
                                AND L.DAY >= DATEADD('day', -30, CURRENT_DATE())
                                AND L.PASSWORD_LOGINS > 0)
                UNION ALL
                SELECT CURRENT_DATE(), 'BREAKGLASS_GRANTS_30D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                  AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())"""


def test_v030_guard_version_and_first_fills():
    assert "EXCEPTION (-20030" in _MIG30
    assert "IF (v < 29) THEN" in _MIG30
    assert "SELECT 30 AS VERSION" in _MIG30
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);" in _MIG30
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 3);" in _MIG30


def test_v030_proc_is_v029_with_exactly_the_three_edits():
    base = _proc(_MIG29)
    expected = base.replace(_OLD_ROLE, _NEW_ROLE).replace(_OLD_SCH, _NEW_SCH)
    i = expected.find("'UNUSED_ROLES_90D'")
    j = expected.find("\n            ) s", i)
    expected = expected[:j] + _POSTURE_RIDER + expected[j:]
    assert _proc(_MIG30) == expected                   # chain: V027->V028->V029->V030


def test_v030_udf_never_touches_an_aggregate():
    proc = _proc(_MIG30)
    assert proc.count("COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME)") == 1
    assert proc.count("COMPANY_FOR_DATABASE(g.DATABASE_NAME)") == 1
    # the correlated-UDF-inside-MAX shape that failed after V029 is gone
    assert "COMPANY_FOR_WAREHOUSE(MAX(" not in proc
    assert "COMPANY_FOR_DATABASE(MAX(" not in proc
    # posture snapshot now carries every governance-score input
    for metric in ("'MFA_GAP_USERS'", "'BREAKGLASS_GRANTS_30D'",
                   "'EXPIRING_CRED_10D'", "'EXPIRED_CRED'"):
        assert metric in proc, metric


# ---------------------------------------------------------------------------
# Tuning adoptions (from the owner's fleet metrics screenshots)
# ---------------------------------------------------------------------------

def test_governance_score_reads_the_posture_snapshot_first():
    body = _SEC.split("def _governance_score_panel", 1)[1].split("\ndef ", 1)[0]
    assert 'key="gov_posture"' in body                 # the cheap daily read
    assert "MFA_GAP_USERS" in body and "BREAKGLASS_GRANTS_30D" in body
    assert "live fallback" in body                     # gov_counts survives as fallback
    assert 'key="gov_counts"' in body


def test_unused_roles_go_mart_first_with_coverage_guard():
    sql = mart27_sql.unused_roles_via_fact(90)
    assert "FACT_QUERY_ROLE_HOURLY" in sql
    assert "FIRST_TS FROM cov" in sql                  # young fact returns 0 rows -> live fallback
    assert "GRANTED_TO_USERS" in sql and "ROLE_NAME" in sql   # live contract kept
    assert "unused_roles_via_fact(90), security_sql.unused_roles(90)" in _SEC


# ---------------------------------------------------------------------------
# CALL / session pricing (the 'three procs, no graph id' question)
# ---------------------------------------------------------------------------

def test_call_pricing_rolls_children_via_root_query_id():
    sql = insights_sql.call_cost_lookup("01abc-123", 7)
    assert "QUERY_TYPE = 'CALL'" in sql
    assert "a.ROOT_QUERY_ID = c.QUERY_ID OR a.QUERY_ID = c.QUERY_ID" in sql
    assert "SESSION_ID::VARCHAR" in sql                # a session prices all its CALLs
    kids = insights_sql.call_children_costs("01abc-123", 7)
    assert "'CALL (own time)'" in kids                 # the root's own row is labeled
    assert "CREDITS_ATTRIBUTED_COMPUTE" in kids
    # injection-safe: quotes escape through sql_literal
    assert "''" in insights_sql.call_cost_lookup("x'y", 7)


def test_unit_costs_panel_prices_calls_and_children():
    assert "Price a specific CALL or session (measured)" in _UC
    assert "uc_call_ident" in _UC
    assert "call_children_costs" in _UC
    assert "if len(cdf) == 1:" in _UC                  # child breakdown for a single CALL
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("insights_sql.call_cost_lookup", "insights_sql.call_children_costs",
                 "mart27_sql.unused_roles_via_fact"):
        assert name in canary, name


# ---------------------------------------------------------------------------
# Primary buttons: never pale-on-pale again
# ---------------------------------------------------------------------------

def test_primary_buttons_force_dark_ink_across_markups():
    theme = (_ROOT / "app" / "theme.py").read_text(encoding="utf-8")
    assert 'button[data-testid="stBaseButton-primary"]' in theme
    assert "color:#06121f !important" in theme
    seg = theme.split('.stButton > button[kind="primary"],', 1)[1][:900]
    assert "span" in seg and "p," in seg               # descendants forced too
