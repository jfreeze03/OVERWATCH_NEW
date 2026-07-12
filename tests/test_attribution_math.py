"""Attribution math locks (v4.33.1) — the live findings from Joe's
screenshots: a database filter made the selected database 'cost' the whole
window, mart and live paths dollarized differently, and USER$SSLONSKY (a
Trexis user's personal database) showed under ALFA."""

from __future__ import annotations

from pathlib import Path

from app import companies
from app.data import cost_sql, mart27_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_shares_are_global_filters_only_choose_display_rows():
    sql = cost_sql.allocated_attribution(7, "DATABASE_NAME", "ALFA",
                                         database="ALFA_EDW_PRD")
    scoped_cte = sql.split("WITH scoped AS", 1)[1].split("\n)\n", 1)[0]
    assert "ALFA_EDW_PRD" not in scoped_cte              # filter NOT in the denominator
    tail = sql.split("FROM scoped", 1)[1]
    assert "ALFA_EDW_PRD" in tail                        # filter picks display rows
    assert "(SELECT SUM(ELAPSED_MS) FROM scoped)" in sql # global denominator
    assert "RATIO_TO_REPORT" not in sql


def test_mart_reader_obeys_the_same_global_share_law():
    sql = mart27_sql.alloc_attribution(30, "DATABASE", "ALFA")
    assert "(SELECT SUM(ALLOC_CREDITS) FROM scoped)" in sql
    assert "RATIO_TO_REPORT" not in sql
    scoped_cte = sql.split("WITH scoped AS", 1)[1].split("\n)\n", 1)[0]
    assert "USER$" not in scoped_cte                     # visibility outside the denominator
    assert "USER$" in sql.split("FROM scoped", 1)[1]     # but applied to display rows


def test_personal_databases_attribute_to_their_owner():
    alfa = companies.database_visibility_clause("ALFA")
    assert "SUBSTR(DATABASE_NAME, 6)" in alfa            # USER$<owner> -> owner
    assert "COMPANY_FOR_USER" in alfa
    assert "TRXS!_%" in alfa                             # other company's dbs excluded
    assert "IS NULL" in alfa and "'NONE'" in alfa        # no-database activity stays
    trx = companies.database_visibility_clause("Trexis", "KEY_NAME")
    assert "= 'Trexis'" in trx and "KEY_NAME" in trx
    assert companies.database_visibility_clause("ALL") == ""


def test_ui_uses_one_formula_on_every_path():
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "dollarize allocated credits directly" not in sp
    assert sp.count('alloc["ALLOCATED_USD"] = alloc["ELAPSED_SHARE"]') == 1
    assert "Shares stay global" in sp                    # the caption says the law
    assert "owner's company" in sp
