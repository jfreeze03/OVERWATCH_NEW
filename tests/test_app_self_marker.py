"""Next-Fifty #7 Slice A (v4.589.0): ONE definition of "OVERWATCH's own traffic". Owner's-rights SiS rejects
ALTER SESSION, so the app's queries are not session-tagged; every self-noise predicate and the Admin self-cost
split now key on a shared helper that accepts EITHER a QUERY_TAG or an appended SQL marker comment (the
per-statement transport lands in Slice B after the owner's probe). The helpers render the legacy bytes, so
the old locks and the DB-side V147 collector keep working."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import config
from app.data import chatter_sql, common, insights_sql, mart_sql, ops_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_marker_constants():
    assert common.APP_QUERY_TAG_LIKE == config.APP_QUERY_TAG_PREFIX + "%"
    assert "OVERWATCH_APP" in common.APP_SQL_MARKER          # the legacy '%OVERWATCH_APP%' filters + V147 catch it
    for bad in ("*", ";", chr(92)):
        assert bad not in common.APP_SQL_MARKER


def test_exact_renders():
    # the marker is spelled as a SPLIT constant so these builders' own statements never self-match;
    # SiS's own app tag (owner diagnostic 2026-09-24) is the primary signal
    sis = '\'"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"\''
    assert sis == common.APP_SIS_TAG_SQL
    assert common.app_self_sql() == ("(QUERY_TAG LIKE 'OVERWATCH%' OR CONTAINS(QUERY_TAG, " + sis + ") "
                                     "OR CONTAINS(QUERY_TEXT, '/* OVERWATCH' || '_APP|'))")
    assert common.app_self_sql("Q", text=False) == ("(Q.QUERY_TAG LIKE 'OVERWATCH%' "
                                                   "OR CONTAINS(Q.QUERY_TAG, " + sis + "))")
    assert common.not_app_self_sql("q") == ("(COALESCE(q.QUERY_TAG, '') NOT LIKE 'OVERWATCH%' AND NOT "
                                            "CONTAINS(COALESCE(q.QUERY_TAG, ''), " + sis + ") AND NOT "
                                            "CONTAINS(COALESCE(q.QUERY_TEXT, ''), '/* OVERWATCH' || '_APP|'))")
    assert common.not_app_self_sql(text=False) == ("(COALESCE(QUERY_TAG, '') NOT LIKE 'OVERWATCH%' AND NOT "
                                                   "CONTAINS(COALESCE(QUERY_TAG, ''), " + sis + "))")


def test_sis_tag_fragment_names_the_deployed_app():
    # the fragment must track snowflake.yml's Streamlit identifier, or the app stops recognising itself
    yml = (_ROOT / "snowflake.yml").read_text(encoding="utf-8")
    ident = yml.split("identifier:", 1)[1]
    assert f"name: {config.APP_STREAMLIT_NAME}" in ident and f"database: {config.OVERWATCH_DB}" in ident
    assert f"schema: {config.CORE_SCHEMA}" in ident
    assert config.APP_SIS_QUERY_TAG_FRAGMENT == '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"'
    assert "'" not in config.APP_SIS_QUERY_TAG_FRAGMENT                  # safe as a SQL literal


def test_every_self_noise_builder_carries_the_marker_leg():
    for sql in (ops_sql.query_opportunity_fingerprints(30), ops_sql.query_optimization_triage(30),
                ops_sql.proc_sla_rollup(30), ops_sql.proc_regression(30),
                chatter_sql.chatter_by_application(), chatter_sql.chatter_families_for_application("APP"),
                insights_sql.repeat_query_fingerprints(30), mart_sql.app_self_cost(14),
                mart_sql.app_statement_stats(7), mart_sql.app_warehouse_queue_by_hour(14)):
        assert "'/* OVERWATCH' || '_APP|'" in sql
        assert common.APP_SIS_TAG_SQL in sql                          # SiS's app tag excludes/claims it
        # review fix: the contiguous marker never appears in a builder's own text, or QUERY_HISTORY would
        # count the diagnostic builder itself as 'INTERACTIVE APP' traffic
        assert common.APP_SQL_MARKER_OPEN not in sql
    # detect_release_days stays tag-only by design (no new 180-day QUERY_TEXT read) - but tag-only now
    # includes SiS's app tag
    assert "_APP|" not in insights_sql.detect_release_days(30)
    assert common.APP_SIS_TAG_SQL in insights_sql.detect_release_days(30)


def test_one_source_of_truth():
    for mod in (ops_sql, chatter_sql, insights_sql, mart_sql):
        assert inspect.getsource(mod).count("LIKE 'OVERWATCH%'") == 0, mod.__name__


def test_marked_builders_parse():
    sqlglot = pytest.importorskip("sqlglot")
    for sql in (mart_sql.app_self_cost(14), ops_sql.query_optimization_triage(30)):
        sqlglot.parse_one(sql, read="snowflake")


def test_admin_note_is_honest():
    src = (_ROOT / "app/ui/pages/admin.py").read_text(encoding="utf-8")
    assert "carries an OVERWATCH query tag" not in src
    body = src.split("def _self_cost_tab(", 1)[1].split("\ndef ", 1)[0]
    # owner diagnostic 2026-09-24: SiS overrides the per-statement tag and stamps its own - the note says so
    assert "rejects ALTER SESSION" in body and "statement_params" in body and "StreamlitName" in body
    assert "mart_sql.APP_OTHER_WORKLOAD" in body and "UNTAGGED APP" not in src
    assert "mart_sql.APP_RUNTIME_WORKLOAD" in body and "APP_FETCH_WORKLOAD" not in src
    assert "_TAGGED_SINCE" not in src                                 # nothing claims our tag lands
