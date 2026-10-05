"""House law 4 ("every SQL builder gets a canary"), enforced for the cost / mart builder modules (v4.608).

R2-062 / R2-064 / R2-068: the ORGANIZATION_USAGE readers (the All-in invoice tile, the org balance behind every
runway, the rate-card reconciliation), ALERT_EVENTS' SNOOZE_* reader, the MART_TABLE_STORAGE_DAILY byte readers,
the email-path ALERT_HISTORY read and MART_TASK_NODE_DAILY's timing reader had no canary. Several are probe=True
reads, which log neither an absent object nor a missing column, so a renamed column failed silently and Admin >
Canary still said "All N applicable canary statements passed".

The ratchet below fails on any public builder in cost_sql / mart_sql / mart27_sql that has neither a canary
nor a named exemption with its reason, and on an exemption that is no longer needed (the list only shrinks).

A twin exemption ("reads only columns a registered canary already compiles") is checked, not trusted: each twin
is rendered, the (table, column) pairs it compiles are read off the parsed SQL (correlated outer references
included), and they must be a subset of the pairs the registered canaries compile (the R2-069 ACCESS_HISTORY
lock's method, for every table); a twin may read no column the reader cannot attribute to a table. Two false
twin claims shipped before this check (holistic review #17 / #18): ai_code_user_daily (FACT_AI_USAGE_DAILY's
V042 EMAIL / FIRST_TS / LAST_TS) and resolutions_for_rule (ALERT_AUDIT.EVENT_ID / NOTE). Both are canaried now.

RUNBOOK names every probe=True reader that still has no canary; the list is derived from app/ here (#7).
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

from app.data import (
    canary,
    change_impact_sql,
    cortex_sql,
    cost_sql,
    insights_sql,
    mart27_sql,
    mart_sql,
    security_sql,
    workbench_sql,
)

_ROOT = Path(__file__).resolve().parents[1]

_TWIN = "reads only columns a registered canary already compiles (R2-062/R2-064 review, 2026-09-30)"
# Names should only LEAVE this map (when they gain a canary). A new builder needs a canary, or a reason here a
# reviewer accepts; a _TWIN reason is checked mechanically (test_twins_read_only_columns_a_canary_compiles).
CANARY_EXEMPT: dict[str, dict[str, str]] = {
    "access_sql": {
        "show_grants_of_role_sql": ("SHOW GRANTS OF ROLE (the v4.610.0 admin-access lookup): EXPLAIN cannot "
                                    "compile a SHOW; a failure is logged and shown in the sidebar instead"),
    },
    "cost_sql": {
        "hourly_credits": _TWIN,
        "contract_consumed_credits": _TWIN,
        "untagged_executions_for_user": _TWIN,
        "marketplace_paid_usage": "deliberately lazy org view, locked out by test_v4134_recommendations",
        "qas_roi": "edition-sensitive QUERY_ACCELERATION views; documented exemption (4.178.0)",
        "qas_eligible_queries": "edition-sensitive QUERY_ACCELERATION views; documented exemption (4.178.0)",
        "native_anomaly_insights": "SELECT * on the optional SNOWFLAKE.LOCAL feed: a canary can only see absence",
    },
    "mart_sql": {
        "app_statement_stats_telemetry": _TWIN,
        "open_alert_severity_counts": _TWIN,
        "since_last_visit": _TWIN,
        "verified_wins": _TWIN,
        "app_self_cost_usd": _TWIN,
        "app_warehouse_queue_by_hour": _TWIN,
        "health_strip": _TWIN,
        "ledger_for_event": _TWIN,
        "events_for_rule": _TWIN,
        "last_delivery_health": _TWIN,
        "action_acceptance": _TWIN,
        "incident_gantt": _TWIN,
        "supersede_ledger_twins_sql": "an UPDATE (a write), not a read",
        "email_alert_objects": "SHOW ALERTS: EXPLAIN cannot compile a SHOW",
        "flyway_history": "deliberately not canaried (docstring + test_prep_iac)",
        "email_notification_history": ("NOTIFICATION_HISTORY(INTEGRATION_NAME => 'OVERWATCH_EMAIL') on an install "
                                       "without the opt-in integration is unproven (owner probe first)"),
        "ml_forecast_daily": "reads the opt-in FORECAST_ML_DAILY (snowflake/ml_forecast_option.sql) only",
    },
    "mart27_sql": {
        "ai_code_user_rollup": ("live: the Cost AI-users fact fallback (cost_parts/ai_chargeback.py). The v4.36.1 "
                                "lock in test_v041_loader_pass keeps it out of canary.py, and it "
                                f"{_TWIN}"),
        "ai_code_daily": _TWIN,
        "compare_pattern_costs_by_warehouse": _TWIN,
    },
}


def _registered_in(source: str) -> frozenset[str]:
    """``module.builder`` for every builder a CANARIES entry calls, read off the tuple itself: a mention in a
    comment (canary.py explains several deliberate non-registrations) does not count as a canary."""
    for node in ast.parse(source).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "CANARIES" and node.value is not None:
            return frozenset(f"{n.value.id}.{n.attr}" for n in ast.walk(node.value)
                             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                             and n.value.id.endswith("_sql"))
    raise AssertionError("no CANARIES tuple in the source")


@functools.cache
def _registered() -> frozenset[str]:
    return _registered_in((_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8"))


def _public_builders(module: str) -> list[str]:
    tree = ast.parse((_ROOT / "app" / "data" / f"{module}.py").read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]


@pytest.mark.parametrize("module", sorted(CANARY_EXEMPT))
def test_every_public_builder_has_a_canary_or_a_named_exemption(module):
    builders = _public_builders(module)
    uncanaried = {b for b in builders if f"{module}.{b}" not in _registered()}
    exempt = CANARY_EXEMPT[module]
    assert all(reason.strip() for reason in exempt.values())
    assert not (uncanaried - set(exempt)), f"{module}: register a canary (or a reasoned exemption) for " \
                                           f"{sorted(uncanaried - set(exempt))}"
    assert not (set(exempt) - uncanaried), f"{module}: drop the now-canaried or removed names " \
                                           f"{sorted(set(exempt) - uncanaried)} from CANARY_EXEMPT"


def test_registration_is_read_from_the_canaries_tuple_not_from_comments():
    src = """
CANARIES: tuple = (
    # mart_sql.only_in_a_comment is deliberately not registered
    ("mart.a", lambda: mart_sql.a(1)),
    ("mart.b", mart_sql.b),
)
OTHER = (cost_sql.c,)
"""
    assert _registered_in(src) == {"mart_sql.a", "mart_sql.b"}
    assert "mart_sql.snoozed_alert_events" in _registered()


_ORG = ("cost.org_all_in_window_usd", "cost.org_usage_in_currency", "cost.org_remaining_balance",
        "cost.org_contract_items", "cost.org_account_month_usd", "cost.org_rate_sheet")
_CORE = {
    "mart.snoozed_alert_events": lambda: mart_sql.snoozed_alert_events(1, "ALFA"),
    "mart.table_storage_waste_mart": lambda: mart_sql.table_storage_waste_mart("ALFA"),
    "mart.table_storage_breakdown_mart": lambda: mart_sql.table_storage_breakdown_mart("ALFA"),
    "mart.email_alert_history": lambda: mart_sql.email_alert_history(1),
    "mart27.task_nodes": lambda: mart27_sql.task_nodes(2, "ALFA"),
    "mart.cs_by_query_type_mart": lambda: mart_sql.cs_by_query_type_mart(1, "ALFA"),
    # holistic #17: FACT_AI_USAGE_DAILY's V042 EMAIL / FIRST_TS / LAST_TS (Security AI guardrails, fact-first)
    "mart27.ai_code_user_daily": lambda: mart27_sql.ai_code_user_daily("ALFA"),
    # holistic #18: ALERT_AUDIT.EVENT_ID / NOTE (the Alerts drawer's 'How this was resolved before')
    "mart.resolutions_for_rule": lambda: mart_sql.resolutions_for_rule("CANARY_PROBE", 1),
    # holistic #7: FACT_SECURITY_CHANGE.CHANGE_KIND, a probe=True read (the change-risk noise diagnostic)
    "security.change_risk_destructive_breakdown": lambda: security_sql.change_risk_destructive_breakdown(1),
}


def test_org_usage_readers_are_declared_gap_canaries():
    """R2-062: registered, reading ORGANIZATION_USAGE, and declared gaps (a missing org-viewer grant is an
    account-feature state); every one parses."""
    reg = dict(canary.CANARIES)
    for name in _ORG:
        assert name in reg, name
        sql = reg[name]()
        assert "SNOWFLAKE.ORGANIZATION_USAGE." in sql, name
        assert name in canary.EXPECTED_GAPS, name
        sqlglot.parse_one(sql, read="snowflake")
    assert reg["cost.org_all_in_window_usd"]() == cost_sql.org_all_in_window_usd(2)
    assert reg["cost.org_remaining_balance"]() == cost_sql.org_remaining_balance(2)


@pytest.mark.parametrize("name", sorted(_CORE))
def test_core_readers_are_canaried_and_fail_on_absence(name):
    """R2-064 / R2-068: core OVERWATCH objects (or an always-present INFORMATION_SCHEMA table function), so an
    absence must FAIL -- never a declared gap."""
    reg = dict(canary.CANARIES)
    assert name in reg
    assert reg[name]() == _CORE[name]()
    assert name not in canary.EXPECTED_GAPS
    sqlglot.parse_one(reg[name](), read="snowflake")


def test_the_new_canaries_compile_the_columns_that_had_none():
    sqls = "\n".join(b() for n, b in canary.CANARIES)
    for col in ("SNOOZED_UNTIL", "SNOOZE_REASON", "FAILSAFE_BYTES", "RETAINED_FOR_CLONE_BYTES", "LAST_DML",
                "ALERT_HISTORY(", "P95_QUEUE_SEC", "FIRST_START", "LAST_COMPLETED", "REMAINING_BALANCE_DAILY",
                "USAGE_IN_CURRENCY_DAILY", "RATE_SHEET_DAILY", "CONTRACT_ITEMS"):
        assert col in sqls, col
    compiled = _canary_columns()
    for pair in (("OVERWATCH.FACT_AI_USAGE_DAILY", "EMAIL"), ("OVERWATCH.FACT_AI_USAGE_DAILY", "FIRST_TS"),
                 ("OVERWATCH.FACT_AI_USAGE_DAILY", "LAST_TS"), ("OVERWATCH.ALERT_AUDIT", "EVENT_ID"),
                 ("OVERWATCH.ALERT_AUDIT", "NOTE"), ("OVERWATCH.FACT_SECURITY_CHANGE", "CHANGE_KIND")):
        assert pair in compiled, pair


# ============================================== the twin claim, checked: (table, column) read off the SQL ====
# A column counts toward the physical table it compiles against: through its alias, as the one source that has
# (or may have) the name, or traced through a CTE / derived table (a plain pass-through projection, a star, or
# each UNION branch at the same position). A correlated reference (NOT EXISTS (... WHERE d.X = e.X)) resolves
# outward through subquery / set-operation / lateral scopes, never across a CTE or derived-table boundary. A
# derived projection (MIN(DAY) AS FIRST_DAY) and a FLATTEN output (f.VALUE) add nothing beyond their own inputs,
# and an unqualified name that is a select alias of its scope is the alias, not a column, only when no source can
# hold that name (GROUP BY SEV over CTEs whose select lists have no SEV). A JOIN ... USING (K) key counts on both
# sides (recheck #10).
# A column the reader cannot attribute (an unqualified name beside several sources that may hold it, a select
# alias beside a source that may hold the name -- any physical table, whose columns the reader cannot see -- a
# USING key with no single side, a NATURAL JOIN's keys, a table function's output) is reported, never counted:
# the canary side stays a lower bound, so a twin never passes on a guess about what a canary compiles, and a twin
# must have none (_UNATTRIBUTED_ADVICE says how to clear each kind).

#: How a twin clears an unattributed column. Beside a physical table every name may be a column (Snowflake binds a
#: same-named column before a select alias), so only the expression or its ordinal clears an alias reference.
_UNATTRIBUTED_ADVICE = ("qualify each column, house law 8; join ON qualified keys, not USING / NATURAL; GROUP BY / "
                        "filter on the expression or its ordinal, not a select alias, beside a physical table "
                        "(Snowflake binds a same-named column first)")

_OPAQUE = frozenset({ScopeType.ROOT, ScopeType.CTE, ScopeType.DERIVED_TABLE})
_FLATTEN_COLUMNS = frozenset({"SEQ", "KEY", "PATH", "INDEX", "VALUE", "THIS"})    # fixed by Snowflake


def _table_key(table: exp.Table) -> str:
    return f"{table.db.upper() or 'OVERWATCH'}.{table.name.upper()}"   # an unqualified name is the app schema


def _branches(scope: Scope) -> list[Scope]:
    # sqlglot renamed Scope.union_scopes to set_operation_scopes (CI's floor leg installs the newer one)
    subs = getattr(scope, "set_operation_scopes", None) or getattr(scope, "union_scopes", None) or []
    return [leaf for s in subs for leaf in _branches(s)] if subs else [scope]


def _projected(scope: Scope, name: str, depth: int) -> set[tuple[str, str]] | None:
    branches = _branches(scope)
    if not all(isinstance(b.expression, exp.Select) for b in branches):
        return None
    names = [p.alias_or_name.upper() for p in branches[0].expression.selects]
    if name in names:
        idx, out = names.index(name), set()
        for branch in branches:
            selects = branch.expression.selects
            if idx >= len(selects):
                return None
            inner = selects[idx].this if isinstance(selects[idx], exp.Alias) else selects[idx]
            if isinstance(inner, exp.Column) and not isinstance(inner.this, exp.Star):
                out |= _resolve(branch, inner, depth + 1) or set()
        return out
    if len(branches) == 1:
        for proj in branches[0].expression.selects:
            if isinstance(proj, exp.Star):
                return _resolve(branches[0], exp.column(name), depth + 1)
            if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                return _resolve(branches[0], exp.column(name, table=proj.table), depth + 1)
    return None


def _is_flatten(source: object) -> bool:
    return isinstance(source, Scope) and source.is_udtf and isinstance(source.expression.this, exp.Explode)


def _projects(source: object, name: str) -> bool | None:
    """Whether a FROM source exposes ``name``: from its select list (or FLATTEN's fixed columns), None when that
    cannot be known (a physical table, a star, another table function)."""
    if _is_flatten(source):
        return name in _FLATTEN_COLUMNS
    if not isinstance(source, Scope):
        return None
    branches = _branches(source)
    if not all(isinstance(b.expression, exp.Select) for b in branches):
        return None
    selects = branches[0].expression.selects
    if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in selects):
        return None
    return name in {p.alias_or_name.upper() for p in selects}


def _resolve(scope: Scope, col: exp.Column, depth: int = 0) -> set[tuple[str, str]] | None:
    """The physical (table, column) pairs ``col`` compiles against: set() for a value no table holds (a derived
    projection, a FLATTEN output), None when the reader cannot attribute it."""
    if depth > 16:
        return None
    qualifier, name = col.table.upper(), col.name.upper()
    while True:
        sources = {alias.upper(): source for alias, (_node, source) in scope.selected_sources.items()}
        if qualifier:
            source = sources.get(qualifier)
        else:                                  # the one source that has (or may have) the name
            maybe = [s for s in sources.values() if _projects(s, name) is not False]
            if len(maybe) > 1:
                return None
            source = maybe[0] if maybe else None
        if source is not None:
            break
        # a correlated reference: resolve outward through subquery / set-operation / lateral scopes, never across
        # a CTE or derived-table boundary (neither can see the enclosing query's FROM)
        if scope.parent is None or scope.scope_type in _OPAQUE:
            return None
        scope = scope.parent
    if isinstance(source, exp.Table):
        return {(_table_key(source), name)} if source.name and not isinstance(source.this, exp.Func) else None
    if _is_flatten(source):
        return set() if name in _FLATTEN_COLUMNS else None
    return _projected(source, name, depth) if isinstance(source, Scope) else None


def _is_alias_reference(scope: Scope, col: exp.Column) -> bool:
    select = scope.expression
    if col.table or not isinstance(select, exp.Select):
        return False
    for proj in select.selects:
        if proj.alias_or_name.upper() != col.name.upper():
            continue
        if isinstance(proj, exp.Column):
            return False                       # SELECT X ... GROUP BY X: the column itself
        node = col
        while node is not None and node is not select:
            if node is proj:
                return False                   # SUM(X) AS X: the X inside is the source column
            node = node.parent
        return True
    return False


def _may_hold(scope: Scope, name: str) -> list[str]:
    """The aliases of ``scope``'s FROM sources that have (or may have) the column ``name``."""
    return [alias for alias, (_node, source) in scope.selected_sources.items() if _projects(source, name) is not False]


def _join_keys(scope: Scope) -> tuple[set[tuple[str, str]], set[str]]:
    """recheck #10: JOIN ... USING (K) compiles K on both sides, but sqlglot keeps K as an identifier, not a column,
    so ``scope.columns`` never holds it. K counts toward the join's own source and the one earlier FROM source that
    has (or may have) it; a side the reader cannot attribute is reported, and so is a NATURAL JOIN (its keys are
    whatever both sides share, which the SQL does not say)."""
    select = scope.expression
    out: set[tuple[str, str]] = set()
    unattributed: set[str] = set()
    if not isinstance(select, exp.Select):
        return out, unattributed
    sources = {alias.upper(): source for alias, (_node, source) in scope.selected_sources.items()}
    from_ = select.args.get("from_") or select.args.get("from")
    earlier = [from_.this.alias_or_name] if isinstance(from_, exp.From) else []
    for join in select.args.get("joins") or []:
        right = join.this.alias_or_name
        if str(join.args.get("method") or "").upper() == "NATURAL":
            unattributed.add(f"NATURAL JOIN {right}")
        for key in join.args.get("using") or []:
            name = key.name.upper()
            left = [a for a in earlier if _projects(sources.get(a.upper()), name) is not False]
            for side, where in ((right, "on"), (left[0] if len(left) == 1 else None, "before")):
                pairs = _resolve(scope, exp.column(name, table=side)) if side is not None else None
                if pairs is None:
                    unattributed.add(f"USING ({key.name}) {where} {right}")
                else:
                    out |= pairs
        earlier.append(right)
    return out, unattributed


def _read_columns(sql: str) -> tuple[set[tuple[str, str]], set[str]]:
    """(the (table, column) pairs ``sql`` compiles, the column references the reader cannot attribute)."""
    out: set[tuple[str, str]] = set()
    unattributed: set[str] = set()
    for scope in traverse_scope(sqlglot.parse_one(sql, read="snowflake")):
        keys, unkeyed = _join_keys(scope)
        out |= keys
        unattributed |= unkeyed
        for col in scope.columns:
            if isinstance(col.this, exp.Star) or col.find_ancestor(exp.Select) is not scope.expression:
                continue
            if _is_alias_reference(scope, col):
                # recheck #10: the alias only when no source can hold the name; Snowflake may bind a same-named
                # source column first, which a reader that cannot see the table's columns cannot rule out
                if _may_hold(scope, col.name.upper()):
                    unattributed.add(col.sql(dialect="snowflake"))
                continue
            pairs = _resolve(scope, col)
            if pairs is None:
                unattributed.add(col.sql(dialect="snowflake"))
            else:
                out |= pairs
    return out, unattributed


def _table_columns(sql: str) -> set[tuple[str, str]]:
    return _read_columns(sql)[0]


@functools.cache
def _canary_columns() -> frozenset[tuple[str, str]]:
    return frozenset().union(*(_table_columns(build()) for _name, build in canary.CANARIES))


def test_the_table_column_reader_is_derived_from_the_sql():
    """The ratchet itself: columns are attributed through aliases, CTE pass-through, a star and UNION branches;
    a derived projection, a select-alias reference and another table's same-named alias add nothing. GROUP BY
    USAGE_DATE beside the physical table may bind a USAGE_DATE column there, so it is reported, not counted."""
    sql = """
WITH cov AS (SELECT MIN(DAY) AS FIRST_DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY),
w AS (SELECT * FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS),
u AS (SELECT USER_ID, 'a' AS SRC FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY
      UNION ALL SELECT USER_ID, 'b' FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY),
d AS (SELECT USER_NAME, ANY_VALUE(EMAIL) AS EMAIL, DAY AS USAGE_DATE
      FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
      WHERE (SELECT FIRST_DAY FROM cov) <= CURRENT_DATE()
      GROUP BY USER_NAME, USAGE_DATE)
SELECT d.EMAIL, w.RESOLVED_AT, u.USER_ID, a.NOTE
FROM d JOIN w ON TRUE JOIN u ON TRUE
LEFT JOIN ALERT_AUDIT a ON a.EVENT_ID = w.INCIDENT_ID
UNION ALL
SELECT a.CREDITS, NULL, NULL, NULL FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY a
"""
    assert _table_columns(sql) == {
        ("OVERWATCH.FACT_AI_USAGE_DAILY", "DAY"), ("OVERWATCH.FACT_AI_USAGE_DAILY", "USER_NAME"),
        ("OVERWATCH.FACT_AI_USAGE_DAILY", "EMAIL"),
        ("OVERWATCH.INCIDENTS", "RESOLVED_AT"), ("OVERWATCH.INCIDENTS", "INCIDENT_ID"),
        ("ACCOUNT_USAGE.CORTEX_CODE_CLI_USAGE_HISTORY", "USER_ID"),
        ("ACCOUNT_USAGE.CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "USER_ID"),
        ("OVERWATCH.ALERT_AUDIT", "NOTE"), ("OVERWATCH.ALERT_AUDIT", "EVENT_ID"),
        ("ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY", "CREDITS"),
    }
    assert _read_columns(sql)[1] == {"USAGE_DATE"}


#: Every twin, rendered with representative arguments (every optional filter and window shape it has). The
#: CANARY_EXEMPT twins, plus the probe=True readers outside the ratchet's modules that RUNBOOK calls twins.
_TWINS: dict[str, Callable[[], tuple[str, ...]]] = {
    "cost_sql.hourly_credits": lambda: (cost_sql.hourly_credits(24, "ALFA"),),
    "cost_sql.contract_consumed_credits": lambda: (cost_sql.contract_consumed_credits("2026-01-01"),
                                                   cost_sql.contract_consumed_credits("2026-01-01", "2026-12-31")),
    "cost_sql.untagged_executions_for_user": lambda: (
        cost_sql.untagged_executions_for_user("U", 7, "ALFA", "DB", "S"),
        cost_sql.untagged_executions_for_user("U", 7, "ALFA", bounds=(date(2026, 9, 1), date(2026, 10, 1)))),
    "mart_sql.app_statement_stats_telemetry": lambda: (mart_sql.app_statement_stats_telemetry(7),),
    "mart_sql.open_alert_severity_counts": lambda: (mart_sql.open_alert_severity_counts("ALFA"),),
    "mart_sql.since_last_visit": lambda: (mart_sql.since_last_visit("ALFA"),),
    "mart_sql.verified_wins": lambda: (mart_sql.verified_wins("ALFA"),),
    "mart_sql.app_self_cost_usd": lambda: (mart_sql.app_self_cost_usd(30),),
    "mart_sql.app_warehouse_queue_by_hour": lambda: (mart_sql.app_warehouse_queue_by_hour(14),),
    "mart_sql.health_strip": lambda: (mart_sql.health_strip(),),
    "mart_sql.ledger_for_event": lambda: (mart_sql.ledger_for_event("abcd1234"),),
    "mart_sql.events_for_rule": lambda: (mart_sql.events_for_rule("CANARY_PROBE"),),
    "mart_sql.last_delivery_health": lambda: (mart_sql.last_delivery_health(),),
    "mart_sql.action_acceptance": lambda: (mart_sql.action_acceptance(90),),
    "mart_sql.incident_gantt": lambda: (mart_sql.incident_gantt(14, "ALFA"),),
    "mart27_sql.ai_code_user_rollup": lambda: (
        mart27_sql.ai_code_user_rollup(30, "ALFA"),
        mart27_sql.ai_code_user_rollup(30, "ALFA", bounds=(date(2026, 9, 1), date(2026, 10, 1)))),
    "mart27_sql.ai_code_daily": lambda: (
        mart27_sql.ai_code_daily(7, "ALFA"),
        mart27_sql.ai_code_daily(7, "ALFA", bounds=(date(2026, 9, 1), date(2026, 10, 1)))),
    "mart27_sql.compare_pattern_costs_by_warehouse": lambda: (mart27_sql.compare_pattern_costs_by_warehouse(
        "2026-09-08", "2026-09-15", "2026-09-01", "2026-09-08", "WH_X"),),
    # probe=True readers RUNBOOK names as twins (no ratchet covers their modules)
    "cortex_sql.cortex_code_user_daily": lambda: (cortex_sql.cortex_code_user_daily("ALFA"),),
    "change_impact_sql.proc_redeploys": lambda: (change_impact_sql.proc_redeploys(30),),
    "workbench_sql.product_mapping_totals": lambda: (
        workbench_sql.product_mapping_totals(30, "ALFA"),
        workbench_sql.product_mapping_totals(30, "ALFA", bounds=(date(2026, 9, 1), date(2026, 10, 1)))),
}
_PROBE_TWINS = {"cortex_sql.cortex_code_user_daily", "change_impact_sql.proc_redeploys",
                "workbench_sql.product_mapping_totals"}


def test_the_twin_map_is_every_twin_claim():
    claimed = {f"{module}.{name}" for module, exempt in CANARY_EXEMPT.items()
               for name, reason in exempt.items() if _TWIN in reason}
    assert set(_TWINS) - _PROBE_TWINS == claimed, (
        f"render new twins in _TWINS: {sorted(claimed - set(_TWINS))}; "
        f"drop gone ones: {sorted(set(_TWINS) - _PROBE_TWINS - claimed)}")
    assert not (_PROBE_TWINS & _registered())


@pytest.mark.parametrize("name", sorted(_TWINS))
def test_twins_read_only_columns_a_canary_compiles(name):
    """holistic #17 / #18: a twin claim that is false leaves a column no canary compiles, so drift there never
    reaches Admin > Canary. Every twin's (table, column) set must be inside what the canaries compile, and a
    twin may read nothing the reader cannot attribute (an unchecked column would make the claim a guess)."""
    reads: set[tuple[str, str]] = set()
    for sql in _TWINS[name]():
        cols, unattributed = _read_columns(sql)
        assert cols, f"{name} rendered SQL that reads no attributable column"
        assert not unattributed, (f"{name}: the twin check cannot attribute {sorted(unattributed)} to a table "
                                  f"({_UNATTRIBUTED_ADVICE}), so it cannot vouch for them")
        reads |= cols
    missing = sorted(reads - _canary_columns())
    assert not missing, f"{name} is not a twin: no registered canary compiles {missing}"


def test_the_unattributed_advice_clears_what_it_reports():
    """Recheck of 6e7d684b: the advice said 'rename a select alias a source may also hold', but a physical table
    may hold any name, so beside one no rename clears the failure. Grouping on the expression or its ordinal
    does, and the advice says so."""
    shape = "SELECT UPPER(e.SEVERITY) AS {a}, COUNT(*) AS N FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e GROUP BY {g}"
    for alias in ("SEV", "NO_TABLE_HAS_A_COLUMN_NAMED_THIS"):
        assert _read_columns(shape.format(a=alias, g=alias))[1] == {alias}       # a rename does not clear it
    for group in ("1", "UPPER(e.SEVERITY)"):
        assert _read_columns(shape.format(a="SEV", g=group)) == ({("OVERWATCH.ALERT_EVENTS", "SEVERITY")}, set())
    assert "rename" not in _UNATTRIBUTED_ADVICE
    assert "ordinal" in _UNATTRIBUTED_ADVICE and "physical table" in _UNATTRIBUTED_ADVICE


def test_a_correlated_outer_reference_counts_toward_the_outer_table():
    """Review of 91e3d3ac: a qualified correlated reference (NOT EXISTS (... WHERE d.EVENT_ID = e.EVENT_ID)) was
    dropped, so a twin could read an outer column no canary compiles and still pass. It resolves outward through
    subquery and set-operation scopes, never across a CTE or derived-table boundary."""
    sql = """
WITH c AS (SELECT x.RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG x
           WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r WHERE r.FAMILY = e.FAMILY))
SELECT e.EVENT_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d
                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = e.SEVERITY
                  UNION ALL
                  SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a WHERE a.NOTE = e.RULE_ID)
"""
    cols, unattributed = _read_columns(sql)
    assert {("OVERWATCH.ALERT_EVENTS", "SEVERITY"), ("OVERWATCH.ALERT_EVENTS", "RULE_ID"),
            ("OVERWATCH.ALERT_DELIVERIES", "ROUTE_ID"), ("OVERWATCH.ALERT_AUDIT", "NOTE")} <= cols
    assert unattributed == {"e.FAMILY"}          # the CTE body cannot see the outer query's alias


def test_a_star_over_several_ctes_and_a_flatten_are_attributed():
    """A star over several CTEs resolves each name to the one CTE that projects it, a FLATTEN's fixed output
    columns are no table's, and an unqualified name beside a physical table stays unattributed."""
    sql = """
WITH a AS (SELECT COUNT(*) AS N FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS),
b AS (SELECT MAX(SOURCE_NAME) AS W, ANY_VALUE(LAST_LOAD_TS) AS LAST_LOAD_TS
      FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE),
s AS (SELECT * FROM a, b)
SELECT s.N, s.W, s.LAST_LOAD_TS, f.value, f.INDEX, UNKNOWN_COL
FROM s, LATERAL FLATTEN(input => ARRAY_CONSTRUCT(1)) f, DBA_MAINT_DB.OVERWATCH.INCIDENTS i
"""
    cols, unattributed = _read_columns(sql)
    assert cols == {("OVERWATCH.SOURCE_FRESHNESS_STATE", "SOURCE_NAME"),
                    ("OVERWATCH.SOURCE_FRESHNESS_STATE", "LAST_LOAD_TS")}
    assert unattributed == {"UNKNOWN_COL"}


def test_a_join_using_key_counts_on_both_sides_or_is_reported():
    """recheck #10 (v4.608): sqlglot keeps a USING key as an identifier, not a column, so the reader never saw it:
    a twin's JOIN ... USING (K) compiled K on two tables with no canary behind it and still passed. K counts toward
    the join's own source and the one earlier source that has (or may have) it; a side the reader cannot
    attribute, and a NATURAL JOIN's implicit keys, are reported."""
    probe = ("SELECT e.EVENT_ID, e.RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e "
             "JOIN DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a USING (NOT_A_REAL_COLUMN)")
    cols, unattributed = _read_columns(probe)
    assert cols == {("OVERWATCH.ALERT_EVENTS", "EVENT_ID"), ("OVERWATCH.ALERT_EVENTS", "RULE_ID"),
                    ("OVERWATCH.ALERT_EVENTS", "NOT_A_REAL_COLUMN"), ("OVERWATCH.ALERT_AUDIT", "NOT_A_REAL_COLUMN")}
    assert not unattributed
    assert ("OVERWATCH.ALERT_AUDIT", "NOT_A_REAL_COLUMN") not in _canary_columns()     # so such a twin FAILs
    sql = """
WITH c AS (SELECT RULE_ID, COUNT(*) AS N FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG GROUP BY RULE_ID)
SELECT c.N, i.INCIDENT_ID
FROM c
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e USING (RULE_ID)
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i USING (SEVERITY)
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT a USING (NOTE)
JOIN (SELECT 1 AS Z) q USING (Y)
NATURAL JOIN DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
"""
    cols, unattributed = _read_columns(sql)
    assert cols == {
        ("OVERWATCH.ALERT_CONFIG", "RULE_ID"), ("OVERWATCH.ALERT_EVENTS", "RULE_ID"),   # the CTE traces to its table
        ("OVERWATCH.ALERT_EVENTS", "SEVERITY"), ("OVERWATCH.INCIDENTS", "SEVERITY"),    # c cannot hold SEVERITY
        ("OVERWATCH.ALERT_AUDIT", "NOTE"), ("OVERWATCH.INCIDENTS", "INCIDENT_ID"),
    }
    assert unattributed == {"USING (NOTE) before a",        # e and i may both hold NOTE
                            "USING (Y) on q", "USING (Y) before q",
                            "NATURAL JOIN r"}


def test_a_select_alias_a_source_may_also_hold_is_reported():
    """recheck #10 (v4.608): an unqualified name matching a derived select alias was always taken as the alias and
    dropped, but Snowflake can bind a same-named source column first, so beside a source that may hold the name it
    is reported, never silently skipped. Beside sources that cannot hold it, it is still the alias."""
    sql = ("SELECT UPPER(e.SEVERITY) AS NOT_A_REAL_COLUMN FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e "
           "WHERE NOT_A_REAL_COLUMN = 'x'")
    assert _read_columns(sql) == ({("OVERWATCH.ALERT_EVENTS", "SEVERITY")}, {"NOT_A_REAL_COLUMN"})
    sql = """
WITH c AS (SELECT RULE_ID, SEVERITY FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS)
SELECT UPPER(c.SEVERITY) AS SEV, COUNT(c.RULE_ID) AS N FROM c GROUP BY SEV ORDER BY N
"""
    assert _read_columns(sql) == ({("OVERWATCH.ALERT_EVENTS", "SEVERITY"), ("OVERWATCH.ALERT_EVENTS", "RULE_ID")},
                                  set())


# ============================================== #7: RUNBOOK names every probe reader without a canary ====
#: The inline (non-builder) probe=True statements, by their opening text, and how RUNBOOK names each.
_INLINE_PROBES = {
    "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE": "SHOW PARAMETERS IN WAREHOUSE",
    "SELECT CURRENT_ORGANIZATION_NAME()": "CURRENT_ORGANIZATION_NAME()",
}
#: probe=True reads whose SQL reaches run() through a name this scan cannot follow, by (file, name).
_UNTRACED = {("app/ui/pages/admin.py", "_sql")}       # the canary runner itself (each CANARIES statement)


_Reads = tuple[set[str], set[str], set[str]]     # (builders, inline SQL, untraced names)


def _wrapper_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], bool]:
    """A function's positional parameters as its callers pass them, and whether it is a method (self / cls bound,
    so a ``x.method(...)`` call's first argument is its second parameter)."""
    params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    is_method = bool(params) and params[0] in ("self", "cls")
    return (params[1:] if is_method else params), is_method


def _probe_scan(source: str, *, probe: bool = True) -> tuple[set[str], set[str], set[str], dict[str, bool]]:
    """(builders, inline SQL, untraced names, {probe wrapper: is a method}) behind every ``run(..., probe=True)``
    in ``source`` (with ``probe=False``: every plain ``run()``, which logs a failure to APP_ERROR_LOG). A builder
    is a ``*_sql`` module function: called inline (as run's first argument or its ``sql=``), assigned to a name
    in the enclosing function (closures included; a tuple's first element; either arm of a conditional), or
    passed to a same-module wrapper that forwards its parameter to such a read (security_center._optional_result;
    a method wrapper too; a parameter the body may also reassign).

    What the scan cannot follow is untraced, not dropped (recheck #11 and its recheck): a conditional's arm that is
    no builder and no literal (an empty string or None runs nothing; a literal arm is inline SQL), a name's other
    binding it cannot follow that may reach the read (an assignment, a later tuple element, a for / with / walrus
    target, untraced as the name; it comes first, a loop holds both, or a closure reads it), a run() with no SQL
    argument, a wrapper passed as a value (map / partial), and a wrapper this module
    never calls (_probe_reads_in reports a use from another module at the caller). It reads source: a run()
    reached through another name for run, or a probe=True passed in **kwargs, is outside it."""
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def merge(into: _Reads, found: _Reads) -> None:
        for acc, part in zip(into, found, strict=True):
            acc |= part

    def sql_of(node: ast.AST | None) -> _Reads | None:
        """What one statement expression runs, or None when it holds no builder and no literal (a name, another
        call: the caller follows or reports it)."""
        if isinstance(node, ast.IfExp):
            arms = [(arm, sql_of(arm)) for arm in (node.body, node.orelse)]
            if all(found is None for _arm, found in arms):
                return None
            reads: _Reads = (set(), set(), set())
            for arm, found in arms:
                merge(reads, found if found is not None else (set(), set(), {ast.unparse(arm)}))
            return reads
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id.endswith("_sql")):
            return {f"{node.func.value.id}.{node.func.attr}"}, set(), set()
        if isinstance(node, ast.Constant) and (node.value is None or node.value == ""):
            return set(), set(), set()                           # runs nothing
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return set(), {node.value.strip()}, set()
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{x}" for v in node.values)
            return set(), {text.strip()}, set()
        return None

    def bound(target: ast.AST) -> list[str]:
        if isinstance(target, ast.Starred):
            return bound(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return [name for elt in target.elts for name in bound(elt)]
        return [target.id] if isinstance(target, ast.Name) else []

    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.ListComp,
                                                         ast.SetComp, ast.DictComp, ast.GeneratorExp))]

    def reaches(fn: ast.AST, line: int, at: int) -> bool:
        """A binding on ``line`` of ``fn`` may reach a read on ``at``: it comes first, or a loop inside ``fn``
        holds both (a later iteration). A later binding in straight-line code does not (optimize.py rebinds
        _msql from the shown state after its probe read)."""
        return line <= at or any(getattr(fn, "lineno", 0) < lp.lineno <= at and line <= (lp.end_lineno or lp.lineno)
                                 for lp in loops)

    def names_in(fn: ast.AST, at: int | None = None) -> dict[str, _Reads]:
        """What each name bound in ``fn`` may hold (with ``at``: by a binding that may reach a read on that line):
        the builders and literals of its assignments (an empty string or None adds nothing), and the name itself,
        untraced, for a binding the scan cannot follow."""
        names: dict[str, _Reads] = {}

        def bind(target: ast.AST, name: str, found: _Reads | None) -> None:
            if at is None or reaches(fn, getattr(target, "lineno", 0), at):
                merge(names.setdefault(name, (set(), set(), set())),
                      found if found is not None else (set(), set(), {name}))

        for n in ast.walk(fn):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(n, ast.Assign):
                targets, value = n.targets, n.value
            elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
                targets, value = [n.target], n.value
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                targets = [n.target]
            elif isinstance(n, ast.withitem) and n.optional_vars is not None:
                targets = [n.optional_vars]
            found = sql_of(value) if value is not None else None
            if found is not None and not any(found):
                continue                                         # an empty string or None: runs nothing
            for t in targets:
                # the value is the first name's (a builder returning (sql, errors)); a later element is not
                first, *rest = bound(t) or [""]
                if first:
                    bind(t, first, found)
                for name in rest:
                    bind(t, name, None)
        return names

    def enclosing(node: ast.AST) -> list:
        return sorted((f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)),
                      key=lambda f: f.lineno)                    # outermost first

    def follow(node: ast.AST | None, scopes: list) -> _Reads | None:
        """What ``node`` runs: its own SQL, or the bindings of its name that may reach it (in its own function;
        every binding in an enclosing one, which a closure may read after any of them)."""
        if (found := sql_of(node)) is not None:
            return found
        named = [r for f in scopes
                 if (r := names_in(f, node.lineno if f is scopes[-1] else None).get(node.id)) is not None] \
            if isinstance(node, ast.Name) else []
        if not named:
            return None
        reads: _Reads = (set(), set(), set())
        for r in named:
            merge(reads, r)
        return reads

    reads: _Reads = (set(), set(), set())
    wrappers: dict[str, tuple[int, str, bool]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "run"
                and (any(k.arg == "probe" and isinstance(k.value, ast.Constant)
                         and k.value.value is True for k in node.keywords) is probe)):
            continue
        arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "sql"), None)
        scopes = enclosing(node)
        if arg is None:
            reads[2].add("run(<no SQL argument>)")
            continue
        if (found := follow(arg, scopes)) is not None:
            merge(reads, found)
        params, is_method = _wrapper_params(scopes[-1]) if scopes else ([], False)
        if isinstance(arg, ast.Name) and arg.id in params:     # its callers' SQL too, even beside a reassignment
            wrappers[scopes[-1].name] = (params.index(arg.id), arg.id, is_method)
        elif found is None:
            reads[2].add(ast.unparse(arg))
    called: set[str] = set()
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
                and id(node) not in call_funcs):                 # a wrapper passed as a value (map / partial)
            used, via_attribute = (node.id, False) if isinstance(node, ast.Name) else (node.attr, True)
            if used in wrappers and wrappers[used][2] is via_attribute:
                called.add(used)
                reads[2].add(f"{used}(<passed as a value>)")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name, via_attribute = ((func.id, False) if isinstance(func, ast.Name) else
                               (func.attr, True) if isinstance(func, ast.Attribute) else ("", False))
        if name not in wrappers or wrappers[name][2] is not via_attribute:
            continue
        idx, param, _is_method = wrappers[name]
        called.add(name)
        passed = node.args[idx] if idx < len(node.args) else next(
            (k.value for k in node.keywords if k.arg == param), None)
        if (found := follow(passed, enclosing(node))) is not None:
            merge(reads, found)
        else:
            reads[2].add(f"{name}({ast.unparse(passed) if passed is not None else '?'})")
    reads[2].update(f"{name}(<no caller in this module>)" for name in set(wrappers) - called)
    return *reads, {name: is_method for name, (_i, _p, is_method) in wrappers.items()}


def _probe_reads(source: str, *, probe: bool = True) -> _Reads:
    """(builders, inline SQL, untraced names) behind the probe reads in one module (see _probe_scan)."""
    builders, inline, untraced, _wrappers = _probe_scan(source, probe=probe)
    return builders, inline, untraced


def _probe_reads_in(files: dict[str, str], *, probe: bool = True) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    """_probe_reads over several modules (``{path: source}``), untraced names keyed by file. A wrapper is followed
    inside its own module only, so a use of it from another module (a call or a value passed on; by its imported
    name or an ``import ... as`` alias, unless that module defines its own; or through any attribute) is untraced
    at the caller, not dropped (recheck #11 and its recheck)."""
    builders: set[str] = set()
    inline: set[str] = set()
    untraced: set[tuple[str, str]] = set()
    wrappers: dict[str, dict[str, bool]] = {}
    for rel, src in files.items():
        b, i, u, wrappers[rel] = _probe_scan(src, probe=probe)
        builders |= b
        inline |= i
        untraced |= {(rel, name) for name in u}
    homes = {name: home for home, found in wrappers.items() for name in found}
    if not homes:
        return builders, inline, untraced
    for rel, src in files.items():
        tree = ast.parse(src)
        own = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        aliases = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                   for a in n.names if a.asname}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in own:
                name = aliases.get(node.id, node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            else:
                continue
            if name in homes and homes[name] != rel:
                untraced.add((rel, f"{name}(<a probe wrapper in {homes[name]}>)"))
    return builders, inline, untraced


def test_the_probe_read_scan_follows_names_and_wrappers():
    import textwrap
    src = textwrap.dedent("""
        def inline():
            return run(ops_sql.a(1), probe=True)

        def through_a_name(flag):
            sql = mart_sql.b(1) if flag else cost_sql.c(2)
            return run(sql, page="p", probe=True)

        def tuple_unpack():
            scan_sql, _errs = etl_control_sql.d([], "X")
            return run(scan_sql, probe=True)

        def closure():
            sql = insights_sql.e(1)

            def go():
                return run(sql, probe=True)
            return go()

        def _optional(sql, key):
            return run(sql, key=key, probe=True)

        def via_wrapper():
            return _optional(security_sql.f(7), "k")

        def literal():
            return run(f"SHOW TASKS IN {DB}", probe=True)

        def not_a_probe():
            return run(ops_sql.g(1))

        def untraceable(builder):
            sql = builder()
            return run(sql, probe=True)
        """)
    assert _probe_reads(src) == (
        {"ops_sql.a", "mart_sql.b", "cost_sql.c", "etl_control_sql.d", "insights_sql.e", "security_sql.f"},
        {"SHOW TASKS IN {x}"}, {"sql"})
    assert _probe_reads(src, probe=False)[0] == {"ops_sql.g"}


def test_each_probe_read_shape_lands_in_builders_inline_or_untraced():
    """recheck #11 (v4.608): a probe wrapper with no caller in its module, a method wrapper, and the non-builder
    arm of a conditional each vanished from the scan (no builder, no inline SQL, nothing untraced), so a probe
    reader routed through them was missing from RUNBOOK with the test green. Recheck of 6e7d684b: so did the SQL
    passed as run(sql=...), a second assignment the scan cannot follow beside one it can, the callers of a wrapper
    whose parameter is also reassigned, a wrapper passed as a value (map / partial), and a wrapper imported under
    another name. Each shape here lands somewhere: an inline arm is matched against _INLINE_PROBES, anything else
    is untraced; an empty string or None runs nothing. The scan covers these shapes, not every program: a run()
    reached through another name for run, or a probe=True passed in **kwargs, is outside it."""
    import textwrap
    src = textwrap.dedent("""
        def optional_read(sql, key):              # its callers live in another module
            return run(sql, key=key, probe=True)

        def mixed(flag, wh):
            sql = mart_sql.a(1) if flag else f"SHOW TASKS IN {wh}"
            return run(sql, probe=True)

        def mixed_direct(flag, other):
            return run(cost_sql.b(1) if flag else other, probe=True)

        def guarded(flag):
            sql = mart_sql.c(1) if flag else ""
            if sql:
                return run(sql, probe=True)

        class Reader:
            def read(self, sql):
                return run(sql, probe=True)

            def go(self):
                return self.read(mart_sql.d(1) if self else None)

            def many(self):
                return list(map(self.read, [mart_sql.z(1)]))

        def keyword():
            return run(sql=mart_sql.e(1), page="p", probe=True)

        def reassigned(flag, fallback):
            chosen = mart_sql.f(1)
            if flag:
                chosen = fallback
            return run(chosen, probe=True)

        def param_or_builder(sql, flag):
            if flag:
                sql = mart_sql.g(1)
            return run(sql, probe=True)

        def passes_a_builder():
            return param_or_builder(cost_sql.h(1), False)

        def _opt(sql):
            return run(sql, probe=True)

        def calls_it():
            return _opt(mart_sql.i(1))

        def maps_it():
            return list(map(_opt, [mart_sql.y(1)]))

        def tuple_rest(pairs):
            pair_sql = mart_sql.j(1)
            if pairs:
                _n, pair_sql = pairs[0]
            return run(pair_sql, probe=True)

        def rebound_in_a_loop(items):
            looped = mart_sql.k(1)
            for item in items:
                run(looped, probe=True)
                looped = item                     # the next iteration reads it

        def closure_rebinds(pick):
            late = mart_sql.l(1)

            def go():
                return run(late, probe=True)
            late = pick()                         # go() runs after it
            return go()

        def rebound_after_the_read(state):        # optimize.py's _msql: straight-line, so it cannot reach the read
            shown = mart_sql.m(1)
            run(shown, probe=True)
            shown = state["sql"]
            return shown
        """)
    assert _probe_reads(src) == (
        {"mart_sql.a", "cost_sql.b", "mart_sql.c", "mart_sql.d", "mart_sql.e", "mart_sql.f", "mart_sql.g",
         "cost_sql.h", "mart_sql.i", "mart_sql.j", "mart_sql.k", "mart_sql.l", "mart_sql.m"},
        {"SHOW TASKS IN {x}"},
        {"optional_read(<no caller in this module>)", "other", "chosen",
         "read(<passed as a value>)", "_opt(<passed as a value>)", "pair_sql", "looped", "late"})
    files = {
        "app/ui/a.py": "def _optional(sql, key):\n    return run(sql, key=key, probe=True)\n\n"
                       "def here():\n    return _optional(mart_sql.a(1), 'k')\n",
        "app/ui/b.py": "from app.ui.a import _optional as opt\n\ndef there():\n    return opt(mart_sql.b(1), 'k')\n",
        "app/ui/c.py": "import functools\nfrom app.ui.a import _optional\n\n"
                       "def there():\n    return functools.partial(_optional, key='k')(cost_sql.c(1))\n",
    }
    assert _probe_reads_in(files) == ({"mart_sql.a"}, set(),
                                      {("app/ui/b.py", "_optional(<a probe wrapper in app/ui/a.py>)"),
                                       ("app/ui/c.py", "_optional(<a probe wrapper in app/ui/a.py>)")})


def test_a_probe_wrapper_called_from_another_module_is_untraced():
    """recheck #11 (v4.608): wrappers are followed inside their own module only, so a call from another module
    (imported by name, or through the module) is reported at the caller, not dropped. A module's own function
    of the same name is its own."""
    wrapper = "def _optional(sql, key):\n    return run(sql, key=key, probe=True)\n\n" \
              "def here():\n    return _optional(mart_sql.a(1), 'k')\n"
    files = {
        "app/ui/a.py": wrapper,
        "app/ui/b.py": "from app.ui.a import _optional\n\ndef there():\n    return _optional(mart_sql.b(1), 'k')\n",
        "app/ui/c.py": "from app.ui import a\n\ndef there():\n    return a._optional(cost_sql.c(1), 'k')\n",
        "app/ui/d.py": "def _optional(x):\n    return x\n\ndef mine():\n    return _optional(1)\n",
    }
    builders, inline, untraced = _probe_reads_in(files)
    assert builders == {"mart_sql.a"} and not inline
    assert untraced == {("app/ui/b.py", "_optional(<a probe wrapper in app/ui/a.py>)"),
                        ("app/ui/c.py", "_optional(<a probe wrapper in app/ui/a.py>)")}


@functools.cache
def _app_probe_reads(probe: bool = True) -> tuple[frozenset[str], frozenset[str], frozenset[tuple[str, str]]]:
    files = {path.relative_to(_ROOT).as_posix(): path.read_text(encoding="utf-8")
             for path in sorted((_ROOT / "app").rglob("*.py"))}
    builders, inline, untraced = _probe_reads_in(files, probe=probe)
    return frozenset(builders), frozenset(inline), frozenset(untraced)


def _runbook_probe_paragraph() -> str:
    text = (_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    para = text.split('**A red "unavailable" on an optional panel**', 1)[1].split("\n\n", 1)[0]
    return re.sub(r"\s+", " ", para)


def test_runbook_names_every_probe_reader_without_a_canary():
    """holistic #7: the merged RUNBOOK sentence said 'Two probe readers are still not registered' while at
    least six more were. The list is derived here: every builder a probe=True read runs that no CANARIES entry
    calls must be named, and nothing canaried may be named as uncanaried."""
    builders, inline, untraced = _app_probe_reads()
    assert untraced == _UNTRACED, f"follow these probe reads to their builder: {sorted(untraced - _UNTRACED)}"
    assert {"mart_sql.flyway_history", "security_sql.change_risk_destructive_breakdown"} <= builders  # sanity
    para = _runbook_probe_paragraph()
    uncanaried = builders - _registered()
    named = set(re.findall(r"\b[a-z0-9_]+_sql\.[a-z0-9_]+\b", para))
    assert named == uncanaried, (f"RUNBOOK must name {sorted(uncanaried - named)}; "
                                 f"it names canaried or gone readers {sorted(named - uncanaried)}")
    for sql in inline:
        assert any(sql.startswith(prefix) for prefix in _INLINE_PROBES), f"name this inline probe in RUNBOOK: {sql}"
    for prefix in _INLINE_PROBES:
        assert any(sql.startswith(prefix) for sql in inline), f"no inline probe starts {prefix!r} any more"
    for phrase in _INLINE_PROBES.values():
        assert phrase in para, phrase
    twins = re.search(r"Twins, whose (.*?)\. [A-Z]", para)
    assert twins, "RUNBOOK's twin sentence is missing"
    assert set(re.findall(r"\b[a-z0-9_]+_sql\.[a-z0-9_]+\b", twins.group(1))) == (
        uncanaried & set(_TWINS)), "RUNBOOK's twins must be exactly the checked twins"
    assert "Two probe readers" not in para


def test_runbook_says_which_uncanaried_probe_readers_also_log():
    """Review of 91e3d3ac: 'For every one but the twins, the expander error is the only record' was false. Operations
    runs reference_gap_scan and cycle_finish_history_scan without probe too, so a missing column there reaches
    APP_ERROR_LOG. The 'also logged' sentence names exactly the uncanaried, non-twin probe readers that a plain
    run() also reads (derived from app/), and the blanket claim is gone."""
    probes, _inline, _untraced = _app_probe_reads()
    plain = _app_probe_reads(probe=False)[0]
    logged = (probes - _registered() - set(_TWINS)) & plain
    assert {"etl_control_sql.reference_gap_scan", "etl_control_sql.cycle_finish_history_scan"} <= logged  # sanity
    para = _runbook_probe_paragraph()
    also = re.search(r"Also logged to APP_ERROR_LOG(.*?)\. [A-Z]", para)
    assert also, "RUNBOOK's 'Also logged to APP_ERROR_LOG' sentence is missing"
    assert set(re.findall(r"\b[a-z0-9_]+_sql\.[a-z0-9_]+\b", also.group(1))) == logged
    only = [s for s in re.split(r"(?<=\.) (?=[A-Z])", para) if "the expander error is the only record" in s]
    assert len(only) == 1 and not set(re.findall(r"\b[a-z0-9_]+_sql\.[a-z0-9_]+\b", only[0])) & logged
    assert "For every one but the twins" not in para


def test_runbook_partly_covered_reader_claims_hold():
    """Review of 91e3d3ac: the sentence credited 'the security.* canaries' with object_reads_confirm's ACCESS_HISTORY
    columns, but QUERY_ID and USER_NAME compile only in graph.object_blast_consumers and
    workbench.product_consumer_reads. The canaries the sentence names must compile every ACCESS_HISTORY column the
    reader reads, and each column no canary compiles must be named."""
    part = re.search(r"Partly covered: insights_sql\.object_reads_confirm \((.*?)\)\. [A-Z]",
                     _runbook_probe_paragraph())
    assert part, "RUNBOOK's 'Partly covered' sentence is missing"
    text = part.group(1)
    reads = _table_columns(insights_sql.object_reads_confirm(("DB.S.T",), 90))
    access = {p for p in reads if p[0] == "ACCOUNT_USAGE.ACCESS_HISTORY"}
    named = {n: b for n, b in canary.CANARIES if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", text)}
    assert named, "name the canaries that compile its ACCESS_HISTORY columns"
    assert access and access <= set().union(*(_table_columns(b()) for b in named.values()))
    uncovered = reads - _canary_columns()
    assert uncovered and not {p for p in uncovered if p in access}
    for _table, column in uncovered:
        assert column in text, f"name {column}: no canary compiles it"
    assert "security.* canaries" not in text


def test_glossary_canary_row_names_every_v4608_security_canary():
    """holistic #19: the canary row listed the ops and cost / mart additions but none of the security ones."""
    row = next(ln for ln in (_ROOT / "FEATURE_GLOSSARY.md").read_text(encoding="utf-8").splitlines()
               if ln.startswith("| **N registered statements"))
    registered = {name for name, _ in canary.CANARIES}
    for name in ("security.access_evidence_days", "security.grant_scope_usage", "security.unused_table_grants",
                 "graph.object_blast_consumers", "workbench.product_consumer_reads",
                 "security.security_change_fact_coverage", "security.change_risk_destructive_breakdown",
                 "mart27.ai_code_user_daily", "mart.resolutions_for_rule"):
        assert name in registered, name
        assert name in row, name
    assert "tests/test_security_e1_fixes.py" in row


def test_glossary_canary_row_v4608_counts_match_the_names_it_lists():
    """recheck #12 (v4.608): the row said '14 cost / mart entries' and left out
    mart.fact_query_window_summary.read_clock (Overview's score read-clock shape), one of the 35 statements v4.608
    added. Each v4.608 clause's stated count must equal the registered canaries it names (a full name, or the bare
    builder after a ' / ': 'cost.org_all_in_window_usd / org_usage_in_currency')."""
    row = next(ln for ln in (_ROOT / "FEATURE_GLOSSARY.md").read_text(encoding="utf-8").splitlines()
               if ln.startswith("| **N registered statements"))
    registered = {name for name, _ in canary.CANARIES}
    clauses = {
        "ops": r"v4\.608 adds (\d+) Operations / ETL entries: (.*?); v4\.608 also adds",
        "cost / mart": r"v4\.608 also adds (\d+) cost / mart entries: (.*?); v4\.608 also adds",
        "security": r"v4\.608 also adds (\d+) security entries(.*?)never GAPs\.",
    }
    named_by: dict[str, set[str]] = {}
    for label, pattern in clauses.items():
        m = re.search(pattern, row)
        assert m, f"the canary row's v4.608 {label} clause is missing"
        text = m.group(2)
        named = {n for n in registered
                 if re.search(rf"(?:(?<![\w.]){re.escape(n)}|(?<=[\w.] / ){re.escape(n.split('.', 1)[1])})(?![\w.])",
                              text)}
        assert len(named) == int(m.group(1)), f"{label}: the row says {m.group(1)}, names {sorted(named)}"
        named_by[label] = named
    assert "mart.fact_query_window_summary.read_clock" in named_by["cost / mart"]
    assert sum(len(v) for v in named_by.values()) == len(set().union(*named_by.values()))   # no name counted twice
