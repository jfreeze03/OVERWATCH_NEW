"""Perf lock (v4.443.0): cost_sql.tag_coverage scopes company by the once-per-distinct-user
membership form (companies.user_scope_subquery), NOT the per-row COMPANY_FOR_USER(USER_NAME).

COMPANY_FOR_USER's body does an EXISTS against SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS, so the
per-row user_clause form was a correlated ACCOUNT_USAGE lookup for every row of a raw
QUERY_HISTORY scan (up to 90d) — the exact cost companies.user_scope_subquery (PERF #15) exists
to remove by evaluating the UDF once per DISTINCT user. This is the primary chargeback path when
a database/schema filter + a specific company are active (cost.py). Byte-equivalent + leak-safe;
mirrors the sibling live builder allocated_attribution. (perf audit 2026-09-02)
"""

from __future__ import annotations

import sqlglot

from app.data import cost_sql


def test_tag_coverage_uses_membership_subquery_not_per_row_udf():
    sql = cost_sql.tag_coverage(30, "ALFA", database="ALFA_EDW_PRD", schema_contains="PUBLIC")
    flat = " ".join(sql.split())
    # membership form: a DISTINCT-user subquery filtered by COMPANY_FOR_USER, fed into USER_NAME IN (...)
    assert "USER_NAME IN (SELECT" in flat
    head, _, tail = flat.partition("USER_NAME IN (SELECT")
    # the UDF is applied ONCE PER DISTINCT USER inside the subquery — never per-row on the main scan
    assert "COMPANY_FOR_USER" not in head, "per-row COMPANY_FOR_USER leaked back onto the main scan"
    assert "SELECT DISTINCT USER_NAME" in tail and "COMPANY_FOR_USER(USER_NAME)" in tail
    sqlglot.parse_one(sql, read="snowflake")


def test_tag_coverage_all_scope_adds_no_user_filter():
    sql = cost_sql.tag_coverage(30, "ALL")
    assert "COMPANY_FOR_USER" not in sql          # ALL => no per-company user scope (unchanged)
    sqlglot.parse_one(sql, read="snowflake")


def test_tag_coverage_distinct_window_matches_the_scan_window():
    """The subquery's distinct-user window must be the SAME START_TIME predicate the outer
    scan uses (a superset of its filters) so no in-scope user is dropped."""
    # trailing window: both the outer scan and the distinct subquery clamp to -30 days
    sql = cost_sql.tag_coverage(30, "Trexis")
    assert sql.count("DATEADD('day', -30") >= 2      # outer WHERE + the distinct-user subquery
    sqlglot.parse_one(sql, read="snowflake")
