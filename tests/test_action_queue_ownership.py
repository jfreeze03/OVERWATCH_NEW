"""Next-Fifty #20 (v4.589.0): the action queue names a person, honors deferral, and dates decisions by
when they were made. A team label such as 'DBA' names nobody (counted Unassigned); an owner picker seeded
from the live SNOW_PRI_GFR_PRD_ALFA_DSA member list (session.admin_roster_users, v4.611.0) replaces the
free-text 'DBA' default; 'Assigned to me' filters the Action Center;
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


def test_team_placeholder_owners_are_unassigned(monkeypatch):
    # Runs the live rule (action_summary's Unassigned KPI); the stand-alone is_unassigned_owner
    # twin it used to call was never wired into the app and was removed in v4.607.
    monkeypatch.setattr(wb, "account_today", lambda: date(2026, 9, 24))
    for v in ("DBA", "dba / ai governance", "", None, float("nan"), "UNASSIGNED", "  dba team "):
        one = pd.DataFrame({"STATUS": ["OPEN"], "OWNER": [v]})
        assert wb.action_summary(one)["unassigned"] == 1.0, v
    assert wb.action_summary(pd.DataFrame({"STATUS": ["OPEN"], "OWNER": ["KEBARR1"]}))["unassigned"] == 0.0


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
        {"OWNER": "owner_a", "STATUS": "OPEN", "DUE_DATE": "2026-09-24", "DEFER_UNTIL": None},   # due today: not late
        {"OWNER": "OWNER_A", "STATUS": "OPEN", "DUE_DATE": "2026-09-20", "DEFER_UNTIL": None},   # overdue
        {"OWNER": "OWNER_A", "STATUS": "OPEN", "DUE_DATE": "2026-09-20", "DEFER_UNTIL": "2026-10-01"},  # parked
        {"OWNER": "OWNER_A", "STATUS": "DONE", "DUE_DATE": "2026-09-20", "DEFER_UNTIL": None},   # closed
        {"OWNER": "OWNER_B", "STATUS": "OPEN", "DUE_DATE": None, "DEFER_UNTIL": None},
    ])
    assert list(wb.owned_by(df, "OwNeR_a")) == [True, True, True, True, False]
    assert wb.my_queue_counts(df, "owner_a") == {"mine": 2, "mine_overdue": 1}
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


def test_owner_picker_is_seeded_from_the_live_admin_roster():
    # v4.611.0 (owner 2026-10-07): no username is hard-coded, so the picker's roster is the direct
    # SNOW_PRI_GFR_PRD_ALFA_DSA members from this session's last good lookup (no query of its own)
    import inspect

    from app.ui import components
    src = inspect.getsource(components.owner_picker)
    assert "OPERATOR_USERS" not in src
    assert "admin_roster_users()" in src


def _picker_entry():
    from app.ui.components import owner_picker

    owner_picker("Owner", key="t_owner", default_to_viewer=False)


def _picker_options(roster: dict | None) -> list[str]:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_picker_entry, default_timeout=30)
    if roster is not None:
        at.session_state["_ow_access_roster"] = roster
    at.run()
    assert not at.exception, at.exception
    return list(at.selectbox(key="t_owner").options)


def test_owner_picker_offers_the_dsa_members_of_a_good_lookup():
    roster = {"status": "ok", "users": ("DSA_PERSON1", "other_dsa"), "roles": (), "at": 1.0, "error": "",
              "fails": 0}
    assert _picker_options(roster) == ["(unassigned)", "DSA_PERSON1", "OTHER_DSA", "Other…"]


def test_owner_picker_without_a_good_lookup_offers_only_unassigned_and_other():
    assert _picker_options(None) == ["(unassigned)", "Other…"]
    failed = {"status": "lookup_failed", "users": (), "roles": (), "at": 1.0, "error": "x", "fails": 1}
    assert _picker_options(failed) == ["(unassigned)", "Other…"]


def test_source_locks():
    w = _src("app/ui/workbench.py")
    assert 'key="action_assigned_to_me"' in w and "include_deferred=True" in w
    ac_list = w.split("def _ac_list(", 1)[1].split("\n        def ", 1)[0][:1200]
    assert '"DEFER_UNTIL"' in ac_list
    for rel in ("app/ui/workbench.py", "app/ui/security_center.py"):
        assert 'st.text_input("Owner", value="DBA"' not in _src(rel), rel
    b = _src("app/ui/pages/brief.py")
    assert 'key=f"brief_ask_{_aid}"' in b and 'context={"action_id": _aid}' in b
