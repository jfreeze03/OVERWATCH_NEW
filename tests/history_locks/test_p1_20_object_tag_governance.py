"""Upgrade Board P1 #20 — object-tag governance coverage.

Locks the builder contract (TABLES ⋈ TAG_REFERENCES, TABLE domain, whitelisted
tag literals, company scoping, sqlglot round-trip), the pure coverage scorer
(empty != 100/Healthy, worst-tag ranked first), and the UI wiring into the
Security ▸ Decision-queue governance block.
"""

from __future__ import annotations

from pathlib import Path

import sqlglot

from app import companies
from app.data import security_sql
from app.logic.governance import tag_coverage_score

_ROOT = Path(__file__).resolve().parents[2]


def test_object_tag_coverage_builder_contract():
    sql = security_sql.object_tag_coverage("ALFA")
    assert "SNOWFLAKE.ACCOUNT_USAGE.TABLES" in sql          # verified denominator
    assert "SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES" in sql  # tag-assignment side
    assert "r.DOMAIN = 'TABLE'" in sql                      # TABLE domain only (v1)
    assert "t.TABLE_TYPE = 'BASE TABLE'" in sql             # honest inventory
    assert "NULLIF(COUNT(*), 0)" in sql                     # no divide-by-zero
    for key in ("COST_OWNER", "SENSITIVITY", "SERVICE_TIER", "APP_OWNER"):
        assert f"'{key}'" in sql, key                       # all four tag literals
    # company scope threads the verified database_clause on TABLE_CATALOG
    assert companies.database_clause("ALFA", "t.TABLE_CATALOG") in sql
    assert security_sql.object_tag_coverage("ALFA") != security_sql.object_tag_coverage("ALL")
    # ALL injects no company predicate (database_clause('ALL') is empty)
    assert companies.database_clause("ALFA", "t.TABLE_CATALOG") not in \
        security_sql.object_tag_coverage("ALL")


def test_untagged_objects_builder_contract():
    sql = security_sql.untagged_objects("Trexis", "SENSITIVITY", limit=50)
    assert "g.OBJECT_NAME IS NULL" in sql                   # the gap: no matching tag
    assert "'SENSITIVITY'" in sql and sql.count("UPPER(r.TAG_NAME) =") == 1  # one key
    assert "LIMIT 50" in sql
    assert companies.database_clause("Trexis", "t.TABLE_CATALOG") in sql
    assert "ORDER BY t.BYTES DESC" in sql                   # biggest untagged first
    # limit is clamped, never interpolated raw
    assert "LIMIT 1000" in security_sql.untagged_objects("ALL", "COST_OWNER", limit=99999)


def test_object_tag_coverage_dedups_case_variant_keys():
    # A duplicate / case-variant tag key must NOT fan out the keys CTE (which would
    # double every TOTAL/TAGGED). Dedup collapses them to one literal each.
    sql = security_sql.object_tag_coverage("ALL", tags=("COST_OWNER", "cost_owner", "Cost_Owner"))
    assert sql.count("SELECT 'COST_OWNER' AS TAG_NAME") == 1
    assert "SELECT DISTINCT t.TABLE_CATALOG" in sql   # distinct-FQN denominator


def test_object_tag_probe_is_cheap():
    p = security_sql.object_tag_probe()
    assert "TAG_REFERENCES" in p and "LIMIT 1" in p


def test_tag_builders_parse_snowflake():
    for sql in (security_sql.object_tag_coverage("ALFA"),
                security_sql.object_tag_coverage("ALL"),
                security_sql.untagged_objects("ALFA", "APP_OWNER"),
                security_sql.object_tag_probe()):
        assert sqlglot.parse_one(sql, read="snowflake") is not None


def test_tag_coverage_score_empty_is_no_data_not_100():
    # C8: an empty inventory must NOT read as a perfect 100/Healthy.
    out = tag_coverage_score([])
    assert out.state == "No data" and out.score == 0 and out.drivers == ()
    # recs present but zero tables in scope -> still No data, not 100.
    zero = tag_coverage_score([{"TAG_NAME": "COST_OWNER", "TOTAL": 0, "TAGGED": 0,
                                "COVERAGE_PCT": None}])
    assert zero.state == "No data" and zero.score == 0


def test_tag_coverage_score_ranks_worst_tag_first():
    rows = [
        {"TAG_NAME": "COST_OWNER", "TOTAL": 100, "TAGGED": 100, "COVERAGE_PCT": 100.0},
        {"TAG_NAME": "SENSITIVITY", "TOTAL": 100, "TAGGED": 0, "COVERAGE_PCT": 0.0},
        {"TAG_NAME": "SERVICE_TIER", "TOTAL": 100, "TAGGED": 40, "COVERAGE_PCT": 40.0},
    ]
    out = tag_coverage_score(rows)
    assert out.score == round((100 + 0 + 40) / 300 * 100)   # pooled coverage = 47
    assert out.state == "Act"                                # < 75
    # fully-covered COST_OWNER produces NO driver; the two gaps do, worst-first.
    assert [d.driver for d in out.drivers] == ["SENSITIVITY", "SERVICE_TIER"]
    assert "0% covered" in out.drivers[0].evidence


def test_tag_coverage_score_uniform_full_is_healthy_no_drivers():
    rows = [{"TAG_NAME": k, "TOTAL": 50, "TAGGED": 50, "COVERAGE_PCT": 100.0}
            for k in ("COST_OWNER", "SENSITIVITY", "SERVICE_TIER", "APP_OWNER")]
    out = tag_coverage_score(rows)
    assert out.score == 100 and out.state == "Healthy" and out.drivers == ()


def test_tag_governance_wired_into_security_decision_queue():
    src = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "def _tag_governance_panel(" in src
    assert "object_tag_coverage(" in src and "tag_coverage_score(" in src
    assert "object_tag_probe()" in src and "probe=True" in src
    # the panel is called in the Decision-queue governance block, after posture trend
    dq = src.split("_posture_trend_panel(_post90)", 1)[1]
    assert "_tag_governance_panel(f[\"company\"])" in dq.split("elif section ==", 1)[0]
    # honest empty branch, not a false clean bill
    assert "No base tables in scope to measure tag coverage against." in src
