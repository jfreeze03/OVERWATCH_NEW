"""v4.51 Tranche A locks — outcome and audit trust (Codex adjudication 2026-07-27).

Pins the round: audit actors are the viewer (not the app owner), savings
captions promise only what actually settles, ETL unit costs aggregate at run
grain with written-rows denominators, batch telemetry reports measured time,
and the perf lint gains a builder-level companion that sees THROUGH page files
to the SQL their builders actually render.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tests"))
from test_p4_filter_matrix import _REQUIRED_ARGS  # noqa: E402 — shared arg table


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Builder-level reachable-scan gate (Codex P2): the literal-count budget in
#    test_perf_budgets is a lint proxy that cannot see builder-mediated scans —
#    unit_costs counted 0 while its builders reach 7 ACCOUNT_USAGE tables.
#    This gate renders every builder a page references (introspected defaults)
#    and pins the DISTINCT ACCOUNT_USAGE tables actually reachable from it.
# ---------------------------------------------------------------------------

_BUILDER_RE = re.compile(r"\b(\w+_sql)\.(\w+)\(")
_AU_RE = re.compile(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)")

_REACHABLE = {
    "app/ui/pages/brief.py": (),
    "app/ui/pages/overview.py": ("WAREHOUSE_METERING_HISTORY",),
    "app/ui/pages/control_room.py": (
        "GRANTS_TO_USERS", "QUERY_HISTORY", "TASK_HISTORY", "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/cost.py": ("QUERY_HISTORY",),
    "app/ui/pages/cost_parts/spend.py": (
        # V077 cost-by-application panel adds QUERY_ATTRIBUTION_HISTORY + SESSIONS
        # (the live 3-way join fallback behind the toggle).
        "DATABASE_REPLICATION_USAGE_HISTORY", "DATABASE_STORAGE_USAGE_HISTORY",
        "METERING_DAILY_HISTORY", "NOTEBOOKS_CONTAINER_RUNTIME_HISTORY",
        "QUERY_ATTRIBUTION_HISTORY", "QUERY_HISTORY", "SESSIONS",
        "SNOWPARK_CONTAINER_SERVICES_HISTORY", "STORAGE_USAGE",
        "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/cost_parts/contract.py": ("QUERY_HISTORY", "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/cost_parts/ai_chargeback.py": (
        "CORTEX_AI_FUNCTIONS_USAGE_HISTORY", "CORTEX_CODE_CLI_USAGE_HISTORY",
        "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "METERING_DAILY_HISTORY",
        "QUERY_HISTORY", "USERS"),
    "app/ui/pages/cost_parts/unit_costs.py": (
        "CORTEX_CODE_CLI_USAGE_HISTORY", "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY",
        "CORTEX_FUNCTIONS_USAGE_HISTORY", "QUERY_ATTRIBUTION_HISTORY",
        "QUERY_HISTORY", "SERVERLESS_TASK_HISTORY", "TASK_HISTORY"),
    "app/ui/pages/cost_parts/compare.py": (),
    # +TABLES (2026-07-31, audit B4): the storage retention-fix estimate counted the whole
    # time-travel + failsafe pile as recoverable, which is systematically high — failsafe
    # drains in 7d regardless of RETENTION_TIME. ACCOUNT_USAGE.TABLES carries the per-table
    # retention the estimate has to scale by, so this is a DELIBERATE scan-surface change
    # buying a correct dollar figure (metadata view; no history scan).
    "app/ui/pages/cost_parts/optimize.py": (
        "ACCESS_HISTORY", "AUTOMATIC_CLUSTERING_HISTORY", "DATABASE_STORAGE_USAGE_HISTORY",
        "QUERY_HISTORY", "TABLES", "TABLE_DML_HISTORY", "TABLE_STORAGE_METRICS",
        "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/operations.py": (
        "COPY_HISTORY", "DYNAMIC_TABLE_REFRESH_HISTORY", "LOCK_WAIT_HISTORY",
        "QUERY_HISTORY", "TABLE_DML_HISTORY", "TASKS", "TASK_HISTORY", "TASK_VERSIONS",
        "WAREHOUSE_LOAD_HISTORY"),
    "app/ui/pages/security.py": (
        "CREDENTIALS", "DATA_TRANSFER_HISTORY", "GRANTS_TO_ROLES", "GRANTS_TO_USERS",
        "LOGIN_HISTORY", "QUERY_HISTORY", "ROLES", "SESSIONS", "USERS"),
    "app/ui/pages/alerts.py": (),
    # v4.52: + the object-ledger recon builder (Codex #7) — QAH and the five
    # maintenance-arm source histories, click-gated on the Canary tab.
    "app/ui/pages/admin.py": (
        "AUTOMATIC_CLUSTERING_HISTORY", "MATERIALIZED_VIEW_REFRESH_HISTORY",
        "METERING_DAILY_HISTORY", "PIPE_USAGE_HISTORY", "QUERY_ATTRIBUTION_HISTORY",
        "QUERY_HISTORY", "SEARCH_OPTIMIZATION_HISTORY", "SERVERLESS_TASK_HISTORY"),
}


def _render_builder(fn):
    kwargs = {}
    for pname, param in inspect.signature(fn).parameters.items():
        if pname == "company":
            kwargs[pname] = "ALFA"
        elif param.default is not inspect.Parameter.empty:
            continue
        else:
            kwargs[pname] = _REQUIRED_ARGS[pname]      # KeyError -> skipped
    out = fn(**kwargs)
    if not isinstance(out, str):
        raise TypeError(fn.__name__)
    return out


def test_reachable_account_usage_tables_per_page():
    """A page's true scan surface is what its BUILDERS render, not what its
    source spells. New reachable tables must be pinned here deliberately —
    growing this set is the honest version of raising a literal budget."""
    for rel, expected in _REACHABLE.items():
        src = _read(rel)
        tables: set = set()
        for mod_name, fn_name in sorted(set(_BUILDER_RE.findall(src))):
            try:
                mod = importlib.import_module(f"app.data.{mod_name}")
            except ModuleNotFoundError:
                continue
            fn = getattr(mod, fn_name, None)
            if fn is None or not inspect.isfunction(fn):
                continue
            try:
                sql = _render_builder(fn)
            except (KeyError, TypeError, ValueError):
                continue                                  # unguessable args
            tables |= set(_AU_RE.findall(sql))
        assert tuple(sorted(tables)) == expected, (
            f"{rel}: reachable ACCOUNT_USAGE set changed — "
            f"got {sorted(tables)}, pinned {list(expected)}. "
            "Update the pin ONLY with a deliberate scan-surface change.")


# ---------------------------------------------------------------------------
# 2. Audit actors are the viewer (Codex P1-B)
# ---------------------------------------------------------------------------

def test_alert_audit_inserts_stamp_the_viewer():
    src = _read("app/ui/pages/alerts.py")
    inserts = re.findall(r"INSERT INTO \{core_object\('ALERT_AUDIT'\)\} \(([^)]+)\)", src)
    assert len(inserts) == 2
    for cols in inserts:
        assert "ACTED_BY" in cols, "ALERT_AUDIT insert lets CURRENT_USER() default record the owner"
    assert "the table has no" not in src                 # the false r27 comment stays dead


def test_remediation_log_inserts_stamp_the_viewer():
    expected = {"app/ui/pages/alerts.py": 1,
                "app/ui/pages/cost_parts/optimize.py": 3,
                "app/ui/pages/operations.py": 2}
    for rel, n in expected.items():
        src = _read(rel)
        total = src.count("INSERT INTO {core_object('REMEDIATION_LOG')}")
        stamped = src.count("RESULT_NOTE, EXECUTED_BY)")
        assert total == n, f"{rel}: REMEDIATION_LOG insert count moved ({total})"
        assert stamped == n, f"{rel}: {n - stamped} insert(s) still default EXECUTED_BY to the owner"


# ---------------------------------------------------------------------------
# 3. Savings captions promise only what settles (Codex P1-A)
# ---------------------------------------------------------------------------

def test_no_screen_promises_the_phantom_verifier():
    """The monthly verifier proposes only, and only for descriptions no
    executed path books; V038 settles only its own rows. Captions must not
    promise automatic settlement for app-booked entries."""
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/pages/alerts.py"):
        src = _read(rel)
        for phrase in ("monthly verifier proves or rejects",
                       "verifier will test actuals",
                       "verifier tests actuals",
                       "monthly verifier compares",
                       "the monthly verifier later proves"):
            assert phrase not in src, f"{rel}: still promises the phantom verifier ({phrase!r})"


# ---------------------------------------------------------------------------
# 4. Telemetry reports measured time and carries the Snowflake query id
# ---------------------------------------------------------------------------

def test_batch_telemetry_is_honest_and_query_id_flows():
    q = _read("app/core/query.py")
    assert "elapsed / max(len(bspecs)" not in q, (
        "per-member batch time must be the measured batch wall (BATCH_SIZE labels "
        "the sharing), not an invented wall/size division")
    assert "query_id: str | None = None" in q            # telemetry accepts it
    assert "_LAST_QUERY_ID" in q                         # captured from the async job handle


# ---------------------------------------------------------------------------
# 5. ETL unit costs: run grain, written rows, honest run_id coverage (Codex #8/#10)
# ---------------------------------------------------------------------------

def test_etl_formulas_are_run_grain_with_written_rows():
    from app.data import etl_sql
    sql = etl_sql.etl_cost_by_pipeline(30, "ALFA", "ALFA_EDW_PRD", "STG")
    assert "GROUP BY t.PIPELINE, t.RUN_ID" in sql        # runs before pipelines
    assert "ROWS_WRITTEN" in sql and "'CREATE_TABLE_AS_SELECT'" in sql
    assert "RUN_ID_CREDIT_PCT" in sql                    # partial tagging degrades honestly
    assert "COUNT_IF(RUN_ID IS NOT NULL)" in sql
    # scope threading reaches the SQL (Codex #10: the panel ignored database/schema)
    assert "ALFA_EDW_PRD" in sql and "STG" in sql
    uc = _read("app/ui/pages/cost_parts/unit_costs.py")
    assert 'etl_sql.etl_cost_by_pipeline(days, company, f["database"], f["schema_contains"])' in uc
    assert 'etl_sql.etl_tag_coverage(days, company, f["database"], f["schema_contains"])' in uc


# ---------------------------------------------------------------------------
# 6. Docs carry the real migration floor (Codex #20)
# ---------------------------------------------------------------------------

def test_deploy_docs_track_the_migration_floor():
    latest = sorted((_ROOT / "snowflake" / "migrations").glob("V0*.sql"))[-1].name
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert latest in _read(rel), (
            f"{rel}: run-list trails the repo — add {latest} (the r27 #9 rewrite "
            "fixed narrative but the list drifted for 15 migrations)")
    assert "OVERWATCH_MONITOR" not in _read("README.md")  # retired-role reference stays dead


def test_validate_sql_floor_tracks_the_latest_migration():
    """validate.sql's 'V001..V0NN applied' floor and its count check must equal the
    latest migration number — generic so a new migration that forgets to bump it
    fails CI, without pinning the moving floor inside any per-migration test."""
    migs = sorted((_ROOT / "snowflake" / "migrations").glob("V0*.sql"))
    n = max(int(m.name[1:4]) for m in migs)
    v = _read("snowflake/validate.sql")
    assert f"V001..V{n:03d} applied" in v, f"validate.sql floor label != V{n:03d}"
    assert f"BETWEEN 1 AND {n}) = {n}" in v, f"validate.sql count check != {n}"


# ---------------------------------------------------------------------------
# 7. Canary compile-only mode + registry caption honesty
# ---------------------------------------------------------------------------

def test_canary_has_compile_only_mode():
    adm = _read("app/ui/pages/admin.py")
    assert "EXPLAIN USING TEXT" in adm and "adm_canary_explain" in adm


def test_registry_caption_stops_overclaiming_discovery():
    adm = _read("app/ui/pages/admin.py")
    assert "register new cost metrics by hand" in adm
    assert "registering it here fails the drift-guard test" not in adm
