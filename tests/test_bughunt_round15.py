"""Regression locks for the round-15 hunt (v4.438.0).

  MC-2   alloc_xdim mart twin scopes company by the COMPANY_SCOPE-aware UDF, not name pattern
  INC-1  manual incident declare pre-checks the family-open guard, so a duplicate reports honestly
  AI-HYP the alert AI-hypothesis append is idempotent (no duplicate on a commit-then-retry)
"""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- MC-2: mart twin uses the COMPANY_FOR_WAREHOUSE UDF axis, matching the live twin ----
def test_alloc_xdim_scopes_by_company_udf_not_name_pattern():
    sql = mart27_sql.alloc_xdim_attribution(30, "USER", "ALFA")
    assert "COMPANY_FOR_WAREHOUSE(x.WAREHOUSE_NAME) = 'ALFA'" in sql
    assert "WH!_ALFA!_%" not in sql and "WH_ALFA_%" not in sql        # name pattern gone
    sqlglot.parse_one(sql, read="snowflake")
    # both the share denominator AND the coverage probe use the UDF
    assert sql.count("COMPANY_FOR_WAREHOUSE(x.WAREHOUSE_NAME) = 'ALFA'") == 2
    # ALL keeps no company filter
    assert "COMPANY_FOR_WAREHOUSE" not in mart27_sql.alloc_xdim_attribution(30, "USER", "ALL")


# --- INC-1: declare pre-checks the SAME family-open predicate the guard applies ---------
def test_incident_declare_pre_checks_family_open():
    from app.ui.pages import control_room as cr

    key = "fam|x|WAREHOUSE|WH1"
    chk = cr._incident_family_open_check_sql("ALFA", key)
    dec = cr._incident_declare_sql("t", "HIGH", "ALFA", key)
    assert "SELECT EXISTS (" in chk and "AS ALREADY_OPEN" in chk
    # the shared family(+entity) predicate appears in BOTH the check and the declare guard
    frag = "SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1)"
    assert frag in chk and frag in dec[0]
    assert "UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2))" in chk  # entity too
    sqlglot.parse_one(chk, read="snowflake")
    # the flow runs the check first and only claims "declared" in the else branch
    src = _src("app/ui/pages/control_room.py")
    assert '_incident_family_open_check_sql(str(_prow["COMPANY"]), _pick)' in src
    assert 'ALREADY_OPEN")' in src
    assert "No new incident — this family already has an open" in src


# --- AI-HYP: the alert-hypothesis append cannot duplicate on a retry --------------------
def test_alert_hypothesis_append_is_idempotent():
    a = _src("app/ui/pages/alerts.py")
    assert "NOT CONTAINS(COALESCE(DETAIL, ''), " in a
    assert "' | AI hypothesis: ' + answer[:800]" in a
