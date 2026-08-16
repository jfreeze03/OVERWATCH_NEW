"""rec#24: least-privilege — held table grants vs. what queries actually used.

Locks the pure verdict logic (logic/least_privilege.py), the two SQL builders'
shape (object-id bridge, not string matching), and the Security "Least privilege"
tab wiring — including the safeguard that suppresses verdicts when ACCESS_HISTORY
yields no usage evidence.
"""

from pathlib import Path

import pandas as pd

from app.data import security_sql
from app.logic.least_privilege import (
    SCOPE_COLUMNS,
    classify_grant_scopes,
    summarize_scopes,
)

_ROOT = Path(__file__).resolve().parents[1]
_SEC = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")


def _scopes(rows):
    return pd.DataFrame(
        rows,
        columns=["ROLE_NAME", "DATABASE_NAME", "SCHEMA_NAME",
                 "GRANTED_TABLES", "TOUCHED_TABLES"],
    )


# --- classify_grant_scopes -------------------------------------------------

def test_empty_or_none_returns_empty_schema():
    for frame in (None, pd.DataFrame()):
        out = classify_grant_scopes(frame)
        assert out.empty
        assert list(out.columns) == list(SCOPE_COLUMNS)


def test_verdicts_by_exercised_share():
    frame = _scopes([
        ("R1", "DB", "S_UNUSED", 5, 0),     # touched none -> UNUSED
        ("R1", "DB", "S_BROAD", 12, 3),     # 3 <= floor(12/3)=4 -> OVER-BROAD
        ("R1", "DB", "S_EDGE", 12, 4),      # 4 <= 4 -> OVER-BROAD (boundary)
        ("R1", "DB", "S_FOCUS", 12, 5),     # 5 > 4 -> FOCUSED
    ]).copy()
    out = classify_grant_scopes(frame).set_index("SCHEMA_NAME")
    assert out.loc["S_UNUSED", "VERDICT"] == "UNUSED"
    assert out.loc["S_BROAD", "VERDICT"] == "OVER-BROAD"
    assert out.loc["S_EDGE", "VERDICT"] == "OVER-BROAD"
    assert out.loc["S_FOCUS", "VERDICT"] == "FOCUSED"
    assert int(out.loc["S_UNUSED", "UNUSED_TABLES"]) == 5


def test_small_footprint_is_not_over_broad_unless_fully_unused():
    # below min_granted, a partly-used scope is FOCUSED, not OVER-BROAD ...
    assert classify_grant_scopes(_scopes([("R", "DB", "S", 3, 1)])).loc[0, "VERDICT"] == "FOCUSED"
    # ... but touching NONE is always UNUSED regardless of footprint size
    assert classify_grant_scopes(_scopes([("R", "DB", "S", 3, 0)])).loc[0, "VERDICT"] == "UNUSED"


def test_touched_is_clamped_to_granted():
    # the two legs are read separately; touched must never exceed granted
    out = classify_grant_scopes(_scopes([("R", "DB", "S", 5, 8)]))
    assert int(out.loc[0, "TOUCHED_TABLES"]) == 5
    assert int(out.loc[0, "UNUSED_TABLES"]) == 0
    assert float(out.loc[0, "USED_PCT"]) == 100.0
    assert out.loc[0, "VERDICT"] == "FOCUSED"


def test_sort_puts_unused_first_then_over_broad():
    frame = _scopes([
        ("R", "DB", "S_FOCUS", 12, 10),
        ("R", "DB", "S_UNUSED", 4, 0),
        ("R", "DB", "S_BROAD", 12, 2),
    ])
    out = classify_grant_scopes(frame)
    assert list(out["SCHEMA_NAME"]) == ["S_UNUSED", "S_BROAD", "S_FOCUS"]


def test_threshold_tunable():
    frame = _scopes([("R", "DB", "S", 10, 4)])   # 40% used
    assert classify_grant_scopes(frame, narrow_ratio=0.5).loc[0, "VERDICT"] == "OVER-BROAD"
    assert classify_grant_scopes(frame, narrow_ratio=0.3).loc[0, "VERDICT"] == "FOCUSED"


# --- summarize_scopes ------------------------------------------------------

def test_summarize_counts():
    out = classify_grant_scopes(_scopes([
        ("R1", "DB", "A", 5, 0),
        ("R1", "DB", "B", 12, 3),
        ("R2", "DB", "C", 8, 8),
    ]))
    stats = summarize_scopes(out)
    assert stats["roles"] == 2
    assert stats["scopes"] == 3
    assert stats["unused"] == 1
    assert stats["over_broad"] == 1
    assert stats["unused_tables"] == 5 + 9 + 0


def test_summarize_empty():
    assert summarize_scopes(pd.DataFrame())["scopes"] == 0
    assert summarize_scopes(None)["unused_tables"] == 0


# --- SQL builders ----------------------------------------------------------

def test_scope_usage_bridges_via_object_id_not_string():
    sql = security_sql.grant_scope_usage(90)
    assert "SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS" in sql
    # touched set keyed on the numeric object id (D4 lesson), both read and write
    assert 'f.value:"objectId"' in sql
    assert "BASE_OBJECTS_ACCESSED" in sql and "OBJECTS_MODIFIED" in sql
    assert "ON u.OBJECT_ID = t.OBJECT_ID" in sql
    # only live tables, only current grants, only data privileges
    assert "DELETED = FALSE" in sql
    assert "DELETED_ON IS NULL" in sql
    assert "GRANTED_ON = 'TABLE'" in sql
    # only ACCESS_HISTORY-observable privileges — REFERENCES (FK DDL) and TRUNCATE
    # (metadata) leave no trace and would ALWAYS read as "unused"
    for priv in ("'SELECT'", "'INSERT'", "'UPDATE'", "'DELETE'"):
        assert priv in sql
    assert "'REFERENCES'" not in sql and "'TRUNCATE'" not in sql
    # usage collapsed to table NAME (MAX over ids) so duplicate DELETED=FALSE ids
    # from a drop->recreate can neither double-count nor split a used table
    assert "MAX(IFF(u.OBJECT_ID IS NULL, 0, 1))" in sql


def test_unused_grants_lists_untouched_objects():
    sql = security_sql.unused_table_grants(90)
    # a name is listed only when NONE of its ids was touched (drop->recreate safe)
    assert "HAVING MAX(IFF(u.OBJECT_ID IS NULL, 0, 1)) = 0" in sql
    assert "ANY_VALUE(t.FQN) AS OBJECT_NAME" in sql
    assert 'f.value:"objectId"' in sql
    assert "'REFERENCES'" not in sql and "'TRUNCATE'" not in sql


def test_access_evidence_days_probe():
    sql = security_sql.access_evidence_days()
    assert "SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY" in sql
    # density, not span: distinct days carrying access rows (a stray old row can't inflate)
    assert "COUNT(DISTINCT DATE(a.QUERY_START_TIME)) AS COVERAGE_DAYS" in sql
    # recency: staleness of the newest row (catches ACCESS_HISTORY that stopped populating)
    assert "DATEDIFF('day', MAX(a.QUERY_START_TIME), CURRENT_TIMESTAMP()) AS LATEST_DAYS_AGO" in sql
    assert "DATEADD('day', -90," in sql
    assert "MIN(a.QUERY_START_TIME)" not in sql   # the fragile span measure is gone


def test_builders_bound_the_window():
    # a hostile window clamps to the 90-day ACCESS_HISTORY horizon, never interpolated raw
    assert "-99999" not in security_sql.grant_scope_usage(99999)
    assert "DATEADD('day', -90," in security_sql.grant_scope_usage(99999)
    assert "-99999" not in security_sql.unused_table_grants(99999)


# --- tab wiring + safeguard ------------------------------------------------

def test_least_privilege_tab_wired():
    assert "def _least_privilege_tab(" in _SEC
    assert '"Least privilege"' in _SEC
    assert "classify_grant_scopes(" in _SEC
    assert "grant_scope_usage(" in _SEC


def test_thin_or_absent_history_suppresses_verdicts():
    # the critical safeguard: verdicts are gated on ACCESS_HISTORY density AND recency,
    # not merely non-empty, so neither a recently-enabled account (thin) nor one whose
    # history stopped populating (stale) can read every grant as unused.
    assert "access_evidence_days(" in _SEC
    assert "coverage_days" in _SEC
    assert "_MIN_COVERAGE_DAYS" in _SEC
    assert "_MAX_STALENESS_DAYS" in _SEC
    assert "latest_days_ago" in _SEC
    assert "have_evidence" in _SEC
