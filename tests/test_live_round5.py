"""Locks for the 2026-07-10 live findings, round 5 (v4.12.1).

The V027 loader's role/schema arms never loaded (hourly GROUP BY failures);
V029 heals them via the derived-proc chain. The compile-heavy reader nested
aggregates through Snowflake's alias shadowing — every at-risk reader is now
fully qualified. Multiselect chips are readable; Heaviest queries carries
the date.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart27_sql, ops_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG28 = (_ROOT / "snowflake" / "migrations" / "V028__cred_expiry_10d.sql").read_text(encoding="utf-8")
_MIG29 = (_ROOT / "snowflake" / "migrations" / "V029__loader_fix.sql").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# V029 — the loader hotfix
# ---------------------------------------------------------------------------

def test_v029_guard_and_version():
    assert "EXCEPTION (-20029" in _MIG29
    assert "IF (v < 28) THEN" in _MIG29
    assert "SELECT 29 AS VERSION" in _MIG29
    assert "CREATE TABLE" not in _MIG29.upper()        # proc replacement, not surgery
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);" in _MIG29


def _proc(text: str) -> str:
    start = text.find("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    assert start > 0
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def test_v029_proc_is_v028_verbatim_except_the_two_group_by_fixes():
    expected = (
        _proc(_MIG28)
        .replace("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(COALESCE(WAREHOUSE_NAME, '')) AS COMPANY,",
                 "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(MAX(COALESCE(WAREHOUSE_NAME, ''))) AS COMPANY,")
        .replace("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')) AS COMPANY,",
                 "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(MAX(COALESCE(DATABASE_NAME, ''))) AS COMPANY,")
    )
    assert _proc(_MIG29) == expected                   # V027 -> V028 -> V029, zero silent drift


def test_v029_arms_aggregate_before_the_udf():
    proc = _proc(_MIG29)
    assert proc.count("COMPANY_FOR_WAREHOUSE(MAX(COALESCE(WAREHOUSE_NAME, '')))") == 1
    assert proc.count("COMPANY_FOR_DATABASE(MAX(COALESCE(DATABASE_NAME, '')))") == 1
    # the exact select items that failed hourly are gone (other arms use
    # COMPANY_FOR_* legitimately — they loaded fine all along, so the
    # negative assertion scopes to the failed "AS COMPANY," items only)
    assert "COMPANY_FOR_WAREHOUSE(COALESCE(WAREHOUSE_NAME, '')) AS COMPANY," not in proc
    assert "COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME, '')) AS COMPANY," not in proc


# ---------------------------------------------------------------------------
# Alias-shadow discipline: qualified aggregates in the wave-2 readers
# ---------------------------------------------------------------------------

def test_family_readers_are_fully_qualified():
    comp = mart27_sql.family_compile_heavy(7, "ALFA")
    assert "SUM(f.RUNS)" in comp and "f.COMPILE_MS_AVG * f.RUNS" in comp
    assert "SUM(RUNS)" not in comp                     # the nested-aggregate trigger, gone
    assert "MART_QUERY_FAMILY_DAILY} f" not in comp    # sanity: alias landed in rendered SQL
    assert " f\nWHERE" in comp
    rq = mart27_sql.family_repeat_fingerprints(7, "ALFA")
    assert "SUM(f.RUNS)" in rq and "SUM(RUNS)" not in rq


def test_sizing_and_ai_readers_are_fully_qualified():
    prof = mart27_sql.eff_sizing_profile(7, "ALFA")
    assert "SUM(e.CREDITS_TOTAL)" in prof
    assert "SUM(CREDITS_TOTAL)" not in prof            # alias shadowed this before it fired
    assert "e.COMPANY = 'ALFA'" in prof
    ai = mart27_sql.ai_costs_by_model(7)
    assert "SUM(COALESCE(a.TOKENS, 0))" in ai
    assert "SUM(COALESCE(TOKENS, 0))" not in ai


# ---------------------------------------------------------------------------
# UI: readable chips + the date column
# ---------------------------------------------------------------------------

def test_multiselect_chips_are_readable():
    theme = (_ROOT / "app" / "theme.py").read_text(encoding="utf-8")
    seg = theme.split('.stMultiSelect [data-baseweb="tag"]', 1)
    assert len(seg) == 2                               # chip styling exists
    assert "rgba(56,189,248,0.16)" in theme            # dark chip, not the pale wash
    assert 'tag"] span { color:#dbeafe' in theme       # readable text


def test_heaviest_queries_carry_the_date():
    sql = ops_sql.top_queries_by_elapsed(7, "ALFA")
    assert "START_TIME," in sql                        # in the builder
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    body = ops.split("Heaviest queries", 1)[1]
    assert '"START_TIME", "USER_NAME"' in body         # first column in the table
    assert 'DatetimeColumn("Started"' in body


def test_design_doc_numbering_followed_the_hotfix():
    d27 = (_ROOT / "docs" / "design" / "V027_MART_FAMILY.md").read_text(encoding="utf-8")
    assert "V029 became the" in d27                    # slot taken by the loader fix
    assert "~V031" in d27                              # owner registry shifted
