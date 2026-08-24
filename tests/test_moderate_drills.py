"""MODERATE UX-sweep per-entity drills (owner 2026-08-20): 8 static tables that now
drill to per-entity detail via new/reused live builders. SQL builders are shape- and
injection-tested (they can't run against Snowflake in CI); UI wiring is source-locked.
(#7 attribution scope-to-warehouse was reverted — it broke the attribution-math
invariants; not in this wave.)"""

from __future__ import annotations

from pathlib import Path

from app.data import cost_sql, etl_sql, mart27_sql, ops_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ============================================= #6 compare per-warehouse =====

def test_compare_pattern_costs_by_warehouse():
    sql = mart27_sql.compare_pattern_costs_by_warehouse(
        "2026-06-01", "2026-06-07", "2026-06-08", "2026-06-14", warehouse="WH_X")
    assert "WAREHOUSE_NAME = 'WH_X'" in sql                       # exact scope
    assert "QUERY_ATTRIBUTION_HISTORY" in sql and "QUERY_HISTORY" in sql
    # an exact warehouse is a complete scope -> no company predicate re-applied
    assert "COMPANY_FOR_WAREHOUSE" not in sql
    # injection contained by sql_literal
    assert "WAREHOUSE_NAME = 'a''b'" in mart27_sql.compare_pattern_costs_by_warehouse(
        "2026-06-01", "2026-06-07", "2026-06-08", "2026-06-14", warehouse="a'b")


# ============================================= #8 SHOW GRANTS TO SHARE ======

def test_show_grants_to_share_is_identifier_quoted():
    sql = security_sql.show_grants_to_share_sql("MY_SHARE")
    assert sql == 'SHOW GRANTS TO SHARE "MY_SHARE"'               # double-quoted IDENTIFIER
    assert "'MY_SHARE'" not in sql                                # NOT a string literal
    assert "LIMIT" not in sql                                     # SHOW rejects LIMIT
    # injection: an embedded double-quote is doubled, closing the identifier safely
    assert security_sql.show_grants_to_share_sql('X" DROP SHARE Y --') == \
        'SHOW GRANTS TO SHARE "X"" DROP SHARE Y --"'


# ============================================= #21 lock-wait per-object =====

def test_lock_wait_object_detail_none_sentinel_and_columns():
    sql = ops_sql.lock_wait_object_detail("MYDB", "MYSCH", "MYTBL", days=2)
    assert "DATABASE_NAME = 'MYDB'" in sql and "OBJECT_NAME = 'MYTBL'" in sql
    assert "LOCK_WAIT_HISTORY" in sql
    # window cap and only-confirmed columns (unconfirmed ones deliberately skipped)
    assert "REQUESTED_AT >= DATEADD('day', -2," in sql
    for absent in ("BLOCKER_QUERIES", "TRANSACTION_ID", "RELEASED_AT"):
        assert absent not in sql, absent
    # 'NONE' sentinel from the mart maps to IS NULL, not = 'NONE'
    none_sql = ops_sql.lock_wait_object_detail("NONE", "NONE", "NONE", days=2)
    assert "DATABASE_NAME IS NULL" in none_sql and "SCHEMA_NAME IS NULL" in none_sql
    assert "= 'NONE'" not in none_sql
    # window hard-capped at 7d
    assert "DATEADD('day', -7," in ops_sql.lock_wait_object_detail("D", "S", "O", days=9999)


# ============================================= #22 ETL failed runs ==========

def test_etl_failed_runs_for_pipeline():
    sql = etl_sql.etl_failed_runs_for_pipeline("nightly_load", 30, "ALFA")
    assert "EXECUTION_STATUS <> 'SUCCESS'" in sql
    assert "'pipeline')::VARCHAR = 'nightly_load'" in sql         # case-preserved, exact
    assert "QUERY_ATTRIBUTION_HISTORY" in sql                     # cred CTE (waste reconciles)
    assert "'NIGHTLY_LOAD'" not in sql                            # not upper-cased


# ============================================= #23 untagged per user ========

def test_untagged_executions_for_user():
    sql = cost_sql.untagged_executions_for_user("SOME_USER", 30, "ALFA")
    assert "NULLIF(QUERY_TAG, '') IS NULL" in sql or "NULLIF(QUERY_TAG,'') IS NULL" in sql
    assert "USER_NAME = 'SOME_USER'" in sql
    assert "DATEADD('day', -30," in sql
    assert "DATEADD('day', -90," in cost_sql.untagged_executions_for_user("U", 9999, "ALL")  # 90d cap


# ============================================= #26 role holders/privileges ==

def test_role_holders_and_privileges():
    h = security_sql.role_holders("ANALYST")
    assert "GRANTS_TO_USERS" in h and "ROLE = 'ANALYST'" in h and "DELETED_ON IS NULL" in h
    p = security_sql.role_privileges("ANALYST")
    assert "GRANTS_TO_ROLES" in p and "GRANTEE_NAME = 'ANALYST'" in p and "DELETED_ON IS NULL" in p
    # injection contained (string-literal position -> sql_literal doubles the quote)
    assert "ROLE = 'A''; DROP--'" in security_sql.role_holders("A'; DROP--")


# ============================================= wiring locks ==================

def test_moderate_drills_wired():
    cmp_src = _src("app/ui/pages/cost_parts/compare.py")
    assert "compare_pattern_costs_by_warehouse(" in cmp_src and "cmp_wh_sel" in cmp_src
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert "table_storage_breakdown(company" in opt          # #24 reuse
    assert 'entity_type="OBJECT"' in opt and 'key_col="OBJECT_FQN"' in opt   # #25
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert "etl_failed_runs_for_pipeline(" in uc and "etl_pipe_sel" in uc
    cost = _src("app/ui/pages/cost.py")
    assert "untagged_executions_for_user(" in cost and "tagcov_sel" in cost
    sec = _src("app/ui/pages/security.py")
    assert "show_grants_to_share_sql(" in sec and "role_holders(" in sec and "role_privileges(" in sec
    cr = _src("app/ui/pages/control_room.py")
    assert "lock_wait_object_detail(" in cr and "cr_lockspike_sel" in cr


def test_scoped_drill_keys_include_db_schema():
    # the drill selection/run keys must include database + schema_contains, else a
    # sticky positional selection survives a filter change and drills the wrong entity.
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert "etl_pipe_sel_{company}_{days}_{f['database']}_{f['schema_contains']}" in uc
    cost = _src("app/ui/pages/cost.py")
    assert "tagcov_sel_{f['company']}_{f['days']}_{f['database']}_{f['schema_contains']}" in cost
