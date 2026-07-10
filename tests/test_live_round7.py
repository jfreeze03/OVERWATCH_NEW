"""Locks for the tuning trio (v4.14.0, owner-approved 2026-07-10).

Change-impact scan v2 bounds its joins to the oldest still-tracking change
and pre-filters with ILIKE before the expensive text normalization; the
tag-coverage mart closes wave 2's last honest non-adoption; lock triage
caps at a week.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.data import mart27_sql, ops_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG10 = (_ROOT / "snowflake" / "migrations" / "V010__change_impact.sql").read_text(encoding="utf-8")
_MIG31 = (_ROOT / "snowflake" / "migrations" / "V031__scan_tuning_and_tagcov.sql").read_text(encoding="utf-8")


def _scan(text: str) -> str:
    start = text.find("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN")
    assert start > 0
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def test_v031_guard_version_and_first_fill():
    assert "EXCEPTION (-20031" in _MIG31
    assert "IF (v < 30) THEN" in _MIG31
    assert "SELECT 31 AS VERSION" in _MIG31
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);" in _MIG31
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY" in _MIG31


def test_scan_v2_is_v010_with_the_enumerated_edits():
    scan31 = _scan(_MIG31)
    # tracking-window variable exists and feeds every after-window join
    assert "trk_lo TIMESTAMP_NTZ;" in scan31
    assert "SELECT COALESCE(MIN(CHANGE_SEEN_AT), CURRENT_TIMESTAMP()) INTO :trk_lo" in scan31
    assert scan31.count("GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)") == 5
    assert "DATEADD('day', -18, CURRENT_TIMESTAMP())\n" not in scan31   # no unbounded -18 remains
    # ILIKE pre-filter rides in front of every POSITION text match
    assert scan31.count("AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'") == 4
    assert scan31.count("AND POSITION(SPLIT_PART(r.OBJECT_NAME, '.', 3)") == 4
    # nothing else drifted: strip the v2 edits and recover V010's scan
    reverted = scan31.replace(
        "GREATEST(DATEADD('day', -18, CURRENT_TIMESTAMP()), :trk_lo)",
        "DATEADD('day', -18, CURRENT_TIMESTAMP())")
    reverted = re.sub(r"^\s+AND q\.QUERY_TEXT ILIKE '%' \|\| SPLIT_PART\(r\.OBJECT_NAME, '\.', 3\) \|\| '%'\n",
                      "", reverted, flags=re.M)
    reverted = reverted.replace(
        """    trk_lo TIMESTAMP_NTZ;      -- v2: oldest still-tracking change (prunes the scans)
    emsg VARCHAR;
BEGIN
    -- v2 (2026-07-10 tuning): the after-window joins used a blanket -18d
    -- bound even when only fresh changes were tracking. Bound them to the
    -- oldest ACTIVE row instead — nothing tracking means near-zero scan.
    SELECT COALESCE(MIN(CHANGE_SEEN_AT), CURRENT_TIMESTAMP()) INTO :trk_lo
    FROM DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY
    WHERE CURRENT_DATE() <= TRACKING_UNTIL;""",
        "    emsg VARCHAR;\nBEGIN")
    assert reverted == _scan(_MIG10)


def test_tag_arm_obeys_the_v030_shape_law():
    assert "MART_TAG_COVERAGE_DAILY t" in _MIG31
    assert "COMPANY_FOR_USER(g.USER_NAME)" in _MIG31           # UDF outside the aggregation
    assert "COMPANY_FOR_USER(MAX(" not in _MIG31               # never the V029 mistake again
    assert "'MART_TAG_COVERAGE_DAILY - other marts unaffected'" in _MIG31
    # freshness view re-emitted with the 17th arm
    view = _MIG31.split("MART_SOURCE_FRESHNESS AS", 1)[1].split("CALL DBA_MAINT_DB", 1)[0]
    assert view.count("UNION ALL") == 16
    assert "'MART_TAG_COVERAGE_DAILY'" in view
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_TAG_COVERAGE_DAILY;" in teardown


def test_tag_reader_matches_live_contract_and_is_adopted():
    sql = mart27_sql.tag_coverage_daily(30, "ALFA")
    for col in ("USER_NAME", "EXEC_SEC", "UNTAGGED_EXEC_SEC", "TAGGED_PCT", "QUERIES"):
        assert col in sql, col
    assert "SUM(c.EXEC_SEC)" in sql and "SUM(EXEC_SEC)" not in sql   # qualified (alias-shadow rule)
    assert "HAVING SUM(c.EXEC_SEC) > 60" in sql                      # same floor as live
    assert "c.COMPANY = 'ALFA'" in sql
    cost = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    assert "mart27_sql.tag_coverage_daily" in cost
    assert "cost_sql.tag_coverage" in cost                           # live fallback kept
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "mart27_sql.tag_coverage_daily" in canary


def test_lock_contention_caps_at_a_week():
    sql = ops_sql.lock_contention(30)
    assert "-7," in sql and "-30," not in sql and "-14," not in sql
