"""Regression locks for the round-6 bug hunt (v4.421.0)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.data import mart27_sql
from app.ui.pages.ask import _numbers_preserved

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- PERMANENT GUARD (r5 tag_coverage + r6 rule_precision class) --------------------
# A projection that references a DIFFERENT projection's aggregate alias, in a FLAT select
# straight from a base table, is invalid Snowflake ("invalid identifier") — but sqlglot
# parses it and snowflake-smoke is skipped, so NEITHER CI gate catches it. This AST sweep
# over every canary-registered builder is the gate that does.
def test_no_builder_references_a_sibling_select_alias():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp

    from app.data import canary
    registry = next(v for v in vars(canary).values()
                    if isinstance(v, (list, tuple)) and v
                    and isinstance(v[0], tuple) and callable(v[0][1]))
    AGG = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.AnyValue)

    def hits(sql: str) -> list:
        out: list[str] = []
        try:
            tree = sqlglot.parse_one(sql, dialect="snowflake")
        except sqlglot.errors.SqlglotError:
            return out
        cte_names = {c.alias for c in tree.find_all(exp.CTE)}
        for sel in tree.find_all(exp.Select):
            frm = sel.args.get("from")
            src = getattr(frm, "this", None) if frm else None
            if not (isinstance(src, exp.Table) and src.name not in cte_names):
                continue  # only FLAT selects straight from a base table can't bind to a source col
            agg_alias = {e.alias for e in sel.expressions
                         if isinstance(e, exp.Alias) and not isinstance(e.this, exp.Column)
                         and any(True for _ in e.this.find_all(*AGG))}
            for e in sel.expressions:
                A = e.alias if isinstance(e, exp.Alias) else None
                tgt = e.this if isinstance(e, exp.Alias) else e
                out.extend(col.name for col in tgt.find_all(exp.Column)
                           if col.name in agg_alias and col.name != A)
        return out

    offenders = {}
    for name, fn in registry:
        try:
            sql = fn()
        except Exception:  # noqa: BLE001 — a builder that raises is a different gate's failure
            continue
        h = hits(sql)
        if h:
            offenders[name] = sorted(set(h))
    assert not offenders, (
        "flat-SELECT references a sibling aggregate alias (invalid in Snowflake, "
        f"invisible to the parse gate) — repeat the aggregate inline: {offenders}")


# --- sqlsem-1 (HIGH): rule_precision inlines the COUNT_IF ----------------------------
def test_rule_precision_inlines_countif():
    from app.data import mart_sql
    sql = mart_sql.rule_precision(90)
    assert "100 * COUNT_IF(RESOLUTION_KIND = 'ACTIONED')" in sql
    assert "* ACTIONED /" not in sql and "ACTIONED + NOISE" not in sql


# --- FBK-1 (HIGH): task_graphs honors Last-month bounds -----------------------------
def test_task_graphs_is_bounds_aware():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    bounded = mart27_sql.task_graphs(30, "ALFA", "DB", "", bounds=b)
    assert "2026-08-01" in bounded and "2026-09-01" in bounded
    assert "DATEADD('day', -30, CURRENT_DATE())" in mart27_sql.task_graphs(30, "ALFA")
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert "mart27_sql.task_graphs(days, company, database, schema_contains, bounds=bounds)" in uc


# --- XM-1 (MED): sidebar open-criticals badge disambiguated as account-wide ---------
def test_sidebar_open_criticals_labeled_account_wide():
    assert '"k": "Open criticals (acct)"' in _src("app/main.py")


# --- XM-2 (MED): control-room verdict reconciles with the freshness board -----------
def test_control_room_stale_verdict_matches_the_board():
    cr = _src("app/ui/pages/control_room.py")
    assert 'f"{_stale_n} telemetry source(s) stale or not loaded"' in cr


# --- SR-2 (MED): task-failure AI panel key folds the filters that change evidence ---
def test_ops_ai_panel_key_includes_database_and_schema():
    ops = _src("app/ui/pages/operations.py")
    assert 'key=f"task_failures_{company}_{database}_{schema_contains}"' in ops


# --- SR-3 (LOW): idle AI panel key carries the Last-month discriminator -------------
def test_idle_ai_panel_key_includes_lm():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert 'key=f"idle_{company}_{days}{_lm}"' in opt          # both the data read AND the AI panel
    assert opt.count('key=f"idle_{company}_{days}{_lm}"') >= 2


# --- CV-1 (LOW): daily_stacked_usd is currency-aware --------------------------------
def test_daily_stacked_currency_aware():
    ch = _src("app/ui/charts.py")
    assert "def daily_stacked_usd(df: pd.DataFrame, day_col: str, category_col: str, usd_col: str,\n                      takeaway: bool = True, *, currency: str = \"USD\")" in ch
    assert '_is_usd = str(currency or "USD").upper() == "USD"' in ch
    contract = _src("app/ui/pages/cost_parts/contract.py")
    assert "currency=currency)" in contract


# --- ASK-G1 (LOW): number guard binds percentages to their role ---------------------
def test_numbers_preserved_binds_percent_role():
    # the window token '30' must NOT license a wrong '30%' when the grounded share is 60%
    grounded = "Over the last 30d, USER_A is the top spender: 900 credits (60% of named-user spend)."
    assert _numbers_preserved(grounded, "USER_A drove 900 credits, 60% of spend.") is True
    assert _numbers_preserved(grounded, "USER_A drove 900 credits, about 30% of spend.") is False
    # a genuinely new number is still rejected by the flat-set check
    assert _numbers_preserved(grounded, "USER_A drove 950 credits.") is False


# --- ASK-G3 (LOW): spend answerer discloses a truncated % base ----------------------
def test_spend_answerer_discloses_truncated_base():
    reg = _src("app/logic/ask/registry.py")
    assert "_capped = len(df) >= 100" in reg
    assert "the top {len(d)} named users' spend" in reg


# --- FBK-2 (LOW): live sizing p95 uses the same peak-daily basis as the mart --------
def test_live_sizing_profile_uses_peak_daily_p95():
    isql = _src("app/data/insights_sql.py")
    assert "day_p95 AS (" in isql and "MAX(DAY_P95) AS P95_ELAPSED_SEC" in isql
    assert "COALESCE(MAX(P.P95_ELAPSED_SEC), 0) AS P95_ELAPSED_SEC" in isql
