"""Locks for the 2026-07-08 live findings, round 3 (v4.8.1).

Break-glass policy is config-as-code (V025); Trexis roles stay off ALFA's
role-grain surfaces; daily charts label days, not hours; bar labels don't
truncate mid-name; the spend tie-out explains the three spend lenses.
"""

from __future__ import annotations

from pathlib import Path

from app.companies import role_clause
from app.data import chargeback_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V025__break_glass_policy.sql").read_text(encoding="utf-8")
_CHARTS = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# V025 — break-glass policy
# ---------------------------------------------------------------------------

def test_v025_guard_and_version():
    assert "EXCEPTION (-20025" in _MIG
    assert "IF (v < 24) THEN" in _MIG
    assert "SELECT 25 AS VERSION" in _MIG


def test_v025_disables_the_rule_and_only_that():
    assert "SET ENABLED = FALSE" in _MIG
    assert "WHERE RULE_ID = 'SEC_BREAK_GLASS_USE'" in _MIG
    assert "DROP" not in _MIG.upper().replace("DROPPED", "")  # policy change, not surgery


def test_validate_expects_at_least_v025():
    import re
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    m = re.search(r"V001\.\.V0(\d+) applied", validate)
    assert m and int(m.group(1)) >= 25                 # floor, not tip


# ---------------------------------------------------------------------------
# Role-grain company scoping
# ---------------------------------------------------------------------------

def test_role_clause_scopes_by_name_heuristic():
    assert "LIKE '%TRXS%'" in role_clause("Trexis")
    alfa = role_clause("ALFA")
    assert "NOT LIKE '%TRXS%'" in alfa
    assert "COALESCE" in alfa                          # NULL role must stay visible for ALFA
    assert role_clause("ALL") == ""


def test_role_share_excludes_foreign_roles():
    sql = chargeback_sql.role_share_within_warehouse(7, "ALFA")
    assert "NOT LIKE '%TRXS%'" in sql                  # the TF_O_TRXS_* leak, fixed
    assert "LIKE '%TRXS%'" in chargeback_sql.role_share_within_warehouse(7, "Trexis")
    assert "TRXS%'" not in chargeback_sql.role_share_within_warehouse(7, "ALL").split("WAREHOUSE_NAME IS NOT NULL")[1].split("GROUP BY")[0].replace("NOT IN ('WH_TRXS_LOAD", "")


def test_day_replay_builders_take_company():
    ddl = security_sql.day_ddl("2026-07-07", "ALFA")
    assert "NOT LIKE '%TRXS%'" in ddl
    grants = security_sql.day_grants("2026-07-07", "ALFA")
    assert "NOT LIKE '%TRXS%'" in grants
    assert "ROLE" in grants
    # default stays account-wide (replay with company ALL)
    assert "TRXS" not in security_sql.day_ddl("2026-07-07")


# ---------------------------------------------------------------------------
# Charts — labels and day-grain axes (source-scraped)
# ---------------------------------------------------------------------------

def test_bar_charts_stop_truncating_names():
    assert _CHARTS.count("labelLimit=260") >= 2        # bar_usd + bar_count


def test_bar_usd_leaves_headroom_for_value_labels():
    assert "dmax * 1.16" in _CHARTS


def test_daily_charts_label_days_not_hours():
    # every daily chart carries the day-format axis; stacked/count bars bin by day
    assert _CHARTS.count('format="%b %d"') >= 4
    assert _CHARTS.count('yearmonthdate(Day)') >= 2


# ---------------------------------------------------------------------------
# Spend clarity + AI fallback wiring (source-scraped)
# ---------------------------------------------------------------------------

def test_spend_tab_explains_the_three_lenses():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "Why totals differ across pages (and vs Snowsight)" in src
    assert "storage and data" in src                   # names the Snowsight delta explicitly


def test_unit_costs_ai_falls_back_to_code_usage():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert "cortex_source_costs" in src                # Cortex Code = where this account bills
    assert "ATTRIBUTED_CALLS" in src                   # proc $0 rows stay diagnosable


def test_new_cost_builders_are_canaried():
    src = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("measured_query_costs", "procedure_costs_usd", "graph_daily_costs",
                 "serverless_task_daily", "warehouse_change_registry", "cortex_source_costs"):
        assert name in src, name


# ---------------------------------------------------------------------------
# V026 — teams-safe delivery (v4.9.0)
# ---------------------------------------------------------------------------

_MIG26 = (_ROOT / "snowflake" / "migrations" / "V026__teams_safe_delivery.sql").read_text(encoding="utf-8")


def test_v026_guard_version_and_scope():
    assert "EXCEPTION (-20026" in _MIG26
    assert "IF (v < 25) THEN" in _MIG26
    assert "SELECT 26 AS VERSION" in _MIG26
    assert "SP_NOTIFY_WEBHOOK" in _MIG26                # sender replaced, nothing else
    assert "CREATE TABLE" not in _MIG26.upper()


def test_v026_sender_escapes_json_via_chr_codes():
    # CHR() codes only — backslashes must not survive multiple string layers
    # (the V022 comma-eater and CALLs+ lessons). Order matters: backslash first.
    body = _MIG26.split("BEGIN", 2)[2]
    for esc in ("CHR(92), CHR(92) || CHR(92)",          # backslash doubled FIRST
                "CHR(34), CHR(92) || CHR(34)",          # quote
                "CHR(10), CHR(92) || 'n'",              # newline -> \n
                "CHR(13), ''",                          # CR dropped
                "CHR(9),  CHR(92) || 't'"):             # tab
        assert esc in body, esc
    assert body.index("CHR(92) || CHR(92)") < body.index("CHR(92) || CHR(34)")
    # the prefix newline is escaped too (a raw one re-breaks the JSON)
    assert "'OVERWATCH alerts:' || CHR(92) || 'n'" in _MIG26


def test_v026_keeps_v22_delivery_semantics():
    # per-route ledger, retries, and the loud expiry path all survive v3
    for marker in ("ALERT_DELIVERIES", "route_send_failed", "undelivered_expired",
                   "NOTIFIED_AT"):
        assert marker in _MIG26, marker


def test_webhook_script_carries_the_teams_recipe():
    script = (_ROOT / "snowflake" / "webhook_delivery.sql").read_text(encoding="utf-8")
    assert "vnd.microsoft.card.adaptive" in script       # Workflows envelope
    assert "SNOWFLAKE_WEBHOOK_MESSAGE" in script.split("card.adaptive", 1)[1]
    assert "same body template works" not in script      # the retired-connector lie
    assert "OVERWATCH_WEBHOOK_TEAMS" in script
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "OVERWATCH_WEBHOOK_TEAMS" in teardown


def test_mart_family_design_doc_exists():
    doc = (_ROOT / "docs" / "design" / "V027_MART_FAMILY.md").read_text(encoding="utf-8")
    assert "MART_WAREHOUSE_EFFICIENCY_DAILY" in doc
    assert "test_perf_budgets" in doc                    # adoption lowers budgets

