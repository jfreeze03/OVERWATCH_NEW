"""v4.606.0 holistic-review fixes (UI seams) -- each test fails on the pre-fix tree (b13e87f0).

(a) Security ▸ Effective access: the row-click sentinel never re-armed (the R1-215 class), so a return to
    the section (or a deselect) then a click on the same row was swallowed -- the highlighted row named one
    user while the selectbox, graph and detail showed another.
(c) Auto-investigation: the grant feed read newest-first LIMIT 500 from now (not onset-anchored like the
    object / warehouse / task feeds), and the "N of TOTAL ... not ranked" disclosure named only the two change
    registries -- a cut grant or task-failure feed went unsaid. Its GRANTS_* prune is two-sided from the
    onset literal (fix-up review): a trailing cutoff wide enough to reach an old onset scanned up to 365 days.
(d) Proof: the page-open verdict listed a FAILED acceptance / precision read as "not yet measured" (and could
    call the page Healthy) while the cards said the read failed.
(e) Overview: the spend-unavailable help said the exec board "does not cover a calendar-month window" under
    Current year too; it now names the calendar window actually picked.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW, LAST_MONTH_WINDOW
from app.core.result import QueryResult
from app.data import change_impact_sql, security_sql
from app.logic.date_windows import window_scope_label
from app.logic.formulas import account_today
from tests.test_control_room_review_r1 import (
    _rca_page,  # noqa: F401  (the auto-investigation harness fixture)
)
from tests.test_pages_shaped import _shaped_from_sql
from tests.test_security_c05_fixes import _harness, _ok

_ROOT = Path(__file__).resolve().parents[1]
_TODAY = account_today()


# ================================================= (a) effective-access row click re-arms ====

def _ea_frame() -> pd.DataFrame:
    # three users with distinct escalation scores, so the summary order is U0, U1, U2
    rows = [{"USER_NAME": f"U{u}", "DIRECT_ROLE": f"R{u}", "EFFECTIVE_ROLE": f"E{u}", "DEPTH": 1,
             "ACCESS_PATH": "x", "PRIVILEGES": 1, "OWNERSHIP_GRANTS": 3 - u, "MANAGE_GRANTS": 0,
             "SENSITIVE_PRIVILEGES": 0, "RISK_SCORE": 90 - 30 * u, "REACHES_ADMIN": False, "USER_PATH_RANK": 1}
            for u in range(3)]
    return pd.DataFrame(rows)


def _drive_effective_access(monkeypatch, selections: list, *, unmount_after: int | None = None) -> list[str]:
    """Render the panel once per emitted table selection; returns the user the selectbox showed each run.
    ``unmount_after`` drops the selectbox's widget key after that run (leaving the section unmounts it)."""
    import app.ui.security_center as sc

    fake, _seen = _harness(monkeypatch, sc, {"sec_effective_ALL": _ok(_ea_frame())})
    emitted = iter(selections)
    monkeypatch.setattr(sc, "selectable_table", lambda *_a, **_k: next(emitted))

    def _selectbox(_label, options, index=0, key=None, **_k):   # a keyed widget reads session_state[key]
        return fake.session_state.get(key, list(options)[index]) if key else list(options)[index]

    monkeypatch.setattr(fake, "selectbox", _selectbox, raising=False)
    shown = []
    for i in range(len(selections)):
        sc.render_effective_access("ALL")
        shown.append(fake.session_state["sec_effective_user_pick"])
        if unmount_after is not None and i == unmount_after:
            fake.session_state.pop("sec_effective_user_pick", None)
    return shown


def test_effective_access_reclick_after_returning_to_the_section_binds_the_clicked_user(monkeypatch):
    # click row 1 (U1); leave the section (the table and the selectbox unmount); come back (the table mounts
    # unselected, the selectbox reseeds to U0); click row 1 again -> the graph must follow to U1
    shown = _drive_effective_access(monkeypatch, [1, None, 1], unmount_after=0)
    assert shown == ["U1", "U0", "U1"]          # was ['U1', 'U0', 'U0']: the re-click was swallowed


def test_effective_access_reclick_after_a_deselect_binds_the_clicked_user(monkeypatch):
    # click row 1 (U1), pick U2 in the selectbox, deselect the row, click row 1 again -> U1
    import app.ui.security_center as sc

    fake, _seen = _harness(monkeypatch, sc, {"sec_effective_ALL": _ok(_ea_frame())})
    emitted = iter([1, 1, None, 1])
    monkeypatch.setattr(sc, "selectable_table", lambda *_a, **_k: next(emitted))
    monkeypatch.setattr(fake, "selectbox", lambda _l, options, index=0, key=None, **_k:
                        fake.session_state.get(key, list(options)[index]), raising=False)
    sc.render_effective_access("ALL")
    assert fake.session_state["sec_effective_user_pick"] == "U1"
    fake.session_state["sec_effective_user_pick"] = "U2"        # the user's own selectbox pick ...
    sc.render_effective_access("ALL")                           # ... survives the sticky re-emit of row 1
    assert fake.session_state["sec_effective_user_pick"] == "U2"
    sc.render_effective_access("ALL")                           # deselect
    sc.render_effective_access("ALL")                           # click row 1 again
    assert fake.session_state["sec_effective_user_pick"] == "U1"


# ============================================== (c) the grant feed is onset-anchored + disclosed ====

_ONSET = pd.Timestamp("2026-09-20 09:00:00")
_LIT = "'2026-09-20 09:00:00'::TIMESTAMP_NTZ"
def _prune_bounds(onset: pd.Timestamp) -> tuple[str, str]:
    # the onset window plus a day of slack each side, as TIMESTAMP_LTZ constants (the GRANTS_* columns' type)
    ltz = f"'{onset.strftime('%Y-%m-%d %H:%M:%S')}'::TIMESTAMP_LTZ"
    return (f"DATEADD('day', -{change_impact_sql.ONSET_LEAD_DAYS + 1}, {ltz})",
            f"DATEADD('day', {change_impact_sql.ONSET_AFTER_DAYS + 1}, {ltz})")


def _prune_span(onset: pd.Timestamp) -> str:
    lo, hi = _prune_bounds(onset)
    return f"CREATED_ON BETWEEN {lo} AND {hi} OR DELETED_ON BETWEEN {lo} AND {hi}"


_LO, _HI = _prune_bounds(_ONSET)


def test_grant_feed_default_mode_is_unchanged():
    sql = security_sql.recent_grant_changes(30, "ALL")
    assert sql.endswith("WHERE CHANGED_AT IS NOT NULL\nORDER BY CHANGED_AT DESC\nLIMIT 500\n")
    assert "ABS(DATEDIFF" not in sql and "TIMESTAMP_NTZ" not in sql


def test_grant_feed_onset_mode_reads_the_onset_window_nearest_first():
    sql = security_sql.recent_grant_changes(13, "ALL", onset=_ONSET)
    where, order = change_impact_sql._onset_window("CHANGED_AT", _ONSET)
    assert f"WHERE CHANGED_AT IS NOT NULL\n  AND {where}\n" in sql
    assert f"BETWEEN DATEADD('day', -{change_impact_sql.ONSET_LEAD_DAYS}, {_LIT})" in sql
    assert f"ORDER BY {order}, CHANGED_AT DESC\nLIMIT 500\n" in sql
    # both arms prune on the onset's two-sided span (no trailing cutoff); the pre-LIMIT total survives
    # (TOTAL_CHANGES_WIN = the onset window's count)
    assert sql.count(f"WHERE ({_prune_span(_ONSET)}) AND IFF(ev.CHG = 'GRANTED', CREATED_ON, DELETED_ON) "
                     f"BETWEEN {_LO} AND {_HI}") == 2
    assert "CURRENT_TIMESTAMP()" not in sql
    assert "COUNT(*) OVER () AS TOTAL_CHANGES_WIN" in sql
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, dialect="snowflake")


@pytest.mark.parametrize("company", ["ALL", "Trexis", "ALFA", "UNKNOWN"])
def test_grant_feed_onset_prune_is_bounded_on_both_sides_for_an_old_onset(company):
    # the fix-up review's case: an open incident whose onset was 120 days ago. A trailing cutoff wide enough
    # to reach it scanned ~124 days of GRANTS_TO_USERS + GRANTS_TO_ROLES (up to 365) to keep ~4 days of rows;
    # the outer onset filter runs over the CTE after the CROSS JOIN, so it cannot prune.
    onset = pd.Timestamp(datetime.combine(_TODAY - timedelta(days=120), datetime.min.time())) + timedelta(hours=9)
    sql = security_sql.recent_grant_changes(124, company, onset=onset)
    assert "CURRENT_TIMESTAMP()" not in sql                         # no trailing cutoff anywhere ...
    assert not re.search(r"DATEADD\('day', -1\d\d,", sql)           # ... and no -124-day one
    span = _prune_span(onset)
    assert sql.count(f"WHERE ({span}) AND ") == 2                   # both UNION arms prune on the upper bound too
    if company in ("Trexis", "ALFA", "UNKNOWN"):                    # the user-scope DISTINCT sub-scan as well
        assert f"FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS WHERE {span})" in sql
    # days no longer shapes the onset-mode read (as in the registries): any lookback gives the same SQL
    assert security_sql.recent_grant_changes(3, company, onset=onset) == sql
    assert security_sql.recent_grant_changes(365, company, onset=onset) == sql
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, dialect="snowflake")


def test_grant_feed_onset_prune_brackets_the_exact_onset_window():
    # the prune is a superset of the exact (Central wall clock) cut, with a day of slack each side for the
    # session-vs-account clock, so it can never drop a row the onset window keeps
    lo, hi = change_impact_sql._onset_prune_bounds(_ONSET)
    assert (lo, hi) == (_LO, _HI)
    assert change_impact_sql._onset_prune_bounds(pd.Timestamp("2026-09-20 14:00:00", tz="UTC")) == (_LO, _HI)
    with pytest.raises(ValueError):
        change_impact_sql._onset_prune_bounds("2026-09-20'; DROP TABLE X; --")


def test_grant_feed_onset_rejects_text():
    with pytest.raises(ValueError):
        security_sql.recent_grant_changes(13, "ALL", onset="2026-09-20'; DROP TABLE X; --")


@pytest.mark.parametrize("age_days", [2, 20, 40, 120, 200])
@pytest.mark.parametrize("company", ["ALL", "Trexis"])
def test_auto_investigation_reads_grants_around_onset(_rca_page, age_days, company):  # noqa: F811
    specs, _out, _feeds = _rca_page
    onset = pd.Timestamp(datetime.combine(_TODAY - timedelta(days=age_days), datetime.min.time())) + timedelta(hours=9)
    from app.ui.pages import control_room
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-g"}), company, 3.0)
    sql = {s["key"]: s["sql"] for s in specs}["ai_grant"]
    lit = f"'{onset.strftime('%Y-%m-%d %H:%M:%S')}'::TIMESTAMP_NTZ"
    assert f"BETWEEN DATEADD('day', -3, {lit})" in sql and "ORDER BY ABS(DATEDIFF(" in sql
    # the GRANTS_* prune is the onset's own few days, however old the incident: bounded on both sides from the
    # onset literal, never a trailing cutoff from now (which had to widen to age + 4 days, up to 365)
    assert sql.count(f"WHERE ({_prune_span(onset)}) AND ") == 2
    assert "CURRENT_TIMESTAMP()" not in sql
    assert not re.search(r"CREATED_ON >= DATEADD", sql)


def _grant_feed(n: int, total: int, onset: pd.Timestamp) -> pd.DataFrame:
    return pd.DataFrame({
        "CHANGED_AT": [onset - timedelta(minutes=i) for i in range(n)], "CHANGE": "GRANTED",
        "GRANT_TYPE": "Privilege -> role", "CHANGED_BY": "CI", "GRANTEE": "R_APP",
        "WHAT": [f"SELECT ON TABLE DB.S.T{i}" for i in range(n)],
        "TOTAL_CHANGES_WIN": total, "GRANTED_WIN": total, "REVOKED_WIN": 0})


def _task_feed(n: int, total: int, onset: pd.Timestamp) -> pd.DataFrame:
    return pd.DataFrame({
        "DATABASE_NAME": "DB", "SCHEMA_NAME": "S", "TASK_NAME": [f"T{i % 7}" for i in range(n)],
        "ROOT_TASK_ID": "r", "GRAPH_RUN_GROUP_ID": None,
        "QUERY_START_TIME": [onset - timedelta(minutes=i) for i in range(n)], "RUN_SEC": 5,
        "ERROR_CODE": "100", "ERROR_MESSAGE": "boom", "TOTAL_FAILURES_WIN": total,
        "REPEAT_FAILURE": [0 if i < 7 else 1 for i in range(n)]})


def test_auto_investigation_discloses_a_cut_grant_and_task_feed(_rca_page):  # noqa: F811
    _specs, out, feeds = _rca_page
    onset = pd.Timestamp(_TODAY - timedelta(days=2)) + timedelta(hours=9)
    feeds["ai_grant"] = QueryResult(df=_grant_feed(500, 1234, onset), ok=True)
    feeds["ai_task"] = QueryResult(df=_task_feed(500, 4300, onset), ok=True)
    from app.ui.pages import control_room
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-c"}), "ALL", 3.0)
    cut = [c for c in out["caption"] if c.startswith("Ranked from the ")]
    assert cut, out["caption"]
    assert "500 of 1,234 grant changes" in cut[0] and "500 of 4,300 task failures" in cut[0]
    assert "the rest of the window's changes and task failures were not ranked" in cut[0]
    assert "Each task's first failure in the window is kept ahead of its repeats." in cut[0]


def test_auto_investigation_discloses_a_cut_task_feed_alone(_rca_page):  # noqa: F811
    _specs, out, feeds = _rca_page
    onset = pd.Timestamp(_TODAY - timedelta(days=2)) + timedelta(hours=9)
    feeds["ai_task"] = QueryResult(df=_task_feed(500, 501, onset), ok=True)
    from app.ui.pages import control_room
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-t"}), "ALL", 3.0)
    cut = [c for c in out["caption"] if c.startswith("Ranked from the ")]
    assert cut == ["Ranked from the 500 of 501 task failures nearest onset — the rest of the window's "
                   "task failures were not ranked. Each task's first failure in the window is kept ahead "
                   "of its repeats."]


def test_auto_investigation_is_silent_when_no_feed_was_cut(_rca_page):  # noqa: F811
    _specs, out, feeds = _rca_page
    onset = pd.Timestamp(_TODAY - timedelta(days=2)) + timedelta(hours=9)
    feeds["ai_grant"] = QueryResult(df=_grant_feed(40, 40, onset), ok=True)
    feeds["ai_task"] = QueryResult(df=_task_feed(30, 30, onset), ok=True)
    from app.ui.pages import control_room
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-n"}), "ALL", 3.0)
    assert not [c for c in out["caption"] if c.startswith("Ranked from the ")]


# ======================================= (d) the Proof verdict names a failed side read ====

def _failed(kind: str) -> QueryResult:
    return QueryResult(df=pd.DataFrame(), ok=False, source="t", error=f"boom ({kind})", error_kind=kind)


def _proof(monkeypatch, failing: dict[str, QueryResult]) -> dict:
    from app.ui import decision_studio as ds

    ds.reset_proof_memo()

    def fake_run(sql, *_a, key: str = "", **_k):
        return failing.get(key) or _shaped_from_sql(sql)

    def fake_batch(specs, **_k):
        return {s["key"]: failing.get(s["key"]) or _shaped_from_sql(s.get("sql", "")) for s in specs or []}

    monkeypatch.setattr(ds, "run", fake_run)
    monkeypatch.setattr(ds, "run_batch", fake_batch)
    try:
        return ds.decision_verdict(3.68)
    finally:
        ds.reset_proof_memo()


@pytest.mark.parametrize("kind", ["timeout", "other", "missing_column"])
def test_proof_verdict_says_a_failed_side_read_could_not_be_read(monkeypatch, kind):
    verdict = _proof(monkeypatch, {"sc_accept": _failed(kind), "sc_precision": _failed(kind)})
    assert verdict["severity"] == "warn"
    assert verdict["body"].startswith("team follow-through could not be read (the ACTION_QUEUE read failed); "
                                      "alert precision could not be read (the ALERT_EVENTS read failed)")
    assert "not yet measured" not in verdict["body"]


def test_proof_verdict_names_an_absent_side_read_as_setup(monkeypatch):
    verdict = _proof(monkeypatch, {"sc_precision": _failed("absent")})
    assert verdict["severity"] == "warn"
    assert "alert precision needs setup (the ALERT_EVENTS read failed)" in verdict["body"]
    assert "team follow-through could not be read" not in verdict["body"]      # only the read that failed
    assert "team follow-through needs setup" not in verdict["body"]


def test_proof_verdict_after_a_failed_read_is_never_healthy(monkeypatch):
    # the reviewer's case: ROI 5x and 85% realization would headline "Healthy ... (not yet measured: alert
    # precision, team follow-through)" off two reads that broke
    from app.logic.proof import roi_multiple
    from app.ui import decision_studio as ds

    sig = {"roi": roi_multiple(500.0, 100.0), "realization": 85.0,
           "acc": {"ACCEPTANCE_PCT": None}, "prec": {"PRECISION_PCT": None, "UNTAGGED_SHARE_PCT": 0},
           "acc_read": _failed("timeout"), "prec_read": _failed("timeout")}
    monkeypatch.setattr(ds, "_proof_signals", lambda _rate: sig)
    verdict = ds.decision_verdict(3.68)
    assert verdict["level"] == "warn" and "Healthy" not in verdict["sentence"]
    assert "not yet measured" not in verdict["sentence"]
    sig.update(acc_read=QueryResult(df=pd.DataFrame(), ok=True), prec_read=QueryResult(df=pd.DataFrame(), ok=True))
    healthy = ds.decision_verdict(3.68)        # ok-but-empty reads ARE "nothing measured yet": Healthy stands
    assert healthy["level"] == "ok" and "not yet measured: alert precision, team follow-through" in healthy["body"]


def test_proof_verdict_with_good_side_reads_adds_no_read_warning(monkeypatch):
    verdict = _proof(monkeypatch, {})
    assert "could not be read" not in verdict.get("body", "") and "needs setup" not in verdict.get("body", "")


# ======================================= (e) the spend-unavailable help names the window ====

_LIVE = QueryResult(ok=False, error="stub timeout", error_kind="timeout",
                    source="ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (bounded fallback)")


@pytest.mark.parametrize("window", [LAST_MONTH_WINDOW, CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW])
def test_spend_failure_help_names_the_calendar_window_picked(window):
    from app.ui.pages import overview as ov

    label = window_scope_label(window, today=date(2026, 9, 30))
    text = ov._spend_failure_help(None, _LIVE, window_label=label)
    assert f"The exec board covers trailing windows only, so it was not read for {label};" in text
    assert "calendar-month" not in text            # was claimed under Current year / Current month too


def test_overview_hands_the_picked_window_to_the_spend_help():
    src = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert '"help": _spend_failure_help(board_res, trend_source, window_label=str(f["window_label"])),' in src


def test_live_fallback_docstring_names_every_calendar_preset():
    # fix-up review (item e's missed twin): the bounded fallback serves Last month, Current month AND Current
    # year (every window with bounds), and only Last month is a closed period
    from app.ui.pages import overview as ov

    doc = " ".join((ov._live_fallback_daily.__doc__ or "").split())
    assert "Last month / Current month / Current year" in doc
    assert "the 'Last month' calendar window (its explicit (start, end) range)" not in doc
    assert "Last month is a closed calendar period, so the fact's hourly loader lag is immaterial there" in doc
    assert "daily_complete" in doc                 # the open presets drop today's partial day downstream
