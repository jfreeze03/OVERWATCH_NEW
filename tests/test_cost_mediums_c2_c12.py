"""Locks for the cost-review MEDIUM findings C2-C12
(docs/reviews/COST_ACCOUNTING_REVIEW_2026-07-29.md). App-side fixes only; the
mart twins (C2 V059/V010, C5/C6 AI arms) are queued for V061.
"""
from pathlib import Path

from app.data import cost_sql, graph_sql, insights_sql, mart_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]


# --- C2: proc/CALL/pipeline attribution carries Query Acceleration credits ----

def test_c2_proc_pipeline_attribution_includes_qas():
    for sql in (insights_sql.procedure_costs_usd(30),
                insights_sql.call_cost_lookup("SOME_PROC"),
                insights_sql.call_children_costs("01a2-b3c4"),
                insights_sql.proc_cost_trend("DB.SCH.P", 30),
                graph_sql.graph_daily_costs(7)):
        assert "CREDITS_ATTRIBUTED_COMPUTE" in sql
        assert "CREDITS_USED_QUERY_ACCELERATION" in sql, sql[:120]


def test_c2_every_attribution_credit_reference_carries_qas():
    """No QUERY_ATTRIBUTION_HISTORY credit SUM/select may reference compute alone."""
    for rel in ("app/data/insights_sql.py", "app/data/graph_sql.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for chunk in text.split("CREDITS_ATTRIBUTED_COMPUTE")[1:]:
            # within a short window after each compute mention, the QAS term appears
            assert "CREDITS_USED_QUERY_ACCELERATION" in chunk[:110], (rel, chunk[:110])


# --- C3: per-DB storage guards include fail-safe bytes ------------------------

def test_c3_storage_having_counts_failsafe():
    builders = [
        cost_sql.storage_by_database(30),
        cost_sql.storage_by_database_live(30),
        cost_sql.storage_by_database_calendar(),
        cost_sql.storage_by_database_calendar_live(),
    ]
    for sql in builders:
        having = sql.upper().split("HAVING", 1)[1]
        assert "FAILSAFE_BYTES" in having, sql


# --- C4: storage MTD coverage guard ------------------------------------------

def test_c4_storage_mtd_has_coverage_guard():
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "fact_stale" in sp                         # partial-load detection
    assert "LATEST_DAY" in sp                         # coverage read
    assert "Coverage:" in sp or "covers" in sp        # coverage surfaced


# --- C7: contract consumed coverage, r14 #8-safe ------------------------------

def test_c7_live_consumed_uses_unfiltered_retention_floor():
    sql = cost_sql.contract_consumed_credits("2026-01-01")
    # windowed sum via IFF, and SOURCE_FIRST_DAY is the UNFILTERED earliest day
    assert "SUM(IFF(USAGE_DATE >= DATE '2026-01-01'" in sql
    assert "MIN(USAGE_DATE) AS SOURCE_FIRST_DAY" in sql
    assert "WHERE" not in sql.upper().split("FROM", 1)[1]   # no contract-filter WHERE (r14 #8)


def test_c7_contract_page_flags_a_coverage_floor():
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "coverage_from" in ct
    assert "SOURCE_FIRST_DAY" in ct
    assert 'get("FIRST_DAY")' not in ct                # never the contract-filtered MIN


# --- C8: contract burn discloses storage/transfer draw ------------------------

def test_c8_contract_burn_credits_only_disclosed():
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "Credits burn only" in ct
    assert "data-transfer" in ct or "storage and transfer" in ct


# --- C9: chargeback discloses compute-only scope ------------------------------

def test_c9_chargeback_manifest_states_scope():
    cb = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")
    assert "Scope: warehouse compute only" in cb
    assert "not tie out" in cb or "NOT allocated" in cb


# --- C10: ops query scope is warehouse-primary, not warehouse AND user --------

def test_c10_ops_query_scope_is_warehouse_primary():
    op = (_ROOT / "app" / "data" / "ops_sql.py").read_text(encoding="utf-8")
    scope = op.split("def _query_scope", 1)[1].split("\ndef ", 1)[0]
    assert "company_case_sql" in scope                 # COMPANY_FOR_WAREHOUSE stamp
    assert "user_clause" not in scope                  # the AND-user intersection is gone


# --- C11: DDL change evidence scoped by actor OR object -----------------------

def test_c11_ddl_scoped_actor_or_object():
    sql = security_sql.recent_ddl_changes(7, "ALFA")
    body = (_ROOT / "app" / "data" / "security_sql.py").read_text(encoding="utf-8")
    fn = body.split("def recent_ddl_changes", 1)[1].split("\ndef ", 1)[0]
    assert "_actor_or_object" in fn
    assert " OR " in sql                               # union of user + database lenses


# --- C12: incident timeline labels company via the loader UDF -----------------

def test_c12_incident_timeline_uses_company_udf():
    sql = mart_sql.incident_timeline(7, "Trexis")
    assert "COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME" in sql
    assert "IFF(DATABASE_NAME LIKE 'TRXS%'" not in sql   # the pre-V044 mislabel is gone
