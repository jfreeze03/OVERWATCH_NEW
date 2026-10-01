"""House law 4 ("every SQL builder gets a canary"), enforced for the cost / mart builder modules (v4.608).

R2-062 / R2-064 / R2-068: the ORGANIZATION_USAGE readers (the All-in invoice tile, the org balance behind every
runway, the rate-card reconciliation), ALERT_EVENTS' SNOOZE_* reader, the MART_TABLE_STORAGE_DAILY byte readers,
the email-path ALERT_HISTORY read and MART_TASK_NODE_DAILY's timing reader had no canary. Several are probe=True
reads, which log neither an absent object nor a missing column, so a renamed column failed silently and Admin >
Canary still said "All N applicable canary statements passed".

The ratchet below fails on any public builder in cost_sql / mart_sql / mart27_sql that has neither a canary
nor a named exemption with its reason, and on an exemption that is no longer needed (the list only shrinks).

A twin exemption ("reads only columns a registered canary already compiles") is checked, not trusted: each twin
is rendered, the (table, column) pairs it compiles are read off the parsed SQL, and they must be a subset of
the pairs the registered canaries compile (the R2-069 ACCESS_HISTORY lock's method, for every table). Two false
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
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.data import (
    canary,
    change_impact_sql,
    cortex_sql,
    cost_sql,
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
# A column counts toward the physical table it compiles against: through its alias, as the scope's only source,
# or traced through a CTE / derived table (a plain pass-through projection, a star, or each UNION branch at the
# same position). A derived projection (MIN(DAY) AS FIRST_DAY) adds nothing beyond its own inputs, and an
# unqualified name that is a select alias of its scope (GROUP BY USAGE_DATE) is the alias, not a column. A
# column the parser cannot attribute (a correlated outer reference, a FLATTEN value, an unqualified name beside
# several sources) is not counted on either side: the canary side stays a lower bound, so a twin never passes
# on a guess about what a canary compiles; on the twin side such a column goes unchecked (qualify it: house
# law 8).

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


def _resolve(scope: Scope, col: exp.Column, depth: int = 0) -> set[tuple[str, str]] | None:
    if depth > 16:
        return None
    sources = {alias.upper(): source for alias, (_node, source) in scope.selected_sources.items()}
    qualifier, name = col.table.upper(), col.name.upper()
    if qualifier:
        source = sources.get(qualifier)
    elif len(sources) == 1:
        source = next(iter(sources.values()))
    else:
        return None
    if isinstance(source, exp.Table):
        return {(_table_key(source), name)} if source.name and not isinstance(source.this, exp.Func) else None
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


def _table_columns(sql: str) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for scope in traverse_scope(sqlglot.parse_one(sql, read="snowflake")):
        for col in scope.columns:
            if (isinstance(col.this, exp.Star) or col.find_ancestor(exp.Select) is not scope.expression
                    or _is_alias_reference(scope, col)):
                continue
            out |= _resolve(scope, col) or set()
    return out


@functools.cache
def _canary_columns() -> frozenset[tuple[str, str]]:
    return frozenset().union(*(_table_columns(build()) for _name, build in canary.CANARIES))


def test_the_table_column_reader_is_derived_from_the_sql():
    """The ratchet itself: columns are attributed through aliases, CTE pass-through, a star and UNION branches;
    a derived projection, a select-alias reference and another table's same-named alias add nothing."""
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
    reaches Admin > Canary. Every twin's (table, column) set must be inside what the canaries compile."""
    reads: set[tuple[str, str]] = set()
    for sql in _TWINS[name]():
        cols = _table_columns(sql)
        assert cols, f"{name} rendered SQL that reads no attributable column"
        reads |= cols
    missing = sorted(reads - _canary_columns())
    assert not missing, f"{name} is not a twin: no registered canary compiles {missing}"


# ============================================== #7: RUNBOOK names every probe reader without a canary ====
#: The inline (non-builder) probe=True statements, by their opening text, and how RUNBOOK names each.
_INLINE_PROBES = {
    "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE": "SHOW PARAMETERS IN WAREHOUSE",
    "SELECT CURRENT_ORGANIZATION_NAME()": "CURRENT_ORGANIZATION_NAME()",
}
#: probe=True reads whose SQL reaches run() through a name this scan cannot follow, by (file, name).
_UNTRACED = {("app/ui/pages/admin.py", "_sql")}       # the canary runner itself (each CANARIES statement)


def _probe_reads(source: str) -> tuple[set[str], set[str], set[str]]:
    """(builders, inline SQL, untraced names) behind every ``run(..., probe=True)`` in ``source``. A builder is
    a ``*_sql`` module function: called inline, assigned to a name in the enclosing function (closures included;
    a tuple's first element; either arm of a conditional), or passed to a same-module wrapper that forwards
    its parameter to such a read (security_center._optional_result)."""
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def builders_of(node: ast.AST | None) -> set[str]:
        if isinstance(node, ast.IfExp):
            return builders_of(node.body) | builders_of(node.orelse)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id.endswith("_sql")):
            return {f"{node.func.value.id}.{node.func.attr}"}
        return set()

    def names_in(fn: ast.AST) -> dict[str, set[str]]:
        names: dict[str, set[str]] = {}
        for n in ast.walk(fn):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and (found := builders_of(n.value)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    t = t.elts[0] if isinstance(t, (ast.Tuple, ast.List)) and t.elts else t
                    if isinstance(t, ast.Name):
                        names.setdefault(t.id, set()).update(found)
        return names

    def enclosing(node: ast.AST) -> list:
        return sorted((f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)),
                      key=lambda f: f.lineno)                    # outermost first

    builders: set[str] = set()
    inline: set[str] = set()
    untraced: set[str] = set()
    wrappers: dict[str, tuple[int, str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "run"
                and node.args and any(k.arg == "probe" and isinstance(k.value, ast.Constant)
                                      and k.value.value is True for k in node.keywords)):
            continue
        arg, scopes = node.args[0], enclosing(node)
        if found := builders_of(arg):
            builders |= found
        elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            inline.add(arg.value.strip())
        elif isinstance(arg, ast.JoinedStr):
            inline.add("".join(v.value if isinstance(v, ast.Constant) else "{x}" for v in arg.values).strip())
        elif isinstance(arg, ast.Name):
            found = set().union(*(names_in(f).get(arg.id, set()) for f in scopes)) if scopes else set()
            params = [a.arg for a in scopes[-1].args.args] if scopes else []
            if found:
                builders |= found
            elif arg.id in params:
                wrappers[scopes[-1].name] = (params.index(arg.id), arg.id)
            else:
                untraced.add(arg.id)
        else:
            untraced.add(ast.unparse(arg))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in wrappers:
            idx, param = wrappers[node.func.id]
            passed = node.args[idx] if idx < len(node.args) else next(
                (k.value for k in node.keywords if k.arg == param), None)
            scopes = enclosing(node)
            found = builders_of(passed) or (
                set().union(*(names_in(f).get(passed.id, set()) for f in scopes))
                if isinstance(passed, ast.Name) and scopes else set())
            if found:
                builders |= found
            else:
                untraced.add(f"{node.func.id}({ast.unparse(passed) if passed is not None else '?'})")
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


@functools.cache
def _app_probe_reads() -> tuple[frozenset[str], frozenset[str], frozenset[tuple[str, str]]]:
    builders: set[str] = set()
    inline: set[str] = set()
    untraced: set[tuple[str, str]] = set()
    for path in sorted((_ROOT / "app").rglob("*.py")):
        b, i, u = _probe_reads(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_ROOT).as_posix()
        builders |= b
        inline |= i
        untraced |= {(rel, name) for name in u}
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
