"""Next-Fifty #20 (v4.589.0): the action queue names a person, honors deferral, and dates decisions by
when they were made. A team label such as 'DBA' names nobody (counted Unassigned); an owner picker seeded
from OPERATOR_USERS replaces the free-text 'DBA' default; 'Assigned to me' filters the Action Center;
DEFER_UNTIL drops parked items from counts and top-N (sinks them in the Action Center list); acceptance
is dated by COMPLETED_AT, not the comment-bumped UPDATED_AT; and unassign writes the 'UNASSIGNED' sentinel
because ACTION_QUEUE.OWNER is NOT NULL (V005) — V092's NULL clear fails live."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

import app.logic.actions as actions_mod
import app.logic.workbench as wb
from app.data import mart_sql
from app.logic.actions import deferred_mask, deferred_summary, rank_actions

_ROOT = Path(__file__).resolve().parents[1]
_TODAY = pd.Timestamp("2026-09-24")


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_deferred_mask_future_only():
    df = pd.DataFrame({"STATUS": ["OPEN", "OPEN", "OPEN", "DONE", "IN_PROGRESS"],
                       "DEFER_UNTIL": ["2026-09-24", "2026-09-25", "2026-09-23", "2026-10-30", None]})
    assert list(deferred_mask(df, _TODAY)) == [False, True, False, False, False]
    assert not deferred_mask(pd.DataFrame({"STATUS": ["OPEN"]}), _TODAY).any()
    assert deferred_mask(None, _TODAY).empty


def test_deferred_summary_counts_and_earliest_resume():
    df = pd.DataFrame({"STATUS": ["OPEN", "OPEN", "OPEN"],
                       "DEFER_UNTIL": ["2026-10-05", "2026-09-30", None]})
    assert deferred_summary(df, _TODAY) == (2, date(2026, 9, 30))
    assert deferred_summary(pd.DataFrame(), _TODAY) == (0, None)


def test_rank_actions_drops_deferred_by_default(monkeypatch):
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 24, 9, 0))
    df = pd.DataFrame([
        {"ACTION_ID": "crit", "SEVERITY": "CRITICAL", "STATUS": "OPEN", "DEFER_UNTIL": "2026-10-01",
         "CREATED_AT": "2026-09-01", "DUE_DATE": None, "ESTIMATED_USD": 0},
        {"ACTION_ID": "low", "SEVERITY": "LOW", "STATUS": "OPEN", "DEFER_UNTIL": None,
         "CREATED_AT": "2026-09-01", "DUE_DATE": None, "ESTIMATED_USD": 0},
    ])
    assert list(rank_actions(df)["ACTION_ID"]) == ["low"]
    both = rank_actions(df, include_deferred=True)
    assert list(both["ACTION_ID"]) == ["low", "crit"]            # the deferred CRITICAL sinks LAST
    assert "_DEFERRED" not in both.columns


def test_action_summary_excludes_deferred_and_counts_it(monkeypatch):
    monkeypatch.setattr(wb, "account_today", lambda: date(2026, 9, 24))
    df = pd.DataFrame([
        {"STATUS": "OPEN", "SEVERITY": "HIGH", "DUE_DATE": "2026-09-01", "OWNER": "",
         "ESTIMATED_USD": 500, "DEFER_UNTIL": "2026-10-01"},
        {"STATUS": "OPEN", "SEVERITY": "LOW", "DUE_DATE": None, "OWNER": "KEBARR1",
         "ESTIMATED_USD": 10, "DEFER_UNTIL": None},
    ])
    s = wb.action_summary(df)
    assert s == {"open": 1.0, "critical_high": 0.0, "overdue": 0.0, "unassigned": 0.0,
                 "estimated_usd": 10.0, "deferred": 1.0}


def test_team_placeholder_owners_are_unassigned():
    for v in ("DBA", "dba / ai governance", "", None, float("nan"), "UNASSIGNED", "  dba team "):
        assert wb.is_unassigned_owner(v), v
    assert not wb.is_unassigned_owner("KEBARR1")


def test_owner_choices_defaults_and_legacy():
    opts, idx = wb.owner_choices(("A", "B"), viewer="b")
    assert opts == ["(unassigned)", "A", "B", "Other…"] and idx == 2
    opts, idx = wb.owner_choices(("A", "B"), current="DBA")
    assert opts[-2] == "DBA" and opts[idx] == "DBA"             # a legacy owner stays selectable verbatim
    opts, idx = wb.owner_choices(("KEBARR1",), current="kebarr1")
    assert "kebarr1" in opts and opts[idx] == "kebarr1"          # exact spelling kept (no case-only 'reassign')
    assert wb.owner_choices(("A",), current="UNASSIGNED")[1] == 0
    opts, idx = wb.owner_choices(("A",), allow_unassigned=False)
    assert opts[idx] == "Other…"


def test_resolve_owner():
    assert wb.resolve_owner("(unassigned)") == ""
    assert wb.resolve_owner("Other…", " Joe ") == "Joe"
    assert wb.resolve_owner("A") == "A"


def test_owned_by_and_my_queue_counts(monkeypatch):
    monkeypatch.setattr(wb, "account_today", lambda: date(2026, 9, 24))
    df = pd.DataFrame([
        {"OWNER": "kebarr1", "STATUS": "OPEN", "DUE_DATE": "2026-09-24", "DEFER_UNTIL": None},   # due today: not late
        {"OWNER": "KEBARR1", "STATUS": "OPEN", "DUE_DATE": "2026-09-20", "DEFER_UNTIL": None},   # overdue
        {"OWNER": "KEBARR1", "STATUS": "OPEN", "DUE_DATE": "2026-09-20", "DEFER_UNTIL": "2026-10-01"},  # parked
        {"OWNER": "KEBARR1", "STATUS": "DONE", "DUE_DATE": "2026-09-20", "DEFER_UNTIL": None},   # closed
        {"OWNER": "CLROY", "STATUS": "OPEN", "DUE_DATE": None, "DEFER_UNTIL": None},
    ])
    assert list(wb.owned_by(df, "KeBaRr1")) == [True, True, True, True, False]
    assert wb.my_queue_counts(df, "kebarr1") == {"mine": 2, "mine_overdue": 1}
    assert wb.my_queue_counts(None, "x") == {"mine": 0, "mine_overdue": 0}


def test_create_action_sql_blank_owner_writes_sentinel():
    sqlglot = pytest.importorskip("sqlglot")
    sql = wb.create_action_sql(title="t", detail="d", company="ALFA", severity="HIGH", owner="",
                               due_date=None, source="MANUAL", entity_type="WAREHOUSE", entity_key="WH_A")
    assert "'UNASSIGNED'" in sql and "'DBA'" not in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_mart_action_queue_selects_defer_until():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.action_queue(200, "ALFA")
    assert "DEFER_UNTIL" in sql
    sqlglot.parse_one(sql, read="snowflake")


def test_action_acceptance_dates_by_completion():
    sql = mart_sql.action_acceptance(90)
    assert sql.count("COALESCE(COMPLETED_AT, UPDATED_AT) >=") >= 3
    assert sql.count(" UPDATED_AT >= ") == 0


def test_owner_clear_uses_sentinel_because_owner_not_null():
    v005 = _src("snowflake/migrations/V005__actions.sql")
    assert "OWNER        VARCHAR(200)  NOT NULL DEFAULT 'DBA'" in v005
    for p in (_ROOT / "snowflake" / "migrations").glob("V*.sql"):
        assert "OWNER DROP NOT NULL" not in p.read_text(encoding="utf-8"), p.name


def test_source_locks():
    w = _src("app/ui/workbench.py")
    assert 'key="action_assigned_to_me"' in w and "include_deferred=True" in w
    ac_list = w.split("def _ac_list(", 1)[1].split("\n        def ", 1)[0][:1200]
    assert '"DEFER_UNTIL"' in ac_list
    for rel in ("app/ui/workbench.py", "app/ui/security_center.py"):
        assert 'st.text_input("Owner", value="DBA"' not in _src(rel), rel
    b = _src("app/ui/pages/brief.py")
    assert 'key=f"brief_ask_{_aid}"' in b and 'context={"action_id": _aid}' in b
