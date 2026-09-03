"""v4.150.0 wave-2: root failure-sort, per-event delivery, dup-guard, arrival note,
Cost Truth dollars, setup checklist. Behavioral tests where the layer is pure SQL/
logic; source-grep guards for the Streamlit UI pieces (house idiom)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import mart_sql, ops_sql

sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- rec34: root picker orders failures-first ------------------------------------
def test_task_graph_roots_orders_failures_first():
    sql = ops_sql.task_graph_roots()
    assert "RECENT_FAILURES" in sql
    assert "ORDER BY RECENT_FAILURES DESC" in sql
    # v4.154: failures come from the day-grain task mart, joined 1:0-or-1 so
    # NODE_COUNT is not inflated. rec34's live TASK_HISTORY scan was the only
    # ACCOUNT_USAGE read in this formerly metadata-only query and dominated the
    # ~21s first paint — it must not return.
    assert "MART_TASK_NODE_DAILY" in sql and "root_fails" in sql
    assert "TASK_HISTORY" not in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_task_graph_nodes_failures_come_from_the_mart_too():
    # The node query carried an identical live TASK_HISTORY CTE — same tax.
    sql = ops_sql.task_graph_nodes("root-id", 7)
    assert "MART_TASK_NODE_DAILY" in sql and "RECENT_FAILURES" in sql
    assert "TASK_HISTORY" not in sql
    sqlglot.parse(sql, dialect="snowflake")


# --- rec38: per-event delivery reader --------------------------------------------
def test_deliveries_for_event_joins_routes_and_scopes_event():
    sql = mart_sql.deliveries_for_event("evt-123")
    assert "ALERT_DELIVERIES" in sql and "ALERT_ROUTES" in sql
    assert "INTEGRATION_NAME" in sql and "SENT_AT" in sql
    assert "'evt-123'" in sql          # scoped to the one event, sql_literal-quoted
    sqlglot.parse(sql, dialect="snowflake")


# --- rec29: billed split reader emits the AI/OTHER partitions ---------------------
def test_billed_split_emits_ai_and_other_partitions():
    sql = mart_sql.billed_split(30)
    assert "CREDITS_BILLED_AI" in sql and "CREDITS_BILLED_OTHER" in sql
    assert "FACT_METERING_DAILY" in sql
    # AI predicate present so the split is real (Cortex/AI/intelligence).
    assert "CORTEX" in sql.upper()
    sqlglot.parse(sql, dialect="snowflake")


def test_cost_truth_kpis_are_dollars_with_ai_aware_billed():
    ds = _src("app/ui/decision_studio.py")
    ct = ds.split("def _cost_truth", 1)[1].split("\ndef ", 1)[0]
    # BILLED uses the blended (AI-aware) dollarization, not a flat rate; the three
    # compute-clean bases use credits_to_usd.
    assert "blended_billed_usd(" in ct
    assert "mart_sql.billed_split(" in ct
    assert 'format_usd(credits_to_usd(metered, rate))' in ct
    # dollars are primary (value), credits are the secondary delta.
    assert '"value": format_usd(billed_usd)' in ct
    assert '"delta": f"{billed:,.0f} cr"' in ct


# --- rec19: duplicate work-item guard at both create sites ------------------------
def test_dup_work_item_guard_at_both_create_sites():
    wb = _src("app/ui/workbench.py")
    assert "related_actions(entity_type, entity_key)" in wb
    assert "already track" in wb
    sec = _src("app/ui/security_center.py")
    assert "workbench_sql" in sec and "related_actions(" in sec
    assert "already track this entity" in sec


# --- rec24: arrival note plumbed and rendered once --------------------------------
def test_filter_arrival_note_plumbed_and_shown_once():
    alerts = _src("app/ui/pages/alerts.py")
    inv = alerts.split('key="alert_investigate"', 1)[1][:1000]   # round-24 comment widened the block
    assert "filter_note" in inv
    assert 'if target["filters"]:' in inv          # note only when filters actually apply
    comp = _src("app/ui/components.py")
    ph = comp.split("def page_header(", 1)[1].split("\ndef ", 1)[0]
    assert 'filter_note' in ph
    # shown once: the note key is dropped, other drill identity (event_id) preserved.
    assert 'k != "filter_note"' in ph


# --- rec44: setup-progress panel registered and reads install state ---------------
def test_setup_progress_panel_registered():
    adm = _src("app/ui/pages/admin.py")
    assert '"Setup progress"' in adm
    assert "def _setup_progress_tab" in adm
    assert 'elif section == "Setup progress":' in adm
    tab = adm.split("def _setup_progress_tab", 1)[1].split("\ndef ", 1)[0]
    # reuses existing probes: migration floor, marts loading, config — applies nothing
    assert "schema_version()" in tab and "_EXPECTED_MIGRATIONS" in tab
    assert "source_freshness_state()" in tab
    # Single migration rollup ("N of M applied"); per-version applied/missing detail
    # lives on the Migrations & freshness tab (audit consolidation removed the duplicated
    # per-VNNN enumeration from Setup progress).
    assert "applied" in tab and "Migrations & freshness tab" in tab
    assert "execute_statement" not in tab           # read-only panel


# --- new SQL builders carry canaries (house rule 4) -------------------------------
def test_new_builders_have_canaries():
    canary = _src("app/data/canary.py")
    assert "deliveries_for_event" in canary
    assert "billed_split" in canary
